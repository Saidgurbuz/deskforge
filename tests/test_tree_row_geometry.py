"""A tree view's expander column is reported displaced from what it draws.

Measured on `incremental_checks/v214_titlebar`:

- xarchiver's archive tree column spans x=89..287. The row at y=869 draws a
  folder icon at 109..125 and "logs" at 128..156; AT-SPI reports the icon cell
  at 129..145 and the label cell at 145..175. The folder icons were the top
  uncovered-ink cluster in that window while being *annotated* 20px away.
- HomeBank's "Accounts" column spans 183..345. The row at y=240 draws its
  expander at 188..198 and "(no type)" at 206..280, reported as one cell at
  223..343. The row at y=264 draws "Paypal Account" at 224..335 - 111px wide -
  and reports a 102px cell at x=241.

Columns of the same tables that carry no expander are reported correctly to the
pixel (xarchiver "Filename": icon cell 292..314 over ink 295..311), so the
correction must be scoped to indented columns and must not touch the rest.
"""

import numpy as np

from deskshot.extraction.tree_row_geometry import align_indented_tree_cells


def _gray(width=400, height=120, ink=()):
    """A white canvas with black boxes, i.e. a screenshot with known content."""
    canvas = np.full((height, width), 255, dtype=int)
    for x0, y0, x1, y1 in ink:
        canvas[y0:y1, x0:x1] = 0
    return canvas


def _label(x0, x1, y0=32, y1=48):
    """Ink shaped like a word: strokes with gaps, not one solid slab.

    A label that fills its span solidly would be the majority of a table
    column, and "content" is defined as what differs from the row's background.
    Real glyph runs cover roughly half their extent, with the first and last
    stroke on the label's own edges.
    """
    return [(x, y0, min(x + 3, x1), y1) for x in range(x0, x1 - 2, 5)] + [
        (x1 - 3, y0, x1, y1)
    ]


def _table(cells, headers, role="tree table", app="app",
           container_rect=None, expandable=False):
    """A table container with column headers and cells, hierarchy resolved."""
    container = {
        "role": role, "app_name": app, "_dom_index": 0,
        "rect": dict(container_rect or {"x": 0, "y": 0, "w": 400, "h": 120}),
        "_children_dom_indices": [],
    }
    elements = [container]
    idx = 1
    for x, w in headers:
        elements.append({
            "role": "table column header", "app_name": app, "_dom_index": idx,
            "rect": {"x": x, "y": 0, "w": w, "h": 20},
            "_parent_dom_index": 0, "_children_dom_indices": [],
        })
        container["_children_dom_indices"].append(idx)
        idx += 1
    for cell in cells:
        x, y, w, h = cell[:4]
        row_expandable = cell[4] if len(cell) > 4 else expandable
        elements.append({
            "role": "table cell", "app_name": app, "_dom_index": idx,
            "rect": {"x": x, "y": y, "w": w, "h": h},
            "attrs": {"states": {"expandable": True}} if row_expandable else {},
            "_parent_dom_index": 0, "_children_dom_indices": [],
        })
        container["_children_dom_indices"].append(idx)
        idx += 1
    return elements


def _cells(elements):
    return [e for e in elements if e["role"] == "table cell"]


def test_an_indented_row_cell_moves_onto_the_leading_icon_it_left_uncovered() -> None:
    """The xarchiver archive-tree row, to the pixel: column 89..287, folder icon
    drawn 109..125 and "logs" 128..156, reported as cells at 129 and 145. Both
    cells belong to one row and both are displaced by the same 20px."""
    gray = _gray(width=300, ink=[(109, 32, 125, 48)] + _label(128, 156))
    els = _table(
        cells=[(129, 30, 16, 22), (145, 32, 30, 18)],
        headers=[(89, 198)],
    )

    meta = align_indented_tree_cells(els, gray)

    assert meta["num_rows"] == 1
    icon, label = _cells(els)
    assert icon["rect"]["x"] == 109
    assert label["rect"]["x"] == 125
    # Width is preserved: AT-SPI's widths agree with what is drawn, only the
    # origin is wrong. The label grows by 1px only to reach its own last column.
    assert icon["rect"]["w"] == 16


