from deskshot.extraction.text_visibility import (
    LineRange,
    TextGeometry,
    _clip_text_with_geometry,
    populate_visible_text,
)


def _mono_geometry(text: str, *, x: int = 0, y: int = 0, cw: int = 10, lh: int = 20) -> TextGeometry:
    lines = []
    char_extents = {}
    offset = 0
    cur_y = y
    for raw_line in text.splitlines(keepends=True):
        content = raw_line
        visible = raw_line[:-1] if raw_line.endswith("\n") else raw_line
        width = len(visible) * cw
        lines.append(LineRange(start=offset, end=offset + len(content), text=content, rect={"x": x, "y": cur_y, "w": width, "h": lh}))
        cur_x = x
        for i, ch in enumerate(content):
            if ch == "\n":
                char_extents[offset + i] = {"x": cur_x, "y": cur_y, "w": cw, "h": lh}
            else:
                char_extents[offset + i] = {"x": cur_x, "y": cur_y, "w": cw, "h": lh}
                cur_x += cw
        offset += len(content)
        cur_y += lh
    return TextGeometry(text=text, lines=lines, char_extents=char_extents)


def test_clip_text_with_geometry_keeps_visible_suffix_and_full_next_line() -> None:
    geometry = _mono_geometry("Hello world\nSecond line\n")
    fragments = [
        {"x": 60, "y": 0, "w": 60, "h": 20},
        {"x": 0, "y": 20, "w": 200, "h": 20},
    ]

    out = _clip_text_with_geometry(geometry, fragments)

    assert out == "world\nSecond line\n"


def test_clip_text_with_geometry_handles_disjoint_visible_runs() -> None:
    geometry = _mono_geometry("ABCDE\n")
    fragments = [
        {"x": 0, "y": 0, "w": 20, "h": 20},
        {"x": 30, "y": 0, "w": 20, "h": 20},
    ]

    out = _clip_text_with_geometry(geometry, fragments)

    assert out == "ABDE\n"


def test_populate_visible_text_keeps_full_text_when_not_occluded() -> None:
    elements = [
        {
            "inner_text": "Visible text",
            "is_occluded": False,
            "rect": {"x": 0, "y": 0, "w": 100, "h": 20},
            "visible_fragments": [{"x": 0, "y": 0, "w": 100, "h": 20}],
        }
    ]

    meta = populate_visible_text(elements)

    assert meta["num_text_elements"] == 1
    assert elements[0]["visible_text"] == "Visible text"
    assert elements[0]["visible_text_status"] == "full_visible"
    assert elements[0]["visible_text_confidence"] == 1.0


def test_populate_visible_text_uses_conservative_empty_fallback_for_partial_unsupported() -> None:
    elements = [
        {
            "inner_text": "Hidden maybe visible maybe not",
            "is_occluded": True,
            "visible_fragments": [{"x": 0, "y": 0, "w": 40, "h": 20}],
            "attrs": {"text_source": "name"},
            "_atspi_app_name": "mousepad",
            "_atspi_path": [1, 2, 3],
        }
    ]

    meta = populate_visible_text(elements)

    assert meta["num_unsupported"] == 1
    assert elements[0]["visible_text"] is None
    assert elements[0]["visible_text_status"] == "unsupported_partial"
    assert elements[0]["visible_text_confidence"] == 0.0


def test_populate_visible_text_does_not_treat_clipped_rect_as_full_visibility() -> None:
    elements = [
        {
            "inner_text": "Hello world\n",
            "is_occluded": True,
            "rect": {"x": 60, "y": 0, "w": 60, "h": 20},
            "_visibility_source_rect": {"x": 0, "y": 0, "w": 110, "h": 20},
            "visible_fragments": [{"x": 60, "y": 0, "w": 60, "h": 20}],
            "attrs": {"text_source": "text_iface"},
            "_atspi_app_name": "mousepad",
            "_atspi_path": [1, 2, 3],
        }
    ]
    geometry_cache = {("mousepad", (1, 2, 3)): _mono_geometry("Hello world\n")}

    meta = populate_visible_text(elements, geometry_cache=geometry_cache)

    assert meta["num_clipped"] == 1
    assert elements[0]["visible_text"] == "world\n"
    assert elements[0]["visible_text_status"] == "clipped"
    assert elements[0]["visible_text_confidence"] == 1.0


