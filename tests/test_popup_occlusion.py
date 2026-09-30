"""An open menu must occlude what is under it, even when it is not accessible.

Measured on a seven-app scene: qalculate-gtk exposes zero menu items, so its own
open menu was never recognised as a popup, stayed low in the stack, and five of
its buttons underneath were annotated as fully visible with no ink behind them.
"""

from deskshot.extraction.occlusion import (
    APP_FRAME_MATCH_IOU,
    MAX_POPUP_SCREEN_SHARE,
    WindowLayer,
    _accessible_frame_rects,
    _is_unmanaged_popup,
    _promote_floating_overlay_windows,
)

SCREEN = 1600 * 900


def _win(wid, x, y, w, h, name="", index=0):
    return WindowLayer(window_id=wid, name=name,
                       rect={"x": x, "y": y, "w": w, "h": h}, stack_index=index)


def _frame(x, y, w, h, app="qalculate-gtk"):
    return {"role": "frame", "app_name": app, "rect": {"x": x, "y": y, "w": w, "h": h}}


def test_a_window_with_no_accessible_frame_is_a_popup() -> None:
    frames = _accessible_frame_rects([_frame(31, 93, 709, 756)])

    popup = _win("p", 66, 118, 355, 385, name="Mousepad")

    assert _is_unmanaged_popup(popup, frames, SCREEN)


def test_an_app_window_is_not_a_popup() -> None:
    frames = _accessible_frame_rects([_frame(31, 93, 709, 756)])

    assert not _is_unmanaged_popup(_win("a", 31, 93, 709, 756), frames, SCREEN)


def test_a_window_manager_decoration_is_matched_to_its_client() -> None:
    """The frame and the client are two X windows for one app; the decoration
    offset must not make the frame look like a popup."""
    frames = _accessible_frame_rects([_frame(31, 93, 709, 756)])

    decoration = _win("d", 26, 64, 719, 790)

    assert not _is_unmanaged_popup(decoration, frames, SCREEN)


def test_a_full_screen_window_is_never_a_popup() -> None:
    """Root and desktop windows match no frame either, and must not be promoted
    above every app on the screen."""
    frames = _accessible_frame_rects([_frame(31, 93, 709, 756)])

    root = _win("r", 0, 0, 1600, 900)

    assert not _is_unmanaged_popup(root, frames, SCREEN)
    assert MAX_POPUP_SCREEN_SHARE < 1.0


def test_system_chrome_is_left_alone() -> None:
    frames = _accessible_frame_rects([])

    assert not _is_unmanaged_popup(_win("p", 0, 0, 1600, 32, name="Top Panel"), frames, SCREEN)


def test_an_inaccessible_popup_is_promoted_above_the_window_it_covers() -> None:
    """The end-to-end behaviour: the popup is listed below its own app window by
    xdotool, and must end up above it."""
    app = _win("app", 31, 93, 709, 756, name="Qalculate!", index=1)
    popup = _win("popup", 66, 300, 355, 385, name="Qalculate!", index=0)
    elements = [_frame(31, 93, 709, 756)]

    stack, promoted = _promote_floating_overlay_windows([popup, app], elements)

    assert promoted == ["popup"]
    order = [w.window_id for w in stack]
    assert order.index("popup") > order.index("app")


def test_a_popup_that_publishes_its_own_accessible_window_is_still_promoted() -> None:
    """GTK gives an override-redirect popup the role `window`, and an ordinary
    toplevel the role `frame`. Counting `window` as a managed frame made every
    such menu match *itself* at IOU 1.00, so the guard that exists to protect
    real app windows switched promotion off for the popups it was meant to keep.

    Taken from a nautilus context menu at (559,367,387,261): all eleven of its
    elements - six menu items, three separators, the menu and the window - were
    dropped as hidden behind the file-manager window they were drawn over.
    """
    app = _win("app", 110, 70, 872, 438, name="Home", index=5)
    popup = _win("popup", 559, 367, 387, 261, name="org.gnome.Nautilus", index=2)
    elements = [
        _frame(110, 70, 872, 438, app="nautilus"),
        {
            "role": "window", "app_name": "nautilus",
            "rect": {"x": 559, "y": 367, "w": 387, "h": 261},
        },
    ]

    stack, promoted = _promote_floating_overlay_windows([popup, app], elements)

    assert promoted == ["popup"]
    order = [w.window_id for w in stack]
    assert order.index("popup") > order.index("app")


def test_an_accessible_window_role_is_not_evidence_of_a_managed_window() -> None:
    frames = _accessible_frame_rects([
        {"role": "window", "rect": {"x": 559, "y": 367, "w": 387, "h": 261}},
    ])

    assert frames == []


def test_a_popup_that_covers_nothing_larger_is_not_promoted() -> None:
    """Promotion exists to fix stacking against a window it overlaps; with no
    such window there is nothing to fix."""
    popup = _win("popup", 1200, 700, 200, 150, name="Qalculate!", index=0)
    app = _win("app", 31, 93, 300, 300, name="Qalculate!", index=1)

    _stack, promoted = _promote_floating_overlay_windows([popup, app], [_frame(31, 93, 300, 300)])

    assert promoted == []


def test_the_iou_threshold_tolerates_decoration_but_not_a_popup() -> None:
    assert 0.0 < APP_FRAME_MATCH_IOU < 1.0
