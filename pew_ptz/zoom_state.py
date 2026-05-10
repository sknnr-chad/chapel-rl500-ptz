"""
Read Zoom's *actual* mute/video state via Windows UI Automation.

Zoom's in-meeting toolbar exposes the mute/video buttons through UIA, and
their accessible names flip with state:

    "mute my microphone"   -> mic is currently UNMUTED  (the button mutes you)
    "unmute my microphone" -> mic is currently MUTED
    "start video"          -> video is currently OFF
    "stop video"           -> video is currently ON

So we walk the Zoom meeting window every ~1.5s in a background thread and
return ground truth instead of guessing from the toggles we sent.

Requires the user to have enabled, in Zoom Workplace 7.x:
    Settings -> Meetings & Webinars -> Controls -> "Keep meeting controls visible"
(In older Zoom builds this was Settings -> Accessibility -> "Always show
meeting controls".) Without it the toolbar auto-hides a few seconds after
the last mouse move and the buttons drop out of the UIA tree.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("pew_ptz.zoom_state")

try:
    import uiautomation as auto  # type: ignore
    import comtypes  # comes in with uiautomation
    _UIA_OK = True
except Exception as e:  # pragma: no cover - non-Windows / missing dep
    auto = None  # type: ignore
    comtypes = None  # type: ignore
    _UIA_OK = False
    log.warning("uiautomation unavailable: %s", e)


# Substrings to look for on the toolbar buttons. Lowercase + substring match
# because Zoom localizes / tweaks these strings across versions.
#
# Zoom Workplace 7.x embeds the *current* state in the button name itself, e.g.
#   "Mute, currently unmuted, Alt+A, Noise removal is on. Mute my audio (Alt+A)"
#   "Unmute, currently muted, Alt+A, ..."
#   "Stop my video, Alt+V"
#   "Start my video, Alt+V"
# So we match on "currently (un)muted" first (unambiguous), then fall back to
# the older button-action strings for legacy Zoom builds.
_MIC_ON_NAMES   = ("currently unmuted", "mute my microphone", "mute my mic", "mute audio")
_MIC_OFF_NAMES  = ("currently muted",   "unmute my microphone", "unmute my mic", "unmute audio")
_VID_ON_NAMES   = ("stop my video", "stop video")
_VID_OFF_NAMES  = ("start my video", "start video")

# Zoom meeting-window class names we've seen. Exact match isn't required —
# we just use this to short-circuit the search to plausible windows first.
_MEETING_CLASS_HINTS = (
    "ConfMultiTabContentWndClass",   # Zoom Workplace 7.x meetings + webinars
    "ZPMeetingWndClass",
    "ZPContentViewWndClass",
    "ZPFTMainFrameWndClassName",
    "ZPMeetingMainWindow",
)

# Top-level window names we should ignore even though they contain "zoom" —
# these are Zoom *Workplace* (the home/launcher app), not an active meeting.
_NON_MEETING_NAME_HINTS = ("zoom workplace",)


@dataclass
class ZoomState:
    observed: bool = False           # True if UIA gave us real state this cycle
    in_meeting: bool = False         # True if we found a meeting window
    mic_on: bool | None = None
    video_on: bool | None = None
    last_read: float = 0.0
    last_observed: float = 0.0       # last successful observation (mic or video)
    error: str | None = None
    poll_count: int = 0
    walk_ms: int = 0                 # how long the last UIA walk took


class ZoomStateReader:
    def __init__(self, poll_interval: float = 1.5, max_walk_depth: int = 10):
        self.poll_interval = poll_interval
        self.max_walk_depth = max_walk_depth
        self._lock = threading.Lock()
        self._state = ZoomState()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Cross-thread debug snapshot: request handlers can't call UIA
        # directly because the COM object is bound to the poller thread's
        # apartment. The handler sets _debug_req, the poller fulfils it on
        # its next wake, and signals _debug_done.
        self._debug_req = threading.Event()
        self._debug_done = threading.Event()
        self._debug_result: dict | None = None

    @property
    def available(self) -> bool:
        return _UIA_OK

    def start(self) -> None:
        if not _UIA_OK or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="zoom-uia-poller"
        )
        self._thread.start()
        log.info("Zoom UIA poller started (interval=%.1fs)", self.poll_interval)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def trigger_refresh(self) -> None:
        """Wake the poller so we re-read state right after sending a toggle."""
        self._wake.set()

    def debug_snapshot(self, timeout: float = 5.0) -> dict:
        """Public entry point: ask the poller thread to take a snapshot."""
        if not _UIA_OK:
            return {"uia_available": False, "windows": []}
        if self._thread is None or not self._thread.is_alive():
            return {"uia_available": True, "error": "poller not running", "windows": []}
        self._debug_done.clear()
        self._debug_req.set()
        self._wake.set()  # wake the poller so it handles us right away
        if not self._debug_done.wait(timeout):
            return {"uia_available": True, "error": "timeout waiting for poller", "windows": []}
        return self._debug_result or {"uia_available": True, "error": "empty", "windows": []}

    def _do_debug_snapshot(self) -> dict:
        """Runs ON the poller thread, where COM is properly initialized."""
        root = auto.GetRootControl()
        windows = []
        for win in root.GetChildren():
            try:
                cls = win.ClassName or ""
                name = win.Name or ""
            except Exception:
                continue
            if not name and not cls:
                continue
            entry = {"name": name, "class": cls}
            looks_zoomish = (
                "zoom" in name.lower() or "zoom" in cls.lower()
                or any(h in cls for h in _MEETING_CLASS_HINTS)
            )
            if looks_zoomish:
                buttons = []
                stack = [(win, 0)]
                while stack and len(buttons) < 80:
                    node, depth = stack.pop()
                    if depth > self.max_walk_depth:
                        continue
                    try:
                        if node.ControlTypeName == "ButtonControl":
                            bn = node.Name or ""
                            if bn:
                                buttons.append(bn)
                        for c in node.GetChildren():
                            stack.append((c, depth + 1))
                    except Exception:
                        continue
                entry["buttons"] = buttons
            windows.append(entry)
        return {"uia_available": True, "windows": windows}

    def get(self) -> dict:
        with self._lock:
            s = self._state
            return {
                "observed":      s.observed,
                "in_meeting":    s.in_meeting,
                "mic_on":        s.mic_on,
                "video_on":      s.video_on,
                "last_read":     s.last_read,
                "last_observed": s.last_observed,
                "error":         s.error,
                "poll_count":    s.poll_count,
                "walk_ms":       s.walk_ms,
                "uia_available": _UIA_OK,
            }

    # ---- internals -------------------------------------------------------

    def _loop(self) -> None:
        # COM is per-thread on Windows. uiautomation does NOT initialize COM
        # for arbitrary threads; if we skip this every UIA call from this
        # thread fails with WinError -2147221008 ("CoInitialize has not been
        # called"). Apartment-threaded (default) is what we want for UIA.
        try:
            comtypes.CoInitialize()
            log.info("Zoom UIA poller: CoInitialize OK on poller thread")
        except Exception as e:
            log.error("Zoom UIA poller: CoInitialize failed: %s", e)
            return

        # Initial small delay so we don't race the Flask startup banner
        time.sleep(0.5)
        consecutive_errors = 0
        while not self._stop.is_set():
            t0 = time.perf_counter()
            try:
                snap = self._read_once()
                err = None
                consecutive_errors = 0
            except Exception as e:
                snap = ZoomState()
                err = f"{type(e).__name__}: {e}"
                consecutive_errors += 1
                # Loud the first time, then quiet down so we don't spam the log
                if consecutive_errors == 1 or consecutive_errors % 20 == 0:
                    log.warning("UIA read failed (%d in a row): %s", consecutive_errors, err)
            walk_ms = int((time.perf_counter() - t0) * 1000)

            now = time.time()
            with self._lock:
                self._state.poll_count += 1
                self._state.last_read = now
                self._state.walk_ms = walk_ms
                self._state.error = err
                self._state.in_meeting = snap.in_meeting
                if snap.observed:
                    self._state.observed = True
                    self._state.mic_on = snap.mic_on
                    self._state.video_on = snap.video_on
                    self._state.last_observed = now
                else:
                    # Lost sight of the toolbar (toolbar hidden, meeting ended,
                    # Zoom minimized). Don't clobber the last known values —
                    # just mark observed=False so callers know it's stale.
                    self._state.observed = False

            # Service any pending debug-snapshot request before sleeping
            if self._debug_req.is_set():
                self._debug_req.clear()
                try:
                    self._debug_result = self._do_debug_snapshot()
                except Exception as e:
                    self._debug_result = {
                        "uia_available": True,
                        "error": f"{type(e).__name__}: {e}",
                        "windows": [],
                    }
                self._debug_done.set()

            self._wake.wait(self.poll_interval)
            self._wake.clear()

    def _read_once(self) -> ZoomState:
        if not _UIA_OK:
            return ZoomState()
        root = auto.GetRootControl()
        meeting_win = None
        for win in root.GetChildren():
            try:
                cls = win.ClassName or ""
                if any(h in cls for h in _MEETING_CLASS_HINTS):
                    meeting_win = win
                    break
            except Exception:
                continue
        # Fallback: any top-level window whose name mentions Zoom Meeting/Webinar.
        # Skip the launcher window ("Zoom Workplace") — it doesn't have the
        # mute/video toolbar.
        if meeting_win is None:
            for win in root.GetChildren():
                try:
                    name = (win.Name or "").lower()
                    if "zoom" not in name:
                        continue
                    if any(skip in name for skip in _NON_MEETING_NAME_HINTS):
                        continue
                    if "meeting" in name or "webinar" in name:
                        meeting_win = win
                        break
                except Exception:
                    continue
        if meeting_win is None:
            return ZoomState(observed=False, in_meeting=False)

        mic_on, video_on = self._scan_buttons(meeting_win)
        observed = (mic_on is not None) or (video_on is not None)
        return ZoomState(
            observed=observed,
            in_meeting=True,
            mic_on=mic_on,
            video_on=video_on,
        )

    def _scan_buttons(self, root_ctrl) -> tuple[bool | None, bool | None]:
        """Walk the meeting window and read mute/video button names."""
        mic_on: bool | None = None
        video_on: bool | None = None

        stack = [(root_ctrl, 0)]
        while stack:
            node, depth = stack.pop()
            if depth > self.max_walk_depth:
                continue
            try:
                ct = node.ControlTypeName
            except Exception:
                ct = ""
            if ct == "ButtonControl":
                try:
                    n = (node.Name or "").lower()
                except Exception:
                    n = ""
                if mic_on is None:
                    if any(s in n for s in _MIC_ON_NAMES):
                        mic_on = True
                    elif any(s in n for s in _MIC_OFF_NAMES):
                        mic_on = False
                if video_on is None:
                    if any(s in n for s in _VID_ON_NAMES):
                        video_on = True
                    elif any(s in n for s in _VID_OFF_NAMES):
                        video_on = False
                if mic_on is not None and video_on is not None:
                    return mic_on, video_on
            try:
                children = node.GetChildren()
            except Exception:
                children = []
            for child in children:
                stack.append((child, depth + 1))
        return mic_on, video_on