def test_populate_visible_text_uses_text_geometry_not_widget_box_for_visibility() -> None:
    elements = [
        {
            "inner_text": "DeskShot Notes\n",
            "is_occluded": True,
            "rect": {"x": 90, "y": 0, "w": 210, "h": 20},
            "_visibility_source_rect": {"x": 0, "y": 0, "w": 300, "h": 20},
            "visible_fragments": [{"x": 90, "y": 0, "w": 210, "h": 20}],
            "attrs": {"text_source": "text_iface"},
            "_atspi_app_name": "mousepad",
            "_atspi_path": [4, 2, 1],
        }
    ]
    geometry_cache = {("mousepad", (4, 2, 1)): _mono_geometry("DeskShot Notes\n", x=100)}

    populate_visible_text(elements, geometry_cache=geometry_cache)

    assert elements[0]["visible_text"] == "DeskShot Notes\n"
    assert elements[0]["visible_text_status"] == "full_visible"
    assert elements[0]["is_occluded"] is False
    assert elements[0]["rect"] == {"x": 100, "y": 0, "w": 140, "h": 20}
    assert elements[0]["visible_fragments"] == [{"x": 100, "y": 0, "w": 140, "h": 20}]


def test_populate_visible_text_keeps_editable_widget_geometry_when_text_is_partially_visible() -> None:
    elements = [
        {
            "inner_text": "Hello world\n",
            "type": "Text Input",
            "is_occluded": True,
            "_is_occluded_by_overlap": True,
            "rect": {"x": 60, "y": 0, "w": 240, "h": 120},
            "_visibility_source_rect": {"x": 0, "y": 0, "w": 300, "h": 120},
            "visible_fragments": [{"x": 60, "y": 0, "w": 240, "h": 120}],
            "attrs": {
                "text_source": "text_iface",
                "states": {"editable": True},
                "interfaces": {"editable_text": True, "text": True},
            },
            "_atspi_app_name": "mousepad",
            "_atspi_path": [7, 1, 2],
        }
    ]
    geometry_cache = {("mousepad", (7, 1, 2)): _mono_geometry("Hello world\n", x=0)}

    meta = populate_visible_text(elements, geometry_cache=geometry_cache)

    assert meta["num_clipped"] == 1
    assert elements[0]["visible_text"] == "world\n"
    assert elements[0]["visible_text_status"] == "clipped"
    assert elements[0]["rect"] == {"x": 60, "y": 0, "w": 240, "h": 120}
    assert elements[0]["visible_fragments"] == [{"x": 60, "y": 0, "w": 240, "h": 120}]
    assert elements[0]["is_occluded"] is True


def test_render_clip_chunks_marks_each_gap() -> None:
    from deskshot.extraction.text_visibility import TEXT_CLIP_MARKER, render_clip_chunks

    trailing = [("text", "the docu"), ("gap", "")]
    leading = [("gap", ""), ("text", "ment is here")]
    internal = [("text", "the "), ("gap", ""), ("text", " is here")]

    assert render_clip_chunks(trailing, marker="") == "the docu"
    assert render_clip_chunks(trailing, marker=TEXT_CLIP_MARKER) == f"the docu{TEXT_CLIP_MARKER}"
    assert render_clip_chunks(leading, marker=TEXT_CLIP_MARKER) == f"{TEXT_CLIP_MARKER}ment is here"
    assert (
        render_clip_chunks(internal, marker=TEXT_CLIP_MARKER)
        == f"the {TEXT_CLIP_MARKER} is here"
    )


def test_render_clip_chunks_drops_marker_when_nothing_visible() -> None:
    """A fully hidden element must not serialize to a bare marker."""
    from deskshot.extraction.text_visibility import TEXT_CLIP_MARKER, render_clip_chunks

    assert render_clip_chunks([("gap", "")], marker=TEXT_CLIP_MARKER) == ""
    assert render_clip_chunks([], marker=TEXT_CLIP_MARKER) == ""


