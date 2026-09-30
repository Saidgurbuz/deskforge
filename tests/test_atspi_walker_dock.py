from __future__ import annotations

import subprocess

from deskshot.extraction.atspi_walker import (
    _complete_plank_dock_slots,
    _plank_item_label,
    _augment_plank_elements,
    _query_plank_dbus_items,
)


def test_query_plank_dbus_items_parses_labels_and_positions(monkeypatch) -> None:
    def fake_run(cmd, capture_output, text, timeout, env):
        method = cmd[5].split(".")[-1]
        if method == "GetPersistentApplications":
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=(
                    'method return\n'
                    '   array [\n'
                    '      string "file:///tmp/chromium-browser.desktop"\n'
                    '      string "file:///tmp/caja-browser.desktop"\n'
                    "   ]\n"
                ),
                stderr="",
            )
        if method == "GetTransientApplications":
            return subprocess.CompletedProcess(
                cmd, 0, stdout="method return\n   array [\n   ]\n", stderr=""
            )
        if method == "GetHoverPosition":
            uri = cmd[6]
            if "chromium-browser.desktop" in uri:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout="method return\n   int32 859\n   int32 1007\n   int32 3\n   boolean true\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="method return\n   int32 1127\n   int32 1007\n   int32 3\n   boolean true\n",
                stderr="",
            )

    monkeypatch.setattr("deskshot.extraction.atspi_walker.subprocess.run", fake_run)

    items = _query_plank_dbus_items()

    assert items == [
        {"label": "Chromium", "x": 859, "y": 1007},
        {"label": "Files", "x": 1127, "y": 1007},
    ]


def test_augment_plank_elements_prefers_live_dbus_positions(monkeypatch) -> None:
    monkeypatch.setattr(
        "deskshot.extraction.atspi_walker._query_plank_dbus_items",
        lambda: [
            {"label": "Chromium", "x": 859, "y": 1007},
            {"label": "Files", "x": 1127, "y": 1007},
        ],
    )
    app_elements = [
        {
            "tag": "frame",
            "role": "frame",
            "name": None,
            "id": None,
            "classes": None,
            "attrs": {},
            "rect": {"x": 0, "y": 923, "w": 1920, "h": 157},
            "z": 0,
            "position": "absolute",
            "inner_text": "",
            "parent_index": None,
            "children_indices": [],
            "_dom_index": 0,
            "_parent_dom_index": None,
            "_children_dom_indices": [],
            "_depth": 0,
            "_atspi_path": [],
            "type": "Window",
            "vlm_label": None,
            "frame_index": 0,
            "reading_order_index": None,
            "source": "desktop_chrome",
        }
    ]

    out = _augment_plank_elements(
        "plank",
        app_elements,
        viewport_w=1920,
        viewport_h=1080,
        launched_apps=["firefox"],
    )

    assert out[0]["rect"] == {"x": 826, "y": 1002, "w": 334, "h": 65}
    assert [e["name"] for e in out[1:]] == ["Chromium", "Files"]
    assert out[1]["rect"] == {"x": 832, "y": 1007, "w": 54, "h": 54}
    assert out[2]["rect"] == {"x": 1100, "y": 1007, "w": 54, "h": 54}


def test_dock_slots_plank_never_reports_are_reconstructed(monkeypatch) -> None:
    """Running apps Plank cannot resolve are drawn but reported nowhere.

    `GetTransientApplications` only lists running apps Plank matched to a
    `.desktop` launcher. Measured live on scene 800001: the dock drew ten icons,
    the API named seven, and VS Code, Bluefish and Chromium's own running
    instance were invisible to every D-Bus call - so no amount of querying
    Plank, and no longer hard-coded app list, would have found them.

    Their positions are still recoverable, because Plank lays items out on one
    uniform pitch. The reported hover centres were 498/565/632/699/766/833 and
    then 1101: a run of pitch 67 with a four-pitch jump, i.e. three drawn icons
    at 900/967/1034.
    """
    monkeypatch.setattr(
        "deskshot.extraction.atspi_walker._plank_dock_is_centered", lambda: True
    )
    reported = [
        {"label": label, "x": x, "y": 827}
        for label, x in [
            ("Thunderbird", 498), ("Chromium", 565), ("Calculator", 632),
            ("Mousepad", 699), ("Xarchiver", 766), ("Caja", 833), ("Files", 1101),
        ]
    ]

    completed = _complete_plank_dock_slots(reported, icon_size=54, viewport_w=1600)

    assert [item["x"] for item in completed] == [
        498, 565, 632, 699, 766, 833, 900, 967, 1034, 1101
    ]
    assert [item["label"] for item in completed[6:9]] == ["", "", ""]
    assert all(item["y"] == 827 for item in completed)


