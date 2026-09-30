"""Drop widgets the accessibility tree reports but the screen does not draw.

AT-SPI answers "does this widget exist and is it mapped", which is not the same
question as "can a person see it". The gap is not hypothetical: GTK3 draws
overlay scrollbars only while the pointer is near them, so in a headless capture
every scrolled window in the pool contributes a 16px-wide scroll bar box over
pixels that are uniformly the background colour. Measured across a 24-capture
batch, 18 of 32 such boxes were exactly that, spread over bluefish, thunar,
nautilus, zim, xarchiver, homebank, mousepad and file-roller - every GTK app
with a scrolled window.

Serializing them trains a model to predict controls that are not on the screen,
which is the definition of a false positive here.

The test is *flatness*, not edge detection. A gradient-based ink check reported
a Plank dock icon as blank - dark art on a dark dock, 24 distinct grey levels,
no adjacent-pixel step above the threshold - and acting on that would have
deleted a correct annotation. Nothing painted means uniform, so uniformity is
what gets measured.

Uniform is not sufficient on its own, because a solid colour swatch or a filled
progress bar is uniform *and* visible. What makes the invisible ones invisible
is that they match their surroundings, so a widget that is uniform but clearly
differs from the ring around it is kept. That guard is skipped for occluded
elements, whose ring is mostly the window covering them and therefore says
nothing about whether the widget itself was drawn.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Roles that cannot be both visible and blank. A button has a border or a
#: label, an icon has pixels, a scroll bar has a trough. Containers, table
#: cells, text bodies, separators and panels are all legitimately uniform and
#: are never considered here.
#: Roles that may legitimately be blank *when they hold something*, and are
#: only examined once they hold nothing. An empty grid cell draws no glyphs, no
#: border of its own and no icon, so its box sits on background: measured on one
#: HomeBank capture, 102 of 439 leaf elements - 23% - were text-less cells that
#: were completely flat, the narrow leading columns of every row and the value
#: columns of group rows. Cells that do draw something (a grid line, a warning
#: triangle) are not flat and are kept by the same test; of the 102, none had a
#: ring contrast above 11.5, so the swatch guard spares none of them either.
EMPTY_CAPABLE_ROLES = frozenset({"table cell"})

MUST_DRAW_ROLES = frozenset({
    "push button", "toggle button", "check box", "radio button",
    "check menu item", "radio menu item", "combo box", "menu item",
    "page tab", "icon", "image", "slider", "spin button", "scroll bar",
    "link", "tree item",
})

#: Uniformity thresholds. Real blanks measure std 0.00 over one grey value;
#: the softest true content observed measures std 0.32, and the dock icon that
#: must never be dropped measures 8.15.
BLANK_STD = 1.5
BLANK_RANGE = 4

#: Shrink each fragment before measuring: a widget flush against a window edge
#: includes a pixel or two of whatever is behind the window, and that boundary
#: step alone makes a flat scroll bar look like content.
EDGE_EROSION = 1

#: Fragments thinner than this after erosion carry too few pixels to judge.
MIN_MEASURE_SIDE = 4

#: How far outside the element to sample when asking "does this differ from its
#: surroundings", and the mean-grey difference above which it counts as visible.
#: Overlay scrollbars measure 0-14 against their background; a solid swatch on a
#: contrasting panel measures far higher.
RING_WIDTH = 4
RING_CONTRAST_MIN = 15.0


def _clip(rect: Any, width: int, height: int) -> Optional[Tuple[int, int, int, int]]:
    if not isinstance(rect, dict):
        return None
    try:
        x, y = int(rect.get("x", 0)), int(rect.get("y", 0))
        w, h = int(rect.get("w", 0)), int(rect.get("h", 0))
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(width, x + w), min(height, y + h)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def visible_boxes(elem: Dict[str, Any], width: int, height: int) -> List[Tuple[int, int, int, int]]:
    """On-screen rectangles the element claims to be showing.

    An empty fragment list means nothing is visible and must return empty - not
    fall back to the full rect, which would measure the pixels of whatever is
    covering the element and judge the element by them.
    """
    for key in ("visible_fragments", "_visible_fragments"):
        frags = elem.get(key)
        if isinstance(frags, list):
            boxes = [_clip(f, width, height) for f in frags]
            return [b for b in boxes if b is not None]
    box = _clip(elem.get("rect"), width, height)
    return [box] if box is not None else []


def _measure(gray, boxes: Sequence[Tuple[int, int, int, int]]):
    """(std, range) over the eroded fragments, or None if unmeasurable.

    The fragments are pooled rather than measured one at a time: a widget split
    by an overlapping window can have each piece uniform on its own while the
    pieces differ from each other, which is still something drawn.
    """
    import numpy as np

    parts = []
    for x0, y0, x1, y1 in boxes:
        ex0, ey0 = x0 + EDGE_EROSION, y0 + EDGE_EROSION
        ex1, ey1 = x1 - EDGE_EROSION, y1 - EDGE_EROSION
        if ex1 - ex0 < MIN_MEASURE_SIDE or ey1 - ey0 < MIN_MEASURE_SIDE:
            continue
        parts.append(gray[ey0:ey1, ex0:ex1].reshape(-1))
    if not parts:
        return None
    joined = np.concatenate(parts).astype(np.int32)
    return float(joined.std()), int(joined.max() - joined.min())


def _ring_contrast(gray, boxes: Sequence[Tuple[int, int, int, int]]) -> float:
    """Mean-grey difference between the element and the band around it."""
    import numpy as np

    height, width = gray.shape
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes)
    y1 = max(b[3] for b in boxes)
    rx0, ry0 = max(0, x0 - RING_WIDTH), max(0, y0 - RING_WIDTH)
    rx1, ry1 = min(width, x1 + RING_WIDTH), min(height, y1 + RING_WIDTH)
    outer = gray[ry0:ry1, rx0:rx1].astype(np.float64).copy()
    inner_vals = gray[y0:y1, x0:x1].astype(np.float64)
    if inner_vals.size == 0 or outer.size == 0:
        return 0.0
    outer[y0 - ry0:y1 - ry0, x0 - rx0:x1 - rx0] = np.nan
    if not np.isfinite(outer).any():
        return 0.0
    return float(abs(float(inner_vals.mean()) - float(np.nanmean(outer))))


def draws_content(elem: Dict[str, Any], gray) -> bool:
    """Does this element's visible area have anything painted in it?

    The same uniformity test `is_blank_widget` uses, without the role gate. It
    answers the question for elements the gate deliberately ignores - notably
    containers, which are allowed to be blank and are only interesting when
    they are not.
    """
    height, width = gray.shape
    boxes = visible_boxes(elem, width, height)
    if not boxes:
        return False
    measured = _measure(gray, boxes)
    if measured is None:
        return False
    std, rng = measured
    return not (std < BLANK_STD and rng <= BLANK_RANGE)


def is_blank_widget(elem: Dict[str, Any], gray) -> bool:
    """True if this element's visible pixels show nothing at all."""
    role = str(elem.get("role") or "").strip().lower()
    if role not in MUST_DRAW_ROLES:
        if role not in EMPTY_CAPABLE_ROLES:
            return False
        # Only once it is empty: a cell with text is judged by the text checks,
        # and a cell with an icon is not flat anyway.
        if str(elem.get("visible_text") or "").strip():
            return False
        if elem.get("_children_dom_indices") or elem.get("children_indices"):
            return False
    height, width = gray.shape
    boxes = visible_boxes(elem, width, height)
    if not boxes:
        return False
    measured = _measure(gray, boxes)
    if measured is None:
        return False
    std, rng = measured
    if not (std < BLANK_STD and rng <= BLANK_RANGE):
        return False
    if elem.get("is_occluded"):
        # The ring is mostly whatever is covering the element, so it cannot
        # speak to whether the widget itself drew anything. The visible part is
        # uniform, which is all we need.
        return True
    return _ring_contrast(gray, boxes) < RING_CONTRAST_MIN