def test_clip_marker_is_not_an_ellipsis() -> None:
    """UI text is full of literal ellipses, so the marker must not collide.

    "Save As..." and toolkit-inserted "…" would otherwise be indistinguishable
    from content hidden by an overlapping window.
    """
    from deskshot.extraction.text_visibility import TEXT_CLIP_MARKER

    assert "..." not in TEXT_CLIP_MARKER
    assert "…" not in TEXT_CLIP_MARKER


def test_annotate_occlusion_state_distinguishes_three_states() -> None:
    from deskshot.extraction.visibility_fragments import annotate_occlusion_state

    elements = [
        {"is_occluded": False, "visible_fragments": [{"x": 0, "y": 0, "w": 10, "h": 10}]},
        {"is_occluded": True, "visible_fragments": [{"x": 0, "y": 0, "w": 4, "h": 10}]},
        {"is_occluded": True, "visible_fragments": []},
    ]

    counts = annotate_occlusion_state(elements)

    assert [e["occlusion_state"] for e in elements] == ["none", "partial", "hidden"]
    assert counts == {"none": 1, "partial": 1, "hidden": 1}


def _geometry_for(text: str, *, char_w: int = 10, y: int = 0, h: int = 20):
    """Lay text out as one line of fixed-width glyphs."""
    from deskshot.extraction.text_visibility import LineRange, TextGeometry

    extents = {
        i: {"x": i * char_w, "y": y, "w": char_w, "h": h} for i in range(len(text))
    }
    line = LineRange(
        start=0,
        end=len(text),
        text=text,
        rect={"x": 0, "y": y, "w": char_w * len(text), "h": h},
    )
    return TextGeometry(text=text, lines=[line], char_extents=extents)


def test_clip_chunks_marks_trailing_occlusion() -> None:
    """Marc's case: 'the document is here' with only 'the docu' left visible."""
    from deskshot.extraction.text_visibility import (
        TEXT_CLIP_MARKER,
        _clip_chunks,
        render_clip_chunks,
    )

    text = "the document is here"
    geometry = _geometry_for(text)
    visible = [{"x": 0, "y": 0, "w": 80, "h": 20}]  # first 8 glyphs

    chunks = _clip_chunks(geometry, visible)

    assert render_clip_chunks(chunks, marker="") == "the docu"
    assert render_clip_chunks(chunks, marker=TEXT_CLIP_MARKER) == f"the docu{TEXT_CLIP_MARKER}"


def test_clip_chunks_marks_leading_and_internal_occlusion() -> None:
    from deskshot.extraction.text_visibility import (
        TEXT_CLIP_MARKER,
        _clip_chunks,
        render_clip_chunks,
    )

    text = "abcdefghij"
    geometry = _geometry_for(text)
    # Hide "ab" at the front and "ef" in the middle.
    visible = [{"x": 20, "y": 0, "w": 20, "h": 20}, {"x": 60, "y": 0, "w": 40, "h": 20}]

    marked = render_clip_chunks(_clip_chunks(geometry, visible), marker=TEXT_CLIP_MARKER)

    assert marked == f"{TEXT_CLIP_MARKER}cd{TEXT_CLIP_MARKER}ghij"


def test_clip_chunks_ignores_whitespace_only_occlusion() -> None:
    """Losing a space costs no content, so it must not produce a marker."""
    from deskshot.extraction.text_visibility import (
        TEXT_CLIP_MARKER,
        _clip_chunks,
        render_clip_chunks,
    )

    text = "ab cd"
    geometry = _geometry_for(text)
    # Hide only the space at index 2.
    visible = [{"x": 0, "y": 0, "w": 20, "h": 20}, {"x": 30, "y": 0, "w": 20, "h": 20}]

    marked = render_clip_chunks(_clip_chunks(geometry, visible), marker=TEXT_CLIP_MARKER)

    assert marked == "abcd"
    assert TEXT_CLIP_MARKER not in marked


def test_element_uid_is_stable_and_distinct() -> None:
    """Element identity across observations depends on this being stable."""
    from deskshot.extraction.text_visibility import _element_uid

    a = _element_uid("homebank", [0, 3, 7])
    assert a == _element_uid("homebank", [0, 3, 7])
    assert a != _element_uid("homebank", [0, 3, 8])
    assert a != _element_uid("xarchiver", [0, 3, 7])


