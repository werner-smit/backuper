"""Logging to the systemd journal (via the syslog socket) and the console."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys

log = logging.getLogger("backuper")

SYSLOG_SOCKET = "/dev/log"


def setup(verbosity: int = 0) -> None:
    """Configure the ``backuper`` logger.

    ``verbosity``: -1 quiet, 0 normal, 1+ debug.
    """
    log.handlers.clear()
    log.setLevel(logging.DEBUG)
    log.propagate = False

    under_systemd = bool(os.environ.get("JOURNAL_STREAM"))
    use_syslog = os.environ.get("BACKUPER_SYSLOG", "1") != "0"

    if use_syslog and os.path.exists(SYSLOG_SOCKET):
        try:
            handler = logging.handlers.SysLogHandler(
                address=SYSLOG_SOCKET, facility=logging.handlers.SysLogHandler.LOG_USER
            )
            # journald parses "IDENT[PID]: message" into SYSLOG_IDENTIFIER / SYSLOG_PID.
            handler.setFormatter(logging.Formatter("backuper[%(process)d]: %(message)s"))
            handler.setLevel(logging.INFO)
            log.addHandler(handler)
        except OSError:
            pass

    if not under_systemd or not log.handlers:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(_ConsoleFormatter())
        if verbosity < 0:
            console.setLevel(logging.WARNING)
        elif verbosity == 0:
            console.setLevel(logging.INFO)
        else:
            console.setLevel(logging.DEBUG)
        log.addHandler(console)


class _ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if record.levelno >= logging.ERROR:
            return f"error: {msg}"
        if record.levelno >= logging.WARNING:
            return f"warning: {msg}"
        return msg