def test_a_shifted_cell_regrows_over_a_label_wider_than_its_reported_width() -> None:
    """HomeBank reports "Paypal Account" as a 102px cell at x=241 while the
    glyphs run 224..335, 111px wide. Translating alone moved the right edge left
    with the left edge and cut the tail off, which just moved the flagged gap
    from the left of the row to the right of it."""
    gray = _gray(width=400, ink=_label(224, 336))
    els = _table(cells=[(241, 30, 102, 22)], headers=[(183, 162)])

    align_indented_tree_cells(els, gray)

    cell = _cells(els)[0]
    assert cell["rect"]["x"] == 224
    assert cell["rect"]["x"] + cell["rect"]["w"] == 336


def test_a_cell_that_already_sits_on_its_content_is_left_alone() -> None:
    """xarchiver's "Filename" column has no expander and is reported correctly.
    A correction that moved it would be trading one error for another."""
    gray = _gray(ink=[(295, 32, 311, 48)] + _label(316, 352))
    els = _table(
        cells=[(292, 30, 22, 22), (316, 32, 37, 18)],
        headers=[(290, 120)],
    )

    meta = align_indented_tree_cells(els, gray)

    assert meta["num_rows"] == 0
    assert [c["rect"]["x"] for c in _cells(els)] == [292, 316]


def test_a_column_separator_rule_does_not_drag_an_indented_cell_left() -> None:
    """Every column is drawn with a 1-2px rule at its left edge. That is ink to
    the left of an indented cell, and treating it as the row's content would
    move every tree cell onto the column boundary."""
    gray = _gray(ink=[(89, 25, 91, 55)] + _label(129, 175))
    els = _table(cells=[(129, 30, 46, 22)], headers=[(89, 198)])

    meta = align_indented_tree_cells(els, gray)

    assert meta["num_rows"] == 0
    assert _cells(els)[0]["rect"]["x"] == 129


def test_a_row_separator_rule_does_not_make_the_whole_band_look_inked() -> None:
    """Rows are drawn with a horizontal rule and alternating backgrounds. Those
    span every column, so counting them as content would report ink at the
    column's first pixel on every row of every tree."""
    gray = _gray(ink=[(89, 30, 287, 32)] + _label(129, 175, y0=34))
    els = _table(cells=[(129, 30, 46, 22)], headers=[(89, 198)])

    meta = align_indented_tree_cells(els, gray)

    assert meta["num_rows"] == 0
    assert _cells(els)[0]["rect"]["x"] == 129


def test_a_headerless_tree_uses_its_own_box_as_the_column() -> None:
    """bluefish's file browser hides its headers, and refusing to act without
    one left every row displaced.

    Measured on `incremental_checks/v228_provenance/run2`: the tree table's box
    is 714..964 and its rows report a 16px icon cell at 754 over an icon drawn
    at 736..748, with the label cell at 770 over glyphs starting at 752. A
    headerless tree view has exactly one column, and the container's own box is
    the same left edge a header would have given.
    """
    gray = _gray(width=1000, ink=[(736, 32, 748, 48)] + _label(752, 890))
    els = _table(
        cells=[(754, 30, 16, 22), (770, 32, 139, 18)],
        headers=[],
        container_rect={"x": 714, "y": 0, "w": 250, "h": 120},
    )

    meta = align_indented_tree_cells(els, gray)

    assert meta["num_rows"] == 1
    icon, label = _cells(els)
    assert icon["rect"]["x"] == 736
    assert label["rect"]["x"] == 752


def test_an_expandable_rows_disclosure_triangle_is_not_the_cell_it_anchors_on() -> None:
    """The over-correction the user reported as "boxes to the left now".

    An expandable row draws its triangle before the cell area, so anchoring on
    the row's first ink asks for the distance to the triangle rather than to the
    icon. Measured on bluefish's upper tree: the row at y=326 draws its expander
    at 737..747, its folder icon at 753..767 and "proj" at 771..794, and reports
    the icon cell at 772. The right answer is -19, not the -35 the triangle
    would give.
    """
    gray = _gray(width=1000,
                 ink=[(737, 32, 747, 48), (753, 32, 767, 48)] + _label(771, 794))
    els = _table(
        cells=[(772, 30, 16, 22, True), (788, 32, 25, 18, True)],
        headers=[],
        container_rect={"x": 714, "y": 0, "w": 250, "h": 120},
    )

    align_indented_tree_cells(els, gray)

    icon, label = _cells(els)
    assert icon["rect"]["x"] == 753
    assert label["rect"]["x"] == 769


