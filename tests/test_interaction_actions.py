"""Tests for interaction action parsing helpers."""

from deskshot.automation.interaction import (
    _choose_heuristic_click_target,
    _parse_heuristic_mode,
    _parse_wh,
    _parse_window_move_value,
    _parse_xy,
    _randomize_layout,
)


def test_parse_window_move_pipe_format() -> None:
    name, x, y = _parse_window_move_value("Mozilla Firefox|280|40")
    assert name == "Mozilla Firefox"
    assert x == 280
    assert y == 40


def test_parse_window_move_comma_format() -> None:
    name, x, y = _parse_window_move_value("Calculator,1500,620")
    assert name == "Calculator"
    assert x == 1500
    assert y == 620


def test_parse_xy_both_formats() -> None:
    assert _parse_xy("280|40") == (280, 40)
    assert _parse_xy("1500,620") == (1500, 620)


def test_parse_wh_both_formats() -> None:
    assert _parse_wh("940|900") == (940, 900)
    assert _parse_wh("1024,768") == (1024, 768)


def test_parse_heuristic_mode() -> None:
    assert _parse_heuristic_mode("button_random|17") == ("button_random", 17)
    assert _parse_heuristic_mode("") == ("button", None)


def test_randomize_layout_moves_only_real_windows(monkeypatch) -> None:
    moved = []
    monkeypatch.setattr(
        "deskshot.automation.interaction.xdotool.get_display_geometry",
        lambda display=None: (1920, 1080),
    )
    monkeypatch.setattr(
        "deskshot.automation.interaction.xdotool.list_visible_windows",
        lambda display=None: [
            {"id": "1", "name": "Desktop", "rect": {"x": 0, "y": 0, "w": 1920, "h": 1080}},
            {"id": "2", "name": "Top Panel", "rect": {"x": 0, "y": 0, "w": 1920, "h": 25}},
            {"id": "3", "name": "Calculator", "rect": {"x": 10, "y": 10, "w": 360, "h": 400}},
            {"id": "4", "name": "Chromium", "rect": {"x": 100, "y": 100, "w": 800, "h": 600}},
        ],
    )
    monkeypatch.setattr(
        "deskshot.automation.interaction.xdotool.window_move",
        lambda wid, x, y, display=None: moved.append((wid, x, y)),
    )

    _randomize_layout("scatter")

    assert [wid for wid, _x, _y in moved] == ["3", "4"]


def test_choose_heuristic_click_target_prefers_visible_button(monkeypatch) -> None:
    elements = [
        {
            "_dom_index": 0,
            "_parent_dom_index": None,
            "_children_dom_indices": [1, 2],
            "role": "frame",
            "rect": {"x": 100, "y": 100, "w": 500, "h": 400},
            "inner_text": "Calc",
        },
        {
            "_dom_index": 1,
            "_parent_dom_index": 0,
            "_children_dom_indices": [],
            "_window_owner_dom_index": 0,
            "role": "push button",
            "rect": {"x": 520, "y": 110, "w": 24, "h": 24},
            "inner_text": "",
        },
        {
            "_dom_index": 2,
            "_parent_dom_index": 0,
            "_children_dom_indices": [],
            "_window_owner_dom_index": 0,
            "role": "push button",
            "rect": {"x": 180, "y": 220, "w": 80, "h": 40},
            "inner_text": "7",
        },
    ]
    monkeypatch.setattr(
        "deskshot.automation.interaction.walk_application",
        lambda app_name: elements,
    )
    monkeypatch.setattr(
        "deskshot.automation.interaction.apply_occlusion_clipping",
        lambda elems, **kwargs: (elems, {}),
    )

    target = _choose_heuristic_click_target("gnome-calculator", "button")

    assert target is not None
    assert target["inner_text"] == "7"
