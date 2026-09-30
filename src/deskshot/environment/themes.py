"""Theme management: list available themes, write GTK settings, XDG isolation.

All config files are written to project-local directories — never to ~/.config.
"""

from __future__ import annotations

import logging
import random
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional

from deskshot.config import EXTRACTED_DIR, ASSETS_DIR, ThemeConfig
from deskshot.environment.wallpaper_catalog import (
    build_wallpaper_catalog,
    select_wallpaper_candidates,
    select_wallpaper_sample_pool,
)

logger = logging.getLogger(__name__)

# Kept as the fallback for a caller that asks for the macOS look without a
# wallpaper seed. The four macOS presets used to pin these outright, and because
# macOS styles are about a third of all scenes, `MacTahoe-day.jpeg` alone was the
# background of 29% of the corpus (172 of 597 captures) - one image, learned as
# if it were what a desktop looks like. They sample from the style pool now,
# like every other preset.
_MAC_TAHOE_DAY_WALLPAPER = str(
    EXTRACTED_DIR / "usr" / "share" / "backgrounds" / "deskshot-stylepacks" / "MacTahoe-day.jpeg"
)
_MAC_TAHOE_NIGHT_WALLPAPER = str(
    EXTRACTED_DIR / "usr" / "share" / "backgrounds" / "deskshot-stylepacks" / "MacTahoe-night.jpeg"
)


def _scan_theme_dirs(theme_roots: list[Path], marker_subdir: str) -> List[str]:
    found: set[str] = set()
    for root in theme_roots:
        if not root.is_dir():
            continue
        for d in root.iterdir():
            if d.is_dir() and (d / marker_subdir).is_dir():
                found.add(d.name)
    return sorted(found)


def list_available_gtk_themes(extracted_dir: Path | None = None) -> List[str]:
    """Scan extracted + system themes for dirs containing gtk-3.0/."""
    roots = [
        (extracted_dir or EXTRACTED_DIR) / "usr" / "share" / "themes",
        Path("/usr/share/themes"),
    ]
    return _scan_theme_dirs(roots, "gtk-3.0")


def list_available_icon_themes(extracted_dir: Path | None = None) -> List[str]:
    """Scan extracted + system icons for dirs containing index.theme."""
    found: set[str] = set()
    for root in [
        (extracted_dir or EXTRACTED_DIR) / "usr" / "share" / "icons",
        Path("/usr/share/icons"),
    ]:
        if not root.is_dir():
            continue
        for d in root.iterdir():
            if d.is_dir() and (d / "index.theme").is_file():
                found.add(d.name)
    return sorted(found)


def list_available_wm_themes(extracted_dir: Path | None = None) -> List[str]:
    """Scan extracted + system themes for dirs containing xfwm4/."""
    roots = [
        (extracted_dir or EXTRACTED_DIR) / "usr" / "share" / "themes",
        Path("/usr/share/themes"),
    ]
    return _scan_theme_dirs(roots, "xfwm4")


def list_available_wallpapers(extracted_dir: Path | None = None) -> List[str]:
    """Scan assets/wallpapers/ and extracted backgrounds for image files."""
    return [entry.path for entry in build_wallpaper_catalog(extracted_dir=extracted_dir, assets_dir=ASSETS_DIR)]


def _select_style_wallpaper(
    wallpaper: str,
    desktop_style: str,
    wallpaper_seed: int = 0,
    extracted_dir: Path | None = None,
) -> str:
    path = Path(wallpaper) if wallpaper else None
    if path and path.is_file():
        return str(path)

    wallpapers = list_available_wallpapers(extracted_dir)
    if not wallpapers:
        return wallpaper

    choices = select_wallpaper_candidates(
        desktop_style,
        extracted_dir=extracted_dir,
        assets_dir=ASSETS_DIR,
    )
    if not choices:
        choices = wallpapers
    if wallpaper_seed == 0:
        return choices[0]
    choices = select_wallpaper_sample_pool(
        desktop_style,
        extracted_dir=extracted_dir,
        assets_dir=ASSETS_DIR,
    ) or choices
    rng = random.Random(wallpaper_seed)
    return choices[rng.randrange(len(choices))]