def test_rows_covered_by_a_popup_take_the_shift_the_clean_rows_agree_on() -> None:
    """A menu drawn over the lower half of a tree makes those rows' own
    evidence useless: their first ink is the popup's, 100-180px away, and a
    median over per-row answers lands in that nonsense once the covered rows
    outnumber the clean ones. The displacement is one property of the column, so
    the value its rows agree on is applied to all of them.
    """
    ink = []
    clean_rows, covered_rows = 2, 4
    cells = []
    for i in range(clean_rows):
        y = 30 + i * 24
        ink += [(736, y + 2, 748, y + 18)] + _label(752, 860, y0=y + 2, y1=y + 18)
        cells.append((754, y, 16, 22))
        cells.append((770, y + 2, 100, 18))
    for i in range(covered_rows):
        y = 30 + (clean_rows + i) * 24
        # The popup: a solid slab of unrelated ink from the column's left edge,
        # which is where a menu drawn over the tree actually starts.
        ink += [(716, y + 2, 900, y + 18)]
        cells.append((880, y, 16, 22))
        cells.append((896, y + 2, 60, 18))
    gray = _gray(width=1000, height=30 + (clean_rows + covered_rows) * 24 + 20, ink=ink)
    els = _table(cells=cells, headers=[],
                 container_rect={"x": 714, "y": 0, "w": 250, "h": 200})

    align_indented_tree_cells(els, gray)

    xs = [c["rect"]["x"] for c in _cells(els)]
    assert xs[:2 * clean_rows] == [736, 752] * clean_rows
    # The deeper rows had no usable evidence of their own and still move by the
    # column's shift, because that is what is wrong with them.
    assert xs[2 * clean_rows:] == [862, 878] * covered_rows


def test_a_shift_larger_than_one_indent_level_is_refused() -> None:
    """The correction repairs a displacement of one level of indentation - 17px
    in HomeBank, 18 in bluefish, 20 in xarchiver. A candidate far beyond that is
    the estimator having found another window's content, not a tree cell."""
    gray = _gray(width=400, ink=[(20, 32, 40, 48)] + _label(129, 175))
    els = _table(cells=[(129, 30, 46, 22)], headers=[(10, 300)])

    meta = align_indented_tree_cells(els, gray)

    assert meta["num_rows"] == 0
    assert _cells(els)[0]["rect"]["x"] == 129


def test_a_cell_never_moves_out_of_its_own_column() -> None:
    """The shift is bounded by the column: ink is only ever searched between the
    column header's own edges, so a neighbouring column's content cannot pull a
    cell across the boundary."""
    gray = _gray(ink=[(40, 32, 60, 48)] + _label(129, 175))
    els = _table(cells=[(129, 30, 46, 22)], headers=[(89, 198)])

    align_indented_tree_cells(els, gray)

    assert _cells(els)[0]["rect"]["x"] >= 89


def test_alignment_is_skipped_when_the_screenshot_is_unavailable() -> None:
    """The pixels are the only evidence for where content is drawn. With no
    screenshot the rects are left exactly as the toolkit reported them."""
    els = _table(cells=[(129, 30, 16, 22)], headers=[(89, 198)])

    meta = align_indented_tree_cells(els, None)

    assert meta["enabled"] is False
    assert _cells(els)[0]["rect"]["x"] == 129


def test_a_nested_cell_moves_with_the_row_it_belongs_to() -> None:
    """xarchiver reports the row as a wide cell (129..285) with the icon and the
    label as its children. Moving only the children would leave the parent's box
    claiming a span the row does not occupy."""
    gray = _gray(width=300, ink=[(109, 32, 125, 48)] + _label(128, 156))
    els = _table(
        cells=[(129, 30, 156, 22), (129, 30, 16, 22), (145, 32, 30, 18)],
        headers=[(89, 198)],
    )
    # Re-parent the icon and label under the wide row cell, as AT-SPI reports.
    row, icon, label = _cells(els)
    els[0]["_children_dom_indices"] = [1, row["_dom_index"]]
    row["_children_dom_indices"] = [icon["_dom_index"], label["_dom_index"]]
    icon["_parent_dom_index"] = row["_dom_index"]
    label["_parent_dom_index"] = row["_dom_index"]

    align_indented_tree_cells(els, gray)

    assert row["rect"]["x"] == 109
    assert icon["rect"]["x"] == 109
    assert label["rect"]["x"] == 125
