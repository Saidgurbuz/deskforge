"""Put a tree view's indented row cells back on the pixels they describe.

GTK reports the cell rects of a tree view's *expander column* displaced to the
right by roughly one level of the row's indentation. The row's leading graphic -
the disclosure triangle, or the file-type icon - then falls outside every
annotated box, and the label box sits partly on empty space. Columns of the same
table that hold no expander are reported correctly, so this is not a
whole-widget coordinate error that a window-level offset would explain.

Measured on ``incremental_checks/v214_titlebar`` (2560x1440 capture):

- HomeBank's account tree. The "Accounts" column header spans x=183..345. The
  row at y=240 draws its expander at 188..198 and "(no type)" at 206..280, and
  AT-SPI reports the only cell for that column at 223..343 - starting 17px to
  the right of the glyphs it names. Every other column of that same table is
  inset 2px from its header; this one is inset 40px.
- xarchiver's archive tree. The column spans 89..287. The row at y=869 draws a
  folder icon at 109..125 and "logs" at 128..156; AT-SPI reports the icon cell
  at 129..145 and the label cell at 145..175. The neighbouring "Filename"
  column, a flat list with no expander, is reported correctly to the pixel
  (icon 292..314 over ink 295..311, label 316..353 over ink 316..352).

Those two rows were the top-ranked uncovered-ink clusters in their windows, and
the folder icons were annotated - just 20px away from where they are drawn.

The correction is a translation, never a resize: AT-SPI's cell *widths* agree
with what is drawn, only the origin is wrong. That is the same finding
``estimate_char_extent_offset`` records for character geometry, and keeping the
change to a translation means a cell can only ever move onto its own content,
inside its own column.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: How far a pixel must sit from its row's background before it counts as drawn
#: content. Same magnitude as the gradient threshold in
#: `scripts/audit_element_coverage.py`, so the fix answers the rule that flagged
#: the gap.
CONTENT_DELTA = 18

#: A normal column's cells sit ~2px inside their header. Anything further in is
#: tree indentation, which is the only case this correction is about.
MIN_INDENT_PX = 8

#: Ignore a 1-2px column separator rule when looking for the row's first ink.
MIN_INK_RUN_PX = 3

#: Below this the cell already overlaps its content and moving it would be
#: chasing antialiasing.
MIN_SHIFT_PX = 4

#: The displacement is one level of the tree's indentation, which is the
#: expander's own width plus the row spacing: measured 17px in HomeBank, 18px in
#: bluefish and 20px in xarchiver. Anything further than this is not an
#: indentation error, it is the estimator having found some other window's ink -
#: rows covered by a popup menu produced candidates of -105, -132 and -184px
#: before this bound existed.
MAX_SHIFT_PX = 40

#: Two candidates this close together are the same answer. Used to find the
#: value the rows of a column agree on.
SHIFT_AGREEMENT_PX = 2

TABLE_CONTAINER_ROLES = {"tree table", "table", "tree"}
CELL_ROLES = {"table cell", "tree item"}
COLUMN_HEADER_ROLE = "table column header"


def _rect(elem: Dict[str, Any]) -> Optional[Tuple[int, int, int, int]]:
    rect = elem.get("rect")
    if not isinstance(rect, dict):
        return None
    try:
        x, y = int(rect.get("x", 0)), int(rect.get("y", 0))
        w, h = int(rect.get("w", 0)), int(rect.get("h", 0))
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return x, y, w, h


def _role(elem: Dict[str, Any]) -> str:
    return str(elem.get("role") or "").strip().lower()


def _ink_columns(gray: Any, y0: int, y1: int, x0: int, x1: int) -> List[bool]:
    """Which columns of the band hold drawn content.

    Content is what differs from the row's *own* background, taken as the median
    of the band inside this column. Measuring against a gradient instead would
    mark only the edges of a filled shape, so a solid icon would read as two
    1px runs; measuring against a fixed colour would mark a whole selected or
    zebra-striped row as content. The median moves with the row, so a selected
    row contributes its label and nothing else, in a light or a dark theme
    alike.
    """
    import numpy as np

    height, width = int(gray.shape[0]), int(gray.shape[1])
    y0, y1 = max(0, y0), min(height, y1)
    x0, x1 = max(0, x0), min(width, x1)
    if y1 - y0 < 1 or x1 - x0 < 1:
        return []
    band = gray[y0:y1, x0:x1].astype(int)
    background = float(np.median(band))
    content = np.abs(band - background) > CONTENT_DELTA
    return [bool(v) for v in content.any(axis=0)]


def _ink_runs(cols: Sequence[bool], min_run: int) -> List[Tuple[int, int]]:
    """Index ranges of every inked run at least `min_run` wide."""
    runs: List[Tuple[int, int]] = []
    start: Optional[int] = None
    for i, on in enumerate(cols):
        if on and start is None:
            start = i
        elif not on and start is not None:
            if i - start >= min_run:
                runs.append((start, i))
            start = None
    if start is not None and len(cols) - start >= min_run:
        runs.append((start, len(cols)))
    return runs


def _first_ink_run(cols: Sequence[bool], min_run: int) -> Optional[Tuple[int, int]]:
    """Index range of the first inked run at least `min_run` wide."""
    runs = _ink_runs(cols, min_run)
    return runs[0] if runs else None


def _row_is_expandable(cells: Iterable[Dict[str, Any]]) -> bool:
    """Does this row draw a disclosure triangle before its first cell?

    GTK draws the expander in the expander column at the row's indentation and
    lays the cell renderers out after it, so for an expandable row the leading
    ink belongs to no cell and anchoring on it overshoots by the width of the
    triangle. Measured on bluefish's file browser: the rows of the upper tree
    are all expandable and anchoring on their first ink asks for -35px, while
    the icon they should land on is 19px away - and the flat file list below,
    whose rows are leaves, anchors correctly at -18px.

    `expandable` is set exactly when the row has children, which is exactly when
    the triangle is drawn, so this needs no pixel heuristic.
    """
    for cell in cells:
        states = (cell.get("attrs") or {}).get("states") or {}
        if states.get("expandable"):
            return True
    return False


def _consensus(candidates: Sequence[int]) -> Optional[int]:
    """The shift the rows of a column agree on, or None if they do not.

    A median is not enough: in a tree half covered by a popup menu, the covered
    rows each produce a different nonsense candidate and can outnumber the clean
    ones, so the median lands in the nonsense. The rows that are right all agree
    to the pixel instead, so the largest cluster wins and the scattered
    candidates cancel each other out.
    """
    if not candidates:
        return None
    best: Optional[Tuple[int, int]] = None
    for anchor in candidates:
        group = [c for c in candidates if abs(c - anchor) <= SHIFT_AGREEMENT_PX]
        value = sorted(group)[len(group) // 2]
        key = (len(group), -abs(value))
        if best is None or key > best[0]:
            best = (key, value)  # type: ignore[assignment]
    return None if best is None else int(best[1])


def _covered_ink(
    cols: Sequence[bool], x0: int, boxes: Iterable[Tuple[int, int]]
) -> int:
    covered = [False] * len(cols)
    for bx0, bx1 in boxes:
        lo = max(0, bx0 - x0)
        hi = min(len(cols), bx1 - x0)
        for i in range(lo, hi):
            covered[i] = True
    return sum(1 for i, on in enumerate(cols) if on and covered[i])


def _cluster_rows(cells: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Group a column's cells into rows by vertical centre.

    Cells of one row do not share a rect: in xarchiver's archive tree the icon
    cell is y=869 h=22 and the label cell y=871 h=18. They do share a centre.
    """
    entries: List[Tuple[float, Dict[str, Any]]] = []
    for cell in cells:
        box = _rect(cell)
        if box is None:
            continue
        _, y, _, h = box
        entries.append((y + h / 2.0, cell))
    entries.sort(key=lambda item: item[0])

    rows: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    anchor: Optional[float] = None
    for centre, cell in entries:
        if anchor is None or abs(centre - anchor) <= 4.0:
            if anchor is None:
                anchor = centre
            current.append(cell)
        else:
            rows.append(current)
            current = [cell]
            anchor = centre
    if current:
        rows.append(current)
    return rows


