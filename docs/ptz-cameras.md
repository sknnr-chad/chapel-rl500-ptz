# PTZ camera compatibility

This app speaks the standard Sony **VISCA-over-IP** protocol. Anything that
also speaks VISCA-over-IP — and most broadcast-class PTZs do — should work
with little or no modification. The bytes in
[`pew_ptz/visca.py`](../pew_ptz/visca.py) are the unmodified Sony spec.

## Quick compatibility check

1. Plug the camera into the LAN with a known IP.
2. Set `PEW_PTZ_CAMERA_IP=<that-ip>` and run `pew-ptz` (or `python -m pew_ptz`).
3. Open the controller in a browser; tap any direction button and hold it.
4. Three outcomes:

| Result | Diagnosis |
|---|---|
| Camera moves smoothly, log shows `[visca] sent ... ack=...` | All good. |
| Camera moves, no ACK in log | Camera replies on a different port or doesn't reply at all. The app handles this — UI works fine. |
| No movement, no ACK | Wrong protocol, wrong port, or VISCA not enabled. See below. |
| Camera moves *opposite* of what you tapped | Pan/tilt direction bytes are inverted for that firmware. Swap `PAN_LEFT`/`PAN_RIGHT` (or `TILT_UP`/`TILT_DOWN`) in `visca.py`. |

## Cameras tested / expected to work

| Camera family | Status | Notes |
|---|---|---|
| ClearTouch RL500 | ✅ reference deployment | UDP 52381, snapshot at `/snapshot.jpg` (no auth) |
| PTZOptics Move/Studio Pro/Eagle/Hive | Expected | UDP 52381 by default. Some older firmware uses TCP 5678 — see below. |
| Sony BRC / SRG | Expected | The reference implementation of VISCA-over-IP. |
| AVer CAM/PTC | Expected | UDP 52381. Snapshot path varies by firmware. |
| Marshall CV-series PTZ | Expected | VISCA must be enabled in the camera web UI; off by default on some models. |
| HuddleCam HC | Expected | UDP 52381. |
| BirdDog PTZ | Expected | VISCA-over-IP available alongside NDI. |
| Lumens VC-A | Expected | UDP 52381. |
| Generic Chinese OEM PTZ | Mixed | Most work. A few only expose HTTP CGI control — see "HTTP CGI" below. |

PRs welcome to confirm or correct the "Expected" rows.

## Camera-side setup

Whatever your camera vendor, you'll typically need to:

1. **Set a static IP** that you'll put in `PEW_PTZ_CAMERA_IP`. Either via
   the camera's web UI or via a DHCP reservation in your router.
2. **Enable VISCA-over-IP** if it isn't on by default. Look for it under
   *Network*, *Control*, *Protocol*, or *VISCA* in the camera's web UI.
3. **Confirm port and address**. The defaults — UDP 52381, address 1 — match
   what this app sends. Some cameras let you change either; if you change
   them, update `PEW_PTZ_VISCA_PORT` and (if needed) the address byte in
   `visca.py`'s command builders.

## Live preview snapshot URL

The web UI's sticky preview tile polls an HTTP snapshot URL. Set
`PEW_PTZ_CAMERA_SNAPSHOT_PATH` to whatever your camera uses:

| Vendor / firmware | Snapshot path | Auth |
|---|---|---|
| ClearTouch RL500 | `/snapshot.jpg` | none |
| Many generic OEMs | `/snapshot.jpg` | varies |
| Hikvision / ISAPI | `/Streaming/channels/1/picture` | basic |
| Generic CGI | `/cgi-bin/snapshot.cgi` | varies |
| Axis | `/axis-cgi/jpg/image.cgi` | basic |
| PTZOptics (newer) | `/snapshot.jpg` | varies |

Quick way to find yours: log into the camera's web UI in a browser, view
the source on the live-preview page, and look for an `<img>` tag — that's
the URL the camera uses internally and it'll work for us too.

If the path needs HTTP basic auth, embed the credentials in the URL on the
camera side, or set the camera to allow unauthenticated snapshot for the
LAN. (A future version may add `PEW_PTZ_CAMERA_SNAPSHOT_USER` / `_PASSWORD`
env vars; for now you'll edit the template if your camera requires it.)

### Cameras without an HTTP snapshot

Some cameras only expose video over **RTSP**. Browsers cannot play RTSP
directly — there is no `<video src="rtsp://...">`. Two practical options:

1. **Run a transcoder** like [MediaMTX](https://github.com/bluenviron/mediamtx)
   alongside `pew-ptz` on the host PC. Point MediaMTX at the camera's RTSP
   URL and have it serve HLS at e.g. `http://localhost:8888/cam/index.m3u8`.
   Then update `pew_ptz/server.py`'s preview `<img>` to a `<video>` element
   pointed at the HLS URL (use [hls.js](https://github.com/video-dev/hls.js/)
   for non-Safari browsers). This is robust but adds a service dependency
   and is out of scope for this repo today.
2. **Drop the embedded preview** and replace the preview tile with a link
   to the camera's own web UI. Operator gets one extra tap per check, but
   you lose nothing else. Edit the `<div class="preview-wrap">` block in
   `server.py`.

If anyone wants to ship MediaMTX integration, PRs welcome.

## If your firmware doesn't speak VISCA-over-IP UDP

Some PTZs ship with VISCA on **TCP 5678** instead of UDP 52381, or expose
only an HTTP CGI control surface. The fix is small: swap the transport in
[`pew_ptz/visca.py::ViscaIP._send`](../pew_ptz/visca.py). The command
builders (`pan_tilt`, `zoom_tele`, etc.) stay identical because those bytes
are the standard Sony VISCA payload, regardless of how it's transported.

### TCP VISCA sketch

```python
def _send(self, payload):
    with self._lock, closing(socket.create_connection((self.host, 5678), 0.5)) as s:
        s.sendall(payload)        # no header, just raw VISCA bytes
        try:
            return s.recv(64)
        except socket.timeout:
            return None
```

### HTTP CGI sketch (varies by firmware)

```python
def _send(self, payload):
    cmd_hex = payload.hex()
    requests.get(f"http://{self.host}/cgi-bin/visca?cmd={cmd_hex}", timeout=0.5)
```

Send a screenshot of the camera's "Network → Control Protocol" page to
yourself before you start — that page tells you which transport the camera
expects.

## Setting presets

The web UI is **recall-only by design** — there is no `/preset/set` HTTP
route. This prevents the operator from accidentally overwriting a preset
mid-event from the phone.

To program presets, use the camera's built-in web UI or the IR remote:

1. Drive the camera into the framing you want.
2. Save it to slot N on the camera.
3. Confirm slot N matches the position you want the button at — the app
   maps slot N on the camera to the Nth name in `PEW_PTZ_PRESETS`
   (1-indexed: slot 1 = first name, slot 2 = second name, …).

The camera stores presets in non-volatile memory; the app stores no preset
state at all.

## A note on preview latency

The sticky preview at the top of the page polls the snapshot URL every
~400 ms. On a typical LAN, expect each snapshot to arrive in 30–80 ms plus
the 400 ms poll period — call it half a second of perceived lag.

That's enough for framing checks but not for chasing fast movement. For
small adjustments, use line-of-sight to the actual scene (you can usually
see what you're framing) and treat the preview as a sanity check, not a
viewfinder.
