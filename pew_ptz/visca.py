"""
VISCA-over-IP client for the ClearTouch RL500.

Wire format (Sony VISCA-over-IP, UDP 52381):

    | payload type (2) | payload length (2) | seq (4) | VISCA payload |

Payload type 0x01 0x00 = VISCA command. Sequence is a per-socket counter.
The camera replies with the same seq, payload type 0x01 0x11 (ACK / completion).
We send best-effort and drain any reply with a short timeout so the UI stays snappy.
"""

from __future__ import annotations

import socket
import threading
from contextlib import closing

DEFAULT_PORT = 52381

# Pan/tilt directions (Sony VISCA pan-tilt-drive byte layout)
PAN_LEFT, PAN_RIGHT, PAN_STOP = 0x01, 0x02, 0x03
TILT_UP, TILT_DOWN, TILT_STOP = 0x01, 0x02, 0x03

# Speed clamps from the VISCA spec
PAN_SPEED_MAX = 0x18   # 1..24
TILT_SPEED_MAX = 0x14  # 1..20
ZOOM_SPEED_MAX = 0x07  # 0..7


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


class ViscaIP:
    def __init__(self, host: str, port: int = DEFAULT_PORT, recv_timeout: float = 0.15):
        self.host = host
        self.port = port
        self.recv_timeout = recv_timeout
        self._seq = 0
        self._lock = threading.Lock()

    def _send(self, payload: bytes) -> bytes | None:
        """Send one VISCA payload, return the camera's reply payload or None."""
        with self._lock:
            self._seq = (self._seq + 1) & 0xFFFFFFFF
            header = (
                b"\x01\x00"
                + len(payload).to_bytes(2, "big")
                + self._seq.to_bytes(4, "big")
            )
            frame = header + payload
            try:
                with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as sock:
                    sock.settimeout(self.recv_timeout)
                    sock.sendto(frame, (self.host, self.port))
                    try:
                        data, _ = sock.recvfrom(64)
                        return data[8:] if len(data) >= 8 else data
                    except socket.timeout:
                        return None
            except OSError as e:
                print(f"[visca] send error: {e}")
                return None

    # ---- pan / tilt ----

    def pan_tilt(self, pan_dir: int, tilt_dir: int, pan_speed: int = 12, tilt_speed: int = 12):
        ps = _clamp(pan_speed, 1, PAN_SPEED_MAX)
        ts = _clamp(tilt_speed, 1, TILT_SPEED_MAX)
        return self._send(bytes([0x81, 0x01, 0x06, 0x01, ps, ts, pan_dir, tilt_dir, 0xFF]))

    def pan_tilt_stop(self):
        return self.pan_tilt(PAN_STOP, TILT_STOP, 1, 1)

    # ---- zoom ----

    def zoom_tele(self, speed: int = 4):
        s = _clamp(speed, 0, ZOOM_SPEED_MAX)
        return self._send(bytes([0x81, 0x01, 0x04, 0x07, 0x20 | s, 0xFF]))

    def zoom_wide(self, speed: int = 4):
        s = _clamp(speed, 0, ZOOM_SPEED_MAX)
        return self._send(bytes([0x81, 0x01, 0x04, 0x07, 0x30 | s, 0xFF]))

    def zoom_stop(self):
        return self._send(bytes([0x81, 0x01, 0x04, 0x07, 0x00, 0xFF]))

    # ---- focus ----

    def focus_auto(self, on: bool = True):
        mode = 0x02 if on else 0x03
        return self._send(bytes([0x81, 0x01, 0x04, 0x38, mode, 0xFF]))

    # ---- presets (0..254) ----

    def preset_recall(self, n: int):
        n = _clamp(n, 0, 254)
        return self._send(bytes([0x81, 0x01, 0x04, 0x3F, 0x02, n, 0xFF]))
