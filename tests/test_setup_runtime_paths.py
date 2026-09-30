"""Tests for recreating runtime bridge paths."""

import os
from pathlib import Path

from deskshot.environment import setup as setup_mod


def test_ensure_runtime_path_bridges_recreates_wrappers_and_symlinks(tmp_path: Path, monkeypatch) -> None:
    extracted = tmp_path / "extracted"
    (extracted / "usr" / "bin").mkdir(parents=True, exist_ok=True)
    (extracted / "usr" / "lib64").mkdir(parents=True, exist_ok=True)
    (extracted / "usr" / "share" / "xfwm4").mkdir(parents=True, exist_ok=True)
    (extracted / "usr" / "share" / "mate-panel" / "layouts").mkdir(parents=True, exist_ok=True)
    (extracted / "usr" / "share" / "mate-panel" / "applets").mkdir(parents=True, exist_ok=True)
    (extracted / "usr" / "lib64" / "mate-panel").mkdir(parents=True, exist_ok=True)
    (extracted / "usr" / "share" / "caja").mkdir(parents=True, exist_ok=True)
    (extracted / "usr" / "share" / "gnome-system-monitor").mkdir(parents=True, exist_ok=True)
    (extracted / "usr" / "share" / "geany").mkdir(parents=True, exist_ok=True)
    chromium_dir = extracted / "usr" / "lib64" / "chromium-browser"
    chromium_dir.mkdir(parents=True, exist_ok=True)

    (extracted / "usr" / "bin" / "xkbcomp").write_text("#!/bin/bash\n", encoding="utf-8")
    (extracted / "usr" / "bin" / "xkbcomp").chmod(0o755)
    (extracted / "usr" / "share" / "xfwm4" / "defaults").write_text("defaults", encoding="utf-8")
    (chromium_dir / "chromium-browser.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (chromium_dir / "chromium-browser").write_text("ELF\n", encoding="utf-8")
    (chromium_dir / "resources.pak").write_text("pak\n", encoding="utf-8")
    (extracted / "usr" / "bin" / "chromium-browser").symlink_to(
        Path("../lib64/chromium-browser/chromium-browser.sh")
    )

    monkeypatch.setattr(setup_mod, "XKBCOMP_DIR", tmp_path / "xkb")
    monkeypatch.setattr(setup_mod, "XFWM4_DATA_DIR", tmp_path / "xfwm4_data")
    monkeypatch.setattr(setup_mod, "MATE_PANEL_DATA_DIR", tmp_path / "mate_panel_data")
    monkeypatch.setattr(setup_mod, "MATE_PANEL_LIB_DIR", tmp_path / "mate_panel_libs")
    monkeypatch.setattr(setup_mod, "CAJA_DATA_DIR", tmp_path / "caja_data")
    monkeypatch.setattr(setup_mod, "GNOME_SYSTEM_MONITOR_DATA_DIR", tmp_path / "gnome_system_monitor_data")
    monkeypatch.setattr(setup_mod, "GEANY_DATA_DIR", tmp_path / "geany_data")
    monkeypatch.setenv("DESKSHOT_BIN_FIX_DIR", str(tmp_path / "bin_fix"))

    setup_mod.ensure_runtime_path_bridges(extracted)

    wrapper = setup_mod.XKBCOMP_DIR / "xkbcomp"
    assert wrapper.is_file()
    assert str(extracted / "usr" / "bin" / "xkbcomp") in wrapper.read_text(encoding="utf-8")

    assert (setup_mod.XFWM4_DATA_DIR / "defaults").is_symlink()
    assert (setup_mod.MATE_PANEL_DATA_DIR / "layouts").is_symlink()
    assert (setup_mod.MATE_PANEL_DATA_DIR / "applets").is_symlink()
    assert setup_mod.CAJA_DATA_DIR.is_symlink()
    assert setup_mod.GNOME_SYSTEM_MONITOR_DATA_DIR.is_symlink()
    assert setup_mod.GEANY_DATA_DIR.is_symlink()

    chromium_entry = tmp_path / "bin_fix" / "chromium-browser"
    chromium_runtime = tmp_path / "bin_fix" / "chromium-runtime"
    assert chromium_entry.is_symlink()
    assert chromium_entry.resolve() == (chromium_runtime / "chromium-browser.sh").resolve()
    assert os.access(chromium_runtime / "chromium-browser.sh", os.X_OK)
    assert os.access(chromium_runtime / "chromium-browser", os.X_OK)
    assert (chromium_runtime / "resources.pak").is_symlink()


def test_ensure_runtime_path_bridges_rewrites_gnome_terminal_service_exec(tmp_path: Path) -> None:
    extracted = tmp_path / "extracted"
    service_dir = extracted / "usr" / "share" / "dbus-1" / "services"
    service_dir.mkdir(parents=True, exist_ok=True)
    libexec_dir = extracted / "usr" / "libexec"
    libexec_dir.mkdir(parents=True, exist_ok=True)

    service_file = service_dir / "org.gnome.Terminal.service"
    service_file.write_text(
        "[D-BUS Service]\n"
        "Name=org.gnome.Terminal\n"
        "Exec=/usr/libexec/gnome-terminal-server\n",
        encoding="utf-8",
    )
    server_bin = libexec_dir / "gnome-terminal-server"
    server_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    server_bin.chmod(0o755)

    setup_mod.ensure_runtime_path_bridges(extracted)

    text = service_file.read_text(encoding="utf-8")
    assert f"Exec={server_bin.resolve()}" in text


def test_refresh_symlink_tolerates_parallel_fileexists(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "bridge"

    original = Path.symlink_to
    injected = {"done": False}

    def flaky_symlink(self: Path, target_path: Path, target_is_directory: bool = False) -> None:
        if self == link and not injected["done"]:
            injected["done"] = True
            os.symlink(target_path, self)
            raise FileExistsError()
        return original(self, target_path, target_is_directory)

    monkeypatch.setattr(Path, "symlink_to", flaky_symlink)

    setup_mod._refresh_symlink(link, target)

    assert link.is_symlink()
    assert link.resolve() == target.resolve()
