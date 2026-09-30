"""Tests for reading-order assignment."""

from deskshot.extraction.reading_order import (
    PageSize,
    assign_reading_order_indices,
    build_reading_order,
)


def _elem(idx, role, rect, parent=None, text="", source="app", app_name="app"):
    return {
        "_dom_index": idx,
        "_parent_dom_index": parent,
        "parent_index": parent,
        "_children_dom_indices": [],
        "children_indices": [],
        "_source_dom_index": idx,
        "_source_parent_dom_index": parent,
        "role": role,
        "type": role,
        "inner_text": text,
        "source": source,
        "app_name": app_name,
        "rect": dict(rect),
        "reading_order_index": None,
    }


def test_build_reading_order_uses_preorder_with_spatial_sibling_sort() -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 300, "h": 200}, None, text="Window"),
        _elem(1, "push button", {"x": 100, "y": 80, "w": 80, "h": 30}, 0, text="B"),
        _elem(2, "push button", {"x": 20, "y": 80, "w": 80, "h": 30}, 0, text="A"),
        _elem(3, "push button", {"x": 20, "y": 130, "w": 80, "h": 30}, 0, text="C"),
    ]
    elements[0]["_children_dom_indices"] = [1, 2, 3]
    elements[0]["children_indices"] = [1, 2, 3]

    order, backend = build_reading_order(elements, PageSize(800, 600))

    assert backend in {"geometric"}
    assert order == [0, 2, 1, 3]


def test_build_reading_order_falls_back_to_source_parent_for_flat_leaf_exports() -> None:
    elements = [
        {
            **_elem(0, "frame", {"x": 0, "y": 0, "w": 400, "h": 300}, None, text="Calculator"),
            "_dom_index": 0,
            "_parent_dom_index": None,
            "parent_index": None,
            "_source_dom_index": 10,
            "_source_parent_dom_index": None,
        },
        {
            **_elem(1, "push button", {"x": 40, "y": 80, "w": 60, "h": 30}, None, text="7"),
            "_dom_index": 1,
            "_parent_dom_index": None,
            "parent_index": None,
            "_source_dom_index": 11,
            "_source_parent_dom_index": 10,
        },
    ]

    order, _ = build_reading_order(elements, PageSize(800, 600))
    assert order == [0, 1]


def test_assign_reading_order_indices_populates_dense_sequence() -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 400, "h": 300}, None),
        _elem(1, "push button", {"x": 10, "y": 10, "w": 40, "h": 20}, 0, text="A"),
        _elem(2, "push button", {"x": 60, "y": 10, "w": 40, "h": 20}, 0, text="B"),
    ]
    elements[0]["_children_dom_indices"] = [1, 2]
    elements[0]["children_indices"] = [1, 2]

    backend = assign_reading_order_indices(elements, PageSize(800, 600))

    assert backend in {"geometric"}
    assert [e["reading_order_index"] for e in elements] == [0, 1, 2]


# --------------------------------------------------------------------------
# top-level blocks are ordered by where they are
# --------------------------------------------------------------------------

def test_windows_on_one_row_read_left_to_right():
    """The order a model must reproduce has to be derivable from the image.

    Sibling order used to come from a document reading-order predictor, applied
    to overlapping desktop windows - a model built for column-flowing page
    content, whose answer for three side-by-side windows is not something a
    reader could infer from pixels.
    """
    from deskshot.extraction.reading_order import geometric_reading_order

    elements = [
        {"rect": {"x": 900, "y": 100, "w": 200, "h": 200}},
        {"rect": {"x": 100, "y": 110, "w": 200, "h": 200}},
        {"rect": {"x": 500, "y": 105, "w": 200, "h": 200}},
    ]
    assert geometric_reading_order([0, 1, 2], elements) == [1, 2, 0]


def test_a_lower_window_comes_after_the_row_above_it():
    from deskshot.extraction.reading_order import geometric_reading_order

    elements = [
        {"rect": {"x": 50, "y": 900, "w": 200, "h": 200}},
        {"rect": {"x": 800, "y": 100, "w": 200, "h": 200}},
    ]
    assert geometric_reading_order([0, 1], elements) == [1, 0]


def test_a_few_pixels_of_vertical_jitter_does_not_flip_the_order():
    """Without a row band a 2px placement difference reorders the whole target."""
    from deskshot.extraction.reading_order import geometric_reading_order

    elements = [
        {"rect": {"x": 100, "y": 100, "w": 200, "h": 200}},
        {"rect": {"x": 900, "y": 98, "w": 200, "h": 200}},
    ]
    assert geometric_reading_order([0, 1], elements) == [0, 1]


def test_the_order_is_total_and_stable():
    """Identical rects must not depend on iteration order."""
    from deskshot.extraction.reading_order import geometric_reading_order

    elements = [{"rect": {"x": 10, "y": 10, "w": 5, "h": 5}} for _ in range(3)]
    assert geometric_reading_order([2, 0, 1], elements) == [0, 1, 2]