def test_strip_internal_state_leaves_uid_behind() -> None:
    from deskshot.extraction.text_visibility import strip_internal_text_visibility_state

    elements = [{"_atspi_app_name": "homebank", "_atspi_path": [0, 2]}]

    strip_internal_text_visibility_state(elements)

    assert "uid" in elements[0]
    assert "_atspi_path" not in elements[0]
    assert "_atspi_app_name" not in elements[0]


def _gray_with_ink(width=520, height=40, ink_spans=(), y0=10, y1=28):
    """A synthetic screenshot: white, with dark glyph columns at ink_spans."""
    import numpy as np

    g = np.full((height, width), 255, dtype=int)
    for lo, hi in ink_spans:
        g[y0:y1, lo:hi] = 20
    return g


def test_estimate_char_extent_offset_finds_the_real_shift() -> None:
    """Reproduces the measured HomeBank case.

    Component 212..302, glyphs actually drawn 241..298, but AT-SPI reported the
    text at 215..272. The popup covers up to 262, so only 262..302 is visible.
    """
    from deskshot.extraction.text_visibility import estimate_char_extent_offset

    cw = 7
    reported = [{"x": 215 + i * cw, "y": 10, "w": cw, "h": 18} for i in range(8)]
    actual = [(241 + i * cw, 241 + (i + 1) * cw) for i in range(8)]
    gray = _gray_with_ink(ink_spans=actual)

    dx, conf = estimate_char_extent_offset(
        gray, reported,
        cell_rect={"x": 212, "y": 10, "w": 90, "h": 18},
        visible_regions=[{"x": 262, "y": 10, "w": 40, "h": 18}],
    )

    assert dx == 26


def test_estimate_char_extent_offset_returns_zero_when_geometry_is_right() -> None:
    from deskshot.extraction.text_visibility import estimate_char_extent_offset

    cw = 7
    rects = [{"x": 241 + i * cw, "y": 10, "w": cw, "h": 18} for i in range(8)]
    gray = _gray_with_ink(ink_spans=[(241 + i * cw, 241 + (i + 1) * cw) for i in range(8)])

    dx, _conf = estimate_char_extent_offset(
        gray, rects,
        cell_rect={"x": 212, "y": 10, "w": 90, "h": 18},
        visible_regions=[{"x": 262, "y": 10, "w": 40, "h": 18}],
    )

    assert dx == 0


def test_estimate_char_extent_offset_refuses_a_weak_match() -> None:
    """No ink at all in the visible region must not produce a confident shift."""
    from deskshot.extraction.text_visibility import estimate_char_extent_offset

    rects = [{"x": 215 + i * 7, "y": 10, "w": 7, "h": 18} for i in range(8)]
    gray = _gray_with_ink(ink_spans=[])

    dx, _conf = estimate_char_extent_offset(
        gray, rects,
        cell_rect={"x": 212, "y": 10, "w": 90, "h": 18},
        visible_regions=[{"x": 262, "y": 10, "w": 40, "h": 18}],
    )

    assert dx == 0


def test_estimate_char_extent_offset_keeps_text_inside_its_cell() -> None:
    """A shift that would push the text out of its own cell is rejected."""
    from deskshot.extraction.text_visibility import estimate_char_extent_offset

    rects = [{"x": 215 + i * 7, "y": 10, "w": 7, "h": 18} for i in range(8)]
    # Ink far to the right, outside the cell entirely.
    gray = _gray_with_ink(ink_spans=[(430 + i * 7, 430 + (i + 1) * 7) for i in range(8)])

    dx, _conf = estimate_char_extent_offset(
        gray, rects,
        cell_rect={"x": 212, "y": 10, "w": 90, "h": 18},
        visible_regions=[{"x": 262, "y": 10, "w": 240, "h": 18}],
    )

    assert 215 + dx >= 210
    assert 215 + 56 + dx <= 304


