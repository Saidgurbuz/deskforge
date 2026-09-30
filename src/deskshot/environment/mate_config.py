"""MATE chrome configuration.

Panel layout is still enforced through deterministic layout files written into
the patched MATE datadir. Background and wallpaper settings are now handled
separately through keyfile-backed GSettings plus `mate-settings-daemon`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess

from deskshot.config import EXTRACTED_DIR, ThemeConfig
from deskshot.environment.panel_assets import dock_background_path

logger = logging.getLogger(__name__)

#: Five seconds was the old value and it was too short. With eight desktop
#: sessions coming up at once on one node, a `gsettings` call can genuinely take
#: longer, and every one of these was best-effort: a timeout was logged at debug
#: and the setting silently did not apply.
GSETTINGS_TIMEOUT_SEC = 25

DESKSHOT_LAYOUT_NAME = "deskshot"
MATE_PANEL_LAYOUTS_DIR = Path("/tmp/mate-panel_data_/layouts")


def _layout_linux() -> str:
    return """[Toplevel top]
expand=true
orientation=top
size=28

[Object menu-bar]
object-type=menu-bar
toplevel-id=top
position=0
locked=true
"""


def _layout_windows() -> str:
    return """[Toplevel bottom]
expand=true
orientation=bottom
size=34

[Object menu-bar]
object-type=menu-bar
toplevel-id=bottom
position=0
locked=true
"""


def _layout_macos() -> str:
    return """[Toplevel top]
expand=true
orientation=top
size=26

[Object menu-bar]
object-type=menu-bar
toplevel-id=top
position=0
locked=true

[Object clock]
object-type=applet
applet-iid=ClockAppletFactory::ClockApplet
toplevel-id=top
position=0
panel-right-stick=true
locked=true
"""


def _layout_ubuntu() -> str:
    return """[Toplevel top]
expand=true
orientation=top
size=28

[Object menu-bar]
object-type=menu-bar
toplevel-id=top
position=0
locked=true
"""


def _layout_for(orientation: str, size: int) -> str:
    return f"""[Toplevel panel]
expand=true
orientation={orientation}
size={size}

[Object menu-bar]
object-type=menu-bar
toplevel-id=panel
position=0
locked=true
"""


def _panel_launcher(path: Path, object_id: str, position: int, *, toplevel_id: str) -> str:
    return f"""[Object {object_id}]
object-type=launcher
launcher-location={path}
toplevel-id={toplevel_id}
position={position}
locked=true
"""


def _macos_dock_launcher(path: Path, object_id: str, position: int) -> str:
    return _panel_launcher(path, object_id, position, toplevel_id="dock")


def _macos_launcher_candidates() -> list[tuple[str, Path]]:
    apps_dir = EXTRACTED_DIR / "usr" / "share" / "applications"
    return [
        ("finder", apps_dir / "caja-browser.desktop"),
        ("browser", apps_dir / "chromium-browser.desktop"),
        ("notes", apps_dir / "org.xfce.mousepad.desktop"),
        ("mail", apps_dir / "thunderbird.desktop"),
        ("calculator", apps_dir / "org.gnome.Calculator.desktop"),
        ("archive", apps_dir / "xarchiver.desktop"),
    ]


def _ubuntu_launcher_candidates() -> list[tuple[str, Path]]:
    apps_dir = EXTRACTED_DIR / "usr" / "share" / "applications"
    return [
        ("files", apps_dir / "org.gnome.Nautilus.desktop"),
        ("browser", apps_dir / "chromium-browser.desktop"),
        ("editor", apps_dir / "org.gnome.gedit.desktop"),
        ("notes", apps_dir / "org.xfce.mousepad.desktop"),
        ("wiki", apps_dir / "zim.desktop"),
        ("mail", apps_dir / "thunderbird.desktop"),
        ("calculator", apps_dir / "org.gnome.Calculator.desktop"),
        ("archive", apps_dir / "xarchiver.desktop"),
        ("pluma", apps_dir / "pluma.desktop"),
    ]


def _layout_macos_dock(slim: bool = True) -> str:
    top_size = 26 if slim else 30
    dock_size = 72 if slim else 78
    dock_offset = 6 if slim else 10
    launcher_blocks = "\n".join(
        _macos_dock_launcher(path, object_id, idx * 10)
        for idx, (object_id, path) in enumerate(_macos_launcher_candidates())
        if path.is_file()
    )
    return f"""[Toplevel top]
