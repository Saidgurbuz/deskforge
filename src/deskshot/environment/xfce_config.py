"""XFCE pre-configuration: write xfconf XML channel files before startup.

Writing config files before starting XFCE components ensures deterministic
startup without needing xfconf-query or a running xfconfd.

All files are written to project-local XFCE_CONFIG_DIR, never to ~/.config.
"""

from __future__ import annotations

import logging
from pathlib import Path
from textwrap import dedent

from deskshot.config import XFCE_CONFIG_DIR, ThemeConfig

logger = logging.getLogger(__name__)


def _ensure_channel_dir(config_dir: Path) -> Path:
    """Create the xfconf channel XML directory and return its path."""
    channel_dir = config_dir / "xfce4" / "xfconf" / "xfce-perchannel-xml"
    channel_dir.mkdir(parents=True, exist_ok=True)
    return channel_dir


def write_xfce_panel_config(config_dir: Path | None = None) -> Path:
    """Write xfce4-panel channel config.

    Panel at top, with: applicationsmenu, tasklist, separator(expand),
    pager, systray, clock, actions plugins.
    """
    channel_dir = _ensure_channel_dir(config_dir or XFCE_CONFIG_DIR)
    path = channel_dir / "xfce4-panel.xml"

    xml = dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <channel name="xfce4-panel" version="1.0">
          <property name="configver" type="int" value="2"/>
          <property name="panels" type="array">
            <value type="int" value="1"/>
            <property name="dark-mode" type="bool" value="false"/>
            <property name="panel-1" type="empty">
              <property name="position" type="string" value="p=6;x=0;y=0"/>
              <property name="length" type="uint" value="100"/>
              <property name="position-locked" type="bool" value="true"/>
              <property name="icon-size" type="uint" value="0"/>
              <property name="size" type="uint" value="26"/>
              <property name="plugin-ids" type="array">
                <value type="int" value="1"/>
                <value type="int" value="2"/>
                <value type="int" value="3"/>
                <value type="int" value="4"/>
                <value type="int" value="5"/>
                <value type="int" value="6"/>
                <value type="int" value="7"/>
              </property>
            </property>
          </property>
          <property name="plugins" type="empty">
            <property name="plugin-1" type="string" value="applicationsmenu"/>
            <property name="plugin-2" type="string" value="tasklist">
              <property name="flat-buttons" type="bool" value="true"/>
              <property name="show-handle" type="bool" value="false"/>
            </property>
            <property name="plugin-3" type="string" value="separator">
              <property name="expand" type="bool" value="true"/>
              <property name="style" type="uint" value="0"/>
            </property>
            <property name="plugin-4" type="string" value="pager"/>
            <property name="plugin-5" type="string" value="systray">
              <property name="square-icons" type="bool" value="true"/>
            </property>
            <property name="plugin-6" type="string" value="clock">
              <property name="digital-format" type="string" value="%R"/>
            </property>
            <property name="plugin-7" type="string" value="actions"/>
          </property>
        </channel>
    """)

    path.write_text(xml, encoding="utf-8")
    logger.debug(f"Wrote xfce4-panel config to {path}")
    return path


def write_xfce_desktop_config(
    theme: ThemeConfig,
    config_dir: Path | None = None,
) -> Path:
    """Write xfdesktop channel config (wallpaper and style)."""
    channel_dir = _ensure_channel_dir(config_dir or XFCE_CONFIG_DIR)
    path = channel_dir / "xfce4-desktop.xml"

    wallpaper = theme.wallpaper or ""
    # image-style: 5 = zoomed (fill), 3 = stretched, 0 = none
    image_style = "5" if wallpaper else "0"
    # color-style: 0 = solid color, 1 = horizontal gradient, 2 = vertical gradient
    color_style = "0"

    xml = dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <channel name="xfce4-desktop" version="1.0">
          <property name="backdrop" type="empty">
            <property name="screen0" type="empty">
              <property name="monitorscreen" type="empty">
                <property name="workspace0" type="empty">
                  <property name="color-style" type="int" value="{color_style}"/>
                  <property name="image-style" type="int" value="{image_style}"/>
                  <property name="last-image" type="string" value="{wallpaper}"/>
                  <property name="rgba1" type="array">
                    <value type="double" value="0.227451"/>
                    <value type="double" value="0.517647"/>
                    <value type="double" value="0.721569"/>
                    <value type="double" value="1.000000"/>
                  </property>
                </property>
              </property>
            </property>
          </property>
        </channel>
    """)

    path.write_text(xml, encoding="utf-8")
    logger.debug(f"Wrote xfce4-desktop config to {path}")
    return path


