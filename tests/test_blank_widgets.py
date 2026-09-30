"""A widget that is reported but not drawn must go; a drawn one must stay.

The dropping direction is easy to get right and easy to over-apply. These tests
pin the two cases that made the rule what it is, both taken from real captures:

- A Plank dock icon, dark art on a dark dock: 24 grey levels, std 8.15, and no
  adjacent-pixel step large enough for a gradient detector to call it ink. An
  edge-based rule deletes it. It must survive.
- A solid colour swatch: perfectly uniform, and visible precisely because it
  differs from everything around it. Uniformity alone deletes it. It must
  survive too.

And the case the rule exists for: a GTK overlay scrollbar, uniform and the same
colour as the pane behind it.
"""

import numpy as np
import pytest

from deskshot.extraction.blank_widgets import (
    MUST_DRAW_ROLES,
    is_blank_widget,
    suppress_blank_widgets,
)


def _canvas(value=255, size=(400, 400)):
    return np.full(size, value, dtype=np.int32)


def _elem(role="scroll bar", rect=(100, 100, 16, 120), **kw):
    x, y, w, h = rect
    elem = {
        "role": role,
        "rect": {"x": x, "y": y, "w": w, "h": h},
        "_dom_index": kw.pop("dom_index", 1),
        "children_indices": kw.pop("children_indices", []),
    }
    elem.update(kw)
    return elem


# --------------------------------------------------------------------------
# must be dropped
# --------------------------------------------------------------------------

def test_overlay_scrollbar_on_uniform_pane_is_blank():
    """The case this exists for: nothing painted, same colour as the pane."""
    gray = _canvas(255)
    assert is_blank_widget(_elem("scroll bar", (100, 100, 16, 120)), gray) is True


def test_blank_widget_on_a_dark_theme_is_blank_too():
    """Polarity must not matter - light-on-dark themes are half the pool."""
    gray = _canvas(36)
    assert is_blank_widget(_elem("push button", (100, 100, 36, 28)), gray) is True


def test_occluded_widget_whose_visible_sliver_is_blank():
    """Visible fragments, not the full rect, decide it."""
    gray = _canvas(255)
    # Something dark covers most of the button; the exposed sliver is blank.
    gray[100:128, 120:200] = 30
    elem = _elem(
        "push button", (100, 100, 100, 28),
        is_occluded=True,
        visible_fragments=[{"x": 100, "y": 100, "w": 20, "h": 28}],
    )
    assert is_blank_widget(elem, gray) is True


# --------------------------------------------------------------------------
# must NOT be dropped
# --------------------------------------------------------------------------

def test_low_contrast_dock_icon_survives():
    """Plank icon: real art, dark on dark, no strong edges. Measured std 8.15."""
    rng = np.random.default_rng(0)
    gray = _canvas(40)
    patch = 40 + rng.integers(-12, 12, size=(54, 54))
    gray[100:154, 100:154] = patch
    elem = _elem("push button", (100, 100, 54, 54))
    assert is_blank_widget(elem, gray) is False


def test_uniform_colour_swatch_against_a_contrasting_panel_survives():
    """Uniform but plainly visible - the ring is what tells them apart."""
    gray = _canvas(240)
    gray[100:130, 100:150] = 60  # a solid dark swatch on a light panel
    elem = _elem("push button", (100, 100, 50, 30))
    assert is_blank_widget(elem, gray) is False


def test_widget_with_any_drawn_detail_survives():
    gray = _canvas(255)
    gray[100:128, 100:136] = 255
    gray[110:118, 108:128] = 0  # a glyph
    assert is_blank_widget(_elem("push button", (100, 100, 36, 28)), gray) is False


def test_roles_that_may_legitimately_be_blank_are_never_examined():
    gray = _canvas(255)
    for role in ("panel", "filler", "separator", "text", "frame"):
        assert role not in MUST_DRAW_ROLES
        assert is_blank_widget(_elem(role, (100, 100, 60, 40)), gray) is False


def test_element_with_no_visible_fragments_is_left_to_the_hidden_pass():
    """Already handled by drop_fully_hidden_elements; not this pass's job."""
    gray = _canvas(255)
    elem = _elem("scroll bar", (100, 100, 16, 120), visible_fragments=[])
    assert is_blank_widget(elem, gray) is False


def test_sliver_too_thin_to_measure_is_kept():
    gray = _canvas(255)
    assert is_blank_widget(_elem("push button", (100, 100, 3, 28)), gray) is False


# --------------------------------------------------------------------------
# suppression bookkeeping
# --------------------------------------------------------------------------

def test_suppression_drops_and_reports():
    gray = _canvas(255)
    gray[200:230, 200:250] = 60
    elements = [
        _elem("scroll bar", (100, 100, 16, 120), dom_index=1),
        _elem("push button", (200, 200, 50, 30), dom_index=2),  # visible swatch
        _elem("panel", (300, 300, 40, 20), dom_index=3),        # never examined
    ]
    meta = suppress_blank_widgets(elements, gray)
    assert meta["num_dropped"] == 1
    assert meta["by_role"] == {"scroll bar": 1}
    assert [e["_dom_index"] for e in elements] == [2, 3]


def test_child_bearing_elements_are_kept_even_when_blank():
    """A visible child still needs its parent in the nesting."""
    gray = _canvas(255)
    elements = [
        _elem("combo box", (100, 100, 80, 30), dom_index=1, children_indices=[2]),
        _elem("menu item", (100, 100, 80, 30), dom_index=2),
    ]
    meta = suppress_blank_widgets(elements, gray)
    assert meta["by_role"] == {"menu item": 1}
    assert [e["_dom_index"] for e in elements] == [1]