def test_shift_char_extents_moves_glyphs_and_lines() -> None:
    from deskshot.extraction.text_visibility import (
        LineRange, TextGeometry, shift_char_extents,
    )

    g = TextGeometry(
        text="ab",
        lines=[LineRange(0, 2, "ab", {"x": 100, "y": 5, "w": 20, "h": 10})],
        char_extents={0: {"x": 100, "y": 5, "w": 10, "h": 10},
                      1: {"x": 110, "y": 5, "w": 10, "h": 10}},
    )

    out = shift_char_extents(g, 26)

    assert out.lines[0].rect["x"] == 126
    assert out.char_extents[0]["x"] == 126 and out.char_extents[1]["x"] == 136
    assert shift_char_extents(g, 0) is g


def test_confidence_is_low_when_there_is_no_ink_to_align_to() -> None:
    """The gate's job is to refuse when the pixels cannot confirm the layout.

    Position itself comes from anchoring, so once a plausible shift exists the
    ink is normally explained. What must never happen is emitting text when the
    visible region shows nothing to align against.
    """
    from deskshot.extraction.text_visibility import (
        CHAR_GEOMETRY_MIN_CONFIDENCE, estimate_char_extent_offset,
    )

    rects = [{"x": 215 + i * 7, "y": 10, "w": 7, "h": 18} for i in range(8)]
    gray = _gray_with_ink(ink_spans=[])          # blank visible region

    dx, conf = estimate_char_extent_offset(
        gray, rects,
        cell_rect={"x": 212, "y": 10, "w": 90, "h": 18},
        visible_regions=[{"x": 262, "y": 10, "w": 40, "h": 18}],
    )

    assert dx == 0
    assert conf < CHAR_GEOMETRY_MIN_CONFIDENCE


def test_confidence_is_high_when_glyphs_land_on_the_ink() -> None:
    from deskshot.extraction.text_visibility import (
        CHAR_GEOMETRY_MIN_CONFIDENCE, estimate_char_extent_offset,
    )

    cw = 7
    reported = [{"x": 215 + i * cw, "y": 10, "w": cw, "h": 18} for i in range(8)]
    actual = [(241 + i * cw, 241 + (i + 1) * cw) for i in range(8)]
    gray = _gray_with_ink(ink_spans=actual)

    dx, conf = estimate_char_extent_offset(
        gray, reported,
        cell_rect={"x": 212, "y": 10, "w": 90, "h": 18},
        visible_regions=[{"x": 262, "y": 10, "w": 40, "h": 18}],
    )

    assert dx == 26
    assert conf >= CHAR_GEOMETRY_MIN_CONFIDENCE


def test_repair_char_advances_rebuilds_collapsed_glyphs() -> None:
    """The measured HomeBank case: trailing glyphs collapse to zero width.

    '0.42 BTC' came back with T and C as zero-width boxes at the line's right
    edge, plus a spurious gap between '4' and '2'. Zero-width boxes can never be
    judged visible, so the tail of an occluded string was unrecoverable.
    """
    from deskshot.extraction.text_visibility import (
        LineRange, TextGeometry, repair_char_advances,
    )

    widths = [8, 4, 8, 9, 7, 9, 0, 0]
    xs = [215, 223, 227, 247, 256, 263, 272, 272]      # note the 235->247 gap
    g = TextGeometry(
        text="0.42 BTC",
        lines=[LineRange(0, 8, "0.42 BTC", {"x": 215, "y": 197, "w": 57, "h": 17})],
        char_extents={i: {"x": xs[i], "y": 197, "w": widths[i], "h": 17} for i in range(8)},
    )

    out = repair_char_advances(g)

    boxes = [out.char_extents[i] for i in range(8)]
    assert all(b["w"] > 0 for b in boxes), "no glyph may stay zero-width"
    # laid out contiguously from the line origin
    for a, b in zip(boxes, boxes[1:]):
        assert a["x"] + a["w"] == b["x"]
    assert boxes[0]["x"] == 215
    # the 12px the line has left over is split between the two collapsed glyphs
    assert boxes[6]["w"] == 6 and boxes[7]["w"] == 6
    assert boxes[-1]["x"] + boxes[-1]["w"] == 215 + 57


