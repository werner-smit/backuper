"""Command line interface."""

from __future__ import annotations

import configparser
import json
import subprocess
import tempfile
from pathlib import Path

import click

from backuper import __version__, engine, paths, systemd
from backuper import log as logmod
from backuper.log import log
from backuper.manifest import Destination, Manifest, ManifestError, atomic_write, normalize_path

ALIASES = {"ls": "list", "rm": "remove"}


class AliasedGroup(click.Group):
    def get_command(self, ctx, cmd_name):
        return super().get_command(ctx, ALIASES.get(cmd_name, cmd_name))

    def resolve_command(self, ctx, args):
        _, cmd, args = super().resolve_command(ctx, args)
        return cmd.name, cmd, args


def load_manifest() -> Manifest:
    try:
        return Manifest.load(paths.manifest_path())
    except ManifestError as exc:
        raise click.ClickException(str(exc)) from exc


def save_manifest(manifest: Manifest) -> None:
    manifest.save(paths.manifest_path())


def require_entry(manifest: Manifest, path: str):
    entry = manifest.find(normalize_path(path))
    if entry is None:
        raise click.ClickException(f"{normalize_path(path)} is not in the manifest")
    return entry


def _guess_nextcloud_target() -> str | None:
    cfg = Path.home() / ".config" / "Nextcloud" / "nextcloud.cfg"
    if not cfg.exists():
        return None
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    try:
        parser.read(cfg)
    except configparser.Error:
        return None
    for section in parser.sections():
        for key, value in parser.items(section):
            if key.endswith("\\localpath") and value:
                return str(Path(value) / "backup")
    return None


def _human(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return str(n)


def _du(path: str) -> str:
    proc = subprocess.run(["du", "-sb", path], capture_output=True, text=True)
    if proc.returncode != 0 or not proc.stdout:
        return "?"
    return _human(int(proc.stdout.split()[0]))


def _report(results: list[engine.EntryResult]) -> None:
    if any(not r.ok for r in results):
        raise SystemExit(1)


# ---------------------------------------------------------------------------


@click.group(cls=AliasedGroup, context_settings={"help_option_names": ["-h", "--help"]})
@click.option("-v", "--verbose", count=True, help="More output (repeatable).")
@click.option("-q", "--quiet", is_flag=True, help="Only warnings and errors.")
@click.version_option(__version__, prog_name="backuper")
def cli(verbose: int, quiet: bool):
    """Manifest-driven rsync backups.

    The manifest lives in ~/.config/backuper/manifest.toml. Each backup also
    stores a copy of it with the data; restore reads that copy.
    """
    logmod.setup(-1 if quiet else verbose)


@cli.command()
@click.argument("target", required=False)
@click.option("--no-host-subdir", is_flag=True, help="Store directly in TARGET, not TARGET/<hostname>.")
@click.option("--timer/--no-timer", default=None, help="Enable the daily systemd timer.")
def setup(target: str | None, no_host_subdir: bool, timer: bool | None):
    """Create the manifest and set the backup destination."""
    path = paths.manifest_path()
    manifest = load_manifest() if path.exists() else Manifest()
    if target is None:
        default = (manifest.destination.target if manifest.destination
                   else _guess_nextcloud_target())
        target = click.prompt("Backup destination (folder or user@host:path)", default=default)

    dest = Destination(target=target)
    if not dest.remote:
        target_path = Path(target).expanduser().resolve()
        dest.target = str(target_path)
        if not target_path.is_dir():
            if not target_path.parent.is_dir():
                raise click.ClickException(f"{target_path.parent} does not exist")
            if click.confirm(f"{target_path} does not exist. Create it?", default=True):
                target_path.mkdir()
            else:
                raise click.ClickException("destination must exist")
    if manifest.destination:
        dest.rsync_args = manifest.destination.rsync_args
    dest.host_subdir = not no_host_subdir
    manifest.destination = dest

    errors, warnings = manifest.validate()
    for w in warnings:
        log.warning(w)
    if errors:
        raise click.ClickException("; ".join(errors))
    save_manifest(manifest)
    click.echo(f"Manifest: {path}")
    click.echo(f"Destination: {dest.host_root()}")

    if timer is None and systemd.unit_installed():
        timer = click.confirm("Enable the daily backup timer?", default=True)
    if timer:
        try:
            systemd.enable_timer()
            click.echo("Timer enabled.")
        except systemd.SystemdError as exc:
            log.warning(str(exc))


@cli.command()
@click.argument("paths_", metavar="PATH...", nargs=-1, required=True,
                type=click.Path(exists=True, file_okay=True, dir_okay=True))
@click.option("-e", "--exclude", multiple=True, help="Exclude pattern for these entries (repeatable).")
def add(paths_: tuple[str, ...], exclude: tuple[str, ...]):
    """Add directories or files to the manifest."""
    manifest = load_manifest()
    for p in paths_:
        try:
            entry = manifest.add(normalize_path(p), list(exclude))
        except ManifestError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"added {entry.path}")
    errors, warnings = manifest.validate()
    for w in warnings:
        log.warning(w)
    if errors:
        raise click.ClickException("; ".join(errors))
    save_manifest(manifest)


