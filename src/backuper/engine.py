"""rsync-based backup and restore."""

from __future__ import annotations

import fcntl
import json
import re
import shutil
import socket
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from backuper import paths
from backuper.log import log
from backuper.manifest import (
    Destination,
    Entry,
    Manifest,
    ManifestError,
    atomic_write,
    normalize_path,
)

META_DIR = ".backuper"
RSYNC = "rsync"
# -a without owner/group: Nextcloud (and most non-root targets) can't keep them.
BASE_ARGS = ["-a", "--no-owner", "--no-group", "--human-readable"]


class BackupError(Exception):
    """A problem that stops the whole run."""


@dataclass
class EntryResult:
    path: str
    status: str  # "ok" | "skipped" | "failed"
    message: str = ""
    stats: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# ---------------------------------------------------------------------------
# helpers


@contextmanager
def run_lock():
    lock_file = paths.lock_path()
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_file, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("another backuper run is in progress") from None
        yield


def rsync_available() -> bool:
    return shutil.which(RSYNC) is not None


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    log.debug("running: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True)


def _parse_stats(output: str) -> dict:
    stats = {}
    patterns = {
        "files_transferred": r"Number of regular files transferred: ([\d,.]+)",
        "files_total": r"Number of files: ([\d,.]+)",
        "deleted": r"Number of deleted files: ([\d,.]+)",
        "transferred_size": r"Total transferred file size: ([^\n]+?)(?: bytes)?\n",
        "total_size": r"Total file size: ([^\n]+?)(?: bytes)?\n",
    }
    for key, pat in patterns.items():
        m = re.search(pat, output)
        if m:
            stats[key] = m.group(1).strip()
    return stats


def _plural(count, singular: str, plural: str | None = None) -> str:
    try:
        one = int(str(count).replace(",", "")) == 1
    except ValueError:
        one = False
    return f"{count} {singular if one else (plural or singular + 's')}"


def _entries(count: int) -> str:
    return _plural(count, "entry", "entries")


def _size(value: str | None) -> str:
    """rsync --human-readable prints plain bytes without a unit ("345")."""
    if not value:
        return "?"
    return f"{value}B" if value[-1].isdigit() else value


def require_destination(manifest: Manifest) -> Destination:
    if manifest.destination is None:
        raise BackupError("no destination configured; run `backuper setup`")
    return manifest.destination


def check_destination(dest: Destination) -> None:
    """Fail unless the destination root is reachable."""
    if dest.remote:
        proc = _run([RSYNC, "--list-only", dest.target.rstrip("/") + "/"])
        if proc.returncode != 0:
            raise BackupError(f"destination {dest.target} unreachable: {proc.stderr.strip()}")
        return
    target = Path(dest.target).expanduser()
    if not target.is_dir():
        # Never create the root ourselves: an unmounted/unsynced folder must not be filled.
        raise BackupError(f"destination {target} does not exist")


def _local_path(path: str) -> Path:
    return Path(path).expanduser()


# ---------------------------------------------------------------------------
# destination metadata (manifest copy + last run)


def read_dest_manifest(dest: Destination, host: str | None = None) -> Manifest | None:
    """Load the manifest stored alongside the backup, or None if there is none."""
    remote_file = f"{dest.host_root(host)}/{META_DIR}/manifest.toml"
    if not dest.remote:
        path = _local_path(remote_file)
        return Manifest.load(path) if path.exists() else None
    with tempfile.TemporaryDirectory() as tmp:
        proc = _run([RSYNC, remote_file, f"{tmp}/manifest.toml"])
        if proc.returncode != 0:
            return None
        return Manifest.load(Path(tmp) / "manifest.toml")


def read_dest_last_run(dest: Destination, host: str | None = None) -> dict | None:
    if dest.remote:
        return None
    path = _local_path(f"{dest.host_root(host)}/{META_DIR}/last-run.json")
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def publish_metadata(dest: Destination, manifest: Manifest, last_run: dict) -> None:
    """Copy manifest + last-run record next to the data (rsync renames atomically)."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "manifest.toml").write_text(manifest.dumps())
        (Path(tmp) / "last-run.json").write_text(json.dumps(last_run, indent=2) + "\n")
        cmd = [
            RSYNC, "--mkpath", "-t", "--chmod=F644",
            f"{tmp}/manifest.toml", f"{tmp}/last-run.json",
            f"{dest.host_root()}/{META_DIR}/",
        ]
        proc = _run(cmd)
        if proc.returncode != 0:
            raise BackupError(f"could not write manifest to destination: {proc.stderr.strip()}")


def list_dest_hosts(dest: Destination) -> list[str]:
    """Hosts with a stored manifest under the destination target (local only)."""
    if dest.remote:
        raise BackupError("listing hosts is only supported for local destinations")
    if not dest.host_subdir:
        root = _local_path(dest.target)
        return [""] if (root / META_DIR / "manifest.toml").exists() else []
    root = _local_path(dest.target)
    if not root.is_dir():
        return []
    return sorted(
        p.name for p in root.iterdir() if (p / META_DIR / "manifest.toml").exists()
    )


# ---------------------------------------------------------------------------
# backup


def _entry_guard(entry: Entry) -> str | None:
    """Reason to skip an entry, or None if it is safe to mirror."""
    src = Path(entry.path)
    if not src.exists():
        return "source does not exist (refusing to mirror, it would delete the backup copy)"
    if src.is_dir():
        try:
            next(src.iterdir())
        except StopIteration:
            return "source directory is empty (refusing to mirror, it would delete the backup copy)"
        except PermissionError:
            return "source directory is not readable"
    return None


def _auto_excludes(dest: Destination, entry: Entry) -> list[str]:
    """Exclude the destination when it lives inside the entry being copied."""
    if dest.remote:
        return []
    target = Path(normalize_path(dest.target))
    if target.is_relative_to(entry.path) and target != Path(entry.path):
        # With -R the transfer root is "/", so an anchored pattern is the full path.
        return [f"/{str(target).lstrip('/')}/"]
    return []


def backup_command(manifest: Manifest, entry: Entry, dry_run: bool = False) -> list[str]:
    dest = require_destination(manifest)
    cmd = [RSYNC, *BASE_ARGS, "--relative", "--mkpath", "--delete", "--delete-excluded", "--stats"]
    if dry_run:
        cmd += ["--dry-run", "--itemize-changes"]
    for pattern in [*manifest.excludes_for(entry), *_auto_excludes(dest, entry)]:
        cmd.append(f"--exclude={pattern}")
    cmd += dest.rsync_args
    cmd += [entry.path, dest.host_root() + "/"]
    return cmd


def select_entries(entries: list[Entry], only: list[str] | None) -> list[Entry]:
    if not only:
        return [e for e in entries if e.enabled]
    wanted = [normalize_path(p) for p in only]
    by_path = {e.path: e for e in entries}
    missing = [p for p in wanted if p not in by_path]
    if missing:
        raise BackupError("not in manifest: " + ", ".join(missing))
    return [by_path[p] for p in wanted]


def backup(manifest: Manifest, only: list[str] | None = None, dry_run: bool = False,
           echo=None) -> list[EntryResult]:
    """Mirror entries to the destination. ``echo`` receives dry-run itemisations."""
    dest = require_destination(manifest)
    errors, warnings = manifest.validate()
    for w in warnings:
        log.warning(w)
    if errors:
        raise BackupError("manifest invalid: " + "; ".join(errors))
    if not rsync_available():
        raise BackupError("rsync not found in PATH")
    check_destination(dest)

    entries = select_entries(manifest.entries, only)
    if not entries:
        log.warning("nothing to back up (no enabled entries)")
    started = datetime.now(timezone.utc)
    log.info("backup started: %s -> %s%s", _entries(len(entries)), dest.host_root(),
             " (dry run)" if dry_run else "")

    results: list[EntryResult] = []
    for entry in entries:
        reason = _entry_guard(entry)
        if reason:
            log.error("%s: skipped, %s", entry.path, reason)
            results.append(EntryResult(entry.path, "skipped", reason))
            continue
        proc = _run(backup_command(manifest, entry, dry_run))
        stats = _parse_stats(proc.stdout)
        if dry_run and echo:
            for line in proc.stdout.splitlines():
                if line and not line.startswith(("Number of", "Total", "Literal", "Matched",
                                                 "File list", "sent ", "total size")):
                    echo(line)
        if proc.returncode == 0:
            log.info("%s: ok, %s transferred (%s), %s deleted",
                     entry.path, _plural(stats.get("files_transferred", "?"), "file"),
                     _size(stats.get("transferred_size")), stats.get("deleted", "0"))
            results.append(EntryResult(entry.path, "ok", stats=stats))
        else:
            # 24 = some source files vanished during transfer: data is still usable.
            status = "ok" if proc.returncode == 24 else "failed"
            msg = f"rsync exit {proc.returncode}: {proc.stderr.strip()[-2000:]}"
            (log.warning if status == "ok" else log.error)("%s: %s", entry.path, msg)
            results.append(EntryResult(entry.path, status, msg, stats))

    if dry_run:
        return results

    finished = datetime.now(timezone.utc)
    last_run = {
        "host": socket.gethostname(),
        "started": started.isoformat(timespec="seconds"),
        "finished": finished.isoformat(timespec="seconds"),
        "success": all(r.ok for r in results),
        "entries": [asdict(r) for r in results],
    }
    atomic_write(paths.last_run_path(), json.dumps(last_run, indent=2) + "\n")
    publish_metadata(dest, _merged_dest_manifest(manifest, dest, results), last_run)
    failed = [r for r in results if not r.ok]
    if failed:
        log.error("backup finished: %d of %s failed", len(failed), _entries(len(results)))
    else:
        log.info("backup finished: %s ok", _entries(len(results)))
    return results


def _merged_dest_manifest(local: Manifest, dest: Destination,
                          results: list[EntryResult]) -> Manifest:
    """Manifest describing what is actually stored at the destination.

    Entries backed up now take their definition from the local manifest; entries
    that were stored before and not touched this run (failed, disabled, removed
    without --purge) keep their previous definition because their data is still there.
    """
    try:
        previous = read_dest_manifest(dest)
    except ManifestError as exc:
        log.warning("ignoring unreadable manifest at destination: %s", exc)
        previous = None
    succeeded = {r.path for r in results if r.ok}
    entries = [e for e in local.entries if e.path in succeeded]
    if previous:
        entries += [e for e in previous.entries if e.path not in succeeded]
    return Manifest(destination=local.destination,
                    default_excludes=list(local.default_excludes),
                    entries=entries)


# ---------------------------------------------------------------------------
# restore


def restore_command(dest: Destination, entry: Entry, to: str | None, host: str | None,
                    delete: bool = False, dry_run: bool = False) -> list[str]:
    cmd = [RSYNC, *BASE_ARGS, "--relative", "--no-implied-dirs", "--mkpath", "--stats"]
    if delete:
        cmd.append("--delete")
    if dry_run:
        cmd += ["--dry-run", "--itemize-changes"]
    cmd += dest.rsync_args
    # "/./" marks where the --relative path starts on the source side.
    src = f"{dest.host_root(host)}/./{entry.path.lstrip('/')}"
    target = (str(Path(to).expanduser().resolve()) if to else "") + "/"
    cmd += [src, target]
    return cmd


def restore(dest: Destination, only: list[str] | None = None, to: str | None = None,
            host: str | None = None, delete: bool = False, dry_run: bool = False,
            echo=None) -> list[EntryResult]:
    """Restore entries listed in the manifest stored at the destination."""
    if not rsync_available():
        raise BackupError("rsync not found in PATH")
    check_destination(dest)
    stored = read_dest_manifest(dest, host)
    if stored is None:
        raise BackupError(f"no manifest found at {dest.host_root(host)}/{META_DIR}/")
    # Only the paths matter here; restore whatever is stored, enabled or not.
    entries = select_entries(stored.entries, only) if only else list(stored.entries)
    log.info("restore started: %s from %s to %s%s", _entries(len(entries)),
             dest.host_root(host), to or "original locations", " (dry run)" if dry_run else "")

    results = []
    for entry in entries:
        if not dest.remote:
            stored_copy = Path(f"{dest.host_root(host)}/{entry.path.lstrip('/')}").expanduser()
            if not stored_copy.exists():
                log.error("%s: no data at destination, skipped", entry.path)
                results.append(EntryResult(entry.path, "skipped", "no data at destination"))
                continue
        proc = _run(restore_command(dest, entry, to, host, delete, dry_run))
        stats = _parse_stats(proc.stdout)
        if dry_run and echo:
            for line in proc.stdout.splitlines():
                if line and not line.startswith(("Number of", "Total", "Literal", "Matched",
                                                 "File list", "sent ", "total size")):
                    echo(line)
        if proc.returncode == 0:
            log.info("%s: restored, %s transferred", entry.path,
                     _plural(stats.get("files_transferred", "?"), "file"))
            results.append(EntryResult(entry.path, "ok", stats=stats))
        else:
            msg = f"rsync exit {proc.returncode}: {proc.stderr.strip()[-2000:]}"
            log.error("%s: restore failed, %s", entry.path, msg)
            results.append(EntryResult(entry.path, "failed", msg, stats))
    return results


# ---------------------------------------------------------------------------
# purge


def purge(dest: Destination, path: str) -> bool:
    """Delete an entry's stored copy and drop it from the destination manifest."""
    if dest.remote:
        raise BackupError("--purge is only supported for local destinations")
    stored_copy = Path(f"{dest.host_root()}/{path.lstrip('/')}").expanduser()
    removed = False
    if stored_copy.is_dir() and not stored_copy.is_symlink():
        shutil.rmtree(stored_copy)
        removed = True
    elif stored_copy.exists() or stored_copy.is_symlink():
        stored_copy.unlink()
        removed = True
    stored = read_dest_manifest(dest)
    if stored and stored.find(path):
        stored.remove(path)
        last_run = read_dest_last_run(dest) or {}
        publish_metadata(dest, stored, last_run)
    log.info("%s: purged from destination", path)
    return removed
