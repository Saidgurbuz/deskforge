"""Plank dock configuration helpers for macOS-style sessions."""

from __future__ import annotations

import subprocess
from pathlib import Path

from deskshot.config import EXTRACTED_DIR, ThemeConfig


def resolve_plank_theme_name(theme: ThemeConfig) -> str:
    gtk = (theme.gtk_theme or "").strip().lower()
    if "mactahoe" in gtk:
        return "MacTahoe-Dark" if "dark" in gtk else "MacTahoe-Light"
    if "whitesur" in gtk:
        return "WhiteSur-Dark" if "dark" in gtk else "WhiteSur-Light"
    return "WhiteSur-Dark" if "dark" in gtk else "WhiteSur-Light"


def write_plank_launchers(config_root: Path) -> list[Path]:
    """Write deterministic pinned dock items for Plank."""
    launchers_dir = config_root / "plank" / "dock1" / "launchers"
    launchers_dir.mkdir(parents=True, exist_ok=True)
    apps_dir = EXTRACTED_DIR / "usr" / "share" / "applications"
    launchers = [
        ("finder", apps_dir / "caja-browser.desktop"),
        ("browser", apps_dir / "chromium-browser.desktop"),
        ("notes", apps_dir / "org.xfce.mousepad.desktop"),
        ("mail", apps_dir / "thunderbird.desktop"),
        ("calculator", apps_dir / "org.gnome.Calculator.desktop"),
        ("archive", apps_dir / "xarchiver.desktop"),
    ]

    created: list[Path] = []
    for index, (name, desktop_file) in enumerate(launchers, start=1):
        if not desktop_file.is_file():
            continue
        dockitem = launchers_dir / f"{index:02d}-{name}.dockitem"
        dockitem.write_text(
            "\n".join(
                [
                    "[PlankItemsDockItemPreferences]",
                    f"Launcher=file://{desktop_file.resolve()}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        created.append(dockitem)
    return created


def apply_plank_settings(theme: ThemeConfig, env: dict[str, str] | None = None) -> None:
    """Apply a deterministic set of Plank dock settings via GSettings."""
    runtime_env = env or None
    schema_path = "net.launchpad.plank.dock.settings:/net/launchpad/plank/docks/dock1/"
    theme_name = resolve_plank_theme_name(theme)

    commands = [
        ["gsettings", "set", schema_path, "theme", theme_name],
        ["gsettings", "set", schema_path, "position", "bottom"],
        ["gsettings", "set", schema_path, "alignment", "center"],
        ["gsettings", "set", schema_path, "hide-mode", "none"],
        ["gsettings", "set", schema_path, "icon-size", "54"],
        ["gsettings", "set", schema_path, "zoom-enabled", "false"],
        ["gsettings", "set", schema_path, "zoom-percent", "100"],
        ["gsettings", "set", schema_path, "show-dock-item", "false"],
        ["gsettings", "set", schema_path, "offset", "0"],
    ]

    for cmd in commands:
        try:
            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=runtime_env,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
