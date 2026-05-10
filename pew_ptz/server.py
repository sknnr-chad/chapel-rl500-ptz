"""
pew-ptz — Phone-based PTZ camera control + Zoom mute/video toggles.

A small Flask app for anyone who runs broadcast from a seat in the audience
instead of a control booth. Drives any VISCA-over-IP PTZ camera (PTZOptics,
Sony, AVer, Marshall, HuddleCam, BirdDog, ClearTouch RL500, etc.) from a
phone, and provides bidirectional Zoom mute/video toggles via the host's
Alt+V / Alt+A keyboard shortcuts.

Runs on the Zoom host PC (so pynput can drive Zoom's hotkeys). The phone
hits this server over the LAN. Camera control is VISCA-over-IP UDP 52381
(see visca.py). On Windows the actual Zoom mute/video state is read back
via UI Automation (see zoom_state.py) so the on-air pills can't drift.

Config via environment variables:

    PEW_PTZ_CAMERA_IP             default 192.168.100.88
    PEW_PTZ_VISCA_PORT            default 52381
    PEW_PTZ_SERVER_PORT           default 8080
    PEW_PTZ_CAMERA_SNAPSHOT_PATH  default /snapshot.jpg  (HTTP path on the
                                  camera that returns a JPEG; varies by
                                  camera vendor — see docs/ptz-cameras.md)
    PEW_PTZ_PRESETS               comma-separated preset names. Slot N on
                                  the camera maps to the Nth name (1-indexed).
                                  default: 9 sacrament-meeting positions
    PEW_PTZ_LOG_DIR               where to put rotating server.log; if unset,
                                  logs only to stdout
    PEW_PTZ_SKIP_FOCUS_CHECK      set to 1 to bypass the "Zoom must be
                                  foreground" guard (useful for UI testing)
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import time
from ctypes import wintypes
from logging.handlers import RotatingFileHandler
from pathlib import Path
from flask import Flask, jsonify, render_template_string, request

from pew_ptz.visca import (
    ViscaIP,
    PAN_LEFT, PAN_RIGHT, PAN_STOP,
    TILT_UP, TILT_DOWN, TILT_STOP,
)
from pew_ptz.zoom_state import ZoomStateReader


def _env(name: str, default: str = "") -> str:
    return os.environ.get("PEW_PTZ_" + name, default)


CAMERA_IP = _env("CAMERA_IP", "192.168.100.88")
VISCA_PORT = int(_env("VISCA_PORT", "52381"))
SERVER_PORT = int(_env("SERVER_PORT", "8080"))
CAMERA_SNAPSHOT_PATH = _env("CAMERA_SNAPSHOT_PATH", "/snapshot.jpg")
PRESETS = [
    p.strip() for p in _env(
        "PRESETS",
        "Speaker,Choir,Chorister,Piano,Organ,Sacrament,North Stand,Congregation,Back Row",
    ).split(",") if p.strip()
]
SKIP_FOCUS_CHECK = _env("SKIP_FOCUS_CHECK", "").lower() in ("1", "true", "yes")

# Log to a rotating file when PEW_PTZ_LOG_DIR is set (the Task Scheduler launch
# uses pythonw.exe so stdout is gone — without this we'd be flying blind).
def _setup_logging():
    log_dir = _env("LOG_DIR")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            Path(log_dir) / "server.log",
            maxBytes=1_000_000, backupCount=5, encoding="utf-8",
        )
        handlers.append(fh)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )

_setup_logging()
log = logging.getLogger("pew_ptz")

camera = ViscaIP(CAMERA_IP, VISCA_PORT)
app = Flask(__name__)
START_TIME = time.time()
zoom_reader = ZoomStateReader(poll_interval=1.5)

# ---- Zoom hotkeys (Windows-only via pynput) ------------------------------

try:
    from pynput.keyboard import Key, Controller
    _kbd = Controller()
except Exception as e:  # pragma: no cover - non-Windows / headless
    _kbd = None
    print(f"[zoom] keyboard controller unavailable: {e}")


def _alt_chord(letter: str):
    """Send Alt+<letter> to the foreground window (must be Zoom)."""
    if _kbd is None:
        return False
    _kbd.press(Key.alt)
    try:
        _kbd.press(letter)
        _kbd.release(letter)
    finally:
        _kbd.release(Key.alt)
    return True


def zoom_toggle_video() -> bool:
    return _alt_chord("v")


def zoom_toggle_mic() -> bool:
    ok = _alt_chord("a")
    return ok


# ---- Foreground-window detection (Windows) -------------------------------
# Used to verify Zoom is the active window before sending hotkeys, so we don't
# accidentally fire Alt+V / Alt+A into whatever else has focus.

ZOOM_PROCESS_NAMES = {"zoom.exe", "cpthost.exe"}

_user32 = None
_kernel32 = None
if sys.platform == "win32":
    try:
        _user32 = ctypes.windll.user32
        _kernel32 = ctypes.windll.kernel32
    except Exception as e:
        print(f"[zoom] Win32 foreground check unavailable: {e}")


def foreground_info() -> tuple[bool, str, str]:
    """Return (is_zoom, window_title, process_basename). is_zoom False off-Windows."""
    if _user32 is None or _kernel32 is None:
        return (False, "", "")
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return (False, "", "")
    length = _user32.GetWindowTextLengthW(hwnd)
    tbuf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, tbuf, length + 1)
    title = tbuf.value
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    proc = ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if h:
        try:
            size = wintypes.DWORD(260)
            pbuf = ctypes.create_unicode_buffer(size.value)
            if _kernel32.QueryFullProcessImageNameW(h, 0, pbuf, ctypes.byref(size)):
                proc = pbuf.value.rsplit("\\", 1)[-1]
        finally:
            _kernel32.CloseHandle(h)
    return (proc.lower() in ZOOM_PROCESS_NAMES, title, proc)


# ---- Optimistic Zoom state ----------------------------------------------
# We can't read Zoom's real mute/video state without its SDK, so we track what
# we *believe* the state is based on toggles we've sent. Resets to "on air"
# every server restart — the operator should glance to confirm at start.

_zoom_state = {"video_on": True, "mic_on": True, "last_toggled": None}


def _public_state() -> dict:
    is_zoom, title, proc = foreground_info()
    focus_active = _user32 is not None and not SKIP_FOCUS_CHECK
    uia = zoom_reader.get()

    # Prefer observed (UIA) values when we have them; fall back to optimistic.
    video_on = uia["video_on"] if uia["observed"] and uia["video_on"] is not None else _zoom_state["video_on"]
    mic_on   = uia["mic_on"]   if uia["observed"] and uia["mic_on"]   is not None else _zoom_state["mic_on"]

    return {
        "video_on": video_on,
        "mic_on": mic_on,
        "air_on": bool(video_on) and bool(mic_on),
        "last_toggled": _zoom_state["last_toggled"],
        "zoom_focused": is_zoom,
        "foreground_title": title,
        "foreground_process": proc,
        "focus_check_active": focus_active,
        # Source-of-truth metadata so the UI can show observed vs assumed:
        "observed": uia["observed"],
        "in_meeting": uia["in_meeting"],
        "uia_available": uia["uia_available"],
        "uia_last_observed": uia["last_observed"],
        "uia_walk_ms": uia["walk_ms"],
    }


def _focus_block():
    """Return a (response, status) tuple if the chord should be blocked, else None."""
    if not _user32 or SKIP_FOCUS_CHECK:
        return None
    is_zoom, title, proc = foreground_info()
    if is_zoom:
        return None
    return jsonify({
        "error": "Zoom is not the foreground window",
        "foreground_title": title,
        "foreground_process": proc,
        **_public_state(),
    }), 409


# ---- Direction map -------------------------------------------------------

DIRS = {
    "up":         (PAN_STOP,  TILT_UP),
    "down":       (PAN_STOP,  TILT_DOWN),
    "left":       (PAN_LEFT,  TILT_STOP),
    "right":      (PAN_RIGHT, TILT_STOP),
    "up_left":    (PAN_LEFT,  TILT_UP),
    "up_right":   (PAN_RIGHT, TILT_UP),
    "down_left":  (PAN_LEFT,  TILT_DOWN),
    "down_right": (PAN_RIGHT, TILT_DOWN),
}


# ---- Routes --------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(
        INDEX_HTML,
        camera_ip=CAMERA_IP,
        camera_snapshot_path=CAMERA_SNAPSHOT_PATH,
        presets=PRESETS,
    )


@app.post("/ptz/move/<direction>")
def ptz_move(direction: str):
    if direction not in DIRS:
        return jsonify({"error": "unknown direction"}), 400
    speed = int(request.args.get("speed", "12"))
    pan_dir, tilt_dir = DIRS[direction]
    camera.pan_tilt(pan_dir, tilt_dir, pan_speed=speed, tilt_speed=speed)
    return jsonify({"status": f"moving {direction}"})


@app.post("/ptz/stop")
def ptz_stop():
    camera.pan_tilt_stop()
    return jsonify({"status": "stopped"})


@app.post("/zoom/<action>")
def zoom_action(action: str):
    speed = int(request.args.get("speed", "2"))
    if action == "tele":
        camera.zoom_tele(speed)
    elif action == "wide":
        camera.zoom_wide(speed)
    elif action == "stop":
        camera.zoom_stop()
    else:
        return jsonify({"error": "unknown zoom action"}), 400
    return jsonify({"status": f"zoom {action}"})


@app.post("/preset/recall/<int:n>")
def preset_recall(n: int):
    camera.preset_recall(n)
    return jsonify({"status": f"recalled preset {n}"})


@app.get("/zoom_meeting/debug")
def http_zoom_debug():
    """Lists top-level windows and (for Zoom-ish ones) all button names.
    Used to figure out why UIA can't find the meeting window on a given
    Zoom build — point a browser at this while in a meeting and look for
    the Mute/Video button labels."""
    return jsonify(zoom_reader.debug_snapshot())


@app.get("/healthz")
def healthz():
    is_zoom, title, proc = foreground_info()
    return jsonify({
        "ok": True,
        "uptime_s": int(time.time() - START_TIME),
        "camera_ip": CAMERA_IP,
        "keyboard_ok": _kbd is not None,
        "foreground_process": proc,
        "zoom_focused": is_zoom,
    })


@app.get("/zoom_meeting/state")
def http_zoom_state():
    return jsonify(_public_state())


def _sync_optimistic_from_uia():
    """Pre-toggle correction: if UIA can see the real state, adopt it before we flip,
    so a single toggle from a drifted optimistic state doesn't compound the drift."""
    uia = zoom_reader.get()
    if uia["observed"]:
        if uia["video_on"] is not None:
            _zoom_state["video_on"] = uia["video_on"]
        if uia["mic_on"] is not None:
            _zoom_state["mic_on"] = uia["mic_on"]


