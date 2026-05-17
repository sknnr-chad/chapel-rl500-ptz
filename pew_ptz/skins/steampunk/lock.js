/* Steampunk dial-lock — driven by window.PEW_LOCK from the server template.
 *
 * The skin's job: render 4 dials, let the user pick a digit on each, then
 * POST {pin: "1234"} to cfg.submitUrl when the operator taps the UNLOCK
 * button. (Earlier versions auto-submitted on the 4th-dial settle, but
 * that made any PIN starting or ending in 0 impossible to enter without
 * pointlessly toggling the dial — explicit button it is.) Auth itself
 * lives entirely server-side in pew_ptz.auth.
 *
 * Dial mechanic: each dial is a "drum" rendered as 3 stacked copies of 0-9
 * (so dragging across the 9→0 boundary stays visually continuous). After
 * each settle we silently fold the logical position back into [0,10) —
 * moving by 10 positions equals exactly one copy-height, so the visible
 * digit never changes.
 */
(function () {
  const cfg = window.PEW_LOCK;

  const SLAT_H = 80;
  const WINDOW_H = 240;
  const DIGITS = 10;
  const COPIES = 3;
  const CENTER_COPY = 1;
  const SNAP_MS = 280;

  const plate = document.getElementById("plate");
  const statusEl = document.getElementById("status");
  const unlockBtn = document.getElementById("unlockBtn");
  const dials = Array.from(document.querySelectorAll(".dial"));

  function buildStrip(strip) {
    for (let c = 0; c < COPIES; c++) {
      for (let i = 0; i < DIGITS; i++) {
        const el = document.createElement("div");
        el.className = "digit";
        el.textContent = String(i);
        strip.appendChild(el);
      }
    }
  }
  dials.forEach((d) => buildStrip(d.querySelector(".strip")));

  const state = dials.map((d) => ({
    el: d,
    strip: d.querySelector(".strip"),
    position: 0,
    pointerId: null,
    dragStartY: 0,
    dragStartPosition: 0,
    didDrag: false,
    normalizeTimer: null,
  }));

  function offsetFor(pos) {
    // Center digit `pos` (within middle copy at index CENTER_COPY*DIGITS+pos)
    // in a window of height WINDOW_H.
    return WINDOW_H / 2 - SLAT_H / 2 - (CENTER_COPY * DIGITS + pos) * SLAT_H;
  }

  function applyTransform(s, animate) {
    s.strip.style.transition = animate
      ? `transform ${SNAP_MS}ms cubic-bezier(.2,1,.3,1)`
      : "none";
    s.strip.style.transform = `translateY(${offsetFor(s.position)}px)`;
  }

  function digitAt(s) {
    return ((Math.round(s.position) % DIGITS) + DIGITS) % DIGITS;
  }

  function scheduleNormalize(s) {
    clearTimeout(s.normalizeTimer);
    s.normalizeTimer = setTimeout(() => {
      const wrapped = ((s.position % DIGITS) + DIGITS) % DIGITS;
      if (wrapped !== s.position) {
        s.position = wrapped;
        applyTransform(s, false);
      }
    }, SNAP_MS + 20);
  }

  function snap(s) {
    s.position = Math.round(s.position);
    applyTransform(s, true);
    scheduleNormalize(s);
  }

  function currentPin() {
    return state.map(digitAt).join("");
  }

  function bindDial(s) {
    s.el.addEventListener("pointerdown", (e) => {
      if (cfg.lockoutSecondsRemaining > 0) return;
      e.preventDefault();
      s.pointerId = e.pointerId;
      try { s.el.setPointerCapture(e.pointerId); } catch (_) {}
      s.dragStartY = e.clientY;
      s.dragStartPosition = s.position;
      s.didDrag = false;
      clearTimeout(s.normalizeTimer);
    });
    s.el.addEventListener("pointermove", (e) => {
      if (e.pointerId !== s.pointerId) return;
      const dy = e.clientY - s.dragStartY;
      if (Math.abs(dy) > 4) s.didDrag = true;
      // Drag up (dy < 0) increases position — next digit revealed at center.
      s.position = s.dragStartPosition - dy / SLAT_H;
      applyTransform(s, false);
    });
    const release = (e) => {
      if (e.pointerId !== s.pointerId) return;
      try { s.el.releasePointerCapture(e.pointerId); } catch (_) {}
      s.pointerId = null;
      if (!s.didDrag) {
        // Tap: top half = +1, bottom half = -1
        const rect = s.el.getBoundingClientRect();
        const localY = e.clientY - rect.top;
        const delta = localY < rect.height / 2 ? +1 : -1;
        s.position = Math.round(s.position) + delta;
      }
      snap(s);
    };
    s.el.addEventListener("pointerup", release);
    s.el.addEventListener("pointercancel", release);
  }
  state.forEach(bindDial);
  state.forEach((s) => applyTransform(s, false));

  // ---- submit / status -------------------------------------------------

  function setStatus(msg, mode) {
    statusEl.textContent = msg;
    statusEl.dataset.mode = mode || "idle";
  }

  function shake() {
    plate.classList.remove("shake");
    void plate.offsetWidth;  // restart the animation
    plate.classList.add("shake");
  }

  function resetDials() {
    state.forEach((s) => {
      s.position = 0;
      clearTimeout(s.normalizeTimer);
      applyTransform(s, true);
    });
  }

  let inFlight = false;
  function setButtonEnabled(enabled) {
    if (!unlockBtn) return;
    unlockBtn.disabled = !enabled;
  }

  async function submit() {
    if (inFlight) return;
    if (cfg.lockoutSecondsRemaining > 0) return;
    inFlight = true;
    setButtonEnabled(false);
    const pin = currentPin();
    setStatus("Checking…", "busy");
    let res;
    try {
      const body = Object.assign({ pin }, cfg.extraPayload || {});
      res = await fetch(cfg.submitUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json",
        },
        body: JSON.stringify(body),
        credentials: "same-origin",
      });
    } catch (e) {
      setStatus("Network error", "error");
      shake();
      inFlight = false;
      setButtonEnabled(true);
      return;
    }
    let data = {};
    try { data = await res.json(); } catch (_) {}

    if (res.ok) {
      setStatus(data.message || "Unlocked", "success");
      setTimeout(() => {
        const dest = data.redirect || cfg.onSuccessUrl || "/";
        window.location.href = dest;
      }, 280);
      return;  // leave inFlight true — navigating away
    }

    if (res.status === 429 && data.lockout_seconds_remaining) {
      cfg.lockoutSecondsRemaining = data.lockout_seconds_remaining;
      startCountdown(data.lockout_seconds_remaining);
      shake();
      resetDials();
      inFlight = false;
      // Button stays disabled — countdown re-enables it.
      return;
    }

    setStatus(data.error || "Wrong PIN", "error");
    shake();
    resetDials();
    inFlight = false;
    setButtonEnabled(true);
  }

  if (unlockBtn) {
    unlockBtn.addEventListener("click", submit);
  }

  let countdownTimer = null;
  function startCountdown(seconds) {
    let remaining = seconds;
    clearInterval(countdownTimer);
    setButtonEnabled(false);
    const render = () => setStatus(`Locked. Try again in ${remaining}s`, "locked");
    render();
    countdownTimer = setInterval(() => {
      remaining -= 1;
      if (remaining <= 0) {
        clearInterval(countdownTimer);
        cfg.lockoutSecondsRemaining = 0;
        setStatus(cfg.idleMessage, "idle");
        setButtonEnabled(true);
      } else {
        render();
      }
    }, 1000);
  }

  if (cfg.lockoutSecondsRemaining > 0) {
    startCountdown(cfg.lockoutSecondsRemaining);
  }
})();
