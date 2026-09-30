"""The cleanup must never touch a running batch.

It kills processes and deletes directories, and it is meant to be safe to run
at any time - including while a 12-scene batch is in flight. Every test here is
about the boundary between "this belongs to a dead run" and "this belongs to a
live one", because getting that wrong destroys hours of collection.

The load-bearing cases, each of which failed at least once while the script was
being written:

- A lock file naming a live PID that is not an X server. PIDs get recycled on a
  box that has churned through thousands of scene subprocesses, so "the PID
  exists" is not evidence the display does.
- A live session's own scratch directory. `/proc/<pid>/environ` is the
  environment a process was *started* with, and DesktopSession sets its scratch
  paths in-process, so an idle session's directories appear in no process
  environment at all. The age floor derived from the oldest running Xvfb is
  what actually protects them.
"""

import importlib.util
import os
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "cleanup_stale_sessions", PROJECT_ROOT / "scripts" / "cleanup_stale_sessions.py"
)
cleanup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cleanup)


# --------------------------------------------------------------------------
# display liveness
# --------------------------------------------------------------------------

def test_lock_naming_a_live_non_x_process_is_stale(tmp_path, monkeypatch):
    """A recycled PID must not keep a dead display number reserved forever."""
    monkeypatch.chdir(tmp_path)
    lock = Path("/tmp/.X31998-lock")
    lock.write_text(f"{os.getpid()}\n")  # alive, but this is pytest, not Xvfb
    try:
        assert cleanup.display_is_live(":31998", None) is False
    finally:
        lock.unlink(missing_ok=True)


def test_lock_naming_a_dead_pid_is_stale():
    lock = Path("/tmp/.X31997-lock")
    lock.write_text("999999\n")
    try:
        assert cleanup.display_is_live(":31997", None) is False
    finally:
        lock.unlink(missing_ok=True)


def test_missing_lock_is_stale():
    assert cleanup.display_is_live(":31996", None) is False


def test_live_x_server_is_detected(monkeypatch):
    """When the lock names a real X server the display is live.

    Simulated by pointing `comm` lookup at this process while claiming it is
    Xvfb, which is the only part of the check that distinguishes the two.
    """
    lock = Path("/tmp/.X31995-lock")
    lock.write_text(f"{os.getpid()}\n")
    real_read_text = Path.read_text

    def fake_read_text(self, *a, **kw):
        if self.name == "comm":
            return "Xvfb\n"
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    try:
        assert cleanup.display_is_live(":31995", None) is True
    finally:
        monkeypatch.undo()
        lock.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# temp sweep
# --------------------------------------------------------------------------

def _make_temp_root(tmp_path, monkeypatch, entries):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("DESKSHOT_BIN_FIX_DIR", str(tmp_path / "deskshot_bin_fix"))
    for name, age_hours in entries.items():
        d = tmp_path / name
        d.mkdir()
        (d / "payload").write_text("x" * 100)
        stamp = time.time() - age_hours * 3600
        os.utime(d, (stamp, stamp))
    return tmp_path


def test_staging_cache_is_never_swept(tmp_path, monkeypatch):
    """deskshot_bin_fix is a deliberate cache - deleting it re-copies every binary."""
    _make_temp_root(tmp_path, monkeypatch,
                    {"deskshot_bin_fix": 500.0, "deskshot_dead_run": 500.0})
    monkeypatch.setattr(cleanup, "oldest_session_start", lambda: None)
    monkeypatch.setattr(cleanup, "paths_in_use", lambda root: set())

    names = {p.name for p, _ in cleanup.find_stale_temp(min_age_hours=6.0)}
    assert names == {"deskshot_dead_run"}


def test_recent_entries_are_left_alone(tmp_path, monkeypatch):
    _make_temp_root(tmp_path, monkeypatch,
                    {"deskshot_fresh": 0.5, "deskshot_old": 50.0})
    monkeypatch.setattr(cleanup, "oldest_session_start", lambda: None)
    monkeypatch.setattr(cleanup, "paths_in_use", lambda root: set())

    names = {p.name for p, _ in cleanup.find_stale_temp(min_age_hours=6.0)}
    assert names == {"deskshot_old"}


