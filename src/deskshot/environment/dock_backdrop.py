"""Small visual backdrop for the macOS-style Plank dock.

Plank exposes good launcher geometry in this environment, but its own themed
background can collapse to a thin base line under Xvfb/picom. This helper draws
the translucent rounded pill over the dock area; Plank still owns and exposes
the actual dock icons.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Iterable

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402


@dataclass(frozen=True)
class DockBackdropGeometry:
    x: int
    y: int
    w: int
    h: int


def _parse_dbus_reply_strings(stdout: str) -> list[str]:
    return re.findall(r'string "([^"]+)"', stdout or "")


def _parse_hover_position(stdout: str) -> tuple[int, int] | None:
    ints = re.findall(r"int32 (-?\d+)", stdout or "")
    bools = re.findall(r"boolean (true|false)", stdout or "")
    if len(ints) < 2 or not bools or bools[0] != "true":
        return None
    return int(ints[0]), int(ints[1])


def _call_plank(method: str, *args: str) -> str | None:
    cmd = [
        "dbus-send",
        "--session",
        "--print-reply",
        "--dest=net.launchpad.plank",
        "/net/launchpad/plank/dock1",
        f"net.launchpad.plank.Items.{method}",
        *args,
    ]
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=2,
            env=dict(os.environ),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    return res.stdout


def query_plank_icon_positions() -> list[tuple[int, int]]:
    uris: list[str] = []
    for method in ("GetPersistentApplications", "GetTransientApplications"):
        stdout = _call_plank(method)
        if stdout:
            uris.extend(_parse_dbus_reply_strings(stdout))

    positions: list[tuple[int, int]] = []
    for uri in uris:
        stdout = _call_plank("GetHoverPosition", f"string:{uri}")
        if not stdout:
            continue
        pos = _parse_hover_position(stdout)
        if pos is not None:
            positions.append(pos)
    return positions


def resolve_geometry(
    *,
    screen_w: int,
    screen_h: int,
    icon_size: int,
    positions: Iterable[tuple[int, int]],
    bottom_margin: int = 12,
) -> DockBackdropGeometry:
    points = list(positions)
    if points:
        half = icon_size // 2
        left = min(x - half for x, _y in points)
        right = max(x + half for x, _y in points)
        pad_x = 18
        pad_top = 8
        pad_bottom = 12
        height = icon_size + pad_top + pad_bottom
        return DockBackdropGeometry(
            x=max(0, left - pad_x),
            y=max(0, screen_h - height - bottom_margin),
            w=min(screen_w, right + pad_x) - max(0, left - pad_x),
            h=height,
        )

    fallback_items = 7
    gap = 13
    pad_x = 18
    pad_top = 8
    pad_bottom = 12
    width = fallback_items * icon_size + (fallback_items - 1) * gap + pad_x * 2
    height = icon_size + pad_top + pad_bottom
    return DockBackdropGeometry(
        x=max(0, int((screen_w - width) / 2)),
        y=max(0, screen_h - height - bottom_margin),
        w=min(width, screen_w),
        h=height,
    )


class DockBackdropWindow(Gtk.Window):
    def __init__(
        self,
        *,
        dark: bool,
        icon_size: int,
        bottom_margin: int,
        screen_width: int | None = None,
        screen_height: int | None = None,
    ) -> None:
        super().__init__(type=Gtk.WindowType.POPUP)
        self.dark = dark
        self.icon_size = icon_size
        self.bottom_margin = bottom_margin
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.geometry: DockBackdropGeometry | None = None

        self.set_name("deskshot-dock-backdrop")
        self.set_title("deskshot-dock-backdrop")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_app_paintable(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_keep_above(True)

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)

        self.canvas = Gtk.EventBox()
        self.canvas.set_visible_window(True)
        self.canvas.set_name("deskshot-dock-backdrop-surface")
        self._install_css()
        self.add(self.canvas)

        GLib.timeout_add(400, self._sync_to_plank)
        self._sync_to_plank()

    def _install_css(self) -> None:
        provider = Gtk.CssProvider()
        if self.dark:
            css = b"""
#deskshot-dock-backdrop-surface {
  background-color: rgba(40, 40, 46, 0.82);
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 28px;
  box-shadow: 0 8px 22px rgba(0, 0, 0, 0.34), inset 0 1px rgba(255, 255, 255, 0.08);
}
"""
        else:
            css = b"""
#deskshot-dock-backdrop-surface {
  background-color: rgba(235, 238, 246, 0.34);
  border: 1px solid rgba(255, 255, 255, 0.56);
  border-radius: 28px;
  box-shadow: 0 8px 22px rgba(0, 0, 0, 0.24), inset 0 1px rgba(255, 255, 255, 0.48);
}
"""
        provider.load_from_data(css)
        self.canvas.get_style_context().add_provider(
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _screen_size(self) -> tuple[int, int]:
        if self.screen_width and self.screen_height:
            return self.screen_width, self.screen_height
        screen = self.get_screen()
        root = screen.get_root_window()
        if root is not None:
            try:
                geometry = root.get_geometry()
                return int(geometry.width), int(geometry.height)
            except (AttributeError, TypeError):
                try:
                    _x, _y, width, height = root.get_geometry()
                    return int(width), int(height)
                except (TypeError, ValueError):
                    pass
        return screen.get_width(), screen.get_height()

    def _sync_to_plank(self) -> bool:
        screen_w, screen_h = self._screen_size()
        geometry = resolve_geometry(
            screen_w=screen_w,
            screen_h=screen_h,
            icon_size=self.icon_size,
            positions=query_plank_icon_positions(),
            bottom_margin=self.bottom_margin,
        )
        if geometry != self.geometry:
            self.geometry = geometry
            self.set_default_size(geometry.w, geometry.h)
            self.set_size_request(geometry.w, geometry.h)
            self.canvas.set_size_request(geometry.w, geometry.h)
            self.move(geometry.x, geometry.y)
            self.resize(geometry.w, geometry.h)
            self.canvas.queue_draw()
        if not self.get_visible():
            self.show_all()
            self.set_keep_above(True)
        return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dark", action="store_true")
    parser.add_argument("--icon-size", type=int, default=54)
    parser.add_argument("--bottom-margin", type=int, default=12)
    parser.add_argument("--screen-width", type=int, default=0)
    parser.add_argument("--screen-height", type=int, default=0)
    args = parser.parse_args()

    DockBackdropWindow(
        dark=args.dark,
        icon_size=args.icon_size,
        bottom_margin=args.bottom_margin,
        screen_width=args.screen_width or None,
        screen_height=args.screen_height or None,
    )
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
