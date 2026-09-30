"""Picom configuration helpers for macOS-style sessions."""

from __future__ import annotations

from pathlib import Path

from deskshot.config import ThemeConfig


def write_picom_config(theme: ThemeConfig, config_root: Path) -> Path:
    """Write a conservative X11 picom config for desktop polish."""
    config_dir = config_root / "picom"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "picom.conf"
    dark = "dark" in (theme.gtk_theme or "").strip().lower()

    shadow_opacity = "0.28" if dark else "0.22"
    inactive_opacity = "0.96" if dark else "0.98"
    active_opacity = "1.0"

    config_path.write_text(
        "\n".join(
            [
                'backend = "xrender";',
                "vsync = false;",
                "shadow = true;",
                "shadow-radius = 18;",
                "shadow-offset-x = 0;",
                "shadow-offset-y = 10;",
                f"shadow-opacity = {shadow_opacity};",
                "fading = false;",
                "use-damage = true;",
                "unredir-if-possible = false;",
                "detect-client-opacity = true;",
                "mark-wmwin-focused = true;",
                "mark-ovredir-focused = true;",
                f"active-opacity = {active_opacity};",
                f"inactive-opacity = {inactive_opacity};",
                'shadow-exclude = [',
                '  "class_g = \'Xfdesktop\'",',
                '  "class_g = \'Plank\'",',
                '  "class_g = \'caja\' && argb",',
                '];',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path
