"""`visible_text` is the glyphs an element draws, and nothing else.

Two failures with the same root: the pipeline treated "the toolkit told us a
string" and "the screen shows that string" as the same fact.

1. Accessible names were copied into `visible_text` unconditionally. A 20x20
   VS Code check box serialized "Match Case (Alt+C)", a 199x24 search field
   serialized "Search: Type Search Term and press Enter to search" while the
   screen draws "Search", and a 48x48 dock tile serialized "Calculator\\nPerform
   arithmetic...". Measured on `incremental_checks/v224_verify/batch`, 1513
   elements carried a name as rendered text and 329 of them were in a box that
   could not physically show it.

2. Text was withheld whenever an element was partly occluded and its character
   geometry scored below the confidence bar - even when the occluder was
   nowhere near the text. A chromium heading holding "Latest News" in the first
   100px of a 397px box, with 12px clipped off its right edge, serialized
   nothing at confidence 0.659.

Both are about provenance, so they are tested together.
"""

import numpy as np

from deskshot.extraction.text_visibility import (
    LineRange,
    TextGeometry,
    name_can_be_rendered,
    occlusion_reaches_text,
    populate_visible_text,
)


def _name_element(text, rect, *, occluded=False, fragments=None):
    return {
        "inner_text": text,
        "name": text,
        "is_occluded": occluded,
        "rect": dict(rect),
        "visible_fragments": [dict(f) for f in (fragments or [rect])],
        "attrs": {"text_source": "name"},
    }


def _single_line_geometry(text, *, x, y=580, cw=9, h=16):
    extents = {
        i: {"x": x + i * cw, "y": y, "w": cw, "h": h} for i, _ in enumerate(text)
    }
    line = LineRange(start=0, end=len(text), text=text,
                     rect={"x": x, "y": y, "w": len(text) * cw, "h": h})
    return TextGeometry(text=text, lines=[line], char_extents=extents)


def _heading_canvas(geometry, box, *, other_ink=(200, 280)):
    """A heading box holding its glyphs and something else on the same line.

    The something else is why the confidence collapses - in the reported case a
    "More" link sharing the heading's band - and it is ink no glyph box can
    explain, so the scorer reports the geometry as unconfirmed while every
    character is exactly where it says it is. That is the state this is about:
    unconfirmed geometry, not wrong geometry.
    """
    gray = np.full((720, 640), 255, dtype=np.int32)
    band_y = min(b["y"] for b in geometry.char_extents.values())
    band_h = max(b["h"] for b in geometry.char_extents.values())
    gray[band_y + 3:band_y + band_h - 3, other_ink[0]:other_ink[1]] = 0
    for offset, ch in enumerate(geometry.text):
        if ch.isspace():
            continue
        b = geometry.char_extents[offset]
        gray[b["y"] + 3:b["y"] + b["h"] - 3, b["x"] + 1:b["x"] + b["w"] - 1] = 0
    return gray


# --- 1. an accessible name is not evidence of glyphs -------------------------

def test_an_icon_buttons_tooltip_name_is_not_reported_as_rendered_text() -> None:
    """VS Code's Match Case control is a 20x20 icon whose accessible name is
    18 characters. No rendering of that string fits, so it is not what is on
    screen."""
    elements = [_name_element("Match Case (Alt+C)", {"x": 317, "y": 169, "w": 20, "h": 20})]

    meta = populate_visible_text(elements)

    assert elements[0]["visible_text"] == ""
    assert elements[0]["visible_text_status"] == "name_only"
    assert meta["num_name_only"] == 1
    # The name itself is not lost - only the claim that it is drawn.
    assert elements[0]["name"] == "Match Case (Alt+C)"


def test_a_search_fields_name_is_not_the_placeholder_it_draws() -> None:
    """`incremental_checks/v226_vscode/after3`: a 199x24 entry named "Search:
    Type Search Term and press Enter to search" over a box drawing "Search".
    49 characters cannot be rendered in 199px at any advance this pool uses."""
    elements = [_name_element(
        "Search: Type Search Term and press Enter to search",
        {"x": 117, "y": 167, "w": 199, "h": 24},
    )]

    populate_visible_text(elements)

    assert elements[0]["visible_text"] == ""
    assert elements[0]["visible_text_status"] == "name_only"