def suppress_blank_widgets(elements: List[Dict[str, Any]], gray) -> Dict[str, Any]:
    """Remove widgets with nothing drawn, in place. Returns a meta dict.

    Child-bearing elements are kept even when blank, exactly as
    `drop_fully_hidden_elements` does: a visible child still needs its parent
    in the nesting. Dangling child references are pruned afterwards.
    """
    meta: Dict[str, Any] = {
        "num_elements_in": len(elements),
        "num_dropped": 0,
        "by_role": {},
        "num_elements_out": len(elements),
    }
    if not elements or gray is None:
        return meta

    keep: List[Dict[str, Any]] = []
    dropped_indices = set()
    for elem in elements:
        has_children = bool(
            elem.get("children_indices") or elem.get("_children_dom_indices")
        )
        if not has_children and is_blank_widget(elem, gray):
            role = str(elem.get("role") or "").strip().lower()
            meta["by_role"][role] = meta["by_role"].get(role, 0) + 1
            meta["num_dropped"] += 1
            idx = elem.get("_dom_index")
            if isinstance(idx, int):
                dropped_indices.add(idx)
            continue
        keep.append(elem)

    if meta["num_dropped"]:
        elements[:] = keep
        for elem in elements:
            for key in ("children_indices", "_children_dom_indices"):
                refs = elem.get(key)
                if isinstance(refs, list) and refs:
                    elem[key] = [r for r in refs if r not in dropped_indices]
    meta["num_elements_out"] = len(elements)
    return meta


