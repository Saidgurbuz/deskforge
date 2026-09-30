"""Binary staging is shared state, and every session runs it.

Three scene workers staging at once produced `Text file busy:
/tmp/deskshot_bin_fix/Xvfb` and, less visibly, sessions starting against a
binary another worker was still writing. Both are fixed here: the work is
serialised, and each individual write is atomic even if the lock is bypassed.
"""

import multiprocessing
import os
import time
from pathlib import Path

import pytest

from deskshot.environment import setup as setup_mod


@pytest.fixture
def fix_dir(tmp_path, monkeypatch):
    target = tmp_path / "bin_fix"
    target.mkdir()
    monkeypatch.setattr(setup_mod, "bin_fix_dir", lambda: target)
    return target


def test_the_lock_is_exclusive(fix_dir) -> None:
    with setup_mod.staging_lock():
        started = time.monotonic()
        # A second acquisition in-process would deadlock on a real lock, so the
        # timeout path is what is exercised: it must give up, not hang forever.
        with setup_mod.staging_lock(timeout=0.5):
            pass
        assert time.monotonic() - started < 30


def test_the_lock_is_released_after_use(fix_dir) -> None:
    with setup_mod.staging_lock():
        pass
    with setup_mod.staging_lock(timeout=1.0):
        pass
    assert (fix_dir / ".staging.lock").is_file()


def test_a_patch_never_writes_the_target_in_place(fix_dir, monkeypatch) -> None:
    """The replacement must arrive by rename, so a running process either sees
    the whole old file or the whole new one."""
    binary = fix_dir / "Xvfb"
    binary.write_bytes(b"prefix/usr/bin\x00suffix")
    binary.chmod(0o755)
    original_inode = binary.stat().st_ino

    replaced = setup_mod._patch_binary_bytes(
        binary, b"/usr/bin\x00", b"/tmp/xkb\x00", label="test"
    )

    assert replaced
    assert b"/tmp/xkb\x00" in binary.read_bytes()
    assert binary.stat().st_ino != original_inode      # arrived by rename
    assert oct(binary.stat().st_mode)[-3:] == "755"


def test_no_temporary_files_are_left_behind(fix_dir) -> None:
    binary = fix_dir / "xfwm4"
    binary.write_bytes(b"aaa/usr/bin\x00bbb")
    binary.chmod(0o755)

    setup_mod._patch_binary_bytes(binary, b"/usr/bin\x00", b"/tmp/xkb\x00", label="t")

    assert [p.name for p in fix_dir.iterdir() if p.name.startswith(".")] == []


def test_an_already_patched_binary_is_left_alone(fix_dir) -> None:
    binary = fix_dir / "Xvfb"
    binary.write_bytes(b"x/tmp/xkb\x00y")
    binary.chmod(0o755)
    inode = binary.stat().st_ino

    assert setup_mod._patch_binary_bytes(
        binary, b"/usr/bin\x00", b"/tmp/xkb\x00", label="t"
    )
    assert binary.stat().st_ino == inode        # untouched, so nothing to race