def test_running_session_protects_newer_dirs_regardless_of_age_flag(tmp_path, monkeypatch):
    """The case that matters: --temp-age-hours 0 against a live batch.

    A session started an hour ago. Its scratch dir is younger than that; a dir
    from a run that died three days ago is not. Even with the age guard fully
    disabled, only the older one may be swept.
    """
    _make_temp_root(tmp_path, monkeypatch,
                    {"deskshot_live_session": 0.2, "deskshot_died_days_ago": 72.0})
    monkeypatch.setattr(cleanup, "oldest_session_start", lambda: time.time() - 3600)
    monkeypatch.setattr(cleanup, "paths_in_use", lambda root: set())

    names = {p.name for p, _ in cleanup.find_stale_temp(min_age_hours=0.0)}
    assert names == {"deskshot_died_days_ago"}


def test_in_use_paths_are_skipped(tmp_path, monkeypatch):
    _make_temp_root(tmp_path, monkeypatch, {"deskshot_busy": 99.0, "deskshot_idle": 99.0})
    monkeypatch.setattr(cleanup, "oldest_session_start", lambda: None)
    monkeypatch.setattr(cleanup, "paths_in_use", lambda root: {tmp_path / "deskshot_busy"})

    names = {p.name for p, _ in cleanup.find_stale_temp(min_age_hours=6.0)}
    assert names == {"deskshot_idle"}


# --------------------------------------------------------------------------
# session floor
# --------------------------------------------------------------------------

def test_oldest_session_start_uses_the_longest_running_xvfb(monkeypatch):
    monkeypatch.setattr(cleanup, "_ps", lambda fields: [
        ["101", "30", "Xvfb"],
        ["102", "9000", "Xvfb"],
        ["103", "99999", "python"],
    ])
    floor = cleanup.oldest_session_start()
    assert floor is not None
    # ~9000s ago, plus the minute of slack.
    assert 9020 < time.time() - floor < 9200


def test_no_xvfb_means_no_floor(monkeypatch):
    monkeypatch.setattr(cleanup, "_ps", lambda fields: [["103", "5", "python"]])
    assert cleanup.oldest_session_start() is None


# --------------------------------------------------------------------------
# orphan daemons
# --------------------------------------------------------------------------

def test_daemon_on_a_live_display_is_kept(monkeypatch):
    monkeypatch.setattr(cleanup, "_ps", lambda fields: [
        ["1", "500", "9000", "7000", "at-spi2-registr"],
    ])
    monkeypatch.setattr(cleanup, "_proc_env", lambda pid, key: ":1850")
    assert cleanup.find_orphan_daemons({":1850"}, 300, None) == []


def test_daemon_with_a_parent_is_kept(monkeypatch):
    """Only re-parented (ppid 1) daemons are orphans; the desktop session's own
    daemons hang off `systemd --user` and must survive."""
    monkeypatch.setattr(cleanup, "_ps", lambda fields: [
        ["2711037", "500", "9000", "7000", "at-spi2-registr"],
    ])
    monkeypatch.setattr(cleanup, "_proc_env", lambda pid, key: ":666")
    assert cleanup.find_orphan_daemons(set(), 300, None) == []


def test_daemon_without_a_display_is_kept(monkeypatch):
    """No DISPLAY means no evidence of staleness."""
    monkeypatch.setattr(cleanup, "_ps", lambda fields: [
        ["1", "500", "9000", "7000", "dbus-daemon"],
    ])
    monkeypatch.setattr(cleanup, "_proc_env", lambda pid, key: None)
    assert cleanup.find_orphan_daemons(set(), 300, None) == []


def test_young_daemon_is_kept(monkeypatch):
    monkeypatch.setattr(cleanup, "_ps", lambda fields: [
        ["1", "500", "10", "7000", "dbus-daemon"],
    ])
    monkeypatch.setattr(cleanup, "_proc_env", lambda pid, key: ":666")
    assert cleanup.find_orphan_daemons(set(), 300, None) == []


def test_orphan_on_a_dead_display_is_found(monkeypatch):
    monkeypatch.setattr(cleanup, "_ps", lambda fields: [
        ["1", "500", "9000", "7168", "at-spi2-registr"],
        ["1", "501", "9000", "7168", "bash"],  # not a session daemon
    ])
    monkeypatch.setattr(cleanup, "_proc_env", lambda pid, key: ":31994")
    found = cleanup.find_orphan_daemons(set(), 300, None)
    assert [d["pid"] for d in found] == [500]
    assert found[0]["rss_mb"] == 7.0