#: Window-like roles. A window that draws nothing at all is not on screen, and
#: nothing inside it can be either.
WINDOW_ROLES = frozenset({"frame", "window", "dialog", "alert", "file chooser"})


def drop_undrawn_windows(elements: List[Dict[str, Any]], gray) -> Dict[str, Any]:
    """Remove windows the screen never drew, and everything inside them.

    An application can keep a dialog in its accessibility tree while it is not
    mapped. Measured: a mousepad "Go To" dialog reported 292x160 at 816,571,
    unoccluded, with a title bar and two labels - and every pixel of that region
    was uniform white. It serialized as a `<window>` with a title and two texts
    that no reader could see.

    The whole rect is required to be flat, not merely part of it, so a real
    window with a plain content area is never mistaken for one of these: its
    border, title bar and controls always vary. Because the test is on the
    window, its contents follow without needing their own evidence - the labels
    inside this one were already known to have no ink, but the boxes remained.
    """
    meta = {"num_windows_dropped": 0, "num_elements_dropped": 0, "titles": []}
    if gray is None or not elements:
        return meta

    height, width = gray.shape
    undrawn = []
    for elem in elements:
        if str(elem.get("role") or "").strip().lower() not in WINDOW_ROLES:
            continue
        boxes = visible_boxes(elem, width, height)
        if not boxes:
            continue
        measured = _measure(gray, boxes)
        if measured is None:
            continue
        std, rng = measured
        if std < BLANK_STD and rng <= BLANK_RANGE:
            undrawn.append(elem)

    if not undrawn:
        return meta

    regions = []
    for elem in undrawn:
        rect = elem.get("rect") or {}
        try:
            x, y = int(rect["x"]), int(rect["y"])
            w, h = int(rect["w"]), int(rect["h"])
        except (KeyError, TypeError, ValueError):
            continue
        regions.append((x, y, x + w, y + h))
        meta["titles"].append(str(elem.get("name") or "")[:40])

    def inside_undrawn(elem):
        rect = elem.get("rect") or {}
        try:
            x, y = int(rect["x"]), int(rect["y"])
            w, h = int(rect["w"]), int(rect["h"])
        except (KeyError, TypeError, ValueError):
            return False
        for rx0, ry0, rx1, ry1 in regions:
            if x >= rx0 - 2 and y >= ry0 - 2 and x + w <= rx1 + 2 and y + h <= ry1 + 2:
                return True
        return False

    before = len(elements)
    elements[:] = [e for e in elements if not inside_undrawn(e)]
    meta["num_windows_dropped"] = len(undrawn)
    meta["num_elements_dropped"] = before - len(elements)
    return meta
