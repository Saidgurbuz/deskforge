"""The order elements are serialized in.

This is part of the training target, so it has to satisfy one property: a model
looking only at the screenshot must be able to derive it. Anything else asks the
model to guess and then scores the guess.

That rules out the two obvious sources. The accessibility tree's child order is
the app's internal order, not a visual fact. A document reading-order model -
which this used to call when `docling_ibm_models` was importable - was built for
non-overlapping, column-flowing page content and has no defensible answer for
three side-by-side windows; worse, making it conditional on an installed package
meant the ground-truth order was a property of the environment rather than of
the screen.

What is left is geometry, which is exactly what is visible: containment gives
the nesting, and rows-then-left-to-right gives the order within a group.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class PageSize:
    width: float
    height: float


def assign_reading_order_indices(
    elements: List[Dict[str, Any]],
    page_size: PageSize,
) -> str:
    """Populate `reading_order_index` in-place and return the backend name."""
    if not elements:
        return "spatial_fallback"

    order, backend = build_reading_order(elements, page_size)
    order_map = {idx: pos for pos, idx in enumerate(order)}
    for idx, elem in enumerate(elements):
        elem["reading_order_index"] = order_map.get(idx)
    return backend


def build_reading_order(
    elements: List[Dict[str, Any]],
    page_size: PageSize,
) -> Tuple[List[int], str]:
    """Build a preorder DFS reading order with predictor-based sibling sort."""
    n = len(elements)
    if n == 0:
        return [], "spatial_fallback"

    parents = _resolve_parent_indices(elements)
    children: Dict[int, List[int]] = {i: [] for i in range(n)}
    roots: List[int] = []
    for idx, parent in enumerate(parents):
        if parent is None:
            roots.append(idx)
        else:
            children[parent].append(idx)

    if not roots:
        roots = list(range(n))

    backend = "geometric"
    # Top-level windows are ordered geometrically, not by the document
    # predictor. The order a model is asked to reproduce has to be derivable
    # from the image, and a reading-order model built for non-overlapping page
    # content cannot tell anyone which of three side-by-side windows comes
    # first - the information it uses is not on the screen. Top-left with a row
    # band is a pure function of the layout, so it can be inferred.
    roots = geometric_reading_order(roots, elements)
    for idx in list(children.keys()):
        children[idx] = geometric_reading_order(children[idx], elements)

    order: List[int] = []
    visited: set[int] = set()

    def dfs(idx: int) -> None:
        if idx in visited:
            return
        visited.add(idx)
        order.append(idx)
        for child in children.get(idx, []):
            dfs(child)

    for root in roots:
        dfs(root)

    if len(visited) != n:
        remaining = [idx for idx in range(n) if idx not in visited]
        order.extend(geometric_reading_order(remaining, elements))

    return order, backend


def _resolve_parent_indices(elements: List[Dict[str, Any]]) -> List[Optional[int]]:
    """Resolve effective parent links for the current export variant.

    Preference order:
    1. `parent_index`
    2. `_source_parent_dom_index` -> `_source_dom_index` mapping
    3. `_parent_dom_index` -> `_dom_index` mapping
    """
    n = len(elements)
    by_dom: Dict[int, int] = {}
    by_source_dom: Dict[int, int] = {}
    for idx, elem in enumerate(elements):
        dom = elem.get("_dom_index")
        if isinstance(dom, int):
            by_dom[dom] = idx
        source_dom = elem.get("_source_dom_index")
        if isinstance(source_dom, int):
            by_source_dom[source_dom] = idx

    parents: List[Optional[int]] = [None] * n
    for idx, elem in enumerate(elements):
        parent: Optional[int] = None

        direct_parent = elem.get("parent_index")
        if isinstance(direct_parent, int) and 0 <= direct_parent < n and direct_parent != idx:
            parent = direct_parent
        else:
            source_parent = elem.get("_source_parent_dom_index")
            if isinstance(source_parent, int):
                mapped = by_source_dom.get(source_parent)
                if mapped is not None and mapped != idx:
                    parent = mapped
            if parent is None:
                raw_parent = elem.get("_parent_dom_index")
                if isinstance(raw_parent, int):
                    mapped = by_dom.get(raw_parent)
                    if mapped is not None and mapped != idx:
                        parent = mapped

        parents[idx] = parent

    return parents


#: Two boxes belong to the same row when their vertical spans overlap by at
#: least this fraction of the shorter one. Overlap rather than a fixed pixel
#: band, because the same rule then works for 17px labels and 900px windows
#: without a magic number per scale.
ROW_OVERLAP = 0.5


def _rect_of(elem: Dict[str, Any]) -> Tuple[float, float, float, float]:
    rect = elem.get("rect") or {}
    try:
        x = float(rect.get("x", 0) or 0)
        y = float(rect.get("y", 0) or 0)
        w = float(rect.get("w", 0) or 0)
        h = float(rect.get("h", 0) or 0)
    except (TypeError, ValueError):
        return 0.0, 0.0, 0.0, 0.0
    return x, y, max(w, 0.0), max(h, 0.0)


def geometric_reading_order(
    indices: List[int], elements: List[Dict[str, Any]]
) -> List[int]:
    """Order a sibling group the way a person reads a screen.

    Rows first, then left to right inside a row. Two boxes share a row when
    their vertical spans overlap - so a row of side-by-side windows reads left
    to right, and a window below them starts a new row - and ties are broken by
    x, then y, then the element's own index so the result is total and cannot
    depend on dict iteration.

    This replaced a document reading-order model (`docling_ibm_models`) that was
    used when importable and a geometric fallback otherwise. Two problems, and
    the second is the serious one:

    - It is the wrong instrument. It was trained on non-overlapping,
      column-flowing page content; desktop windows overlap and have no reading
      flow between them. Whatever it returns for three side-by-side windows is
      not something a reader - or a model looking at the pixels - could derive.
    - **The order depended on whether a package was installed.** Every capture
      to date used the fallback because the package is absent here, but adding
      it as a dependency would silently change the ground-truth ordering of
      every future capture, and two shards of one corpus could disagree. Order
      is part of the training target; it must not be a property of the
      environment.

    A single documented geometric rule is inferable from the image, identical
    everywhere, and needs no model to be loaded per sibling group.
    """
    if len(indices) <= 1:
        return list(indices)

    by_top = sorted(indices, key=lambda i: (_rect_of(elements[i])[1], _rect_of(elements[i])[0], i))

    rows: List[List[int]] = []
    for idx in by_top:
        _x, y, _w, h = _rect_of(elements[idx])
        bottom = y + h
        placed = False
        for row in rows:
            ry0 = min(_rect_of(elements[j])[1] for j in row)
            ry1 = max(_rect_of(elements[j])[1] + _rect_of(elements[j])[3] for j in row)
            overlap = min(bottom, ry1) - max(y, ry0)
            shorter = max(1.0, min(h, ry1 - ry0))
            if overlap / shorter >= ROW_OVERLAP:
                row.append(idx)
                placed = True
                break
        if not placed:
            rows.append([idx])

    order: List[int] = []
    for row in rows:
        order.extend(
            sorted(row, key=lambda i: (_rect_of(elements[i])[0], _rect_of(elements[i])[1], i))
        )
    return order
