"""backuper: manifest-driven rsync backups."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("backuper")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0+unknown"