def test_a_window_frames_title_is_not_drawn_across_the_whole_window() -> None:
    """A frame's name is its title, drawn in the title bar - which is its own
    annotated element. Reporting it as the frame's rendered text puts a sentence
    over a 1709x1875 box."""
    elements = [_name_element(
        "example.xhb - HomeBank Sample File - HomeBank",
        {"x": 100, "y": 60, "w": 1709, "h": 1875},
    )]

    populate_visible_text(elements)

    assert elements[0]["visible_text_status"] == "name_only"


def test_a_menu_bar_label_that_fits_its_box_is_still_reported() -> None:
    """The test is one-directional on purpose: too small to render is a proof,
    big enough is not. A menu whose label is drawn must keep it."""
    elements = [_name_element("Applications", {"x": 0, "y": 0, "w": 92, "h": 25})]

    populate_visible_text(elements)

    assert elements[0]["visible_text"] == "Applications"
    assert elements[0]["visible_text_status"] == "full_visible"


def test_the_name_size_test_reads_the_longest_line_of_a_multi_line_name() -> None:
    """mate-panel launchers carry "Calculator\\nPerform arithmetic..." on a 48x48
    tile: neither line fits, and two lines do not fit the height either."""
    assert not name_can_be_rendered(
        "Calculator\nPerform arithmetic, scientific or financial calculations",
        {"x": 0, "y": 0, "w": 48, "h": 48},
    )
    assert name_can_be_rendered("Contents", {"x": 0, "y": 0, "w": 96, "h": 19})


# --- 2. occlusion that never reaches the text is not a reason to withhold ----

def test_an_occluder_clear_of_the_glyphs_does_not_withhold_the_text() -> None:
    """The reported case: `v228_provenance/run2`, heading "Latest News" at
    rect 77,575 397x32 clipped to 385 wide, confidence 0.659, `visible_text`
    empty. Twelve pixels of padding were removed from the right of a box whose
    text ends 270px earlier."""
    geometry = _single_line_geometry("Latest News", x=95)
    box = {"x": 77, "y": 575, "w": 397, "h": 32}
    gray = _heading_canvas(geometry, box)
    elements = [{
        "inner_text": "Latest News",
        "is_occluded": True,
        "rect": {"x": 77, "y": 575, "w": 385, "h": 32},
        "_occlusion_original_rect": dict(box),
        "visible_fragments": [{"x": 77, "y": 575, "w": 385, "h": 32}],
        "attrs": {"text_source": "text_iface"},
        "_atspi_app_name": "chromium-browser",
        "_atspi_path": [1, 4],
    }]

    meta = populate_visible_text(
        elements, gray=gray,
        geometry_cache={("chromium-browser", (1, 4)): geometry},
    )

    assert elements[0]["text_geometry_confidence"] < 0.90
    assert elements[0]["visible_text"] == "Latest News"
    assert elements[0]["visible_text_status"] == "full_visible"
    assert elements[0]["text_geometry_unvalidated"] is True
    assert meta["num_occluder_misses_text"] == 1
    assert meta["num_unsupported"] == 0


def test_an_occluder_over_the_glyphs_still_withholds_them() -> None:
    """The safety property. Same box, same unconfirmed geometry, but now the
    text runs into the cut - the character-level question is real again and the
    boxes still cannot answer it."""
    geometry = _single_line_geometry("Latest News", x=380)
    box = {"x": 77, "y": 575, "w": 397, "h": 32}
    gray = _heading_canvas(geometry, box)
    elements = [{
        "inner_text": "Latest News",
        "is_occluded": True,
        "rect": {"x": 77, "y": 575, "w": 385, "h": 32},
        "_occlusion_original_rect": dict(box),
        "visible_fragments": [{"x": 77, "y": 575, "w": 385, "h": 32}],
        "attrs": {"text_source": "text_iface"},
        "_atspi_app_name": "chromium-browser",
        "_atspi_path": [1, 4],
    }]

    meta = populate_visible_text(
        elements, gray=gray,
        geometry_cache={("chromium-browser", (1, 4)): geometry},
    )

    assert elements[0]["visible_text_status"] == "unsupported_partial"
    assert meta["num_unsupported"] == 1