def _shift_group(cells: List[Dict[str, Any]], dx: int) -> None:
    for cell in cells:
        rect = cell.get("rect")
        if isinstance(rect, dict):
            rect["x"] = int(rect.get("x", 0)) + dx


def _extend_to_row_ink(
    cells: List[Dict[str, Any]],
    cols: Sequence[bool],
    col_x0: int,
    col_x1: int,
) -> None:
    """Grow a shifted cell's right edge back over content it now falls short of.

    A translation alone moves the right edge left by the same amount as the
    left edge, and a cell that was already too narrow for its own label then
    loses the tail of it. Measured: HomeBank reports "Paypal Account" as a
    102px cell at x=241 while the glyphs run 224..335, 111px wide - shifting to
    224 without regrowing cut "unt" off the end and moved the flagged gap from
    the left of the row to the right of it.

    Each cell may only grow up to the next cell in the row, or to the column's
    own right edge for the last one, so growth stays inside the row's own
    layout and one cell can never swallow the next.
    """
    ordered = sorted(cells, key=lambda c: (_rect(c) or (0,))[0])
    for position, cell in enumerate(ordered):
        box = _rect(cell)
        if box is None:
            continue
        x, y, w, h = box
        boundary = col_x1
        for other in ordered[position + 1:]:
            other_box = _rect(other)
            if other_box is not None and other_box[0] > x:
                boundary = min(boundary, other_box[0])
                break
        ink_hi = None
        for i in range(max(0, x - col_x0), min(len(cols), boundary - col_x0)):
            if cols[i]:
                ink_hi = col_x0 + i
        if ink_hi is None:
            continue
        wanted = min(ink_hi + 1, boundary)
        if wanted > x + w:
            cell["rect"]["w"] = wanted - x