@app.post("/zoom_meeting/toggle_video")
def http_toggle_video():
    blocked = _focus_block()
    if blocked:
        return blocked
    _sync_optimistic_from_uia()
    if not zoom_toggle_video():
        return jsonify({"error": "keyboard unavailable on this host"}), 500
    _zoom_state["video_on"] = not _zoom_state["video_on"]
    _zoom_state["last_toggled"] = time.time()
    zoom_reader.trigger_refresh()
    return jsonify({"status": "toggled video (Alt+V)", **_public_state()})


@app.post("/zoom_meeting/toggle_mic")
def http_toggle_mic():
    blocked = _focus_block()
    if blocked:
        return blocked
    _sync_optimistic_from_uia()
    if not zoom_toggle_mic():
        return jsonify({"error": "keyboard unavailable on this host"}), 500
    _zoom_state["mic_on"] = not _zoom_state["mic_on"]
    _zoom_state["last_toggled"] = time.time()
    zoom_reader.trigger_refresh()
    return jsonify({"status": "toggled mic (Alt+A)", **_public_state()})


@app.post("/zoom_meeting/toggle_air")
def http_toggle_air():
    """Toggle both video and mic together — the 'panic' button."""
    if _kbd is None:
        return jsonify({"error": "keyboard unavailable on this host"}), 500
    blocked = _focus_block()
    if blocked:
        return blocked
    _sync_optimistic_from_uia()
    zoom_toggle_video()
    time.sleep(0.12)
    zoom_toggle_mic()
    _zoom_state["video_on"] = not _zoom_state["video_on"]
    _zoom_state["mic_on"] = not _zoom_state["mic_on"]
    _zoom_state["last_toggled"] = time.time()
    zoom_reader.trigger_refresh()
    return jsonify({"status": "toggled video + mic", **_public_state()})


