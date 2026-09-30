"""Tests for setup download helpers."""

from pathlib import Path

from deskshot.environment import setup as setup_mod


class _Result:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode


def test_download_rpm_with_deps_uses_resolve_and_writes_marker(
    monkeypatch, tmp_path: Path
) -> None:
    calls = []

    def _fake_run(cmd, capture_output):
        calls.append((cmd, capture_output))
        return _Result(0)

    monkeypatch.setattr(setup_mod.subprocess, "run", _fake_run)

    assert setup_mod.download_rpm_with_deps("geany", tmp_path) is True
    assert calls
    cmd, capture_output = calls[0]
    assert capture_output is True
    assert cmd[:5] == ["dnf", "download", "-y", "--resolve", "--alldeps"]
    assert "--arch=x86_64,noarch" in cmd
    assert str(tmp_path) in cmd
    assert "geany" == cmd[-1]
    assert (tmp_path / ".resolved" / "geany.done").read_text(encoding="utf-8") == "ok\n"


def test_download_rpm_with_deps_skips_when_marker_exists(
    monkeypatch, tmp_path: Path
) -> None:
    marker = tmp_path / ".resolved" / "mousepad.done"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("ok\n", encoding="utf-8")

    def _fail_run(*args, **kwargs):
        raise AssertionError("subprocess.run should not be called when marker exists")

    monkeypatch.setattr(setup_mod.subprocess, "run", _fail_run)

    assert setup_mod.download_rpm_with_deps("mousepad", tmp_path) is True


def test_all_rpms_include_editor_companion_packages() -> None:
    for pkg in setup_mod.EDITOR_IDE_COMPANION_RPMS:
        assert pkg in setup_mod.ALL_RPMS


def test_editor_rpms_include_zim_packages() -> None:
    for pkg in ("Zim", "python3-zim"):
        assert pkg in setup_mod.EDITOR_IDE_RPMS
        assert pkg in setup_mod.ALL_RPMS


def test_business_rpms_include_libreoffice_langpack() -> None:
    assert "libreoffice-langpack-en" in setup_mod.BUSINESS_APP_RPMS
    assert "libreoffice-langpack-en" in setup_mod.ALL_RPMS


def test_worker_app_rpms_include_promoted_candidates() -> None:
    for pkg in (
        "filezilla",
        "gnome-logs",
        "homebank",
        "qalculate-gtk",
        "seahorse",
        "transmission-gtk",
    ):
        assert pkg in setup_mod.WORKER_APP_RPMS
        assert pkg in setup_mod.ALL_RPMS


def test_editor_companion_rpms_include_qt_x11_extras() -> None:
    assert "qt5-qtx11extras" in setup_mod.EDITOR_IDE_COMPANION_RPMS
    assert "qt5-qtx11extras" in setup_mod.ALL_RPMS


def test_app_rpms_include_evince_libs() -> None:
    assert "evince-libs" in setup_mod.APP_RPMS
    assert "evince-libs" in setup_mod.ALL_RPMS


def test_mate_rpms_include_gvfs_client_for_icon_metadata() -> None:
    assert "gvfs-client" in setup_mod.MATE_RPMS
    assert "gvfs-client" in setup_mod.ALL_RPMS


def test_native_probe_rpms_are_included() -> None:
    for pkg in ("keepassxc", "xarchiver", "xournalpp", "dia", "gucharmap"):
        assert pkg in setup_mod.NATIVE_PROBE_RPMS
        assert pkg in setup_mod.ALL_RPMS


def test_dependency_rpms_cover_candidate_runtime_libs() -> None:
    for pkg in (
        "gucharmap-libs",
        "qrencode-libs",
        "libzip",
        "gperftools-libs",
        "portaudio",
    ):
        assert pkg in setup_mod.DEPENDENCY_RPMS
        assert pkg in setup_mod.ALL_RPMS


def test_all_rpms_include_picom_build_packages() -> None:
    for pkg in ("meson", "ninja-build", "libconfig-devel", "libev-devel", "libX11-xcb"):
        assert pkg in setup_mod.PICOM_BUILD_RPMS
        assert pkg in setup_mod.ALL_RPMS