def test_repair_char_advances_leaves_sane_geometry_alone() -> None:
    from deskshot.extraction.text_visibility import (
        LineRange, TextGeometry, repair_char_advances,
    )

    g = TextGeometry(
        text="abc",
        lines=[LineRange(0, 3, "abc", {"x": 10, "y": 0, "w": 30, "h": 10})],
        char_extents={i: {"x": 10 + i * 10, "y": 0, "w": 10, "h": 10} for i in range(3)},
    )

    assert repair_char_advances(g) is g


def test_repair_char_advances_refuses_when_nothing_to_distribute() -> None:
    """If the known widths already fill the line, do not fabricate advances."""
    from deskshot.extraction.text_visibility import (
        LineRange, TextGeometry, repair_char_advances,
    )

    g = TextGeometry(
        text="ab",
        lines=[LineRange(0, 2, "ab", {"x": 0, "y": 0, "w": 10, "h": 10})],
        char_extents={0: {"x": 0, "y": 0, "w": 10, "h": 10},
                      1: {"x": 10, "y": 0, "w": 0, "h": 10}},
    )

    assert repair_char_advances(g) is g


def test_ink_oracle_rejects_a_blank_glyph_box() -> None:
    from deskshot.extraction.text_visibility import make_ink_oracle

    gray = _gray_with_ink(ink_spans=[(262, 269)])
    has_ink = make_ink_oracle(gray)

    assert has_ink({"x": 262, "y": 10, "w": 7, "h": 18}) is True
    assert has_ink({"x": 253, "y": 10, "w": 8, "h": 18}) is False


def test_clip_chunks_drops_a_glyph_with_no_ink_behind_it() -> None:
    """Geometry alone puts a boundary glyph on the wrong side of an edge.

    Measured: '0.42 BTC' occluded from the left yielded '42 BTC', one glyph more
    than the screen shows. The '4' straddles the occlusion edge and counts as
    half visible, but carries no ink, so the pixels settle it.
    """
    from deskshot.extraction.text_visibility import (
        LineRange, TextGeometry, _clip_chunks, make_ink_oracle, render_clip_chunks,
    )

    text = "42"
    # '4' straddles the edge at 262 (half visible, no ink); '2' is inked.
    geometry = TextGeometry(
        text=text,
        lines=[LineRange(0, 2, text, {"x": 258, "y": 10, "w": 17, "h": 18})],
        char_extents={0: {"x": 258, "y": 10, "w": 8, "h": 18},
                      1: {"x": 266, "y": 10, "w": 9, "h": 18}},
    )
    visible = [{"x": 262, "y": 10, "w": 40, "h": 18}]
    gray = _gray_with_ink(ink_spans=[(267, 275)])

    without = render_clip_chunks(_clip_chunks(geometry, visible), marker="")
    with_ink = render_clip_chunks(
        _clip_chunks(geometry, visible, has_ink=make_ink_oracle(gray)), marker=""
    )

    assert without == "42"
    assert with_ink == "2"


def test_ink_check_keeps_whitespace() -> None:
    """Spaces have no ink to find and must not be dropped."""
    from deskshot.extraction.text_visibility import (
        LineRange, TextGeometry, _clip_chunks, make_ink_oracle, render_clip_chunks,
    )

    text = "a b"
    geometry = TextGeometry(
        text=text,
        lines=[LineRange(0, 3, text, {"x": 100, "y": 10, "w": 30, "h": 18})],
        char_extents={i: {"x": 100 + i * 10, "y": 10, "w": 10, "h": 18} for i in range(3)},
    )
    gray = _gray_with_ink(ink_spans=[(100, 110), (120, 130)])

    out = render_clip_chunks(
        _clip_chunks(geometry, [{"x": 100, "y": 10, "w": 30, "h": 18}],
                     has_ink=make_ink_oracle(gray)),
        marker="",
    )

    assert out == "a b"


