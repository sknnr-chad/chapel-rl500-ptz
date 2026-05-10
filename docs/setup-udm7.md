# UniFi UDM7 setup

> This is the reference router setup for the chapel deployment. Hostnames
> and SSIDs (`Chapel-AV-PC`, `chapel-ptz`) are examples — substitute your
> own. The IP plan (`192.168.100.0/24` to match the camera's factory IP)
> applies to any deployment of a camera that ships on `192.168.100.x`.

## Why the UDM7

One device, three jobs:

1. **PoE-out on port 1** powers the RL500 directly. No PoE+ injector, no
   extra brick, no extra cable run.
2. **Integrated switching** for Chapel-AV-PC (and anything else the AV
   team wants to plug in later).
3. **Wi-Fi** for the operator's phone, on a dedicated SSID isolated from
   the church's public network.

The alternative is a small router + a PoE+ injector + an unmanaged switch
— three boxes, three power bricks, three points of failure on a shelf in
the back. The UDM7 collapses all of it. Same money, less to mount, easier
to reason about when something breaks.

## Physical wiring

```
  Church network ── [WAN] UDM7  [Port 1, PoE]  ─── RL500 camera
                          │     [LAN port]      ─── Chapel-AV-PC
                          │     Wi-Fi           ··· Operator's phone
```

Plug the camera into **port 1** specifically — that's the PoE-capable
port that powers the RL500. Chapel-AV-PC goes on any other LAN port.

## LAN subnet

The chapel LAN uses **`192.168.100.0/24`**. This was chosen to match the
RL500's factory default IP (`192.168.100.88`), so the camera stays on its
shipping address and the network is configured around it rather than the
other way around.

Reserved/known hosts:

| Host | IP | How |
|---|---|---|
| RL500 PTZ camera | `192.168.100.88` | Static (camera factory default) |
| Chapel-AV-PC | `192.168.100.10` | DHCP reservation in the UDM7 |
| UDM7 itself | `192.168.100.1` (typical) | Default gateway |

The DHCP reservation for Chapel-AV-PC keeps the operator's phone bookmark
(`http://192.168.100.10:8080`) stable across reboots.

## Initial config

1. **Adopt the UDM7** through the UniFi setup flow. Pick "Standalone" /
   site-local controller; no UI Hosting required.
2. **WAN** — DHCP from the church uplink unless the church IT person hands
   you a static.
3. **LAN** — set the default network to `192.168.100.0/24`. Gateway
   `192.168.100.1`, DHCP pool e.g. `192.168.100.50–200`.
4. **DHCP reservation** — once Chapel-AV-PC has booted on the LAN, find
   its MAC in the UDM7 client list and reserve `192.168.100.10`.
5. **Wi-Fi** — create a single SSID like `chapel-ptz` with a strong
   password. Only the operator's phone connects to it. Optionally hide
   the SSID so visitors don't try to join.

## Static lease for the camera

Two options:

- **Camera-side static** (preferred — survives router replacement):
  log into the camera's web UI, set `192.168.100.88` static, gateway
  `192.168.100.1`, DNS `1.1.1.1` (the camera doesn't need internet but
  some firmwares fault without DNS configured).
- **DHCP reservation** in the UDM7: only works if the camera is willing to
  pull DHCP, which the RL500 isn't always.

## Verify

From the operator's phone, after joining the `chapel-ptz` Wi-Fi:

- `http://192.168.100.88/snapshot.jpg` — should return a JPEG (the camera
  itself, no auth required).
- `http://192.168.100.10:8080` — should load the pew-ptz controller.
- `http://192.168.100.10:8080/healthz` — should return JSON with
  `keyboard_ok: true`.

If any fail, see [Troubleshooting](troubleshooting.md).

## Internet access

The phone, Chapel-AV-PC, and camera **don't** need internet for this app to
work. They do need it for Zoom itself to broadcast — that's what the WAN
port is for. If the church Wi-Fi is flaky and you're tempted to put
Chapel-AV-PC on the UDM7's Wi-Fi instead of cabled LAN, **don't** — wired
is far more reliable for a live broadcast.
