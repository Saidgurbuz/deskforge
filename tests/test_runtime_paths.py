from __future__ import annotations

from pathlib import Path

from deskshot.config import bin_fix_dir, runtime_tmp_dir


def test_bin_fix_dir_defaults_under_tmpdir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DESKSHOT_BIN_FIX_DIR", raising=False)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "job_tmp"))

    assert runtime_tmp_dir() == tmp_path / "job_tmp"
    assert bin_fix_dir() == tmp_path / "job_tmp" / "deskshot_bin_fix"


def test_bin_fix_dir_allows_explicit_override(monkeypatch, tmp_path: Path) -> None:
    custom = tmp_path / "custom_fix"
    monkeypatch.setenv("TMPDIR", str(tmp_path / "job_tmp"))
    monkeypatch.setenv("DESKSHOT_BIN_FIX_DIR", str(custom))

    assert bin_fix_dir() == custom