@cli.command()
@click.argument("paths_", metavar="PATH...", nargs=-1, required=True)
@click.option("--purge", is_flag=True, help="Also delete the stored copy at the destination.")
@click.option("-y", "--yes", is_flag=True, help="Don't ask for confirmation.")
def remove(paths_: tuple[str, ...], purge: bool, yes: bool):
    """Remove entries from the manifest (alias: rm)."""
    manifest = load_manifest()
    entries = [require_entry(manifest, p) for p in paths_]
    if purge:
        dest = manifest.destination
        if dest is None:
            raise click.ClickException("no destination configured")
        if not yes:
            click.confirm(f"Delete stored copies of {len(entries)} entries from "
                          f"{dest.host_root()}?", abort=True)
    for entry in entries:
        manifest.remove(entry.path)
        if purge:
            try:
                engine.purge(manifest.destination, entry.path)
            except engine.BackupError as exc:
                raise click.ClickException(str(exc)) from exc
        click.echo(f"removed {entry.path}")
    save_manifest(manifest)


@cli.command(name="list")
@click.option("-s", "--size", is_flag=True, help="Show the size of each source (slow for big trees).")
def list_(size: bool):
    """Show the manifest entries (alias: ls)."""
    manifest = load_manifest()
    if manifest.destination:
        click.echo(f"destination: {manifest.destination.host_root()}")
    else:
        click.echo("destination: (not set, run `backuper setup`)")
    if manifest.default_excludes:
        click.echo(f"default excludes: {', '.join(manifest.default_excludes)}")
    if not manifest.entries:
        click.echo("no entries; add some with `backuper add PATH`")
        return
    click.echo()
    for entry in manifest.entries:
        exists = Path(entry.path).exists()
        flag = click.style("on ", fg="green") if entry.enabled else click.style("off", fg="yellow")
        line = f"  [{flag}] {entry.path}"
        if not exists:
            line += click.style("  (missing)", fg="red")
        elif size:
            line += f"  {_du(entry.path)}"
        if entry.exclude:
            line += click.style(f"  exclude: {', '.join(entry.exclude)}", dim=True)
        click.echo(line)


def _set_enabled(paths_: tuple[str, ...], enabled: bool) -> None:
    manifest = load_manifest()
    for p in paths_:
        entry = require_entry(manifest, p)
        entry.enabled = enabled
        click.echo(f"{'enabled' if enabled else 'disabled'} {entry.path}")
    save_manifest(manifest)


@cli.command()
@click.argument("paths_", metavar="PATH...", nargs=-1, required=True)
def enable(paths_):
    """Include entries in backups again."""
    _set_enabled(paths_, True)


@cli.command()
@click.argument("paths_", metavar="PATH...", nargs=-1, required=True)
def disable(paths_):
    """Skip entries in backups without removing them."""
    _set_enabled(paths_, False)


@cli.command()
@click.argument("args", metavar="[PATH] [PATTERN]...", nargs=-1)
@click.option("-r", "--remove", "remove_", is_flag=True, help="Remove the patterns instead of adding.")
@click.option("-d", "--default", "default", is_flag=True,
              help="Edit the default excludes that apply to every entry (no PATH).")
def exclude(args: tuple[str, ...], remove_: bool, default: bool):
    """Add or remove exclude patterns (no patterns: show them).

    \b
    backuper exclude ~/src '*.o' build/     # per entry
    backuper exclude --default .cache       # all entries
    """
    manifest = load_manifest()
    if default:
        target, patterns = manifest.default_excludes, args
    else:
        if not args:
            raise click.UsageError("PATH is required unless --default is given")
        target, patterns = require_entry(manifest, args[0]).exclude, args[1:]
    if not patterns:
        for p in target:
            click.echo(p)
        return
    for pat in patterns:
        if remove_:
            if pat in target:
                target.remove(pat)
        elif pat not in target:
            target.append(pat)
    save_manifest(manifest)
    click.echo(f"excludes: {', '.join(target) or '(none)'}")