def _theme_dir(theme_name: str, extracted_dir: Path | None = None) -> Optional[Path]:
    roots = [
        (extracted_dir or EXTRACTED_DIR) / "usr" / "share" / "themes",
        Path("/usr/share/themes"),
    ]
    for root in roots:
        candidate = root / theme_name
        if candidate.is_dir():
            return candidate
    return None


def _prefer_dark_theme(theme: ThemeConfig) -> bool:
    return "dark" in (theme.gtk_theme or "").strip().lower()


def _write_gtk4_theme_override(
    theme: ThemeConfig,
    config_dir: Path,
    extracted_dir: Path | None = None,
) -> None:
    """Mirror theme gtk-4.0 assets into XDG config for libadwaita-aware apps."""
    theme_dir = _theme_dir(theme.gtk_theme, extracted_dir=extracted_dir)
    if theme_dir is None:
        return
    src = theme_dir / "gtk-4.0"
    if not src.is_dir():
        return
    dst = config_dir / "gtk-4.0"
    shutil.copytree(src, dst, dirs_exist_ok=True)


def write_gtk_settings_ini(
    theme: ThemeConfig,
    config_dir: Path,
    *,
    extracted_dir: Path | None = None,
) -> Path:
    """Write gtk-3.0/settings.ini to a project-local config directory.

    Args:
        theme: Theme configuration to write.
        config_dir: Project-local XDG_CONFIG_HOME equivalent.

    Returns:
        Path to the written settings.ini file.
    """
    gtk_dir = config_dir / "gtk-3.0"
    gtk_dir.mkdir(parents=True, exist_ok=True)

    settings_path = gtk_dir / "settings.ini"
    settings_path.write_text(
        f"[Settings]\n"
        f"gtk-theme-name={theme.gtk_theme}\n"
        f"gtk-icon-theme-name={theme.icon_theme}\n"
        f"gtk-cursor-theme-name={theme.cursor_theme}\n"
        f"gtk-font-name={theme.font}\n"
        f"gtk-application-prefer-dark-theme={1 if _prefer_dark_theme(theme) else 0}\n",
        encoding="utf-8",
    )
    _write_gtk4_theme_override(theme, config_dir, extracted_dir=extracted_dir)
    write_gtk_custom_css(theme, config_dir)
    logger.debug(f"Wrote GTK settings to {settings_path}")
    return settings_path


