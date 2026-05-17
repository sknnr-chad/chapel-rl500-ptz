"""
pew_ptz.skins — skinnable lock-screen plumbing.

A "skin" is a directory under pew_ptz/skins/ containing:

    template.html  — Jinja fragment rendered by GET /login (and /admin/change-pin)
    lock.css       — stylesheet served at /skin/<name>/lock.css
    lock.js        — script served at /skin/<name>/lock.js
    assets/        — optional, served at /skin/<name>/assets/...

The skin renders the lock UI and is responsible for POSTing {pin: "1234"} to
the login endpoint. Auth logic stays in pew_ptz.auth — skins are pure
presentation, so a new visual style is a directory drop, not a code change.

PEW_PTZ_AUTH_SKIN selects which one is active (default: "steampunk"). A
missing or malformed skin makes the server refuse to start — loud failure
beats silent fallback for an auth surface.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple, Optional

SKINS_DIR = Path(__file__).resolve().parent / "skins"
DEFAULT_SKIN = "steampunk"
REQUIRED_FILES = ("template.html", "lock.css", "lock.js")


class Skin(NamedTuple):
    name: str
    dir: Path
    template_path: Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get("PEW_PTZ_" + name, default)


def list_skins() -> list[str]:
    if not SKINS_DIR.exists():
        return []
    return sorted(p.name for p in SKINS_DIR.iterdir() if p.is_dir())


def _validate(name: str) -> Path:
    skin_dir = SKINS_DIR / name
    if not skin_dir.is_dir():
        available = list_skins() or "(none)"
        raise RuntimeError(
            f"Skin {name!r} not found in {SKINS_DIR}. Available: {available}"
        )
    missing = [f for f in REQUIRED_FILES if not (skin_dir / f).is_file()]
    if missing:
        raise RuntimeError(f"Skin {name!r} is missing required files: {missing}")
    return skin_dir


def load_active() -> Skin:
    name = _env("AUTH_SKIN", DEFAULT_SKIN)
    skin_dir = _validate(name)
    return Skin(name=name, dir=skin_dir, template_path=skin_dir / "template.html")


def skin_dir_for(name: str) -> Optional[Path]:
    """Return the skin's directory if it exists, else None. Used by the asset
    route to 404 cleanly on requests for an unknown skin."""
    skin_dir = SKINS_DIR / name
    return skin_dir if skin_dir.is_dir() else None