def write_xfce_xsettings_config(
    theme: ThemeConfig,
    config_dir: Path | None = None,
) -> Path:
    """Write xsettings channel config (GTK theme, icon theme, font, cursor)."""
    channel_dir = _ensure_channel_dir(config_dir or XFCE_CONFIG_DIR)
    path = channel_dir / "xsettings.xml"

    xml = dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <channel name="xsettings" version="1.0">
          <property name="Net" type="empty">
            <property name="ThemeName" type="string" value="{theme.gtk_theme}"/>
            <property name="IconThemeName" type="string" value="{theme.icon_theme}"/>
            <property name="CursorThemeName" type="string" value="{theme.cursor_theme}"/>
          </property>
          <property name="Gtk" type="empty">
            <property name="FontName" type="string" value="{theme.font}"/>
            <property name="CursorThemeName" type="string" value="{theme.cursor_theme}"/>
          </property>
          <property name="Xft" type="empty">
            <property name="Antialias" type="int" value="1"/>
            <property name="HintStyle" type="string" value="hintslight"/>
            <property name="RGBA" type="string" value="rgb"/>
          </property>
        </channel>
    """)

    path.write_text(xml, encoding="utf-8")
    logger.debug(f"Wrote xsettings config to {path}")
    return path


def write_xfwm4_config(
    theme: ThemeConfig,
    config_dir: Path | None = None,
    *,
    external_compositor: bool = False,
) -> Path:
    """Write xfwm4 channel config (window manager theme)."""
    channel_dir = _ensure_channel_dir(config_dir or XFCE_CONFIG_DIR)
    path = channel_dir / "xfwm4.xml"

    button_layout_xml = ""
    if (theme.desktop_style or "").strip().lower() == "macos":
        button_layout_xml = (
            '    <property name="button_layout" type="string" value="CHM|"/>\n'
        )
    macos_style = (theme.desktop_style or "").strip().lower() == "macos"
    glass_variant = macos_style and "solid" not in (theme.gtk_theme or "").strip().lower()
    dark_variant = "dark" in (theme.gtk_theme or "").strip().lower()
    compositing_xml = "false" if external_compositor else ("true" if macos_style else "false")
    frame_opacity = "90" if glass_variant else "95"
    inactive_opacity = "88" if macos_style else "100"
    popup_opacity = "97" if macos_style else "100"
    shadow_opacity = "75" if dark_variant else "65"
    title_shadow_xml = "true" if macos_style else "false"

    xml = dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <channel name="xfwm4" version="1.0">
          <property name="general" type="empty">
            <property name="theme" type="string" value="{theme.wm_theme}"/>
            <property name="title_font" type="string" value="{theme.font}"/>
{button_layout_xml}\
            <property name="use_compositing" type="bool" value="{compositing_xml}"/>
            <property name="frame_opacity" type="int" value="{frame_opacity}"/>
            <property name="inactive_opacity" type="int" value="{inactive_opacity}"/>
            <property name="popup_opacity" type="int" value="{popup_opacity}"/>
            <property name="shadow_opacity" type="int" value="{shadow_opacity}"/>
            <property name="show_frame_shadow" type="bool" value="{str(macos_style).lower()}"/>
            <property name="show_popup_shadow" type="bool" value="{str(macos_style).lower()}"/>
            <property name="title_shadow_active" type="bool" value="{title_shadow_xml}"/>
            <property name="title_shadow_inactive" type="bool" value="false"/>
            <property name="placement_ratio" type="int" value="20"/>
          </property>
        </channel>
    """)

    path.write_text(xml, encoding="utf-8")
    logger.debug(f"Wrote xfwm4 config to {path}")
    return path


def write_all_xfce_configs(
    theme: ThemeConfig,
    config_dir: Path | None = None,
    *,
    external_compositor: bool = False,
) -> list[Path]:
    """Write all XFCE configuration channel files.

    Args:
        theme: Theme configuration to apply.
        config_dir: Project-local config directory (default: XFCE_CONFIG_DIR).

    Returns:
        List of paths to written config files.
    """
    cdir = config_dir or XFCE_CONFIG_DIR
    paths = [
        write_xfce_panel_config(cdir),
        write_xfce_desktop_config(theme, cdir),
        write_xfce_xsettings_config(theme, cdir),
        write_xfwm4_config(theme, cdir, external_compositor=external_compositor),
    ]
    logger.info(f"Wrote {len(paths)} XFCE config files to {cdir}")
    return paths
