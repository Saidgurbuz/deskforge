"""Edge colouring must say *where* an element was cut, not merely that it was."""

from deskshot.extraction.visualize import EDGE_TOLERANCE, occluded_edges


RECT = {"x": 100, "y": 100, "w": 200, "h": 100}


def test_a_fully_visible_element_has_no_cut_edges() -> None:
    assert occluded_edges(RECT, [dict(RECT)]) == set()


def test_a_box_clipped_on_the_right_reports_only_that_side() -> None:
    """The case the old all-green drawing could not show at all."""
    visible = {"x": 100, "y": 100, "w": 120, "h": 100}

    assert occluded_edges(RECT, [visible]) == {"right"}


def test_each_side_is_detected_independently() -> None:
    assert occluded_edges(RECT, [{"x": 140, "y": 100, "w": 160, "h": 100}]) == {"left"}
    assert occluded_edges(RECT, [{"x": 100, "y": 130, "w": 200, "h": 70}]) == {"top"}
    assert occluded_edges(RECT, [{"x": 100, "y": 100, "w": 200, "h": 60}]) == {"bottom"}


def test_several_sides_can_be_cut_at_once() -> None:
    visible = {"x": 130, "y": 130, "w": 100, "h": 40}

    assert occluded_edges(RECT, [visible]) == {"left", "top", "right", "bottom"}


def test_an_element_with_no_visible_region_is_cut_on_every_side() -> None:
    assert occluded_edges(RECT, []) == {"left", "top", "right", "bottom"}


def test_fragments_are_combined_before_deciding() -> None:
    """Two fragments spanning the box between them leave no side truncated."""
    left = {"x": 100, "y": 100, "w": 90, "h": 100}
    right = {"x": 210, "y": 100, "w": 90, "h": 100}

    assert occluded_edges(RECT, [left, right]) == set()


def test_a_rounding_pixel_does_not_paint_an_edge() -> None:
    """Otherwise almost every box would render as truncated."""
    near = {"x": 100, "y": 100, "w": 200 - EDGE_TOLERANCE, "h": 100}

    assert occluded_edges(RECT, [near]) == set()
    assert occluded_edges(RECT, [{**near, "w": 200 - EDGE_TOLERANCE - 2}]) == {"right"}


def test_render_marks_states_and_counts_them() -> None:
    from PIL import Image
    from deskshot.extraction.visualize import render_occlusion_visualization_v2

    elements = [
        {"rect": RECT, "occlusion_state": "partial",
         "visible_fragments": [{"x": 100, "y": 100, "w": 120, "h": 100}]},
        {"rect": {"x": 10, "y": 10, "w": 50, "h": 50}, "occlusion_state": "none",
         "visible_fragments": [{"x": 10, "y": 10, "w": 50, "h": 50}]},
    ]

    out = render_occlusion_visualization_v2(Image.new("RGB", (400, 300), "white"), elements)

    assert out.size == (400, 300)
    assert out.mode == "RGB"


# --- rectangle subtraction ---------------------------------------------------


from deskshot.extraction.visualize import subtract_rects

BOX = {"x": 0, "y": 0, "w": 100, "h": 100}


def _area(pieces):
    return sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in pieces)


def test_a_fully_covered_box_leaves_nothing() -> None:
    assert subtract_rects(BOX, [dict(BOX)]) == []


def test_an_uncovered_box_is_returned_whole() -> None:
    assert subtract_rects(BOX, []) == [(0, 0, 100, 100)]


def test_a_hole_in_the_middle_leaves_four_bands() -> None:
    """The case a naive implementation gets wrong: the covered region is not a
    rectangle, and drawing it as one would tint visible content."""
    pieces = subtract_rects(BOX, [{"x": 25, "y": 25, "w": 50, "h": 50}])

    assert _area(pieces) == 100 * 100 - 50 * 50
    assert all(x1 > x0 and y1 > y0 for x0, y0, x1, y1 in pieces)


def test_pieces_never_overlap_the_hole() -> None:
    hole = {"x": 40, "y": 10, "w": 30, "h": 80}
    pieces = subtract_rects(BOX, [hole])

    for x0, y0, x1, y1 in pieces:
        assert x1 <= 40 or x0 >= 70 or y1 <= 10 or y0 >= 90


def test_several_holes_are_all_removed() -> None:
    holes = [{"x": 0, "y": 0, "w": 50, "h": 100}, {"x": 50, "y": 0, "w": 50, "h": 50}]

    pieces = subtract_rects(BOX, holes)

    assert _area(pieces) == 50 * 50


def test_a_degenerate_box_yields_nothing() -> None:
    assert subtract_rects({"x": 0, "y": 0, "w": 0, "h": 10}, []) == []


def test_v3_renders_and_keeps_the_image_size() -> None:
    from PIL import Image
    from deskshot.extraction.visualize import render_occlusion_visualization_v3

    elements = [
        {"role": "frame", "rect": {"x": 0, "y": 0, "w": 200, "h": 200},
         "occlusion_state": "partial",
         "visible_fragments": [{"x": 0, "y": 0, "w": 120, "h": 200}]},
        {"role": "push button", "rect": {"x": 100, "y": 50, "w": 60, "h": 20},
         "occlusion_state": "partial",
         "visible_fragments": [{"x": 100, "y": 50, "w": 20, "h": 20}]},
    ]

    out = render_occlusion_visualization_v3(Image.new("RGB", (400, 300), "white"), elements)

    assert out.size == (400, 300) and out.mode == "RGB"