def _build_macos_panel_css(theme: ThemeConfig) -> str:
    """Build GTK CSS overrides for macOS-style MATE panel polish."""
    dark = _prefer_dark_theme(theme)

    # Top bar colors: translucent dark or light
    if dark:
        top_bg = "rgba(22, 22, 22, 0.78)"
        top_text = "rgba(255, 255, 255, 0.90)"
        btn_hover = "rgba(255, 255, 255, 0.12)"
        btn_active = "rgba(255, 255, 255, 0.20)"
        menu_hover = "rgba(255, 255, 255, 0.10)"
    else:
        top_bg = "rgba(240, 240, 240, 0.72)"
        top_text = "rgba(30, 30, 30, 0.88)"
        btn_hover = "rgba(0, 0, 0, 0.08)"
        btn_active = "rgba(0, 0, 0, 0.14)"
        menu_hover = "rgba(0, 0, 0, 0.06)"

    return f"""\
/* DeskShot macOS-style panel overrides
 * Written by write_gtk_custom_css() — loaded after theme CSS.            */

/* ── Top bar: translucent background ────────────────────────────────── */
.mate-panel-menu-bar,
panel-toplevel.background {{
    background-color: {top_bg};
    color: {top_text};
    box-shadow: none;
    border: none;
}}

/* ── ALL panel buttons: strip button chrome completely ───────────────── */
.mate-panel-menu-bar button,
.mate-panel-menu-bar button.flat,
.mate-panel-menu-bar button.toggle,
.mate-panel-menu-bar button:backdrop,
.mate-panel-menu-bar #PanelApplet button,
.mate-panel-menu-bar #PanelApplet button.toggle,
panel-toplevel.background button,
panel-toplevel.background button.flat,
panel-toplevel.background button.toggle {{
    background: none;
    background-color: transparent;
    background-image: none;
    border: none;
    border-width: 0;
    border-color: transparent;
    box-shadow: none;
    outline: none;
    outline-style: none;
    -gtk-icon-shadow: none;
    text-shadow: none;
    icon-shadow: none;
}}

/* ── Hover: subtle translucent rounded highlight ─────────────────── */
.mate-panel-menu-bar button:hover,
panel-toplevel.background button:hover {{
    background-color: {btn_hover};
    border-radius: 6px;
    border: none;
    box-shadow: none;
}}

/* ── Active / checked: slightly stronger highlight ───────────────── */
.mate-panel-menu-bar button:active,
.mate-panel-menu-bar button:checked,
panel-toplevel.background button:active,
panel-toplevel.background button:checked {{
    background-color: {btn_active};
    border-radius: 6px;
    border: none;
    box-shadow: none;
}}

/* ── Focus: no ring, no outline ──────────────────────────────────── */
.mate-panel-menu-bar button:focus,
.mate-panel-menu-bar button:focus:hover,
panel-toplevel.background button:focus,
panel-toplevel.background button:focus:hover {{
    outline: none;
    outline-style: none;
    box-shadow: none;
    border: none;
    border-color: transparent;
}}

/* ── Menu-bar items: clean macOS-like appearance ─────────────────── */
.mate-panel-menu-bar menubar,
panel-toplevel.background menubar {{
    background: transparent;
    box-shadow: none;
    border: none;
    color: {top_text};
}}

.mate-panel-menu-bar menubar > menuitem {{
    padding: 2px 8px;
    border-radius: 4px;
    color: {top_text};
}}

.mate-panel-menu-bar menubar > menuitem:hover {{
    background-color: {menu_hover};
    border-radius: 4px;
}}

/* ── Panel applet labels / images: inherit text color ────────────── */
.mate-panel-menu-bar #PanelApplet label,
.mate-panel-menu-bar #PanelApplet image,
panel-toplevel.background #PanelApplet label,
panel-toplevel.background #PanelApplet image {{
    color: {top_text};
}}

/* ── Tasklist buttons: clean ─────────────────────────────────────── */
.mate-panel-menu-bar #tasklist-button,
.mate-panel-menu-bar #tasklist-button:checked,
panel-toplevel.background #tasklist-button,
panel-toplevel.background #tasklist-button:checked {{
    background: none;
    background-color: transparent;
    border: none;
    box-shadow: none;
}}

.mate-panel-menu-bar #tasklist-button:checked {{
    background-color: {btn_active};
    border-radius: 6px;
}}
"""


def write_gtk_custom_css(theme: ThemeConfig, config_dir: Path) -> Path:
    """Write user-level GTK CSS overrides for panel styling polish.

    For macOS-style sessions this strips MATE panel button chrome and adds
    translucency.  For other styles the file is removed so no overrides
    leak across presets.
    """
    gtk_dir = config_dir / "gtk-3.0"
    gtk_dir.mkdir(parents=True, exist_ok=True)
    css_path = gtk_dir / "gtk.css"

    style = (theme.desktop_style or "").strip().lower()
    if style != "macos":
        if css_path.exists():
            css_path.unlink()
        return css_path

    css_path.write_text(_build_macos_panel_css(theme), encoding="utf-8")
    logger.debug("Wrote macOS panel CSS overrides to %s", css_path)
    return css_path