def test_manifest_binary_targets_cover_plain_binaries(monkeypatch, tmp_path: Path) -> None:
    """App binaries are staged from the manifests, not a hand-kept list."""
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    (apps_dir / "demo.yaml").write_text(
        "app_name: demo\nbinary: demo-bin\natspi_name: demo\n", encoding="utf-8"
    )
    (apps_dir / "bundled.yaml").write_text(
        "app_name: bundled\nbinary: /opt/vendor/bundled\natspi_name: bundled\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("deskshot.config.CONFIGS_DIR", tmp_path)

    targets = setup_mod._manifest_binary_targets(Path("/extracted"))

    assert "demo-bin" in targets
    assert Path("/extracted/usr/bin/demo-bin") in targets["demo-bin"]
    # Absolute manifest binaries are user-owned bundles that keep their own bits.
    assert "/opt/vendor/bundled" not in targets
    assert "bundled" not in targets


def test_manifest_binary_targets_include_previously_missed_pool_apps() -> None:
    """Regression: these pool apps lacked +x and were absent from the fixed list."""
    targets = setup_mod._manifest_binary_targets(Path("/extracted"))

    for binary in ("thunar", "eog", "gnome-calculator"):
        assert binary in targets, f"{binary} must be staged from its manifest"


def test_prepend_fix_dir_to_path_is_idempotent(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    setup_mod._prepend_fix_dir_to_path(Path("/tmp/fixdir"))
    setup_mod._prepend_fix_dir_to_path(Path("/tmp/fixdir"))

    import os

    assert os.environ["PATH"] == "/tmp/fixdir:/usr/bin:/bin"


def test_staged_copy_is_current_requires_exec_and_matching_size(tmp_path: Path) -> None:
    src = tmp_path / "src-bin"
    dest = tmp_path / "dest-bin"
    src.write_bytes(b"abcdef")

    assert setup_mod._staged_copy_is_current(src, dest) is False

    dest.write_bytes(b"abcdef")
    dest.chmod(0o755)
    assert setup_mod._staged_copy_is_current(src, dest) is True

    dest.write_bytes(b"abc")
    dest.chmod(0o755)
    assert setup_mod._staged_copy_is_current(src, dest) is False


def test_runtime_stage_is_current_detects_matching_stage(tmp_path: Path) -> None:
    """Browser runtimes must not be re-copied once staged for the same source."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "browser").write_bytes(b"binary")
    (src_dir / "lib.so").write_bytes(b"lib")

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    entry = tmp_path / "entry"
    entry.write_bytes(b"binary")
    entry.chmod(0o755)

    # No stamp yet -> must stage.
    assert setup_mod._runtime_stage_is_current(src_dir, runtime_dir, entry) is False

    setup_mod._write_runtime_stage_stamp(src_dir, runtime_dir)
    assert setup_mod._runtime_stage_is_current(src_dir, runtime_dir, entry) is True

    # A changed source invalidates the stage.
    (src_dir / "browser").write_bytes(b"binary-v2-longer")
    assert setup_mod._runtime_stage_is_current(src_dir, runtime_dir, entry) is False

    setup_mod._write_runtime_stage_stamp(src_dir, runtime_dir)
    assert setup_mod._runtime_stage_is_current(src_dir, runtime_dir, entry) is True

    # A missing entry point invalidates the stage even when the stamp matches.
    entry.unlink()
    assert setup_mod._runtime_stage_is_current(src_dir, runtime_dir, entry) is False


def test_runtime_stage_is_current_requires_executable_entry(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "browser").write_bytes(b"binary")
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    entry = tmp_path / "entry"
    entry.write_bytes(b"binary")
    entry.chmod(0o644)

    setup_mod._write_runtime_stage_stamp(src_dir, runtime_dir)

    assert setup_mod._runtime_stage_is_current(src_dir, runtime_dir, entry) is False