@cli.command()
def edit():
    """Open the manifest in $EDITOR and validate it afterwards."""
    path = paths.manifest_path()
    original = path.read_text() if path.exists() else Manifest().dumps()
    with tempfile.NamedTemporaryFile("w", suffix=".toml", prefix="backuper-", delete=False) as fh:
        fh.write(original)
        tmp = Path(fh.name)
    try:
        while True:
            click.edit(filename=str(tmp))
            text = tmp.read_text()
            if text == original:
                click.echo("no changes")
                return
            try:
                manifest = Manifest.loads(text)
                errors, warnings = manifest.validate()
            except ManifestError as exc:
                errors, warnings = [str(exc)], []
            for w in warnings:
                log.warning(w)
            if not errors:
                break
            for e in errors:
                click.echo(f"error: {e}", err=True)
            if not click.confirm("Edit again? (no discards your changes)", default=True):
                click.echo("changes discarded")
                return
        # Keep the user's formatting and comments: write their text, not a re-dump.
        atomic_write(path, text)
        click.echo(f"saved {path}")
    finally:
        tmp.unlink(missing_ok=True)


@cli.command()
@click.argument("paths_", metavar="[PATH]...", nargs=-1)
@click.option("-n", "--dry-run", is_flag=True, help="Show what would change.")
def backup(paths_: tuple[str, ...], dry_run: bool):
    """Mirror all enabled entries (or only PATHs) to the destination."""
    manifest = load_manifest()
    try:
        with engine.run_lock():
            results = engine.backup(manifest, list(paths_), dry_run, echo=click.echo)
    except engine.BackupError as exc:
        log.error(str(exc))
        raise SystemExit(1) from exc
    _report(results)


@cli.command()
@click.argument("paths_", metavar="[PATH]...", nargs=-1)
@click.option("--to", "to", type=click.Path(file_okay=False),
              help="Restore under this directory instead of the original locations.")
@click.option("--from", "from_", metavar="DEST",
              help="Destination to restore from (default: configured target).")
@click.option("--host", help="Restore another machine's backup (default: this hostname or $BACKUPER_HOST).")
@click.option("--delete", is_flag=True, help="Delete local files that are not in the backup.")
@click.option("-n", "--dry-run", is_flag=True, help="Show what would change.")
@click.option("-l", "--list", "list_only", is_flag=True, help="List stored hosts and entries.")
@click.option("-y", "--yes", is_flag=True, help="Don't ask for confirmation.")
def restore(paths_, to, from_, host, delete, dry_run, list_only, yes):
    """Restore from the manifest stored at the destination.

    The local manifest is not used, so this works on a fresh machine:
    `backuper restore --from ~/Nextcloud/backup --host oldbox --to ~/restored`.
    """
    dest = _restore_destination(from_)
    try:
        if list_only:
            _list_stored(dest, host)
            return
        if not to and not dry_run and not yes:
            what = ", ".join(paths_) if paths_ else "all entries"
            warn = " Local files missing from the backup will be DELETED." if delete else ""
            click.confirm(f"Restore {what} into the original locations, overwriting "
                          f"newer local files?{warn}", abort=True)
        with engine.run_lock():
            results = engine.restore(dest, list(paths_), to, host, delete, dry_run,
                                     echo=click.echo)
    except (engine.BackupError, ManifestError) as exc:
        log.error(str(exc))
        raise SystemExit(1) from exc
    _report(results)


def _restore_destination(from_: str | None) -> Destination:
    if from_:
        target = from_ if ":" in from_.split("/", 1)[0] else str(Path(from_).expanduser().resolve())
        dest = Destination(target=target)
        # A folder that directly holds .backuper/ was created with --no-host-subdir,
        # or the user pointed at the host folder itself.
        if not dest.remote and (Path(target) / engine.META_DIR / "manifest.toml").exists():
            dest.host_subdir = False
        return dest
    path = paths.manifest_path()
    if path.exists():
        manifest = load_manifest()
        if manifest.destination:
            return manifest.destination
    raise click.ClickException("no destination configured; pass --from DEST")


def _list_stored(dest: Destination, host: str | None) -> None:
    hosts = [host] if host else engine.list_dest_hosts(dest)
    if not hosts:
        click.echo(f"no backups found under {dest.target}")
        return
    for h in hosts:
        stored = engine.read_dest_manifest(dest, h or None)
        last = engine.read_dest_last_run(dest, h or None) or {}
        click.echo(click.style(h or dest.target, bold=True)
                   + f"  last run: {last.get('finished', '?')}"
                   + ("" if last.get("success", True) else click.style("  (errors)", fg="red")))
        for entry in (stored.entries if stored else []):
            click.echo(f"  {entry.path}")