def test_trailing_dock_slots_are_recovered_from_centre_alignment(monkeypatch) -> None:
    """Unreported items after the last reported one leave no interior gap.

    Interpolation alone only sees holes between reported icons. When the
    unresolvable apps happen to sit at the end of the row - which depends only
    on the order their windows appeared - the row simply looks short. A centred
    dock gives the missing constraint: its first and last slot centres straddle
    the screen centre, so a run starting at 498 on a 1600px screen with pitch 67
    must contain ten slots, not six.
    """
    monkeypatch.setattr(
        "deskshot.extraction.atspi_walker._plank_dock_is_centered", lambda: True
    )
    reported = [
        {"label": label, "x": x, "y": 827}
        for label, x in [
            ("Thunderbird", 498), ("Chromium", 565), ("Calculator", 632),
            ("Mousepad", 699), ("Xarchiver", 766), ("Caja", 833),
        ]
    ]

    completed = _complete_plank_dock_slots(reported, icon_size=54, viewport_w=1600)

    assert [item["x"] for item in completed] == [
        498, 565, 632, 699, 766, 833, 900, 967, 1034, 1101
    ]


def test_centre_extrapolation_is_skipped_for_a_dock_plank_says_is_not_centred(monkeypatch) -> None:
    """Screen symmetry is only evidence for a dock that is actually centred.

    A left-aligned dock's positions can pass the integer-span test by accident
    and would then be padded with a row of invented icons. Ask Plank instead of
    assuming; interior interpolation, which needs no such assumption, still runs.
    """
    monkeypatch.setattr(
        "deskshot.extraction.atspi_walker._plank_dock_is_centered", lambda: False
    )
    reported = [
        {"label": "A", "x": 33, "y": 827},
        {"label": "B", "x": 100, "y": 827},
        {"label": "C", "x": 301, "y": 827},
    ]

    completed = _complete_plank_dock_slots(reported, icon_size=54, viewport_w=1600)

    assert [item["x"] for item in completed] == [33, 100, 167, 234, 301]


def test_non_uniform_dock_positions_are_left_alone(monkeypatch) -> None:
    """A layout we do not recognise must be under-reported, never invented.

    Interpolation is only valid for one uniform row. Gaps that are not whole
    multiples of the pitch mean the model does not hold, and guessing there
    would put boxes on empty dock background.
    """
    monkeypatch.setattr(
        "deskshot.extraction.atspi_walker._plank_dock_is_centered", lambda: True
    )
    reported = [
        {"label": "A", "x": 100, "y": 10},
        {"label": "B", "x": 190, "y": 10},
        {"label": "C", "x": 313, "y": 10},
    ]

    completed = _complete_plank_dock_slots(reported, icon_size=54, viewport_w=1600)

    assert [item["x"] for item in completed] == [100, 190, 313]


def test_dock_labels_come_from_the_launcher_desktop_file(tmp_path) -> None:
    """The launcher names itself; a hard-coded map goes stale.

    `org.gnome.Nautilus.desktop` declares `Name=Files`, but the hard-coded map
    had no entry for it and the fallback title-cased the filename into
    "Org.Gnome.Nautilus". Later `[Desktop Action ...]` groups carry their own
    `Name=` keys ("New Window") and must not be read.
    """
    desktop = tmp_path / "org.gnome.Nautilus.desktop"
    desktop.write_text(
        "[Desktop Entry]\nName=Files\nExec=nautilus\n\n"
        "[Desktop Action new-window]\nName=New Window\n",
        encoding="utf-8",
    )

    assert _plank_item_label(f"file://{desktop}") == "Files"


def test_dock_label_falls_back_when_the_launcher_file_is_gone(tmp_path) -> None:
    """A launcher pointing at a deleted file still needs a usable label."""
    missing = tmp_path / "chromium-browser.desktop"

    assert _plank_item_label(f"file://{missing}") == "Chromium"