def test_dangling_child_references_are_pruned():
    gray = _canvas(255)
    elements = [
        {"role": "panel", "rect": {"x": 0, "y": 0, "w": 300, "h": 300},
         "_dom_index": 1, "children_indices": [2, 3], "_children_dom_indices": [2, 3]},
        _elem("scroll bar", (100, 100, 16, 120), dom_index=2),
        _elem("panel", (150, 150, 40, 20), dom_index=3),
    ]
    suppress_blank_widgets(elements, gray)
    assert elements[0]["children_indices"] == [3]
    assert elements[0]["_children_dom_indices"] == [3]


def test_no_screenshot_means_no_suppression():
    elements = [_elem("scroll bar", (100, 100, 16, 120))]
    meta = suppress_blank_widgets(elements, None)
    assert meta["num_dropped"] == 0
    assert len(elements) == 1


@pytest.mark.parametrize("role", sorted(MUST_DRAW_ROLES))
def test_every_must_draw_role_is_droppable_when_blank(role):
    gray = _canvas(255)
    assert is_blank_widget(_elem(role, (100, 100, 40, 40)), gray) is True


# --------------------------------------------------------------------------
# text that cannot fit the box it is attached to
# --------------------------------------------------------------------------

def test_text_overflow_guard():
    """VS Code mirrors its whole editor into a 523x19 entry; the text there is
    not what is drawn in that strip."""
    from deskshot.extraction.text_visibility import text_overflows_box

    ten_lines = "\n".join(f"line {i}" for i in range(10))
    assert text_overflows_box(ten_lines, {"h": 19}) is True
    # A normal multi-line label with room to stack is fine.
    assert text_overflows_box("two\nlines", {"h": 40}) is False
    # Single-line text is never flagged: it may be ellipsized or scrolled.
    assert text_overflows_box("a very long single line " * 20, {"h": 19}) is False
    assert text_overflows_box("", {"h": 19}) is False
    assert text_overflows_box("a\nb", {"h": 0}) is False


# --------------------------------------------------------------------------
# empty grid cells
# --------------------------------------------------------------------------

def test_an_empty_cell_that_draws_nothing_is_blank():
    """23% of one HomeBank capture was text-less cells over background.

    An empty grid cell has no glyphs, no border of its own and no icon, so its
    box sits on the window's background colour. Annotating it teaches a model
    to predict boxes over blank space.
    """
    gray = _canvas(255)
    assert is_blank_widget(_elem("table cell", (100, 100, 60, 21)), gray) is True


def test_a_cell_that_draws_something_is_kept():
    """A grid line or a warning icon makes the cell non-flat, and the same
    measurement keeps it - 46 of 148 empty-text cells in that capture."""
    gray = _canvas(255)
    gray[104:116, 104:116] = 0
    assert is_blank_widget(_elem("table cell", (100, 100, 60, 21)), gray) is False


def test_a_cell_with_text_is_left_to_the_text_checks():
    gray = _canvas(255)
    elem = _elem("table cell", (100, 100, 60, 21), visible_text="500,00")
    assert is_blank_widget(elem, gray) is False


def test_a_cell_with_children_is_not_examined():
    """It is a container for something else, which carries the annotation."""
    gray = _canvas(255)
    elem = _elem("table cell", (100, 100, 60, 21), children_indices=[7])
    assert is_blank_widget(elem, gray) is False


# --------------------------------------------------------------------------
# windows the screen never drew
# --------------------------------------------------------------------------

def test_an_undrawn_window_and_its_contents_are_dropped():
    """A mousepad "Go To" dialog reported 292x160, unoccluded, with a title bar
    and two labels - and every pixel of that region was uniform white. It
    serialized as a window with a title and two texts nobody could see."""
    from deskshot.extraction.blank_widgets import drop_undrawn_windows

    gray = _canvas(255)
    elements = [
        _elem("dialog", (100, 100, 120, 80), dom_index=1),
        _elem("title bar", (100, 100, 120, 20), dom_index=2),
        _elem("label", (110, 130, 60, 15), dom_index=3),
        _elem("push button", (300, 300, 40, 20), dom_index=4),  # elsewhere
    ]
    meta = drop_undrawn_windows(elements, gray)
    assert meta["num_windows_dropped"] == 1
    assert [e["_dom_index"] for e in elements] == [4]


def test_a_window_that_draws_anything_is_kept_with_its_contents():
    """The whole rect must be flat. A real window's border, title bar and
    controls always vary, even when its content area is plain."""
    from deskshot.extraction.blank_widgets import drop_undrawn_windows

    gray = _canvas(255)
    gray[100:104, 100:220] = 0  # a title bar
    elements = [
        _elem("dialog", (100, 100, 120, 80), dom_index=1),
        _elem("label", (110, 130, 60, 15), dom_index=2),
    ]
    meta = drop_undrawn_windows(elements, gray)
    assert meta["num_windows_dropped"] == 0
    assert len(elements) == 2


def test_no_screenshot_means_no_window_is_dropped():
    from deskshot.extraction.blank_widgets import drop_undrawn_windows

    elements = [_elem("dialog", (100, 100, 120, 80))]
    assert drop_undrawn_windows(elements, None)["num_windows_dropped"] == 0
    assert len(elements) == 1
