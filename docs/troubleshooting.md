# Troubleshooting

> The server runs as a Scheduled Task with output redirected to
> `C:\tools\pew-ptz\logs\server.log`. There is no console window.
> `Get-Content C:\tools\pew-ptz\logs\server.log -Tail 80` is your friend.

## "Page won't load on my phone"

1. Make sure the phone is on the **same Wi-Fi as the host PC and the camera**.
   If you have a dedicated controller SSID, double-check the phone is on it
   and not your guest network or personal hotspot.
2. From a desktop on the same LAN, try `http://<host-pc-ip>:8080` first to
   isolate phone vs. server.
3. Confirm the server is actually running:
   `Get-ScheduledTaskInfo -TaskName "Pew PTZ Controller"` and look at
   `LastTaskResult` (0 = running). Or just hit
   `http://localhost:8080/healthz` from the booth PC.
4. If the firewall rule went missing:
   `Get-NetFirewallRule -DisplayName "Pew PTZ Controller (TCP 8080)"`.
   Re-run `scripts\install.ps1` to recreate it.

## "The camera doesn't move"

Check `server.log` after pressing a direction button.

- **No log entry at all** → the request isn't reaching the server. See the
  page-loading section above.
- **`[visca] send error: ...`** → the server can't reach the camera's IP.
  From PowerShell on the booth PC: `ping 192.168.100.88` and
  `Test-NetConnection 192.168.100.88 -Port 52381`. (UDP isn't reliably
  probeable, so the port test will succeed even if VISCA itself isn't
  responding — start with the ping.)
- **Logs scroll, no movement, no ACK** → wrong protocol/port. See
  [docs/ptz-cameras.md](ptz-cameras.md#if-your-firmware-doesnt-speak-visca-over-ip-udp).
- **Logs scroll, camera moves wrong direction** → almost certainly the
  pan/tilt-direction bytes are swapped for *your* firmware. Try swapping
  `PAN_LEFT`/`PAN_RIGHT` (or `TILT_UP`/`TILT_DOWN`) values in
  `pew_ptz/visca.py` and report back.

## "The camera moves, then keeps moving forever"

That means `pointerup` isn't firing — usually because you dragged off the
button before releasing. The app catches `pointerleave` and `pointercancel`
too, so this should be rare. As a fallback, tap any other direction button:
the next drive command implicitly stops the previous motion. The HOME
button also resets framing back to preset 1.

## "Zoom video toggles but mic doesn't (or vice versa)"

Open Zoom → **Settings → Keyboard Shortcuts** and confirm:

- **Mute/unmute my audio** is `Alt+A`
- **Start/stop my video** is `Alt+V`
- Both have **Enable Global Shortcut** *off* — we want them to require Zoom
  focus so they can't fire while the operator is in another window.

If shortcuts are bound to different keys, change them in Zoom or update
`_alt_chord` calls in `pew_ptz/server.py`.

## "TOGGLE AIR does nothing"

Almost always: Zoom isn't the foreground window on the host PC. The
controller will pop a "⚠ Zoom not focused" toast on the phone when this
happens. Click on the Zoom meeting window to give it focus, then try again.

## "Zoom Meeting card pills say 'assumed' instead of '● live'"

The UI Automation reader can't see the toolbar. Check the small status pill
on the Zoom Meeting card:

| Pill | Meaning | Fix |
|---|---|---|
| `● live` | UIA observed real state | nothing to do |
| `toolbar hidden` | meeting open, toolbar auto-hid | enable *Settings → Meetings & Webinars → Controls → "Keep meeting controls visible"* in Zoom |
| `no meeting` | not in a meeting/webinar | join one |
| `UIA off` | `uiautomation` package didn't install or isn't loadable | re-run `scripts\install.ps1`; check `server.log` for `uiautomation unavailable: ...` |

If `● live` still doesn't appear after enabling the always-visible toolbar
and being in a meeting, hit `http://localhost:8080/zoom_meeting/debug` from
the booth PC. That endpoint dumps every top-level window's class+name and
all button names for any window that looks Zoom-ish — useful when a Zoom
upgrade renames the window class or button labels.

## "Server starts but logs `keyboard controller unavailable`"

You're not on Windows, or `pynput` couldn't get the permissions it needs.
Camera control still works; only the Zoom endpoints are disabled. On macOS
this typically means the Python interpreter needs to be granted Accessibility
permission in *System Settings → Privacy & Security*.

## "I can't reach the camera's web UI from the phone"

The phone needs to be able to route to `192.168.100.88` (or wherever the
camera lives). With the phone and camera on the same flat LAN this just
works. If you've put the camera on a separate VLAN or subnet, route
accordingly.

## "Latency on hold-to-move feels sluggish"

Two likely causes:

1. **Slow Wi-Fi between phone and router.** Check signal at the operator's
   seat; consider a closer access point.
2. **Camera ACK timeouts.** `ViscaIP(recv_timeout=...)` defaults to 150 ms.
   If your camera doesn't reply at all, every command waits the full
   timeout. Drop it to `0.05` or `0.0` (fire-and-forget) in
   `pew_ptz/server.py` where `ViscaIP` is constructed.

## Restarting the service

```powershell
Stop-ScheduledTask  -TaskName "Pew PTZ Controller"
Start-ScheduledTask -TaskName "Pew PTZ Controller"
```

Or just sign out and back in as the operator user — the task fires at logon.

## Updating the app

```powershell
cd C:\tools\pew-ptz
git pull
.\scripts\install.ps1   # idempotent: stops the task, upgrades, restarts
```

The installer always runs `pip install --upgrade` and re-registers the
Scheduled Task, so re-running it picks up new dependencies and any changes
to the launch wrapper.
