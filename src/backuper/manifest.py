"""Loading, validating and saving the TOML manifest."""

from __future__ import annotations

import os
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w

from backuper.paths import hostname

DEFAULT_EXCLUDES = [".cache", "node_modules", "__pycache__", "*.tmp"]


class ManifestError(Exception):
    """Raised when a manifest cannot be loaded or is invalid."""


def is_remote(target: str) -> bool:
    """rsync convention: ``host:path`` or ``user@host:path`` is remote."""
    head = target.split("/", 1)[0]
    return ":" in head


def normalize_path(path: str | Path) -> str:
    """Absolute, user-expanded, resolved path as a string."""
    return str(Path(path).expanduser().resolve())


@dataclass
class Entry:
    path: str
    exclude: list[str] = field(default_factory=list)
    enabled: bool = True

    def to_dict(self) -> dict:
        return {"path": self.path, "exclude": list(self.exclude), "enabled": self.enabled}


@dataclass
class Destination:
    target: str
    host_subdir: bool = True
    rsync_args: list[str] = field(default_factory=list)

    @property
    def remote(self) -> bool:
        return is_remote(self.target)

    def host_root(self, host: str | None = None) -> str:
        """Directory holding this host's data (``<target>/<host>``)."""
        target = self.target.rstrip("/") or "/"
        if not self.host_subdir:
            return target
        return f"{target}/{host or hostname()}"

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "host_subdir": self.host_subdir,
            "rsync_args": list(self.rsync_args),
        }


@dataclass
class Manifest:
    destination: Destination | None = None
    default_excludes: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    entries: list[Entry] = field(default_factory=list)

    # ---- (de)serialisation -------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict) -> "Manifest":
        try:
            dest_data = data.get("destination")
            destination = None
            if dest_data:
                if not isinstance(dest_data.get("target"), str) or not dest_data["target"]:
                    raise ManifestError("[destination] target must be a non-empty string")
                destination = Destination(
                    target=dest_data["target"],
                    host_subdir=bool(dest_data.get("host_subdir", True)),
                    rsync_args=_str_list(dest_data.get("rsync_args", []), "destination.rsync_args"),
                )
            defaults = data.get("defaults", {})
            excludes = _str_list(
                defaults.get("exclude", DEFAULT_EXCLUDES), "defaults.exclude"
            )
            entries = []
            for i, raw in enumerate(data.get("entries", [])):
                if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
                    raise ManifestError(f"entries[{i}] needs a 'path' string")
                entries.append(
                    Entry(
                        path=raw["path"],
                        exclude=_str_list(raw.get("exclude", []), f"entries[{i}].exclude"),
                        enabled=bool(raw.get("enabled", True)),
                    )
                )
        except AttributeError as exc:  # wrong table/array types
            raise ManifestError(f"malformed manifest: {exc}") from exc
        return cls(destination=destination, default_excludes=excludes, entries=entries)

    def to_dict(self) -> dict:
        data: dict = {}
        if self.destination:
            data["destination"] = self.destination.to_dict()
        data["defaults"] = {"exclude": list(self.default_excludes)}
        data["entries"] = [e.to_dict() for e in self.entries]
        return data

    def dumps(self) -> str:
        header = "# backuper manifest — edit with `backuper edit`\n\n"
        return header + tomli_w.dumps(self.to_dict())

    @classmethod
    def loads(cls, text: str) -> "Manifest":
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ManifestError(f"invalid TOML: {exc}") from exc
        return cls.from_dict(data)

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        if not path.exists():
            raise ManifestError(f"no manifest at {path}; run `backuper setup` first")
        return cls.loads(path.read_text())

    def save(self, path: Path) -> None:
        atomic_write(path, self.dumps())

    # ---- queries / mutation ------------------------------------------------

    def find(self, path: str) -> Entry | None:
        for entry in self.entries:
            if entry.path == path:
                return entry
        return None

    def add(self, path: str, exclude: list[str] | None = None) -> Entry:
        if self.find(path):
            raise ManifestError(f"{path} is already in the manifest")
        entry = Entry(path=path, exclude=list(exclude or []))
        self.entries.append(entry)
        return entry

    def remove(self, path: str) -> Entry:
        entry = self.find(path)
        if entry is None:
            raise ManifestError(f"{path} is not in the manifest")
        self.entries.remove(entry)
        return entry

    def excludes_for(self, entry: Entry) -> list[str]:
        return [*self.default_excludes, *entry.exclude]

    def validate(self) -> tuple[list[str], list[str]]:
        """Return ``(errors, warnings)``."""
        errors: list[str] = []
        warnings: list[str] = []
        seen: set[str] = set()
        for entry in self.entries:
            if not entry.path.startswith("/"):
                errors.append(f"entry path must be absolute: {entry.path}")
            if entry.path in seen:
                errors.append(f"duplicate entry: {entry.path}")
            seen.add(entry.path)

        paths = [Path(e.path) for e in self.entries]
        for a in paths:
            for b in paths:
                if a != b and a.is_relative_to(b):
                    warnings.append(f"{a} is nested inside {b} and will be copied twice")

        if self.destination and not self.destination.remote:
            target = Path(normalize_path(self.destination.target))
            for p in paths:
                if p.is_relative_to(target):
                    errors.append(f"entry {p} lies inside the destination {target}")
                elif target.is_relative_to(p):
                    warnings.append(
                        f"destination {target} lies inside entry {p}; it will be excluded"
                    )
        return errors, warnings


def _str_list(value, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ManifestError(f"{name} must be a list of strings")
    return list(value)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