expand=true
orientation=top
size={top_size}

[Toplevel dock]
expand=false
orientation=bottom
size={dock_size}
x-centered=true
y-bottom={dock_offset}

[Object menu-bar]
object-type=menu-bar
toplevel-id=top
position=0
locked=true

[Object clock]
object-type=applet
applet-iid=ClockAppletFactory::ClockApplet
toplevel-id=top
position=0
panel-right-stick=true
locked=true

{launcher_blocks}
"""


def _layout_ubuntu_left_dock(slim: bool = False) -> str:
    top_size = 26 if slim else 28
    dock_size = 44 if slim else 48
    launcher_blocks = "\n".join(
        _panel_launcher(path, object_id, idx * 10, toplevel_id="dock")
        for idx, (object_id, path) in enumerate(_ubuntu_launcher_candidates())
        if path.is_file()
    )
    return f"""[Toplevel top]
expand=true
orientation=top
size={top_size}

[Toplevel dock]
expand=true
orientation=left
size={dock_size}

[Object menu-bar]
object-type=menu-bar
toplevel-id=top
position=0
locked=true

[Object clock]
object-type=applet
applet-iid=ClockAppletFactory::ClockApplet
toplevel-id=top
position=0
panel-right-stick=true
locked=true

{launcher_blocks}
"""


def _default_panel_variant(style: str) -> str:
    key = (style or "linux").strip().lower()
    if key == "windows":
        return "bottom_tall"
    if key == "macos":
        return "top_slim_dock"
    if key == "ubuntu":
        return "left_dock"
    return "top"


def _layout_slug(value: str) -> str:
    cleaned = []
    for char in value.strip().lower():
        if char.isalnum():
            cleaned.append(char)
        elif char in {"-", "_"}:
            cleaned.append(char)
        else:
            cleaned.append("-")
    slug = "".join(cleaned).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "default"


def panel_layout_name(theme: ThemeConfig, *, use_plank: bool = False) -> str:
    """Return the immutable MATE layout name for one session configuration."""
    style = _layout_slug(theme.desktop_style or "linux")
    variant = _layout_slug(theme.panel_variant or _default_panel_variant(style))
    plank = "-plank" if use_plank else ""
    return f"{DESKSHOT_LAYOUT_NAME}-{style}-{variant}{plank}"


def _build_panel_layout_file(style: str, panel_variant: str = "", *, use_plank: bool = False) -> str:
    """Deterministic panel layout for a given desktop style or explicit variant."""
    variant = (panel_variant or _default_panel_variant(style)).strip().lower()
    style_key = style.strip().lower()
    if style_key == "macos" and use_plank:
        return _layout_macos()
    if style_key == "macos" and "dock" in variant:
        return _layout_macos_dock(slim="slim" in variant or variant == "top_dock")
    if variant.startswith("left"):
        return _layout_ubuntu_left_dock(slim="slim" in variant)
    if variant.startswith("bottom"):
        orientation = "bottom"
    elif variant.startswith("left"):
        orientation = "left"
    else:
        orientation = "top"

    if variant.endswith("slim"):
        size = 24
    elif variant.endswith("tall"):
        size = 34
    else:
        default_variant = _default_panel_variant(style)
        if default_variant.endswith("slim"):
            size = 24
        elif default_variant.endswith("tall"):
            size = 34
        else:
            size = 28

    return _layout_for(orientation, size)


def _build_debug_dump(theme: ThemeConfig, layout_name: str) -> str:
    """Debug-only snapshot of intended MATE keys."""
    lines: list[str] = []

    lines.append("[org/mate/panel/general]")
    lines.append(f"default-layout='{layout_name}'")
    lines.append("")

    lines.append("[org/mate/caja/desktop]")
    lines.append("computer-icon-visible=true")
    lines.append("home-icon-visible=true")
    lines.append("trash-icon-visible=true")
    lines.append("")

    lines.append("[org/mate/desktop/background]")
    if theme.wallpaper:
        lines.append(f"picture-filename='{theme.wallpaper}'")
        lines.append("picture-options='zoom'")
    else:
        lines.append("picture-options='wallpaper'")
        lines.append("color-shading-type='solid'")
        lines.append("primary-color='#3A84B8'")
        lines.append("secondary-color='#3A84B8'")
    lines.append("show-desktop-icons=true")
    lines.append("")

    return "\n".join(lines)


def _write_text_if_changed_atomic(path: Path, text: str) -> None:
    """Write text with atomic replace so concurrent readers never see truncation."""
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            return
    except OSError:
        pass

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_mate_dconf_config(
    theme: ThemeConfig,
    config_root: Path | None = None,
    *,
    use_plank: bool = False,
) -> Path:
    """Write deterministic panel layout file and debug config dump.

    Returns:
        Path to the selected variant layout file.
    """
    root = config_root or Path.cwd()
    root.mkdir(parents=True, exist_ok=True)

    # mate-panel binary is patched to read this datadir path. Keep layout files
    # variant-specific so concurrent jobs do not rewrite a shared default.layout.
    layouts_dir = MATE_PANEL_LAYOUTS_DIR
    layouts_dir.mkdir(parents=True, exist_ok=True)

    layout_text = _build_panel_layout_file(
        theme.desktop_style,
        theme.panel_variant,
        use_plank=use_plank,
    )
    layout_name = panel_layout_name(theme, use_plank=use_plank)
    layout_file = layouts_dir / f"{layout_name}.layout"
    _write_text_if_changed_atomic(layout_file, layout_text)
    logger.debug("Ensured MATE layout: %s", layout_file)

    dump_file = root / "mate_dconf_dump.ini"
    dump_file.write_text(_build_debug_dump(theme, layout_name), encoding="utf-8")

    return layout_file


def get_mate_env(config_root: Path | None = None) -> dict[str, str]:
    """No extra env required for the deterministic layout strategy."""
    return {}


def apply_mate_panel_settings(
    theme: ThemeConfig,
    env: dict[str, str] | None = None,
    *,
    use_plank: bool = False,
) -> None:
    """Apply small MATE panel tweaks after reset for style-specific polish."""
    runtime_env = env or {}
    style = (theme.desktop_style or "").strip().lower()
    commands: list[list[str]] = []
    gtk_theme = (theme.gtk_theme or "").strip().lower()
    dark_variant = "dark" in gtk_theme
    solid_variant = "solid" in gtk_theme

    def _set_panel_background(panel_id: str, color: str, opacity: int) -> None:
        base = f"/org/mate/panel/toplevels/{panel_id}/background/"
        commands.extend(
            [
                ["gsettings", "set", f"org.mate.panel.toplevel.background:{base}", "type", "color"],
                ["gsettings", "set", f"org.mate.panel.toplevel.background:{base}", "color", color],
                ["gsettings", "set", f"org.mate.panel.toplevel.background:{base}", "opacity", str(opacity)],
            ]
        )

    def _set_panel_none(panel_id: str) -> None:
        base = f"/org/mate/panel/toplevels/{panel_id}/background/"
        commands.extend(
            [
                ["gsettings", "set", f"org.mate.panel.toplevel.background:{base}", "type", "none"],
                ["gsettings", "set", f"org.mate.panel.toplevel.background:{base}", "image", ""],
            ]
        )

    def _set_panel_image(panel_id: str, image_path: Path) -> None:
        base = f"/org/mate/panel/toplevels/{panel_id}/background/"
        commands.extend(
            [
                ["gsettings", "set", f"org.mate.panel.toplevel.background:{base}", "type", "image"],
                ["gsettings", "set", f"org.mate.panel.toplevel.background:{base}", "image", str(image_path)],
                ["gsettings", "set", f"org.mate.panel.toplevel.background:{base}", "fit", "false"],
                ["gsettings", "set", f"org.mate.panel.toplevel.background:{base}", "stretch", "true"],
                ["gsettings", "set", f"org.mate.panel.toplevel.background:{base}", "rotate", "false"],
            ]
        )

    if style == "macos":
        dock_color = "#1a1a1a" if dark_variant else "#e8e8ec"
        dock_opacity = 55000 if solid_variant else 45000
        commands.extend(
            [
                ["gsettings", "set", "org.mate.panel.menubar", "show-applications", "false"],
                ["gsettings", "set", "org.mate.panel.menubar", "show-places", "false"],
                ["gsettings", "set", "org.mate.panel.menubar", "show-desktop", "false"],
                ["gsettings", "set", "org.mate.panel.menubar", "show-icon", "true"],
                ["gsettings", "set", "org.mate.panel.menubar", "icon-name", "start-here-symbolic"],
            ]
        )
        _set_panel_none("top")
        if not use_plank and "dock" in (theme.panel_variant or "top_slim_dock").strip().lower():
            dock_asset = dock_background_path(
                theme.gtk_theme,
                EXTRACTED_DIR / "usr" / "share" / "backgrounds" / "deskshot-stylepacks" / "panel-assets",
            )
            if dock_asset is not None:
                _set_panel_image("dock", dock_asset)
            else:
                _set_panel_background("dock", dock_color, dock_opacity)
    elif style == "ubuntu":
        commands.extend(
            [
                ["gsettings", "set", "org.mate.panel.menubar", "show-applications", "true"],
                # Places lists the host's mounted volumes, which on this cluster
                # means its GPFS device name. Same reasoning as show-desktop.
                ["gsettings", "set", "org.mate.panel.menubar", "show-places", "false"],
                ["gsettings", "set", "org.mate.panel.menubar", "show-desktop", "false"],
                ["gsettings", "set", "org.mate.panel.menubar", "show-icon", "true"],
                ["gsettings", "set", "org.mate.panel.menubar", "icon-name", "start-here-symbolic"],
            ]
        )
        _set_panel_background("dock", "#20233a" if dark_variant else "#2c2145", 58000)
    else:
        commands.extend(
            [
                ["gsettings", "set", "org.mate.panel.menubar", "show-applications", "true"],
                # Places lists the host's mounted volumes, which on this cluster
                # means its GPFS device name. Same reasoning as show-desktop.
                ["gsettings", "set", "org.mate.panel.menubar", "show-places", "false"],
                # The System menu's last item is "Log Out <real name>...", and on this
                # host glib's real name is a work email address and an employee
                # serial number (see src/deskshot/privacy.py). glibc reads that
                # from passwd, so no environment override reaches it - the menu
                # itself has to go.
                ["gsettings", "set", "org.mate.panel.menubar", "show-desktop", "false"],
                ["gsettings", "set", "org.mate.panel.menubar", "show-icon", "false"],
                ["gsettings", "set", "org.mate.panel.menubar", "icon-name", "start-here"],
            ]
        )

    for cmd in commands:
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=runtime_env or None,
                timeout=GSETTINGS_TIMEOUT_SEC,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            logger.debug("Failed to apply MATE panel tweak: %s", " ".join(cmd))
            continue
        if result.returncode != 0:
            logger.debug("MATE panel tweak returned %s: %s", result.returncode, " ".join(cmd))
