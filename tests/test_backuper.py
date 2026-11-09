import json
import socket
from pathlib import Path

import pytest
from click.testing import CliRunner

from backuper.cli import cli
from backuper.manifest import Manifest, ManifestError

HOST = socket.gethostname()


@pytest.fixture
def env(tmp_path, monkeypatch):
    for var, sub in [("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"),
                     ("XDG_RUNTIME_DIR", "run")]:
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(var, str(tmp_path / sub))
    monkeypatch.setenv("BACKUPER_SYSLOG", "0")
    monkeypatch.delenv("BACKUPER_MANIFEST", raising=False)
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    dest = tmp_path / "dest"
    dest.mkdir()
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("a")
    (src / "sub" / "b.txt").write_text("b")
    (src / "skip.tmp").write_text("excluded by default")
    (src / "build").mkdir()
    (src / "build" / "out.o").write_text("o")
    return tmp_path


def run(*args, input=None):
    result = CliRunner().invoke(cli, list(args), input=input, catch_exceptions=False)
    return result


def stored(env, path: Path) -> Path:
    return env / "dest" / HOST / str(path).lstrip("/")


def setup_and_add(env, *extra):
    assert run("setup", str(env / "dest"), "--no-timer").exit_code == 0
    r = run("add", str(env / "src"), *extra)
    assert r.exit_code == 0, r.output


def test_add_list_remove_roundtrip(env):
    setup_and_add(env, "-e", "build")
    m = Manifest.load(env / "config" / "backuper" / "manifest.toml")
    assert [e.path for e in m.entries] == [str(env / "src")]
    assert m.entries[0].exclude == ["build"]

    out = run("ls").output
    assert str(env / "src") in out and "build" in out

    assert run("add", str(env / "src")).exit_code != 0  # duplicate
    assert run("disable", str(env / "src")).exit_code == 0
    assert Manifest.load(env / "config" / "backuper" / "manifest.toml").entries[0].enabled is False
    assert run("rm", str(env / "src")).exit_code == 0
    assert Manifest.load(env / "config" / "backuper" / "manifest.toml").entries == []


def test_exclude_command(env):
    setup_and_add(env)
    run("exclude", str(env / "src"), "x", "y")
    run("exclude", str(env / "src"), "x", "-r")
    run("exclude", "--default", "zzz")
    m = Manifest.load(env / "config" / "backuper" / "manifest.toml")
    assert m.entries[0].exclude == ["y"]
    assert "zzz" in m.default_excludes


def test_backup_mirrors_excludes_and_deletes(env):
    setup_and_add(env, "-e", "build")
    r = run("backup")
    assert r.exit_code == 0, r.output
    copy = stored(env, env / "src")
    assert (copy / "a.txt").read_text() == "a"
    assert (copy / "sub" / "b.txt").read_text() == "b"
    assert not (copy / "skip.tmp").exists()
    assert not (copy / "build").exists()

    (env / "src" / "a.txt").unlink()
    assert run("backup").exit_code == 0
    assert not (copy / "a.txt").exists()
    assert (copy / "sub" / "b.txt").exists()


def test_dry_run_changes_nothing(env):
    setup_and_add(env)
    r = run("backup", "--dry-run")
    assert r.exit_code == 0
    assert "a.txt" in r.output
    assert not stored(env, env / "src").exists()
    assert not (env / "dest" / HOST / ".backuper").exists()


def test_manifest_copied_to_destination(env):
    setup_and_add(env)
    run("backup")
    meta = env / "dest" / HOST / ".backuper"
    m = Manifest.load(meta / "manifest.toml")
    assert [e.path for e in m.entries] == [str(env / "src")]
    last = json.loads((meta / "last-run.json").read_text())
    assert last["success"] is True


