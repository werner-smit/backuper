# backuper

Manifest-driven backups for one user. A TOML manifest lists the directories to
back up. `backuper backup` mirrors them with rsync into a destination folder
(for example a Nextcloud sync folder), and `backuper restore` copies them back.
A systemd user timer runs the backup daily, and everything is logged to the journal.

![backuper demo](docs/demo.gif)

## Quick start

```sh
backuper setup ~/Nextcloud/backup   # set the destination, optionally enable the timer
backuper add ~/src ~/Documents      # add entries
backuper add ~/projects -e build -e '*.iso'
backuper list
backuper backup --dry-run
backuper backup
backuper status
backuper logs -f
```

## Commands

| command | |
|---|---|
| `setup [TARGET]` | set the destination (a folder, or `user@host:path` for rsync over ssh) |
| `add PATH... [-e PATTERN]` | add entries |
| `remove PATH... [--purge]` / `rm` | remove entries; `--purge` also deletes the stored copy |
| `list [-s]` / `ls` | show entries (`-s` adds sizes) |
| `enable` / `disable PATH...` | include or skip an entry without removing it |
| `exclude PATH [PATTERN...] [-r]` | per-entry excludes; `exclude --default ...` sets the global ones |
| `edit` | edit the manifest in `$EDITOR`; it is validated before saving |
| `backup [PATH...] [-n]` | back up everything, or only the given entries |
| `restore [PATH...] [--to DIR] [--from DEST] [--host H] [--delete] [-n]` | restore from the backup |
| `restore --list [--from DEST]` | show the hosts and entries stored at a destination |
| `status` | last run, timer state, next run |
| `timer enable [--on-calendar SPEC]` / `disable` / `reset` | manage the systemd user timer |
| `logs [-f] [-n N]` | journal entries (`journalctl -t backuper`) |
| `check` | validate the manifest and check that the destination is reachable |
| `path` | print the manifest location |

## How it works

* **Manifest:** `~/.config/backuper/manifest.toml`, or `$BACKUPER_MANIFEST` if set.
* **Layout:** each entry is mirrored with `rsync -a --delete --relative` to
  `<target>/<hostname>/<absolute path>`. Set `$BACKUPER_HOST` to use a
  different folder name than the machine's hostname. For example, `/home/me/src` ends up at
  `~/Nextcloud/backup/myhost/home/me/src`.
* **Manifest copy:** after each run, the manifest and a `last-run.json` record are
  written to `<target>/<hostname>/.backuper/`.
* **Restore uses that stored manifest, not the local one.** It restores exactly what
  was backed up, even on a fresh machine:
  ```sh
  backuper restore --list --from ~/Nextcloud/backup
  backuper restore --from ~/Nextcloud/backup --host oldbox --to ~/restored
  ```
  Without `--to`, files go back to their original paths, after a confirmation.
  Restore never deletes local files unless `--delete` is given.
* **Missing or empty sources:** if an entry's source is missing or empty (an
  unmounted drive, a moved directory), that entry is skipped. Its stored copy is
  not wiped. The run then exits non-zero, and the error appears in the journal
  and in `backuper status`.
* **Destination must exist:** backuper never creates the destination root, so an
  unmounted or unsynced folder is never filled by accident.
* **Excluding the destination:** if the destination lies inside an entry (for
  example, backing up `~` to `~/Nextcloud/backup`), the destination is excluded
  automatically.
* **Overlapping runs:** a lock file stops the timer and a manual run from
  overlapping.

### History and the Nextcloud mirror

backuper keeps only one mirror and no snapshots. Older versions and deleted files
can be recovered from Nextcloud's *Versions* and *Deleted files* features. Keep in
mind:

* The Nextcloud desktop client does not sync symlinks, and it does not keep
  permissions or ownership.
* Files matching the client's ignore list (`~/.config/Nextcloud/sync-exclude.lst`,
  for example `*~` and `*.part`) never reach the server.
* Dotfiles reach the server only if *Sync hidden files* is enabled in the client.
  backuper's own `.backuper/` metadata depends on this too.

## Installation (Arch)

```sh
cd packaging/arch
makepkg -si            # build GitHub main
LOCAL=1 makepkg -si    # build this checkout's committed HEAD
backuper setup
```

The package installs `backuper.service` and `backuper.timer` as systemd user
units, plus shell completions for bash, zsh and fish.

## Development

```sh
uv sync
uv run backuper --help
uv run pytest
```

uv is used only for development. The PKGBUILD builds the wheel with
`python -m build` (using the hatchling backend) and takes its dependencies from
the Arch repos.

## Commits and releases

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/):
`feat: ...`, `fix: ...`, `docs: ...`, `refactor: ...`, `chore: ...`. A breaking change
is marked with `feat!: ...` or a `BREAKING CHANGE:` footer. `uv run cz commit`
prompts for a message in this format if you want help.

To release, run:

```sh
uv run cz bump --dry-run   # preview the next version and changelog
uv run cz bump             # bump pyproject.toml + uv.lock, update CHANGELOG.md, commit, tag vX.Y.Z
git push --follow-tags
```

The version is worked out from the commits since the last tag: `fix` gives a
patch, `feat` a minor and a breaking change a major. While the version is
0.x, a breaking change bumps the minor version instead (`major_version_zero`).
The PKGBUILD's `pkgver()` comes from `git describe`: a build of a tagged commit
is `0.2.0`, and a build three commits later is `0.2.0.r3.gabc1234`.