def setup_xdg_isolation(config_root: Path) -> Dict[str, str]:
    """Create project-local XDG directories and return env var dict.

    Args:
        config_root: Root directory for isolated XDG dirs.

    Returns:
        Dict of XDG environment variable names to directory paths.
    """
    xdg_dirs = {
        "XDG_CONFIG_HOME": config_root / "config",
        "XDG_DATA_HOME": config_root / "data",
        "XDG_CACHE_HOME": config_root / "cache",
        "XDG_STATE_HOME": config_root / "state",
        "XDG_RUNTIME_DIR": config_root / "runtime",
    }

    for name, path in xdg_dirs.items():
        path.mkdir(parents=True, exist_ok=True)
        if name == "XDG_RUNTIME_DIR":
            path.chmod(0o700)

    env = {name: str(path) for name, path in xdg_dirs.items()}
    logger.debug(f"XDG isolation dirs created under {config_root}")
    return env


THEME_PRESETS: dict[str, ThemeConfig] = {
    # Baseline Linux look used for dense panel/caja extraction.
    "linux_classic": ThemeConfig(
        gtk_theme="Adwaita",
        icon_theme="Papirus",
        wm_theme="Default",
        cursor_theme="Adwaita",
        font="Sans 10",
        desktop_style="linux",
    ),
    # Bottom panel + Windows-11-like theme stack (if style packs are installed).
    "windows_redmond": ThemeConfig(
        gtk_theme="Win11-Light",
        icon_theme="Win11",
        wm_theme="Win11-Light",
        cursor_theme="Adwaita",
        font="Sans 10",
        desktop_style="windows",
    ),
    # Top bar-centric style with macOS Tahoe-like GTK theme.
    "macos_tahoe_like": ThemeConfig(
        gtk_theme="MacTahoe-Light-solid",
        icon_theme="WhiteSur-light",
        wm_theme="MacTahoe-Light-solid",
        cursor_theme="WhiteSur-cursors",
        font="Sans 11",
        desktop_style="macos",
        panel_variant="top_slim_dock",
    ),
    # Darker macOS-inspired GTK stack from WhiteSur.
    "quartz_night": ThemeConfig(
        gtk_theme="MacTahoe-Dark-solid",
        icon_theme="WhiteSur-dark",
        wm_theme="MacTahoe-Dark-solid",
        cursor_theme="WhiteSur-cursors",
        font="Sans 11",
        desktop_style="macos",
        panel_variant="top_slim_dock",
    ),
    # Lighter glassy WhiteSur variant for a softer recent-macOS look.
    "macos_tahoe_glass": ThemeConfig(
        gtk_theme="MacTahoe-Light",
        icon_theme="WhiteSur-light",
        wm_theme="MacTahoe-Light",
        cursor_theme="WhiteSur-cursors",
        font="Sans 11",
        desktop_style="macos",
        panel_variant="top_slim_dock",
    ),
    # Nord-flavored darker variant for added macOS-style diversity.
    "quartz_night_nord": ThemeConfig(
        gtk_theme="MacTahoe-Dark-solid-nord",
        icon_theme="WhiteSur-dark",
        wm_theme="MacTahoe-Dark-solid-nord",
        cursor_theme="WhiteSur-cursors",
        font="Sans 11",
        desktop_style="macos",
        panel_variant="top_slim_dock",
    ),
    # Ubuntu-like layout emphasis (top + left rail).
    "ubuntu_like": ThemeConfig(
        gtk_theme="Adwaita",
        icon_theme="Papirus",
        wm_theme="Default-hdpi",
        cursor_theme="Adwaita",
        font="Sans 10",
        desktop_style="ubuntu",
    ),
}


def list_theme_presets() -> Dict[str, Dict[str, str]]:
    """Return all named UI presets as serializable dictionaries."""
    return {name: asdict(cfg) for name, cfg in THEME_PRESETS.items()}


def _first_available(choices: list[str], available: set[str], fallback: str) -> str:
    for c in choices:
        if c in available:
            return c
    return fallback