def test_empty_or_missing_source_keeps_copy(env):
    setup_and_add(env)
    run("backup")
    copy = stored(env, env / "src")
    for p in sorted((env / "src").rglob("*"), reverse=True):
        p.rmdir() if p.is_dir() else p.unlink()
    r = run("backup")
    assert r.exit_code == 1
    assert (copy / "a.txt").exists()
    (env / "src").rmdir()
    assert run("backup").exit_code == 1
    assert (copy / "a.txt").exists()


def test_missing_destination_root_is_fatal(env):
    setup_and_add(env)
    (env / "dest").rmdir()
    assert run("backup").exit_code == 1
    assert not (env / "dest").exists()


def test_restore_to_dir(env):
    setup_and_add(env)
    run("backup")
    out = env / "restored"
    r = run("restore", "--to", str(out))
    assert r.exit_code == 0, r.output
    restored = out / str(env / "src").lstrip("/")
    assert (restored / "a.txt").read_text() == "a"
    assert (restored / "sub" / "b.txt").read_text() == "b"


def test_restore_in_place_without_local_manifest(env):
    setup_and_add(env)
    run("backup")
    (env / "src" / "a.txt").write_text("changed")
    (env / "src" / "new.txt").write_text("new")
    (env / "config" / "backuper" / "manifest.toml").unlink()

    r = run("restore", "--from", str(env / "dest"), "-y")
    assert r.exit_code == 0, r.output
    assert (env / "src" / "a.txt").read_text() == "a"
    assert (env / "src" / "new.txt").exists()  # no --delete: nothing removed

    r = run("restore", "--from", str(env / "dest"), "-y", "--delete")
    assert r.exit_code == 0
    assert not (env / "src" / "new.txt").exists()


def test_restore_ignores_entries_added_after_backup(env):
    setup_and_add(env)
    run("backup")
    other = env / "other"
    other.mkdir()
    (other / "x").write_text("x")
    run("add", str(other))
    r = run("restore", "--to", str(env / "restored"))
    assert r.exit_code == 0
    assert not (env / "restored" / str(other).lstrip("/")).exists()
    assert run("restore", str(other), "--to", str(env / "r2")).exit_code == 1


def test_restore_list(env):
    setup_and_add(env)
    run("backup")
    out = run("restore", "--list").output
    assert HOST in out and str(env / "src") in out


def test_purge(env):
    setup_and_add(env)
    run("backup")
    assert run("rm", str(env / "src"), "--purge", "-y").exit_code == 0
    assert not stored(env, env / "src").exists()
    m = Manifest.load(env / "dest" / HOST / ".backuper" / "manifest.toml")
    assert m.entries == []


def test_removed_entry_stays_in_dest_manifest_until_purged(env):
    setup_and_add(env)
    run("backup")
    other = env / "other"
    other.mkdir()
    (other / "x").write_text("x")
    run("add", str(other))
    run("rm", str(env / "src"))
    run("backup")
    m = Manifest.load(env / "dest" / HOST / ".backuper" / "manifest.toml")
    assert {e.path for e in m.entries} == {str(env / "src"), str(other)}


def test_destination_inside_source_is_excluded(env, tmp_path):
    home = tmp_path / "home"
    (home / "docs").mkdir(parents=True)
    (home / "docs" / "d.txt").write_text("d")
    (home / "nc" / "backup").mkdir(parents=True)
    assert run("setup", str(home / "nc" / "backup"), "--no-timer").exit_code == 0
    assert run("add", str(home)).exit_code == 0
    assert run("backup").exit_code == 0
    copy = home / "nc" / "backup" / HOST / str(home).lstrip("/")
    assert (copy / "docs" / "d.txt").exists()
    assert not (copy / "nc" / "backup").exists()


def test_validation():
    m = Manifest.loads('[destination]\ntarget = "/b"\n[[entries]]\npath = "/b/x"\n')
    errors, _ = m.validate()
    assert any("inside the destination" in e for e in errors)
    with pytest.raises(ManifestError):
        Manifest.loads("[[entries]]\nexclude = []\n")
    with pytest.raises(ManifestError):
        Manifest.loads("not toml [")
