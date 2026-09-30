from __future__ import annotations

import subprocess

from deskshot.config import ThemeConfig
from deskshot.environment.plank_config import apply_plank_settings


def test_apply_plank_settings_disables_zoom(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, stdout, stderr, env, timeout):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("deskshot.environment.plank_config.subprocess.run", fake_run)

    apply_plank_settings(
        ThemeConfig(
            gtk_theme="MacTahoe-Light-solid",
            icon_theme="WhiteSur-light",
            wm_theme="MacTahoe-Light-solid",
            desktop_style="macos",
        ),
        env={},
    )

    schema_path = "net.launchpad.plank.dock.settings:/net/launchpad/plank/docks/dock1/"
    assert ["gsettings", "set", schema_path, "zoom-enabled", "false"] in calls
    assert ["gsettings", "set", schema_path, "zoom-percent", "100"] in calls