def resolve_theme_config(theme: ThemeConfig, extracted_dir: Path | None = None) -> ThemeConfig:
    """Resolve missing theme/icon/wm names to available local alternatives."""
    resolved = ThemeConfig(**asdict(theme))

    gtk_available = set(list_available_gtk_themes(extracted_dir))
    icon_available = set(list_available_icon_themes(extracted_dir))
    wm_available = set(list_available_wm_themes(extracted_dir))

    if gtk_available and resolved.gtk_theme not in gtk_available:
        gtk_choices = ["Tahoe-Light", "WhiteSur-Light", "WhiteSur-Dark", "Quartz Night", "Win11-Light", "Adwaita", "Default"]
        if resolved.desktop_style == "macos":
            gtk_choices = [
                "MacTahoe-Light-solid",
                "MacTahoe-Light",
                "MacTahoe-Light-solid-nord",
                "MacTahoe-Dark-solid",
                "MacTahoe-Dark",
                "MacTahoe-Dark-solid-nord",
                "WhiteSur-Light-solid",
                "WhiteSur-Light",
                "WhiteSur-Light-solid-nord",
                "WhiteSur-Dark-solid",
                "WhiteSur-Dark",
                "WhiteSur-Dark-solid-nord",
                "Tahoe-Light",
                "Tahoe-Dark",
                "Quartz Night",
                "Adwaita",
                "Default",
            ]
        resolved.gtk_theme = _first_available(gtk_choices, gtk_available, sorted(gtk_available)[0])

    if icon_available and resolved.icon_theme not in icon_available:
        icon_choices = ["Win11", "Papirus", "Adwaita", "hicolor"]
        if resolved.desktop_style == "macos":
            if _prefer_dark_theme(resolved):
                icon_choices = ["WhiteSur-dark", "WhiteSur-light", "WhiteSur", "Papirus", "Adwaita", "hicolor"]
            else:
                icon_choices = ["WhiteSur-light", "WhiteSur", "WhiteSur-dark", "Papirus", "Adwaita", "hicolor"]
        resolved.icon_theme = _first_available(icon_choices, icon_available, sorted(icon_available)[0])

    if wm_available and resolved.wm_theme not in wm_available:
        wm_choices = ["Win11-Light", "Redmond", "Keramik", "Default", "Default-hdpi"]
        if resolved.desktop_style == "macos":
            wm_choices = [
                "MacTahoe-Light-solid",
                "MacTahoe-Light",
                "MacTahoe-Light-solid-nord",
                "MacTahoe-Dark-solid",
                "MacTahoe-Dark",
                "MacTahoe-Dark-solid-nord",
                "WhiteSur-Light-solid",
                "WhiteSur-Light",
                "WhiteSur-Light-solid-nord",
                "WhiteSur-Dark-solid",
                "WhiteSur-Dark",
                "WhiteSur-Dark-solid-nord",
                "Keramik",
                "Default",
                "Default-hdpi",
            ]
        resolved.wm_theme = _first_available(wm_choices, wm_available, sorted(wm_available)[0])

    resolved.wallpaper = _select_style_wallpaper(
        resolved.wallpaper,
        resolved.desktop_style,
        resolved.wallpaper_seed,
        extracted_dir=extracted_dir,
    )

    return resolved


def resolve_firefox_theme_hint(theme: ThemeConfig) -> str:
    """Map the current desktop theme to a staged Firefox theme variant."""
    if (theme.desktop_style or "").strip().lower() != "macos":
        return ""

    gtk = (theme.gtk_theme or "").strip().lower()
    if "nord" in gtk:
        if "whitesur" in gtk:
            return "whitesur-nord"
        return "mactahoe-nord"
    if "whitesur" in gtk:
        return "whitesur-darker" if "dark" in gtk else "whitesur-light"
    if "mactahoe" in gtk and "solid" not in gtk:
        return "monterey-darker" if "dark" in gtk else "monterey-light"
    if "mactahoe" in gtk:
        return "mactahoe-darker" if "dark" in gtk else "mactahoe-light"
    return ""


def get_theme_preset(
    name: str,
    *,
    resolve: bool = True,
    extracted_dir: Path | None = None,
) -> ThemeConfig:
    """Resolve a preset name to ThemeConfig."""
    if name not in THEME_PRESETS:
        raise KeyError(f"Unknown theme preset: {name}")
    base = ThemeConfig(**asdict(THEME_PRESETS[name]))
    if not resolve:
        return base
    return resolve_theme_config(base, extracted_dir=extracted_dir)
