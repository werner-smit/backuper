"""Thin wrappers around ``systemctl --user`` and ``journalctl``."""

from __future__ import annotations

import os
import shutil
import subprocess

from backuper import paths

TIMER = "backuper.timer"
SERVICE = "backuper.service"


class SystemdError(Exception):
    pass


def _systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    if not shutil.which("systemctl"):
        raise SystemdError("systemctl not found")
    proc = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise SystemdError(proc.stderr.strip() or f"systemctl {' '.join(args)} failed")
    return proc


def unit_installed() -> bool:
    try:
        proc = _systemctl("cat", TIMER, check=False)
    except SystemdError:
        return False
    return proc.returncode == 0


def set_schedule(on_calendar: str | None) -> None:
    """Override OnCalendar via a drop-in; ``None`` removes the override."""
    dropin = paths.systemd_user_dir() / f"{TIMER}.d" / "schedule.conf"
    if on_calendar is None:
        dropin.unlink(missing_ok=True)
    else:
        proc = subprocess.run(["systemd-analyze", "calendar", on_calendar],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise SystemdError(f"invalid OnCalendar expression: {on_calendar}")
        dropin.parent.mkdir(parents=True, exist_ok=True)
        dropin.write_text(f"[Timer]\nOnCalendar=\nOnCalendar={on_calendar}\n")
    _systemctl("daemon-reload")


def enable_timer() -> None:
    if not unit_installed():
        raise SystemdError(f"{TIMER} not found; is backuper installed via the package?")
    _systemctl("enable", "--now", TIMER)


def disable_timer() -> None:
    _systemctl("disable", "--now", TIMER)


def timer_info() -> dict[str, str]:
    """Selected properties of the timer, or {} if unavailable."""
    try:
        proc = _systemctl(
            "show", TIMER,
            "-p", "ActiveState", "-p", "UnitFileState",
            "-p", "NextElapseUSecRealtime", "-p", "LastTriggerUSec",
            check=False,
        )
    except SystemdError:
        return {}
    info = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition("=")
        info[key] = value
    return info


def exec_journal(follow: bool = False, lines: int | None = None) -> None:
    """Replace the process with journalctl showing backuper's messages."""
    cmd = ["journalctl", "-t", "backuper", "--no-hostname"]
    if follow:
        cmd.append("-f")
    if lines:
        cmd += ["-n", str(lines)]
    os.execvp(cmd[0], cmd)