# ---- Inline template -----------------------------------------------------

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no" />
  <title>Chapel RL500 PTZ</title>
  <style>
    :root { color-scheme: dark; }
    * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
    body {
      margin: 0; padding: 16px; max-width: 560px; margin-inline: auto;
      font-family: -apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
      background: #0f172a; color: #e0f2fe;
    }
    .card { background: #111827; border-radius: 18px; padding: 14px; margin-bottom: 18px; }
    .card h2 { margin: 0 0 10px; font-size: 0.85rem; letter-spacing: 0.12em;
               text-transform: uppercase; color: #fbbf24; }
    button {
      font: inherit; color: inherit; border: 0; cursor: pointer;
      background: #1f2937; border-radius: 14px; padding: 14px;
      touch-action: manipulation; user-select: none;
    }
    button:active { transform: scale(0.96); background: #0b1220; }
    .preview-wrap {
      background: #000; border-radius: 18px; overflow: hidden;
      aspect-ratio: 16 / 9;
      position: sticky; top: 0; z-index: 50;
      box-shadow: 0 6px 18px rgba(0,0,0,0.6);
      margin-bottom: 14px;
    }
    .preview-wrap img {
      width: 100%; height: 100%; display: block; object-fit: cover;
      background: #000;
    }
    .dpad { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px;
            max-width: 280px; margin-inline: auto; }
    .dpad button { height: 76px; font-size: 1.6rem; }
    .dpad .home { background: #334155; font-size: 0.95rem; font-weight: 600; }
    .row { display: flex; gap: 10px; justify-content: center; margin-top: 14px; }
    .row .group { flex: 1; text-align: center; }
    .row .label { font-size: 0.7rem; letter-spacing: 0.15em;
                  text-transform: uppercase; color: #94a3b8; margin-bottom: 6px; }
    .row .pair { display: flex; gap: 8px; }
    .row .pair button { flex: 1; padding: 16px 0; font-size: 1.4rem; font-weight: 700; }
    .zoombtn { background: #2563eb; }
    .presets { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
    .presets button {
      width: 100%; height: 64px; margin: 0; padding: 6px 8px;
      font-weight: 600; line-height: 1.15;
      display: flex; align-items: center; justify-content: center;
      text-align: center;
    }
    .air {
      width: 100%; padding: 26px; font-size: 1.4rem; font-weight: 800;
      border-radius: 18px; letter-spacing: 0.05em;
      background: #1f2937;          /* neutral fallback before state arrives */
      transition: background 0.18s ease;
    }
    .air.off-air { background: #047857; }   /* tally-light: safe / not broadcasting */
    .air.on-air  { background: #b91c1c; }   /* tally-light: live right now */
    .air-row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 10px; }
    .air-row button { padding: 14px; font-weight: 600; }
    .toast {
      position: fixed; left: 50%; bottom: 20px; transform: translateX(-50%);
      background: #0b1220; border: 1px solid #1f2937; padding: 8px 14px;
      border-radius: 999px; font-size: 0.85rem; opacity: 0; transition: opacity 0.2s;
      pointer-events: none;
    }
    .toast.show { opacity: 1; }
    .mode { display: flex; gap: 6px; justify-content: center; margin-top: 10px; }
    .mode button { background: #1e293b; padding: 8px 14px; font-size: 0.8rem; }
    .mode button.active { background: #2563eb; }
    .status { display: flex; gap: 6px; justify-content: center; flex-wrap: wrap;
              margin-top: 12px; font-size: 0.72rem; }
    .pill { padding: 4px 10px; border-radius: 999px; background: #1f2937;
            color: #cbd5e1; font-weight: 700; letter-spacing: 0.06em; }
    .pill.on   { background: #065f46; color: #d1fae5; }
    .pill.off  { background: #7f1d1d; color: #fecaca; }
    .pill.warn { background: #92400e; color: #fde68a; }
    .pill.air-on  { background: #b91c1c; color: #fff1f2; }
    .pill.air-off { background: #064e3b; color: #d1fae5; }
  </style>
</head>
<body>
  <div class="preview-wrap">
    <img id="preview" alt="Live preview"
         src="http://{{ camera_ip }}{{ camera_snapshot_path }}" />
  </div>

  <div class="card">
    <h2>Presets</h2>
    <div class="presets" id="presets"></div>
  </div>

  <div class="card">
    <h2>Pan / Tilt</h2>
    <div class="dpad">
      <button data-dir="up_left">↖</button>
      <button data-dir="up">↑</button>
      <button data-dir="up_right">↗</button>
      <button data-dir="left">←</button>
      <button class="home" id="homeBtn">HOME</button>
      <button data-dir="right">→</button>
      <button data-dir="down_left">↙</button>
      <button data-dir="down">↓</button>
      <button data-dir="down_right">↘</button>
    </div>
    <div class="mode">
      <span style="align-self:center; font-size:0.75rem; color:#94a3b8;">Speed:</span>
      <button data-speed="6" class="active">Slow</button>
      <button data-speed="12">Med</button>
      <button data-speed="20">Fast</button>
    </div>

    <div class="row">
      <div class="group">
        <div class="label">Zoom</div>
        <div class="pair">
          <button class="zoombtn" data-zoom="wide">−</button>
          <button class="zoombtn" data-zoom="tele">+</button>
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>Zoom Meeting</h2>
    <button class="air" id="airBtn">TOGGLE AIR (video + mic)</button>
    <div class="air-row">
      <button id="vidBtn">Video only</button>
      <button id="micBtn">Mic only</button>
    </div>
    <div class="status" id="zoomStatus"></div>
  </div>

  <div class="toast" id="toast"></div>

<script>
  const PRESETS = {{ presets | tojson }};
  let speed = 6;

  const toast = document.getElementById("toast");
  let toastTimer = null;
  function flash(msg) {
    toast.textContent = msg;
    toast.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("show"), 1200);
  }

  async function post(path) {
    try {
      const r = await fetch(path, { method: "POST" });
      const j = await r.json().catch(() => ({}));
      if (r.status === 409 && j.foreground_process !== undefined) {
        flash("⚠ Zoom not focused (" + (j.foreground_process || "unknown") + ")");
      } else if (!r.ok) {
        flash("⚠ " + (j.error || r.statusText));
      }
      return j;
    } catch (e) {
      flash("⚠ network");
    }
  }

  // ---- hold-to-move for D-pad ----
  function bindHold(btn, onStart, onEnd) {
    let active = false;
    const start = (e) => {
      e.preventDefault();
      if (active) return;
      active = true;
      onStart();
    };
    const end = (e) => {
      if (!active) return;
      active = false;
      onEnd();
    };
    btn.addEventListener("pointerdown", start);
    btn.addEventListener("pointerup", end);
    btn.addEventListener("pointerleave", end);
    btn.addEventListener("pointercancel", end);
  }

  document.querySelectorAll(".dpad button[data-dir]").forEach(btn => {
    const dir = btn.dataset.dir;
    bindHold(btn,
      () => post(`/ptz/move/${dir}?speed=${speed}`),
      () => post(`/ptz/stop`));
  });

  document.querySelectorAll("button[data-zoom]").forEach(btn => {
    const action = btn.dataset.zoom;
    bindHold(btn,
      () => post(`/zoom/${action}`),
      () => post(`/zoom/stop`));
  });

  // "HOME" = jump to preset 1 (Speaker), the most useful default framing
  document.getElementById("homeBtn").addEventListener("click", () => post("/preset/recall/1"));

  document.querySelectorAll(".mode button[data-speed]").forEach(btn => {
    btn.addEventListener("click", () => {
      speed = parseInt(btn.dataset.speed, 10);
      document.querySelectorAll(".mode button[data-speed]").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
    });
  });

  // ---- presets ----
  const presetsEl = document.getElementById("presets");
  PRESETS.forEach((name, i) => {
    const b = document.createElement("button");
    b.textContent = name;
    const slot = i + 1;
    b.addEventListener("click", async () => {
      await post(`/preset/recall/${slot}`);
      flash(name);
    });
    presetsEl.appendChild(b);
  });

  // ---- zoom meeting ----
  document.getElementById("airBtn").addEventListener("click", async () => {
    const j = await post("/zoom_meeting/toggle_air");
    if (j && j.status) flash("Toggled video + mic");
    refreshZoomStatus();
  });
  document.getElementById("vidBtn").addEventListener("click", async () => {
    const j = await post("/zoom_meeting/toggle_video");
    if (j && j.status) flash("Toggled video");
    refreshZoomStatus();
  });
  document.getElementById("micBtn").addEventListener("click", async () => {
    const j = await post("/zoom_meeting/toggle_mic");
    if (j && j.status) flash("Toggled mic");
    refreshZoomStatus();
  });

  // ---- zoom state polling ----
  const zoomStatusEl = document.getElementById("zoomStatus");
  function fmtTime(epoch) {
    if (!epoch) return "—";
    const d = new Date(epoch * 1000);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  const airBtnEl = document.getElementById("airBtn");
  function renderZoomStatus(s) {
    if (!s) { zoomStatusEl.innerHTML = ""; return; }
    const observed = !!s.observed;
    const tag = observed ? "live" : "assumed";
    const airCls = s.air_on ? "air-on" : "air-off";
    const airTxt = (s.air_on ? "ON AIR" : "OFF AIR") + " (" + tag + ")";
    // Tally-light: green when off-air, red when on-air. Pills carry the same
    // info; this just makes the state legible at a glance.
    airBtnEl.classList.toggle("on-air",  !!s.air_on);
    airBtnEl.classList.toggle("off-air", !s.air_on);
    const vidCls = s.video_on ? "on" : "off";
    const micCls = s.mic_on ? "on" : "off";
    let focusPill;
    if (!s.focus_check_active) {
      focusPill = `<span class="pill warn">focus check off</span>`;
    } else if (s.zoom_focused) {
      focusPill = `<span class="pill on">Zoom focused</span>`;
    } else {
      const fg = s.foreground_process || "no window";
      focusPill = `<span class="pill warn">⚠ ${fg}</span>`;
    }
    let truthPill;
    if (!s.uia_available) {
      truthPill = `<span class="pill warn">UIA off</span>`;
    } else if (observed) {
      truthPill = `<span class="pill on">● live</span>`;
    } else if (s.in_meeting === false) {
      truthPill = `<span class="pill warn">no meeting</span>`;
    } else {
      truthPill = `<span class="pill warn">toolbar hidden</span>`;
    }
    zoomStatusEl.innerHTML =
      `<span class="pill ${airCls}">${airTxt}</span>` +
      `<span class="pill ${vidCls}">VID ${s.video_on ? "ON" : "OFF"}</span>` +
      `<span class="pill ${micCls}">MIC ${s.mic_on ? "ON" : "OFF"}</span>` +
      truthPill +
      focusPill +
      `<span class="pill">last ${fmtTime(s.last_toggled)}</span>`;
  }
  async function refreshZoomStatus() {
    try {
      const r = await fetch("/zoom_meeting/state");
      if (r.ok) renderZoomStatus(await r.json());
    } catch (e) { /* network blip — ignore */ }
  }
  refreshZoomStatus();
  setInterval(refreshZoomStatus, 2000);

  // ---- live snapshot preview ----
  // Poll the camera's snapshot URL (configured server-side) and swap into the
  // visible <img> only after the new frame finishes loading.
  // only after the new frame finishes loading. Avoids flicker / blanks.
  (function() {
    const imgEl = document.getElementById("preview");
    const base = imgEl.src.split("?")[0];
    const PERIOD_MS = 400;
    function tick() {
      const next = new Image();
      next.onload = () => { imgEl.src = next.src; setTimeout(tick, PERIOD_MS); };
      next.onerror = () => { setTimeout(tick, 1500); };
      next.src = base + "?t=" + Date.now();
    }
    tick();
  })();
</script>
</body>
</html>
"""


def main():
    log.info("Chapel RL500 Controller starting on http://0.0.0.0:%s", SERVER_PORT)
    log.info("  Camera:        %s:%s", CAMERA_IP, VISCA_PORT)
    log.info("  Presets:       %s", PRESETS)
    log.info("  Open on phone: http://<this-pc-ip>:%s", SERVER_PORT)
    if _kbd is None:
        log.warning("  pynput keyboard unavailable — Zoom hotkeys disabled.")
    if zoom_reader.available:
        zoom_reader.start()
    else:
        log.warning("  uiautomation unavailable — Zoom state will be optimistic only.")
    # AF should always be on at the chapel — assert it now so a power-cycled
    # camera or a stray manual-focus poke from the IR remote can't leave us
    # stuck out of focus mid-service.
    try:
        camera.focus_auto(True)
        log.info("  Autofocus: enabled")
    except Exception as e:
        log.warning("  Could not enable autofocus at startup: %s", e)
    # Quiet Werkzeug's per-request access spam in the rotating log; healthz polls every few seconds.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    app.run(host="0.0.0.0", port=SERVER_PORT, debug=False, threaded=True)


if __name__ == "__main__":
    sys.exit(main())