def test_glyphs_within_the_geometry_error_of_the_cut_count_as_reached() -> None:
    """The boxes are allowed to be wrong. The largest box-vs-glyph displacement
    measured in this repo is 26px, so a glyph that ends 10px before the cut is
    not safely clear of it."""
    geometry = _single_line_geometry("abc", x=430, cw=9)
    fragments = [{"x": 77, "y": 575, "w": 385, "h": 32}]
    source = {"x": 77, "y": 575, "w": 397, "h": 32}

    assert occlusion_reaches_text(geometry, source_rect=source, fragments=fragments)
    assert not occlusion_reaches_text(
        _single_line_geometry("abc", x=95), source_rect=source, fragments=fragments
    )


def test_a_line_clipped_to_a_sliver_is_reached_by_the_occluder() -> None:
    """A HomeBank row cut to 5px of its 17px line still carried its full text.

    The coverage mask is built over `source_rect`, which for a viewport-clipped
    cell is already the clipped rect - so the glyph boxes clamp into the
    surviving rows, every one reads as covered, and the occluder is judged to
    have missed the text. 28 cells serialized "02/27/2023" and the like over a
    5px box. Height settles it: below a fraction of a line, nothing is legible.
    """
    from deskshot.extraction.text_visibility import (
        LineRange,
        TextGeometry,
        occlusion_reaches_text,
    )

    text = "02/27/2023"
    extents = {i: {"x": 100 + i * 8, "y": 200, "w": 8, "h": 17}
               for i in range(len(text))}
    geometry = TextGeometry(
        text=text,
        lines=[LineRange(start=0, end=len(text), text=text,
                         rect={"x": 100, "y": 200, "w": 8 * len(text), "h": 17})],
        char_extents=extents,
    )
    sliver = {"x": 100, "y": 200, "w": 8 * len(text), "h": 5}
    assert occlusion_reaches_text(
        geometry, source_rect=sliver, fragments=[dict(sliver)]
    ) is True

    # The same row with its full height visible is untouched by the guard.
    full = {"x": 100, "y": 200, "w": 8 * len(text), "h": 17}
    assert occlusion_reaches_text(
        geometry, source_rect=full, fragments=[dict(full)]
    ) is False


def test_text_is_not_emitted_over_a_box_with_no_ink() -> None:
    """The guard against displaced boxes.

    Relaxing occlusion recovered a lot of real text, but also let through
    Chromium page tokens whose boxes land in the gaps *between* glyphs - 22
    one- and two-character `static` elements sitting on blank pixels while the
    text was drawn a few pixels away. Withheld text is a gap; text on the wrong
    box is a wrong answer.
    """
    import numpy as np

    from deskshot.extraction.text_visibility import box_has_ink_for_text

    blank = np.full((60, 60), 255, dtype=np.int32)
    box = [{"x": 10, "y": 10, "w": 12, "h": 20}]
    assert box_has_ink_for_text(blank, box, "=") is False

    drawn = blank.copy()
    drawn[14:24, 12:20] = 0
    assert box_has_ink_for_text(drawn, box, "=") is True


def test_thin_punctuation_is_not_mistaken_for_an_empty_box() -> None:
    """A calculator's "." was once called a phantom by a stricter rule; the
    expectation is per character and deliberately generous."""
    import numpy as np

    from deskshot.extraction.text_visibility import box_has_ink_for_text

    gray = np.full((60, 60), 255, dtype=np.int32)
    gray[26:30, 14:18] = 0  # a dot: 16 dark pixels
    assert box_has_ink_for_text(gray, [{"x": 10, "y": 10, "w": 12, "h": 24}], ".") is True


def test_whitespace_only_text_is_not_judged() -> None:
    import numpy as np

    from deskshot.extraction.text_visibility import box_has_ink_for_text

    blank = np.full((60, 60), 255, dtype=np.int32)
    assert box_has_ink_for_text(blank, [{"x": 10, "y": 10, "w": 12, "h": 20}], "   ") is True
