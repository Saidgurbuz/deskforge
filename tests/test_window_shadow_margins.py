"""A window's rect must be what it draws, not what X allocated it.

A client-side-decorated GTK window is one X window larger than the window a
person sees: the surplus is an invisible margin holding the drop shadow, and
`_GTK_FRAME_EXTENTS` states it exactly. Measured on this stack, nautilus reports
the X window (-45,-3,980,640) with extents (45,45,29,61), so it draws in
(0,26,890,550) - 39% smaller by area.

Left uncorrected the margin is wrong in both directions at once. It occludes:
anything behind those 45 transparent pixels is clipped away as hidden. And it
inflates: the window's annotation covers pixels it never painted, so its own
uncovered-ink share is measured against a box a third too big.
"""

from deskshot.extraction import occlusion
from deskshot.extraction.occlusion import (
    WindowLayer,
    apply_occlusion_clipping,
    shrink_window_elements_to_content,
    strip_shadow_margins,
)

#: (left, right, top, bottom), as read from a live nautilus window.
NAUTILUS_EXTENTS = (45, 45, 29, 61)


def _win(wid, x, y, w, h, name="", index=0):
    return WindowLayer(
        window_id=wid, name=name,
        rect={"x": x, "y": y, "w": w, "h": h}, stack_index=index,
    )


def _extents_for(mapping):
    def fake(window_id, display=None):
        return mapping.get(str(window_id))
    return fake


def test_a_csd_window_reports_the_rect_it_draws_in(monkeypatch) -> None:
    monkeypatch.setattr(
        occlusion, "_gtk_frame_extents", _extents_for({"nautilus": NAUTILUS_EXTENTS})
    )

    stack, corrections = strip_shadow_margins([_win("nautilus", -45, -3, 980, 640)])

    assert stack[0].rect == {"x": 0, "y": 26, "w": 890, "h": 550}
    assert corrections == [
        ({"x": -45, "y": -3, "w": 980, "h": 640}, {"x": 0, "y": 26, "w": 890, "h": 550}),
    ]


def test_a_window_without_shadow_margins_is_left_alone(monkeypatch) -> None:
    """Server-side-decorated windows publish no `_GTK_FRAME_EXTENTS`, and their
    xdotool rect is already the client area."""
    monkeypatch.setattr(occlusion, "_gtk_frame_extents", _extents_for({}))

    stack, corrections = strip_shadow_margins([_win("pluma", 635, 281, 650, 500)])

    assert stack[0].rect == {"x": 635, "y": 281, "w": 650, "h": 500}
    assert corrections == []


def test_the_accessible_window_rect_is_shrunk_to_match(monkeypatch) -> None:
    """GTK gives the toplevel accessible the same padded extents as the X
    window, so the annotation for the window covers the shadow too."""
    monkeypatch.setattr(
        occlusion, "_gtk_frame_extents", _extents_for({"nautilus": NAUTILUS_EXTENTS})
    )
    _stack, corrections = strip_shadow_margins([_win("nautilus", -45, -3, 980, 640)])

    frame = {
        "role": "frame", "app_name": "nautilus", "name": "Home",
        "rect": {"x": -45, "y": -3, "w": 980, "h": 640},
    }
    child = {
        "role": "push button", "app_name": "nautilus", "name": "Back",
        "rect": {"x": 40, "y": 60, "w": 30, "h": 30},
    }

    meta = shrink_window_elements_to_content([frame, child], corrections)

    assert frame["rect"] == {"x": 0, "y": 26, "w": 890, "h": 550}
    assert child["rect"] == {"x": 40, "y": 60, "w": 30, "h": 30}
    assert meta == {"num_windows": 1, "by_app": {"nautilus": 1}}


def test_an_unrelated_window_of_the_same_size_is_not_shrunk(monkeypatch) -> None:
    monkeypatch.setattr(
        occlusion, "_gtk_frame_extents", _extents_for({"nautilus": NAUTILUS_EXTENTS})
    )
    _stack, corrections = strip_shadow_margins([_win("nautilus", -45, -3, 980, 640)])

    elsewhere = {
        "role": "frame", "app_name": "mousepad",
        "rect": {"x": 500, "y": 300, "w": 980, "h": 640},
    }

    meta = shrink_window_elements_to_content([elsewhere], corrections)

    assert elsewhere["rect"] == {"x": 500, "y": 300, "w": 980, "h": 640}
    assert meta["num_windows"] == 0


def _elem(dom, role, rect, parent, text, app):
    return {
        "role": role, "tag": role, "name": text, "inner_text": text,
        "rect": dict(rect), "app_name": app,
        "_dom_index": dom, "_parent_dom_index": parent,
        "_children_dom_indices": [], "children_indices": [], "_depth": 0 if parent is None else 1,
    }


def test_nothing_is_occluded_by_a_window_s_invisible_shadow(monkeypatch) -> None:
    """The behaviour the correction exists for.

    A button of the lower window sits in the 45px transparent margin of the
    window above it. Nothing is drawn over it, so it must survive.
    """
    monkeypatch.setattr(
        occlusion, "_gtk_frame_extents", _extents_for({"nautilus": NAUTILUS_EXTENTS})
    )

    top_padded = {"x": 400, "y": 100, "w": 980, "h": 640}
    top_frame = _elem(0, "frame", top_padded, None, "Home", "nautilus")
    lower_frame = _elem(1, "frame", {"x": 100, "y": 100, "w": 400, "h": 600}, None, "Notes", "mousepad")
    # x 450..470 falls inside the padded rect but outside the drawn window,
    # whose left edge is 400 + 45 = 445 ... and whose top edge is 129.
    in_the_margin = _elem(
        2, "push button", {"x": 450, "y": 104, "w": 20, "h": 20}, 1, "Save", "mousepad"
    )
    lower_frame["_children_dom_indices"] = [2]
    lower_frame["children_indices"] = [2]
    elements = [top_frame, lower_frame, in_the_margin]

    raw_stack = [
        _win("mousepad", 100, 100, 400, 600, name="Notes", index=0),
        _win("nautilus", 400, 100, 980, 640, name="Home", index=1),
    ]

    stack, corrections = strip_shadow_margins(raw_stack)
    shrink_window_elements_to_content(elements, corrections)
    out, _meta = apply_occlusion_clipping(elements, window_stack=stack, allow_partial_clip=True)

    kept = {e.get("name") for e in out}
    assert "Save" in kept
    assert top_frame["rect"] == {"x": 445, "y": 129, "w": 890, "h": 550}