@cli.command()
def status():
    """Show destination, last run and timer state."""
    path = paths.manifest_path()
    click.echo(f"manifest:    {path}{'' if path.exists() else ' (missing)'}")
    manifest = Manifest.load(path) if path.exists() else None
    if manifest:
        dest = manifest.destination
        click.echo(f"destination: {dest.host_root() if dest else '(not set)'}")
        enabled = sum(e.enabled for e in manifest.entries)
        click.echo(f"entries:     {enabled} enabled, {len(manifest.entries) - enabled} disabled")
    try:
        last = json.loads(paths.last_run_path().read_text())
    except (OSError, ValueError):
        last = None
    if last:
        ok = sum(1 for e in last["entries"] if e["status"] == "ok")
        state = (click.style("ok", fg="green") if last["success"]
                 else click.style("errors", fg="red"))
        click.echo(f"last run:    {last['finished']}  {state}  ({ok}/{len(last['entries'])} entries ok)")
        for e in last["entries"]:
            if e["status"] != "ok":
                click.echo(f"             {e['status']}: {e['path']}: {e['message'][:200]}")
    else:
        click.echo("last run:    never")
    info = systemd.timer_info()
    if info and info.get("UnitFileState") not in (None, "", "not-found"):
        click.echo(f"timer:       {info.get('UnitFileState')}, {info.get('ActiveState')}")
        if info.get("NextElapseUSecRealtime"):
            click.echo(f"next run:    {info['NextElapseUSecRealtime']}")
    else:
        click.echo("timer:       not installed")


@cli.group()
def timer():
    """Manage the systemd user timer."""


@timer.command(name="enable")
@click.option("--on-calendar", metavar="SPEC",
              help="systemd OnCalendar schedule, e.g. 'daily', '*-*-* 02:30', 'hourly'.")
def timer_enable(on_calendar):
    """Enable (and optionally reschedule) the backup timer."""
    try:
        if on_calendar:
            systemd.set_schedule(on_calendar)
        systemd.enable_timer()
        if on_calendar:
            # Pick up the new schedule on an already running timer.
            systemd._systemctl("restart", systemd.TIMER)
    except systemd.SystemdError as exc:
        raise click.ClickException(str(exc)) from exc
    info = systemd.timer_info()
    click.echo(f"timer enabled; next run: {info.get('NextElapseUSecRealtime', '?')}")


@timer.command(name="disable")
def timer_disable():
    """Disable the backup timer."""
    try:
        systemd.disable_timer()
    except systemd.SystemdError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo("timer disabled")


@timer.command(name="reset")
def timer_reset():
    """Drop a custom schedule and go back to daily."""
    try:
        systemd.set_schedule(None)
        systemd._systemctl("restart", systemd.TIMER, check=False)
    except systemd.SystemdError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo("schedule reset to the packaged default")


@cli.command()
@click.option("-f", "--follow", is_flag=True)
@click.option("-n", "--lines", type=int, help="Number of lines to show.")
def logs(follow, lines):
    """Show backuper's journal entries."""
    systemd.exec_journal(follow, lines)


@cli.command()
def check():
    """Validate the manifest and check that rsync and the destination work."""
    problems = 0

    def report(ok: bool, msg: str):
        nonlocal problems
        problems += not ok
        mark = click.style("ok  ", fg="green") if ok else click.style("FAIL", fg="red")
        click.echo(f"[{mark}] {msg}")

    report(engine.rsync_available(), "rsync installed")
    try:
        manifest = Manifest.load(paths.manifest_path())
        errors, warnings = manifest.validate()
        report(not errors, "manifest valid" + (": " + "; ".join(errors) if errors else ""))
        for w in warnings:
            click.echo(f"[{click.style('warn', fg='yellow')}] {w}")
    except ManifestError as exc:
        report(False, str(exc))
        raise SystemExit(1) from exc
    if manifest.destination:
        try:
            engine.check_destination(manifest.destination)
            report(True, f"destination reachable: {manifest.destination.target}")
        except engine.BackupError as exc:
            report(False, str(exc))
    else:
        report(False, "destination configured")
    for entry in manifest.entries:
        if entry.enabled:
            report(Path(entry.path).exists(), f"source exists: {entry.path}")
    if problems:
        raise SystemExit(1)


@cli.command(name="path")
def path_cmd():
    """Print the manifest path."""
    click.echo(paths.manifest_path())


def main() -> None:
    cli(prog_name="backuper")
