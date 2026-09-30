"""A partially covered document must serialize the part you can see.

A bluefish editor showing 30 lines of HTML, with a window over its right half,
serialized as an empty <text_input>: every character was withheld. The pipeline
had the text and exact per-character geometry - queried live, bluefish returns
precise 8x17 monospace cells - and still emitted nothing, so the coverage rule
("every visible element is annotated") was violated by the element that had the
most to say.

Two independent causes, both about applying a single-line instrument to a
document:

1. The offset scorer vetoed every candidate whose text box left the widget. A
   cell renderer clips text to its cell, so overflow means a bad correction; a
   text view reports true extents for characters scrolled out of sight, so the
   veto fired on the identity too and the confidence collapsed to 0.0.
2. Ink was projected onto one x axis across all 30 rows, which makes the score
   meaningless. Judged per line it is meaningful again.
"""

import numpy as np
import pytest

from deskshot.extraction.text_visibility import (
    LineRange,
    TextGeometry,
    estimate_char_extent_offset,
    validate_char_extents_by_line,
)

CHAR_W, CHAR_H = 8, 17


def _document(lines, origin=(100, 100)):
    """Geometry for a monospace document laid out from `origin`."""
    x0, y0 = origin
    text = "\n".join(lines)
    ranges, extents = [], {}
    offset = 0
    for row, line in enumerate(lines):
        y = y0 + row * CHAR_H
        start = offset
        for col, _ in enumerate(line):
            extents[offset] = {"x": x0 + col * CHAR_W, "y": y, "w": CHAR_W, "h": CHAR_H}
            offset += 1
        ranges.append(LineRange(start=start, end=offset, text=line,
                                rect={"x": x0, "y": y, "w": len(line) * CHAR_W, "h": CHAR_H}))
        offset += 1  # the newline
    return TextGeometry(text=text, lines=ranges, char_extents=extents)


def _canvas_for(geometry, *, ink_upto=None, size=(600, 800)):
    """White canvas with dark ink painted under each non-space glyph box."""
    gray = np.full(size, 255, dtype=np.int32)
    for offset, box in geometry.char_extents.items():
        if geometry.text[offset].isspace():
            continue
        if ink_upto is not None and box["x"] >= ink_upto:
            continue
        gray[box["y"] + 3:box["y"] + CHAR_H - 3, box["x"] + 1:box["x"] + CHAR_W - 1] = 0
    return gray


LINES = [
    "<!DOCTYPE html>",
    '<html lang="en">',
    "  <head>",
    '    <meta charset="utf-8">',
    "    <title>DeskShot</title>",
    "  </head>",
    "  <body>",
    "    <header>",
    "      <h1>DeskShot Notes</h1>",
    "    </header>",
]


def test_document_geometry_is_confirmed_when_boxes_match_the_ink():
    geometry = _document(LINES)
    gray = _canvas_for(geometry)
    visible = [{"x": 100, "y": 100, "w": 400, "h": len(LINES) * CHAR_H}]
    conf = validate_char_extents_by_line(
        gray, geometry, cell_rect=visible[0], visible_regions=visible
    )
    assert conf > 0.9


def test_indented_lines_do_not_look_like_an_offset():
    """The regression that made this a validator instead of an estimator.

    Anchoring the first glyph box to the first inked column reads a line's own
    indent as a positional error. Every line here is indented; none of them is
    misplaced, and the score must say so.
    """
    indented = ["        <a href='/docs'>Docs</a>"] * 6
    geometry = _document(indented)
    gray = _canvas_for(geometry)
    visible = [{"x": 100, "y": 100, "w": 400, "h": len(indented) * CHAR_H}]
    assert validate_char_extents_by_line(
        gray, geometry, cell_rect=visible[0], visible_regions=visible
    ) > 0.9


def test_confidence_collapses_when_boxes_sit_where_no_ink_is():
    """The safety property: wrong geometry must still be rejected."""
    geometry = _document(LINES)
    gray = _canvas_for(geometry)
    shifted = TextGeometry(
        text=geometry.text,
        lines=geometry.lines,
        char_extents={o: {**r, "x": r["x"] + 220} for o, r in geometry.char_extents.items()},
    )
    visible = [{"x": 100, "y": 100, "w": 500, "h": len(LINES) * CHAR_H}]
    assert validate_char_extents_by_line(
        gray, shifted, cell_rect=visible[0], visible_regions=visible
    ) < 0.5


def test_lines_scrolled_out_of_view_are_not_counted_against_the_document():
    geometry = _document(LINES)
    gray = _canvas_for(geometry)
    # Only the first three lines are on screen.
    visible = [{"x": 100, "y": 100, "w": 400, "h": 3 * CHAR_H}]
    assert validate_char_extents_by_line(
        gray, geometry, cell_rect=visible[0], visible_regions=visible
    ) > 0.9


def test_no_visible_region_scores_zero():
    geometry = _document(LINES)
    gray = _canvas_for(geometry)
    assert validate_char_extents_by_line(
        gray, geometry, cell_rect={"x": 0, "y": 0, "w": 10, "h": 10}, visible_regions=[]
    ) == 0.0


def test_single_line_estimator_still_allows_the_identity_when_text_overflows():
    """Cause 1, in isolation.

    A document's text block legitimately extends past the widget. dx=0 is not a
    shift and must never be vetoed for leaving the cell, or the confidence
    collapses to 0.0 and the caller withholds the whole document.
    """
    geometry = _document(["a line that is much wider than the widget shows"])
    gray = _canvas_for(geometry)
    rects = [geometry.char_extents[o] for o in sorted(geometry.char_extents)]
    narrow_cell = {"x": 100, "y": 100, "w": 120, "h": CHAR_H}
    visible = [narrow_cell]
    dx, conf = estimate_char_extent_offset(
        gray, rects, cell_rect=narrow_cell, visible_regions=visible
    )
    assert dx == 0
    assert conf > 0.5
