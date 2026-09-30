"""Boxes outside their own window are dropped; popups are left alone.

The false positive: VS Code publishes `section` nodes with stale geometry that
land on bare wallpaper, sometimes outside the viewport entirely. The trap: 2.64%
of leaves sit outside their window and nearly all of them are real, visible menu
items, so a naive containment rule would delete far more truth than error. These
tests pin both sides of that line.
"""

from __future__ import annotations

from deskshot.extraction.run_extraction import _drop_boxes_outside_their_window


def _window(dom: int, stack: int, rect, app="vscode"):
    return {"_dom_index": dom, "role": "frame", "source": "app", "app_name": app,
            "_window_stack_index": stack, "rect": dict(zip("xywh", rect))}


def _child(dom: int, stack: int, rect, owner: int, role="section", app="vscode"):
    return {"_dom_index": dom, "role": role, "source": "app", "app_name": app,
            "_window_stack_index": stack, "_window_owner_dom_index": owner,
            "rect": dict(zip("xywh", rect)), "type": "Text"}


def test_drops_a_box_entirely_outside_its_own_window() -> None:
    """The measured case: a vscode section 121px below a window that ends at 740."""
    elements = [
        _window(1, 2, (32, 64, 1136, 676)),
        _child(2, 2, (892, 861, 1028, 185), owner=1),
    ]
    meta = _drop_boxes_outside_their_window(elements)
    assert meta["num_dropped"] == 1
    assert [e["_dom_index"] for e in elements] == [1]


def test_keeps_a_popup_menu_that_extends_past_its_parent() -> None:
    """gnome-calculator's menu: outside the frame, but its own window on top."""
    elements = [
        _window(1, 5, (539, 324, 520, 277), app="gnome-calculator"),
        _child(2, 7, (557, 585, 206, 28), owner=1, role="menu item",
               app="gnome-calculator"),
    ]
    meta = _drop_boxes_outside_their_window(elements)
    assert meta["num_dropped"] == 0
    assert len(elements) == 2


def test_keeps_a_partially_clipped_child() -> None:
    """Overlapping the window at all is ordinary clipping, not an escape."""
    elements = [
        _window(1, 2, (100, 100, 400, 400)),
        _child(2, 2, (450, 450, 200, 200), owner=1),
    ]
    meta = _drop_boxes_outside_their_window(elements)
    assert meta["num_dropped"] == 0


def test_keeps_windows_themselves() -> None:
    elements = [
        _window(1, 2, (0, 0, 100, 100)),
        _window(2, 2, (900, 900, 100, 100)),
    ]
    elements[1]["_window_owner_dom_index"] = 1
    assert _drop_boxes_outside_their_window(elements)["num_dropped"] == 0


def test_survives_missing_or_malformed_geometry() -> None:
    """A rare branch that must not raise: absent owner, absent rect, zero size."""
    elements = [
        _window(1, 2, (0, 0, 100, 100)),
        {"_dom_index": 2, "role": "label", "source": "app", "app_name": "vscode",
         "_window_stack_index": 2, "_window_owner_dom_index": 99, "rect": {}},
        {"_dom_index": 3, "role": "label", "source": "app", "app_name": "vscode",
         "_window_stack_index": 2, "_window_owner_dom_index": 1,
         "rect": {"x": 500, "y": 500, "w": 0, "h": 10}},
        {"_dom_index": 4, "role": "label", "source": "app", "app_name": "vscode",
         "_window_owner_dom_index": 1, "rect": {"x": 500, "y": 500, "w": 5, "h": 5}},
    ]
    before = len(elements)
    meta = _drop_boxes_outside_their_window(elements)
    assert meta["num_dropped"] == 0
    assert len(elements) == before


def test_empty_input() -> None:
    assert _drop_boxes_outside_their_window([])["num_dropped"] == 0
