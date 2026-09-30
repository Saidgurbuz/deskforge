"""A visible element gets its fragment back; a hidden one stays hidden.

Ten leaf elements in 20,206 shipped a valid box with an empty
`visible_fragments` list. Because `check_visible_fragments_present` is
all-or-nothing over a capture, each one rejected an entire otherwise-good
sample - 4.1% of captures. Every one was VS Code's extension-recommendation
notification, and the pixels showed it plainly on screen, so the fragment list
was wrong, not the box. The repair must restore those and must not resurrect
anything that really is behind another window.
"""

from __future__ import annotations

from deskshot.extraction.run_extraction import (
    _repair_fragments_against_the_window_stack,
)


def _rect(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}


def _window(stack, rect, app="vscode", role="frame"):
    return {"role": role, "app_name": app, "_window_stack_index": stack,
            "rect": rect, "visible_fragments": [dict(rect)]}


def test_restores_the_measured_vscode_notification() -> None:
    """The real geometry: homebank ends exactly where the dialog begins."""
    dialog = {"role": "dialog", "type": "Window", "app_name": "vscode",
              "_window_stack_index": 2, "rect": _rect(1028, 377, 308, 106),
              "visible_fragments": []}
    elements = [
        _window(2, _rect(700, 75, 644, 438)),
        dialog,
        _window(3, _rect(0, 26, 1028, 630), app="homebank"),
        _window(5, _rect(39, 591, 576, 509), app="thunar"),
        _window(6, _rect(745, 606, 595, 441), app="gnome-logs"),
    ]
    meta = _repair_fragments_against_the_window_stack(elements)
    assert meta["num_repaired"] == 1
    assert dialog["visible_fragments"] == [_rect(1028, 377, 308, 106)]
    assert dialog["occlusion_state"] == "none"


def test_leaves_a_genuinely_covered_element_hidden() -> None:
    buried = {"role": "dialog", "app_name": "vscode", "_window_stack_index": 2,
              "rect": _rect(100, 100, 200, 100), "visible_fragments": []}
    elements = [buried, _window(4, _rect(0, 0, 1920, 1080), app="mousepad")]
    meta = _repair_fragments_against_the_window_stack(elements)
    assert meta["num_repaired"] == 0
    assert buried["visible_fragments"] == []


def test_partial_cover_is_reported_as_partial() -> None:
    elem = {"role": "dialog", "app_name": "vscode", "_window_stack_index": 2,
            "rect": _rect(100, 100, 400, 100), "visible_fragments": []}
    elements = [elem, _window(4, _rect(300, 0, 1000, 1080), app="mousepad")]
    meta = _repair_fragments_against_the_window_stack(elements)
    assert meta["num_repaired"] == 1
    assert elem["occlusion_state"] == "partial"
    assert elem["visible_fragments"] == [_rect(100, 100, 200, 100)]


def test_elements_that_already_have_fragments_are_untouched() -> None:
    keep = [_rect(5, 5, 10, 10)]
    elem = {"role": "label", "app_name": "vscode", "_window_stack_index": 2,
            "rect": _rect(0, 0, 100, 100), "visible_fragments": keep}
    meta = _repair_fragments_against_the_window_stack([elem])
    assert meta["num_repaired"] == 0
    assert elem["visible_fragments"] is keep


def test_windows_below_in_the_stack_never_occlude() -> None:
    elem = {"role": "dialog", "app_name": "vscode", "_window_stack_index": 9,
            "rect": _rect(100, 100, 200, 100), "visible_fragments": []}
    elements = [elem, _window(1, _rect(0, 0, 1920, 1080), app="homebank")]
    assert _repair_fragments_against_the_window_stack(elements)["num_repaired"] == 1


def test_malformed_input_does_not_raise() -> None:
    elements = [
        {"role": "label", "rect": {}, "visible_fragments": []},
        {"role": "label", "rect": _rect(0, 0, 0, 10), "visible_fragments": []},
        {"role": "label", "rect": _rect(0, 0, 10, 10), "visible_fragments": []},
        {"role": "frame", "_window_stack_index": "x", "rect": _rect(0, 0, 5, 5)},
    ]
    assert _repair_fragments_against_the_window_stack(elements)["num_repaired"] == 0
    assert _repair_fragments_against_the_window_stack([])["num_repaired"] == 0


# --- the cause, not just the symptom -------------------------------------
#
# The fragments were not lost by the occlusion pass at all. `populate_visible_text`
# clears an element's geometry when no glyph of it survives the pixel test, which
# is right for a label and wrong for a window: VS Code gives its notification
# dialogs an `inner_text` of U+FFFC, which has no glyphs to find.

from deskshot.extraction.text_visibility import (  # noqa: E402
    _preserve_widget_geometry_for_visible_text,
)


def test_a_dialog_keeps_its_box_when_its_text_is_unreadable() -> None:
    dialog = {"role": "dialog", "type": "Window", "app_name": "vscode",
              "inner_text": "￼"}
    assert _preserve_widget_geometry_for_visible_text(dialog) is True


def test_every_window_like_role_is_preserved() -> None:
    for role in ("frame", "window", "dialog", "alert", "desktop frame"):
        assert _preserve_widget_geometry_for_visible_text({"role": role}) is True


def test_a_label_still_loses_its_box_when_its_glyphs_are_covered() -> None:
    """The behaviour that must not change: a label's only ink is its text."""
    assert _preserve_widget_geometry_for_visible_text(
        {"role": "label", "type": "Text", "inner_text": "Save"}
    ) is False
    assert _preserve_widget_geometry_for_visible_text(
        {"role": "push button", "type": "Button"}
    ) is False


def test_editable_text_is_still_preserved() -> None:
    assert _preserve_widget_geometry_for_visible_text(
        {"role": "entry", "attrs": {"interfaces": {"editable_text": True}}}
    ) is True
    assert _preserve_widget_geometry_for_visible_text({"type": "Text Input"}) is True


def test_role_casing_and_whitespace_do_not_matter() -> None:
    assert _preserve_widget_geometry_for_visible_text({"role": " Dialog "}) is True
