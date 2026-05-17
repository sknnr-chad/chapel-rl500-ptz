"""
pew_ptz.auth — PIN-based authentication for the LAN web UI.

Stores hashed PINs (and a persistent Flask session secret) in a JSON file so
PINs can be rotated at runtime without redeploying or restarting. On first
run the store seeds PINs from env vars (PEW_PTZ_USER_PIN_HASH,
PEW_PTZ_ADMIN_PIN_HASH); subsequent runs read the file. The session
secret_key is auto-generated on first run and persisted so phone sessions
survive server restarts.

State file:  <state_dir>/auth.json
  state_dir = $PEW_PTZ_STATE_DIR (default: current working directory)

CLI (the installer uses these — never pass PINs as argv):

    python -m pew_ptz.auth set --role user|admin   # set / rotate one PIN
    python -m pew_ptz.auth hash                    # print a hash for env seeding
    python -m pew_ptz.auth reset                   # delete the PIN store
    python -m pew_ptz.auth status                  # show changed_at timestamps
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import re
import secrets
import sys
import time
from functools import wraps
from pathlib import Path
from typing import Optional

from flask import jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

log = logging.getLogger("pew_ptz.auth")

PIN_RE = re.compile(r"^\d{4}$")
STATE_VERSION = 1
ROLES = ("user", "admin")

# 4-digit PINs = 10_000 combos. 5 failures inside RL_WINDOW_SEC triggers an
# RL_LOCKOUT_SEC cooldown for that IP. pbkdf2 hashing already costs ~50–100ms
# per attempt, so this is layered defense, not the only line.
RL_WINDOW_SEC = 60
RL_MAX_FAILS = 5
RL_LOCKOUT_SEC = 30


def _env(name: str, default: str = "") -> str:
    return os.environ.get("PEW_PTZ_" + name, default)


def hash_pin(pin: str) -> str:
    if not PIN_RE.match(pin):
        raise ValueError("PIN must be exactly 4 digits (0-9)")
    return generate_password_hash(pin)


def verify_pin(pin: str, pin_hash: str) -> bool:
    if not pin or not pin_hash:
        return False
    try:
        return check_password_hash(pin_hash, pin)
    except Exception:
        return False


class PinStore:
    """Persistent PIN + session-key store.

    File schema (auth.json):
        {
          "version": 1,
          "secret_key": "<hex>",
          "user":  { "hash": "...", "changed_at": 1715900000 },
          "admin": { "hash": "...", "changed_at": 1715900000 }
        }
    """

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "auth.json"
        self._data: dict = {}

    def load(self) -> None:
        """Load from file if present, else seed from env. Always leaves a
        valid secret_key in place (auto-generated on first run)."""
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as f:
                self._data = json.load(f)
            if self._data.get("version") != STATE_VERSION:
                raise RuntimeError(
                    f"auth.json has unexpected version {self._data.get('version')!r} "
                    f"(expected {STATE_VERSION})"
                )
        else:
            self._seed_from_env()

        if not self._data.get("secret_key"):
            self._data["secret_key"] = secrets.token_hex(32)
            self._save()

    def _seed_from_env(self) -> None:
        self._data = {
            "version": STATE_VERSION,
            "secret_key": secrets.token_hex(32),
            "user": None,
            "admin": None,
        }
        now = int(time.time())
        for role in ROLES:
            env_hash = _env(f"{role.upper()}_PIN_HASH")
            if env_hash:
                self._data[role] = {"hash": env_hash, "changed_at": now}
        self._save()

    def _save(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp, self.path)  # atomic on Windows + POSIX

    @property
    def secret_key(self) -> str:
        return self._data["secret_key"]

    def is_configured(self) -> bool:
        return all(self._data.get(r) for r in ROLES)

    def changed_at(self, role: str) -> Optional[int]:
        entry = self._data.get(role)
        return entry["changed_at"] if entry else None

    def set_pin(self, role: str, new_pin: str) -> None:
        if role not in ROLES:
            raise ValueError(f"unknown role: {role}")
        # Disallow PIN == other-role-PIN; otherwise verify() can't disambiguate
        # which role logged in.
        other = "admin" if role == "user" else "user"
        other_entry = self._data.get(other)
        if other_entry and verify_pin(new_pin, other_entry["hash"]):
            raise ValueError("PIN must differ from the other role's PIN")
        self._data[role] = {
            "hash": hash_pin(new_pin),
            "changed_at": int(time.time()),
        }
        self._save()

    def verify(self, pin: str) -> Optional[str]:
        """Return the matching role name, or None if no match. Checks admin
        first so a (disallowed) collision still favors higher privilege."""
        for role in ("admin", "user"):
            entry = self._data.get(role)
            if entry and verify_pin(pin, entry["hash"]):
                return role
        return None


class RateLimiter:
    """In-memory per-IP failure tracker. Resets on process restart, which is
    fine: a restart is rare on the chapel PC and brute force from LAN would
    leave loud traces in server.log either way."""

    def __init__(self):
        self._fails: dict[str, list[float]] = {}
        self._locks: dict[str, float] = {}

    def _prune(self, ip: str, now: float) -> None:
        cutoff = now - RL_WINDOW_SEC
        self._fails[ip] = [t for t in self._fails.get(ip, []) if t > cutoff]
        if not self._fails[ip]:
            self._fails.pop(ip, None)

    def locked_until(self, ip: str) -> Optional[float]:
        until = self._locks.get(ip)
        if until and until > time.time():
            return until
        if until:
            self._locks.pop(ip, None)
        return None

    def record_failure(self, ip: str) -> Optional[float]:
        """Record a failed attempt. Returns lockout-until epoch if this push
        the IP over the threshold, else None."""
        now = time.time()
        self._fails.setdefault(ip, []).append(now)
        self._prune(ip, now)
        if len(self._fails.get(ip, [])) >= RL_MAX_FAILS:
            self._locks[ip] = now + RL_LOCKOUT_SEC
            self._fails.pop(ip, None)
            return self._locks[ip]
        return None

    def record_success(self, ip: str) -> None:
        self._fails.pop(ip, None)
        self._locks.pop(ip, None)


# ---- Flask integration ---------------------------------------------------


def auth_disabled() -> bool:
    return _env("AUTH_DISABLED", "").lower() in ("1", "true", "yes")


def current_role() -> Optional[str]:
    if auth_disabled():
        return "admin"
    return session.get("role")


def _deny(status: int, message: str):
    """JSON 401/403 for fetch() callers, redirect to /login for browser navs."""
    accept = request.headers.get("Accept", "")
    if "text/html" in accept and "application/json" not in accept:
        return redirect(url_for("login"))
    return jsonify({"error": message}), status


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if current_role() is None:
            return _deny(401, "auth required")
        return fn(*args, **kwargs)
    return wrapper


def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        role = current_role()
        if role is None:
            return _deny(401, "auth required")
        if role != "admin":
            return _deny(403, "admin required")
        return fn(*args, **kwargs)
    return wrapper


# ---- CLI -----------------------------------------------------------------


def _prompt_pin(label: str) -> str:
    while True:
        pin = getpass.getpass(f"{label}: ")
        if not PIN_RE.match(pin):
            print("  PIN must be exactly 4 digits. Try again.", file=sys.stderr)
            continue
        confirm = getpass.getpass(f"{label} (confirm): ")
        if pin != confirm:
            print("  PINs did not match. Try again.", file=sys.stderr)
            continue
        return pin


def _state_dir_from_env() -> Path:
    return Path(_env("STATE_DIR", os.getcwd()))


def _cli_set(args) -> int:
    role = args.role
    store = PinStore(_state_dir_from_env())
    store.load()
    pin = _prompt_pin(f"New {role.upper()} PIN")
    try:
        store.set_pin(role, pin)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"OK: {role} PIN set ({store.path})")
    return 0


def _cli_hash(args) -> int:
    pin = _prompt_pin("PIN to hash")
    print(hash_pin(pin))
    return 0


def _cli_reset(args) -> int:
    store = PinStore(_state_dir_from_env())
    if store.path.exists():
        store.path.unlink()
        print(f"Removed {store.path}. Re-run 'set --role user' and 'set --role admin'.")
    else:
        print(f"No store at {store.path}.")
    return 0


def _cli_status(args) -> int:
    store = PinStore(_state_dir_from_env())
    store.load()
    for role in ROLES:
        ts = store.changed_at(role)
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "(not set)"
        print(f"  {role:5} PIN: {when}")
    print(f"  file:       {store.path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pew_ptz.auth",
        description="Manage PINs for pew-ptz",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_set = sub.add_parser("set", help="set/rotate a PIN (interactive)")
    p_set.add_argument("--role", required=True, choices=ROLES)
    p_set.set_defaults(func=_cli_set)

    p_hash = sub.add_parser("hash", help="print a PIN hash (for env-var seeding)")
    p_hash.set_defaults(func=_cli_hash)

    p_reset = sub.add_parser("reset", help="delete the PIN store")
    p_reset.set_defaults(func=_cli_reset)

    p_status = sub.add_parser("status", help="show when each PIN was last changed")
    p_status.set_defaults(func=_cli_status)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