def test_whitespace_only_survivor_is_treated_as_no_text() -> None:
    """A cell that draws nothing must not report its spaces as visible.

    Found on a HomeBank column that renders empty: every inked glyph failed the
    pixel test and only the space survived, which would have been a phantom
    annotation.
    """
    from deskshot.extraction.text_visibility import (
        LineRange, TextGeometry, _clip_chunks, make_ink_oracle, render_clip_chunks,
    )

    text = "$ 5"
    geometry = TextGeometry(
        text=text,
        lines=[LineRange(0, 3, text, {"x": 401, "y": 10, "w": 21, "h": 18})],
        char_extents={i: {"x": 401 + i * 7, "y": 10, "w": 7, "h": 18} for i in range(3)},
    )
    gray = _gray_with_ink(ink_spans=[])          # nothing rendered here

    # A partially covering fragment, so the per-character path runs; a fragment
    # that covers the whole line is taken as fully visible without pixel checks.
    out = render_clip_chunks(
        _clip_chunks(geometry, [{"x": 405, "y": 10, "w": 17, "h": 18}],
                     has_ink=make_ink_oracle(gray)),
        marker="",
    )

    assert out.strip() == ""


def test_viewport_clipped_cell_is_corrected_against_the_pixels() -> None:
    """A cell narrowed by an ancestor viewport, not by an overlapping window.

    Measured in HomeBank: the amount cell spans 346..436, the tree view clips it
    at 420, and the glyphs are drawn right-aligned at 398..421. AT-SPI reports
    the character boxes at 349..385 - left-aligned, a full text-width away from
    the ink. Nothing overlaps the cell, so `is_occluded` is false, and the
    correction used to be skipped on exactly that basis; the uncorrected boxes
    then became the element rect and the text was published as fully visible.
    """
    import numpy as np

    gray = np.full((500, 1024), 255, dtype=int)
    gray[362:379, 398:421] = 0  # the glyphs the screen actually draws

    elements = [
        {
            "inner_text": "560",
            "is_occluded": False,
            "rect": {"x": 346, "y": 360, "w": 74, "h": 21},
            "_visibility_source_rect": {"x": 346, "y": 360, "w": 90, "h": 21},
            "visible_fragments": [{"x": 346, "y": 360, "w": 74, "h": 21}],
            "attrs": {"text_source": "text_iface"},
            "_atspi_app_name": "homebank",
            "_atspi_path": [0, 3, 7],
        }
    ]
    geometry_cache = {
        ("homebank", (0, 3, 7)): _mono_geometry("560", x=349, y=362, cw=12, lh=17)
    }

    populate_visible_text(elements, geometry_cache=geometry_cache, gray=gray)

    elem = elements[0]
    assert elem["text_geometry_offset_px"] == 49
    assert elem["visible_text_status"] == "clipped"
    assert elem["is_occluded"] is True
    # The box now sits on the ink instead of 49px to its left. Its right edge
    # may pass the clip by up to one glyph, because a partly visible character
    # is kept whole rather than cut down the middle.
    assert elem["rect"]["x"] == 398
    assert elem["rect"]["x"] + elem["rect"]["w"] <= 421 + 12


def test_unvalidatable_geometry_keeps_the_widget_box_and_the_text() -> None:
    """Ink the glyph boxes cannot explain is not a reason to withhold a label.

    A cell holding an icon beside its text has ink no glyph box accounts for, so
    the alignment score stays low. Nothing overlaps the element, so every
    character is on screen: the honest answer is the full text with the widget
    rect, never a text-derived box the pixels do not confirm.
    """
    import numpy as np

    gray = np.full((500, 1024), 255, dtype=int)
    gray[10:27, 0:200] = 0  # a solid block: no glyph layout explains this

    elements = [
        {
            "inner_text": "560",
            "is_occluded": False,
            "rect": {"x": 0, "y": 10, "w": 180, "h": 20},
            "_visibility_source_rect": {"x": 0, "y": 10, "w": 200, "h": 20},
            "visible_fragments": [{"x": 0, "y": 10, "w": 180, "h": 20}],
            "attrs": {"text_source": "text_iface"},
            "_atspi_app_name": "homebank",
            "_atspi_path": [0, 4, 1],
        }
    ]
    geometry_cache = {
        ("homebank", (0, 4, 1)): _mono_geometry("560", x=5, y=10, cw=12, lh=17)
    }

    populate_visible_text(elements, geometry_cache=geometry_cache, gray=gray)

    elem = elements[0]
    assert elem["visible_text"] == "560"
    assert elem["visible_text_status"] == "full_visible"
    assert elem["text_geometry_unvalidated"] is True
    assert elem["rect"] == {"x": 0, "y": 10, "w": 180, "h": 20}