def align_indented_tree_cells(
    elements: List[Dict[str, Any]], gray: Any
) -> Dict[str, Any]:
    """Translate indented tree cells onto their row's drawn content.

    Runs before any clipping so every later stage - occlusion, filtering, the
    leaf export, text geometry - sees the corrected rect. Every guard below
    exists to keep a correctly reported column from moving:

    - the column's left edge comes from a `table column header` when the table
      publishes one, and from the container's own box when it does not - a
      headerless tree has exactly one column, and refusing those left bluefish's
      file browser displaced by 18px on every row
    - the row's cells must start more than `MIN_INDENT_PX` inside that edge,
      which only tree indentation does (measured: 2px for plain columns, 40px
      for the expander column in HomeBank, xarchiver and bluefish alike)
    - the ink to move onto must be a run of at least `MIN_INK_RUN_PX`, so a
      column separator rule is not mistaken for content, and it must not be the
      row's disclosure triangle, which belongs to no cell
    - the shift is decided once per column, from the value its rows agree on,
      and may not exceed one level of indentation (`MAX_SHIFT_PX`)
    - the shift must strictly increase how much of the row's ink the cells
      cover, and it can never move a cell out of its own column
    """
    if gray is None or not elements:
        return {"enabled": False, "num_rows": 0, "num_cells": 0, "apps": {}}

    by_idx = {int(e["_dom_index"]): e for e in elements if isinstance(e.get("_dom_index"), int)}

    def descendants(root: Dict[str, Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        stack = list(root.get("_children_dom_indices") or root.get("children_indices") or [])
        while stack:
            idx = stack.pop()
            child = by_idx.get(int(idx))
            if child is None:
                continue
            out.append(child)
            stack.extend(child.get("_children_dom_indices") or child.get("children_indices") or [])
        return out

    num_rows = 0
    num_cells = 0
    apps: Dict[str, int] = {}

    for container in elements:
        if _role(container) not in TABLE_CONTAINER_ROLES:
            continue
        kin = descendants(container)
        headers = []
        for child in kin:
            if _role(child) != COLUMN_HEADER_ROLE:
                continue
            box = _rect(child)
            if box is not None:
                headers.append((box[0], box[0] + box[2]))
        if not headers:
            # A tree view with its headers hidden publishes no column header at
            # all, and refusing to act then is refusing to act on every such
            # tree. Its single column is the container's own box, which is the
            # same measurement a header would have given.
            box = _rect(container)
            if box is None:
                continue
            headers = [(box[0], box[0] + box[2])]
        headers.sort()

        cells = [c for c in kin if _role(c) in CELL_ROLES and _rect(c) is not None]
        if not cells:
            continue

        for col_x0, col_x1 in headers:
            in_column = [
                c for c in cells
                if col_x0 <= _rect(c)[0] < col_x1  # type: ignore[index]
            ]
            if not in_column:
                continue

            rows: List[Tuple[List[Dict[str, Any]], int, List[bool], Optional[int]]] = []
            candidates: List[int] = []
            for row in _cluster_rows(in_column):
                boxes = [_rect(c) for c in row]
                lead_x = min(b[0] for b in boxes if b)  # type: ignore[index]
                if lead_x - col_x0 <= MIN_INDENT_PX:
                    continue
                y0 = min(b[1] for b in boxes if b)  # type: ignore[index]
                y1 = max(b[1] + b[3] for b in boxes if b)  # type: ignore[index]
                # Inset the band so the row's own top and bottom edges, which
                # are horizontal rules shared by every column, stay out of it.
                cols = _ink_columns(gray, y0 + 1, y1 - 1, col_x0, col_x1)
                if not cols:
                    continue

                runs = _ink_runs(cols, MIN_INK_RUN_PX)
                if _row_is_expandable(row):
                    runs = runs[1:]
                candidate: Optional[int] = None
                if runs:
                    dx = col_x0 + runs[0][0] - lead_x
                    if -MAX_SHIFT_PX <= dx <= -MIN_SHIFT_PX:
                        candidate = dx
                        candidates.append(dx)
                rows.append((row, lead_x, cols, candidate))

            dx = _consensus(candidates)
            if dx is None:
                continue

            # Judge the agreed shift on the rows that voted for one: a row whose
            # own evidence was unusable - covered by another window, or empty -
            # cannot confirm or refute it, and counting it would let a popup
            # drawn over half a tree decide the answer.
            before = after = 0
            for row, lead_x, cols, candidate in rows:
                if candidate is None or lead_x + dx < col_x0:
                    continue
                current = [(b[0], b[0] + b[2]) for b in map(_rect, row) if b]
                before += _covered_ink(cols, col_x0, current)
                after += _covered_ink(cols, col_x0, [(a + dx, b + dx) for a, b in current])
            if after <= before:
                continue

            for row, lead_x, cols, _candidate in rows:
                if lead_x + dx < col_x0:
                    continue
                _shift_group(row, dx)
                _extend_to_row_ink(row, cols, col_x0, col_x1)
                num_rows += 1
                num_cells += len(row)
                app = str(container.get("app_name") or "")
                apps[app] = apps.get(app, 0) + len(row)

    return {
        "enabled": True,
        "num_rows": num_rows,
        "num_cells": num_cells,
        "apps": apps,
    }
