from __future__ import annotations

from deskshot.environment.dock_backdrop import (
    _parse_dbus_reply_strings,
    _parse_hover_position,
    resolve_geometry,
)


def test_parse_plank_dbus_replies() -> None:
    assert _parse_dbus_reply_strings(
        'method return\n   array [\n      string "file:///tmp/firefox.desktop"\n   ]\n'
    ) == ["file:///tmp/firefox.desktop"]
    assert _parse_hover_position(
        "method return\n   int32 859\n   int32 1007\n   int32 3\n   boolean true\n"
    ) == (859, 1007)


def test_resolve_geometry_tracks_live_plank_positions() -> None:
    geom = resolve_geometry(
        screen_w=1920,
        screen_h=1080,
        icon_size=54,
        positions=[(758, 1007), (825, 1007), (892, 1007)],
    )

    assert geom.x == 713
    assert geom.y == 994
    assert geom.w == 224
    assert geom.h == 74
