"""XDG locations used by backuper."""

from __future__ import annotations

import os
import socket
from pathlib import Path

APP = "backuper"


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / fallback


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / APP


def manifest_path() -> Path:
    override = os.environ.get("BACKUPER_MANIFEST")
    return Path(override) if override else config_dir() / "manifest.toml"


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / APP


def last_run_path() -> Path:
    return state_dir() / "last-run.json"


def lock_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else state_dir()
    return base / f"{APP}.lock"


def systemd_user_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "systemd" / "user"


def hostname() -> str:
    """Name of this machine's folder at the destination (``$BACKUPER_HOST`` overrides)."""
    return os.environ.get("BACKUPER_HOST") or socket.gethostname()
