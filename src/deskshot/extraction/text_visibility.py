"""Conservative visible-text extraction for partially occluded text nodes."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi

from deskshot.extraction.visibility_fragments import (
    clone_rect,
    intersect_rect,
    is_valid_rect,
    normalize_fragments,
    rect_area,
    set_visible_fragments,
    union_rect,
)


Rect = Dict[str, int]

OBJECT_REPLACEMENT = "\ufffc"
VISIBLE_CHAR_RATIO_THRESHOLD = 0.5
MAX_TEXT_CHARS = 4096
# Confidence is the fraction of visible ink the corrected glyph boxes account
# for. Below this, the character-level result is not trustworthy and no visible
# text is emitted rather than a guess.
CHAR_GEOMETRY_MIN_CONFIDENCE = 0.90

#: A line clipped below this fraction of its own height cannot be read, so the
#: occluder has reached the text no matter where the glyph boxes fall. Measured
#: on HomeBank rows sliced to 5px of a 17px line - 29%.
MIN_READABLE_LINE_FRACTION = 0.6

#: A box that claims text must have ink behind it, at roughly this much per
#: character. Same expectation the pixel audit scores with, so the pipeline
#: refuses what the audit would flag rather than emitting it and being told.
#: Deliberately generous - thin punctuation is exactly the case a stricter
#: number gets wrong, and a calculator's "." was once called a phantom this way.
MIN_INK_PER_CHAR = 1.0
MIN_INK_ABSOLUTE = 4
# Documents are held to a lower bar than cells, because the score means
# something different for them. No shift is being chosen - the reported boxes
# are simply being confirmed - so the risk the threshold guards against is
# absent. And a text view legitimately draws ink no glyph box accounts for: a
# caret, a selection band, the gutter between the margin and the first column.
# Measured across a 30-line editor, the per-line median settled at 0.84-0.89
# with the boxes provably correct, so 0.90 rejected geometry that was right.
MULTILINE_GEOMETRY_MIN_CONFIDENCE = 0.75

# Largest displacement between AT-SPI's character geometry and the rendered
# glyphs measured anywhere in this repo: a HomeBank amount cell reported
# 215..272 for glyphs drawn at 241..298, i.e. 26px. Rounded up, this is how far
# the reported boxes are allowed to be wrong when asking whether an occluder
# reaches an element's text.
MAX_CHAR_GEOMETRY_ERROR_PX = 32

# Narrowest average advance a rendered character has in this pool. Measured on
# the elements whose rect *is* their glyph run (text-interface geometry,
# unoccluded): the per-app minimum is 6.1-6.7px for every GTK app, and only
# proportional web fonts at small sizes reach 3-4px. 5.0 is therefore a floor
# no real rendering crosses, which is what makes "this box is narrower than
# len(text) * 5" a proof that the string is *not* what is drawn there.
MIN_NAME_CHAR_ADVANCE_PX = 5.0

# A single line of UI text in this pool is 13-19px tall including padding.
# Below MIN nothing can be read; above MAX the box is a window, a picture or a
# dock tile rather than a label, and an accessible name is a single line.
MIN_NAME_BOX_HEIGHT_PX = 10
MAX_NAME_BOX_HEIGHT_PX = 48


@dataclass
class LineRange:
    start: int
    end: int
    text: str
    rect: Rect


@dataclass
class TextGeometry:
    text: str
    lines: List[LineRange]
    char_extents: Dict[int, Rect]


def _iter_line_ranges(text_iface: Any, count: int) -> Iterable[Tuple[int, int, str]]:
    offset = 0
    seen: set[int] = set()
    while 0 <= offset < count and offset not in seen:
        seen.add(offset)
        try:
            text_range = text_iface.get_string_at_offset(offset, Atspi.TextGranularity.LINE)
        except Exception:
            text_range = text_iface.get_text_at_offset(offset, Atspi.TextBoundaryType.LINE_START)
        start = int(getattr(text_range, "start_offset", -1))
        end = int(getattr(text_range, "end_offset", -1))
        content = str(getattr(text_range, "content", "") or "")
        if start < 0 or end <= start:
            offset += 1
            continue
        yield start, end, content
        offset = end


def _collect_text_geometry(node: Any) -> Optional[TextGeometry]:
    try:
        text_iface = node.get_text_iface()
    except Exception:
        return None
    if text_iface is None:
        return None

    try:
        count = int(text_iface.get_character_count())
    except Exception:
        return None
    if count <= 0 or count > MAX_TEXT_CHARS:
        return None

    try:
        full_text = text_iface.get_text(0, count)
    except Exception:
        return None
    if not full_text:
        return None

    lines: List[LineRange] = []
    char_extents: Dict[int, Rect] = {}
    for start, end, content in _iter_line_ranges(text_iface, count):
        try:
            ext = text_iface.get_range_extents(start, end, Atspi.CoordType.SCREEN)
            rect = {"x": int(ext.x), "y": int(ext.y), "w": int(ext.width), "h": int(ext.height)}
        except Exception:
            rect = {"x": 0, "y": 0, "w": 0, "h": 0}
        lines.append(LineRange(start=start, end=end, text=content, rect=rect))

        for offset in range(start, end):
            if offset in char_extents:
                continue
            try:
                ext = text_iface.get_character_extents(offset, Atspi.CoordType.SCREEN)
            except Exception:
                continue
            char_extents[offset] = {
                "x": int(ext.x),
                "y": int(ext.y),
                "w": int(ext.width),
                "h": int(ext.height),
            }

    if not lines:
        return None
    return TextGeometry(text=full_text, lines=lines, char_extents=char_extents)


def _ink_columns(gray: Any, y0: int, y1: int, width: int, *, dark: int = 170):
    """Boolean per-x mask of glyph ink in a horizontal band of the screenshot."""
    import numpy as np

    y0 = max(0, y0)
    y1 = min(gray.shape[0], y1)
    if y1 <= y0:
        return np.zeros(width, dtype=bool)
    band = gray[y0:y1, :width]
    return (band < dark).any(axis=0)


def make_ink_oracle(gray: Any, *, dark: int = 170, min_ratio: float = 0.02) -> Any:
    """Return a predicate telling whether a glyph box actually contains ink."""
    if gray is None:
        return None

    def _has_ink(rect: Rect) -> bool:
        x0 = max(0, int(rect["x"]))
        x1 = min(int(gray.shape[1]), x0 + max(1, int(rect["w"])))
        y0 = max(0, int(rect["y"]))
        y1 = min(int(gray.shape[0]), y0 + max(1, int(rect["h"])))
        if x1 <= x0 or y1 <= y0:
            return False
        patch = gray[y0:y1, x0:x1]
        return bool((patch < dark).sum() >= max(1, int(patch.size * min_ratio)))

    return _has_ink


def repair_char_advances(geometry: "TextGeometry") -> "TextGeometry":
    """Rebuild degenerate per-character boxes from the line's own width.

    GTK cell renderers report character extents that collapse once the text
    reaches the width the widget thinks it has: measured on `0.42 BTC`, the
    trailing `T` and `C` both came back as zero-width boxes at the line's right
    edge, and a spurious 12px gap sat between `4` and `2`. Zero-width boxes can
    never be judged visible, so the tail of an occluded string was silently
    unrecoverable.

    The line's total width is right even when the individual boxes are not, so
    the advances are rebuilt: keep the widths that look sane, split the leftover
    evenly across the collapsed ones, and lay the run out contiguously from the
    line origin. Positioning is still corrected separately against the
    screenshot ink.
    """
    if not geometry.lines:
        return geometry

    repaired: Dict[int, Rect] = dict(geometry.char_extents)
    changed = False

    for line in geometry.lines:
        offsets = [o for o in range(line.start, line.end) if o in geometry.char_extents]
        if not offsets:
            continue
        line_w = int(line.rect.get("w", 0))
        if line_w <= 0:
            continue

        widths = {o: int(geometry.char_extents[o].get("w", 0)) for o in offsets}
        collapsed = [o for o in offsets if widths[o] <= 0]
        known = sum(w for w in widths.values() if w > 0)
        spans_contiguously = True
        cursor = None
        for o in offsets:
            box = geometry.char_extents[o]
            if cursor is not None and abs(int(box["x"]) - cursor) > 1:
                spans_contiguously = False
                break
            cursor = int(box["x"]) + max(0, int(box.get("w", 0)))
        if not collapsed and spans_contiguously:
            continue

        if collapsed:
            leftover = max(0, line_w - known)
            share = leftover / len(collapsed)
            if share < 1.0:
                # Nothing sensible to distribute; leave this line alone rather
                # than fabricate advances.
                continue
            for o in collapsed:
                widths[o] = int(round(share))

        x = int(line.rect.get("x", 0))
        y = int(line.rect.get("y", geometry.char_extents[offsets[0]].get("y", 0)))
        h = int(line.rect.get("h", geometry.char_extents[offsets[0]].get("h", 0)))
        for o in offsets:
            repaired[o] = {"x": x, "y": y, "w": widths[o], "h": h}
            x += widths[o]
        changed = True

    if not changed:
        return geometry
    return TextGeometry(text=geometry.text, lines=list(geometry.lines), char_extents=repaired)


def estimate_char_extent_offset(
    gray: Any,
    char_rects: List[Rect],
    *,
    cell_rect: Rect,
    visible_regions: List[Rect],
    search: int = 60,
    min_iou: float = 0.60,
) -> Tuple[int, float]:
    """Find the x-offset that puts AT-SPI's glyph boxes onto the actual ink.

    GTK cell renderers can report text geometry offset from what they draw: in a
    measured HomeBank amount cell the component extents were 212..302 and the
    rendered glyphs 241..298, while both `get_range_extents` and
    `get_character_extents` reported 215..272. Clipping those boxes against the
    visible region then selected the wrong characters - " B" where the screen
    shows "2 BTC".

    The glyph *widths* are right, only the origin is wrong, so the offset is
    recovered by anchoring: whichever edge of the text is not hidden must line up
    with the outermost ink column on that side. Anchoring beats sliding for the
    best overlap, which lands a few pixels out and shifts the character selection
    by half a glyph.

    Two things keep it honest:

    - the visible region must come from the element's component rect minus its
      occluders, never from the text-derived fragments, or the estimate scores
      against the very geometry it is trying to correct
    - the shifted text must stay inside its own cell and the resulting overlap
      must clear `min_iou`; otherwise 0 is returned and the original geometry is
      kept

    Returns `(offset_px, confidence)`. Confidence is the overlap between the
    corrected glyph columns and the real ink inside the visible region; callers
    use it to decide whether the character-level result can be trusted at all.
    """
    import numpy as np

    if not char_rects or gray is None or not is_valid_rect(cell_rect):
        return 0, 0.0
    visible = [r for r in visible_regions if is_valid_rect(r)]
    if not visible:
        return 0, 0.0

    width = int(gray.shape[1])
    text_lo = min(r["x"] for r in char_rects)
    text_hi = max(r["x"] + r["w"] for r in char_rects)
    y0 = min(r["y"] for r in char_rects)
    y1 = max(r["y"] + r["h"] for r in char_rects)

    ink = _ink_columns(gray, y0, y1, width)
    vis = np.zeros(width, dtype=bool)
    for r in visible:
        vis[max(0, r["x"]):max(0, r["x"] + r["w"])] = True
    visible_ink = np.flatnonzero(ink & vis)
    if visible_ink.size == 0:
        return 0, 0.0

    cell_lo = int(cell_rect["x"])
    cell_hi = cell_lo + int(cell_rect["w"])
    vis_cols = np.flatnonzero(vis)
    vis_lo, vis_hi = int(vis_cols[0]), int(vis_cols[-1]) + 1

    # Anchor on the side that is not hidden. More of the cell surviving on the
    # right means the occluder is on the left, so the text's right edge is the
    # trustworthy landmark, and vice versa.
    right_gap = cell_hi - vis_hi
    left_gap = vis_lo - cell_lo
    if left_gap >= right_gap:
        candidates = [int(visible_ink[-1]) + 1 - text_hi]
    else:
        candidates = [int(visible_ink[0]) - text_lo]
    candidates.append(0)

    base = np.zeros(width, dtype=bool)
    for r in char_rects:
        base[max(0, r["x"]):max(0, r["x"] + r["w"])] = True
    actual = ink & vis

    ink_total = int(actual.sum())

    def _explained(dx: int) -> float:
        """Fraction of the visible ink that the shifted glyph boxes account for.

        Not IoU: glyph boxes are advances and include side bearings, so even a
        perfect alignment overlaps the ink only partially and an IoU threshold
        rejects correct results. What must hold is that every inked column falls
        inside some predicted glyph - position itself comes from anchoring.
        """
        # The containment guard rejects *shifts* that would push glyphs outside
        # the widget. It must not veto dx=0, which is not a shift at all.
        #
        # A cell renderer clips its text to the cell, so overflow there means a
        # bad correction. A text view does not: AT-SPI reports true extents for
        # characters scrolled past the edge, so the text block legitimately
        # sticks out of the widget and every candidate - including the identity -
        # was vetoed. `_explained` then returned -1 for all of them, the caller
        # read the resulting 0.0 confidence as "the glyph boxes do not match the
        # ink", and dropped the text of every multi-line element that was
        # occluded at all. Measured on one scene: bluefish's 30-line editor and
        # mousepad's 6-line document both scored conf=0.000 while their
        # single-line siblings scored 1.000, and both serialized as empty.
        if dx and (text_lo + dx < cell_lo - 2 or text_hi + dx > cell_hi + 2):
            return -1.0
        shifted = np.roll(base, dx) & vis
        if not shifted.any() or ink_total == 0:
            return -1.0
        return float((shifted & actual).sum()) / float(ink_total)

    best_dx, best_score = 0, -1.0
    for dx in candidates:
        if abs(dx) > search:
            continue
        score = _explained(dx)
        if score > best_score:
            best_dx, best_score = dx, score

    if best_score < min_iou:
        return 0, max(0.0, best_score)
    return best_dx, best_score


#: Roles that can legitimately be the box a reader would draw around a document.
#: A window or frame is too big to be an answer - re-homing onto one would claim
#: the whole app draws the text.
REHOME_CONTAINER_ROLES = frozenset({
    "section", "scroll pane", "viewport", "panel", "document web", "document",
    "document frame", "text", "split pane", "filler", "group",
})


def rehome_overflowing_text(
    elem: Dict[str, Any],
    text: str,
    hierarchy: List[Dict[str, Any]],
) -> Optional[Rect]:
    """Find the box that actually shows `text`, when the element's own does not.

    Some apps mirror the visible page of a document into a small offscreen
    control for screen readers. VS Code does exactly this: the editor's text is
    correct and complete - the ten lines `editor.accessibilityPageSize` exposes -
    but it is attached to a 523x19 entry that is not where the code is drawn.
    Dropping the text loses content the screen is showing; keeping it on that
    rect pairs ten lines with a one-line box. Neither is needed, because the box
    a reader would draw is in the tree: the editor viewport.

    Resolved geometrically rather than by walking parent links, because the
    three element lists are separately indexed - a leaf's `_dom_index` is
    leaf-local and its `_parent_dom_index` points into a different numbering, so
    an ancestor walk silently resolves to the wrong node. Smallest container of
    the same app that encloses the mirror and is tall enough to render the lines
    lands on the viewport rather than the window, without depending on any of
    that bookkeeping.

    Returns the rect to adopt, or None when nothing qualifies - the caller then
    withholds the text as before.
    """
    lines = text.count("\n") + 1
    needed_h = lines * MIN_RENDERED_LINE_HEIGHT
    own = elem.get("rect") or {}
    if not is_valid_rect(own):
        return None
    app = elem.get("app_name")

    ox0, oy0 = int(own["x"]), int(own["y"])
    ox1, oy1 = ox0 + int(own["w"]), oy0 + int(own["h"])

    best: Optional[Rect] = None
    best_area: Optional[int] = None
    for node in hierarchy:
        if node is elem or node.get("app_name") != app:
            continue
        role = str(node.get("role") or "").strip().lower()
        if role not in REHOME_CONTAINER_ROLES:
            continue
        rect = node.get("rect") or {}
        if not is_valid_rect(rect):
            continue
        if int(rect["h"]) < needed_h or int(rect["w"]) < int(own["w"]):
            continue
        x0, y0 = int(rect["x"]), int(rect["y"])
        x1, y1 = x0 + int(rect["w"]), y0 + int(rect["h"])
        if not (x0 <= ox0 and y0 <= oy0 and x1 >= ox1 and y1 >= oy1):
            continue
        area = rect_area(rect)
        if best_area is None or area < best_area:
            best, best_area = clone_rect(rect), area
    return best


def validate_char_extents_by_line(
    gray: Any,
    geometry: "TextGeometry",
    *,
    cell_rect: Rect,
    visible_regions: List[Rect],
) -> float:
    """How well multi-line glyph boxes already explain the ink, judged per line.

    No offset is searched for. The correction machinery exists for GTK *cell
    renderers*, which misreport where they drew a single line of text; a text
    view reports character extents that are exact - bluefish's came back as
    precise 8x17 monospace cells when queried live. So the question for a
    document is not "what shift repairs this" but "are the boxes already right",
    and searching for a shift actively harms:

    - anchoring lines up the first glyph box with the first inked column, so
      every indented line reads its own indent as a positional error (twenty
      lines of one HTML file each reported -17 or -18px, two monospace cells)
    - once a window narrows to a few visible characters per line, the anchor has
      almost no ink to work with and the estimates scatter from 0 to -50

    Either way the shift lands the boxes on *some* ink and reports high
    confidence, so the wrong characters get selected convincingly. Scoring the
    identity instead cannot select the wrong characters: it either confirms the
    reported geometry or fails and the text is withheld.
    """
    import numpy as np

    if not geometry.lines:
        return 0.0
    visible = [r for r in visible_regions if is_valid_rect(r)]
    if not visible:
        return 0.0

    scores: List[float] = []
    for line in geometry.lines:
        rects = [
            geometry.char_extents[o]
            for o in range(line.start, line.end)
            if o in geometry.char_extents
            and is_valid_rect(geometry.char_extents[o])
            and o < len(geometry.text)
            and not geometry.text[o].isspace()
        ]
        if not rects:
            continue
        if not any(intersect_rect(r, v) for r in rects for v in visible):
            # Nothing of this line is on screen, so it is not evidence either
            # way. Counting it as a failure would sink a document whose visible
            # lines are all fine.
            continue
        scores.append(_explained_at_origin(gray, rects, visible_regions=visible))

    if not scores:
        return 0.0
    if os.environ.get("DESKSHOT_DEBUG_TEXT_GEOMETRY"):
        print(
            f"[textgeom-lines] n={len(scores)} scores={[round(v, 2) for v in scores]}",
            file=sys.stderr,
        )
    return float(np.median(scores))


def _explained_at_origin(
    gray: Any, char_rects: List[Rect], *, visible_regions: List[Rect]
) -> float:
    """Fraction of the visible ink in one line's band that its glyph boxes cover."""
    import numpy as np

    if not char_rects or gray is None:
        return 0.0
    width = int(gray.shape[1])
    y0 = min(r["y"] for r in char_rects)
    y1 = max(r["y"] + r["h"] for r in char_rects)

    ink = _ink_columns(gray, y0, y1, width)
    vis = np.zeros(width, dtype=bool)
    for r in visible_regions:
        vis[max(0, r["x"]):max(0, r["x"] + r["w"])] = True
    actual = ink & vis
    total = int(actual.sum())
    if total == 0:
        return 0.0

    boxes = np.zeros(width, dtype=bool)
    for r in char_rects:
        boxes[max(0, r["x"]):max(0, r["x"] + r["w"])] = True
    return float((boxes & actual).sum()) / float(total)


def shift_char_extents(geometry: "TextGeometry", dx: int) -> "TextGeometry":
    """Return a copy of `geometry` with all glyph and line boxes shifted by dx."""
    if not dx:
        return geometry
    return TextGeometry(
        text=geometry.text,
        lines=[
            LineRange(
                start=l.start,
                end=l.end,
                text=l.text,
                rect={**l.rect, "x": int(l.rect["x"]) + dx},
            )
            for l in geometry.lines
        ],
        char_extents={
            o: {**r, "x": int(r["x"]) + dx} for o, r in geometry.char_extents.items()
        },
    )


def _resolve_app(app_name: str) -> Any:
    desktop = Atspi.get_desktop(0)
    if desktop is None:
        return None
    for i in range(desktop.get_child_count()):
        child = desktop.get_child_at_index(i)
        if child is None:
            continue
        name = child.get_name() or ""
        if app_name.lower() in name.lower():
            return child
    return None


def _resolve_node(app_name: str, path: List[int]) -> Any:
    node = _resolve_app(app_name)
    if node is None:
        return None
    for index in path:
        try:
            node = node.get_child_at_index(int(index))
        except Exception:
            return None
        if node is None:
            return None
    return node


#: Smallest plausible rendered line height. Real UI text in this pool is
#: 14-19px per line; 8 leaves a wide margin so only impossible cases are caught.
MIN_RENDERED_LINE_HEIGHT = 8


def text_overflows_box(text: str, rect: Rect) -> bool:
    """True if `text` has more lines than `rect` is tall enough to render.

    Single-line text is never flagged: a long string can legitimately be
    ellipsized or scrolled horizontally within a short box, and deciding that
    from width alone needs a font metric we do not have. Multiple lines in a
    box too short to stack them is unambiguous.
    """
    if not text:
        return False
    lines = text.count("\n") + 1
    if lines <= 1:
        return False
    try:
        height = int(rect.get("h", 0) or 0)
    except (TypeError, ValueError):
        return False
    return 0 < height < lines * MIN_RENDERED_LINE_HEIGHT


def _fragments_cover_rect(rect: Rect, fragments: List[Rect], *, threshold: float = 0.98) -> bool:
    if not is_valid_rect(rect):
        return False
    covered = 0
    for frag in fragments:
        inter = intersect_rect(rect, frag)
        if inter is not None:
            covered += rect_area(inter)
    area = rect_area(rect)
    return area > 0 and (covered / area) >= threshold


def _char_visible(ext: Rect, fragments: List[Rect], *, threshold: float = VISIBLE_CHAR_RATIO_THRESHOLD) -> bool:
    area = rect_area(ext)
    if area <= 0:
        return False
    best_ratio = 0.0
    for frag in fragments:
        inter = intersect_rect(ext, frag)
        if inter is None:
            continue
        best_ratio = max(best_ratio, rect_area(inter) / area)
        if best_ratio >= threshold:
            return True
    return False


def _same_rect(a: Rect, b: Rect) -> bool:
    return (
        int(a.get("x", 0)) == int(b.get("x", 0))
        and int(a.get("y", 0)) == int(b.get("y", 0))
        and int(a.get("w", 0)) == int(b.get("w", 0))
        and int(a.get("h", 0)) == int(b.get("h", 0))
    )


# Marker for text removed by occlusion. Deliberately a dedicated token rather
# than "..." or "…": UI text uses literal ellipses constantly ("Save As...",
# "Open Recent...") and toolkits insert "…" themselves when a label does not fit
# its widget. Reusing them would make a real ellipsis indistinguishable from
# content hidden by another window, which is exactly the distinction this marks.
TEXT_CLIP_MARKER = "<occluded/>"


def render_clip_chunks(chunks: List[Tuple[str, str]], *, marker: str) -> str:
    """Render clip chunks, substituting `marker` for each gap.

    Gaps are dropped entirely when nothing was visible, so a fully hidden element
    never serializes to a lone marker.
    """
    if not any(kind == "text" for kind, _ in chunks):
        return ""
    return "".join(marker if kind == "gap" else value for kind, value in chunks)


def _clip_chunks(
    geometry: TextGeometry,
    fragments: List[Rect],
    *,
    has_ink: Optional[Any] = None,
) -> List[Tuple[str, str]]:
    """Split the text into visible runs and the gaps where glyphs were hidden.

    Returns ``("text", s)`` and ``("gap", "")`` chunks in reading order. A gap is
    recorded only when a non-whitespace glyph was dropped: losing a space to an
    overlapping window costs no content, so marking it would be noise.

    When `has_ink` is given, a non-whitespace glyph also has to actually show ink
    in the screenshot to count as visible. Geometry alone puts the character
    straddling an occlusion edge on the wrong side by up to a glyph; the pixels
    settle it. Whitespace is exempt, having no ink to find.
    """
    chunks: List[Tuple[str, str]] = []

    def add_gap() -> None:
        if chunks and chunks[-1][0] == "gap":
            return
        chunks.append(("gap", ""))

    def add_text(value: str) -> None:
        if not value:
            return
        if chunks and chunks[-1][0] == "text":
            chunks[-1] = ("text", chunks[-1][1] + value)
        else:
            chunks.append(("text", value))

    for line in geometry.lines:
        line_frags = normalize_fragments(
            [
                inter
                for frag in fragments
                if (inter := intersect_rect(line.rect, frag)) is not None
            ]
        )
        if not line_frags:
            if any(
                not ch.isspace() and ch != OBJECT_REPLACEMENT
                for ch in geometry.text[line.start:line.end]
            ):
                add_gap()
            continue
        if _fragments_cover_rect(line.rect, line_frags):
            add_text(line.text)
            continue

        kept_non_newline = False
        line_text = geometry.text[line.start:line.end]
        for offset in range(line.start, line.end):
            if offset >= len(geometry.text):
                break
            ch = geometry.text[offset]
            if ch == OBJECT_REPLACEMENT:
                continue
            if ch in "\r\n":
                continue
            ext = geometry.char_extents.get(offset)
            if ext is None:
                continue
            visible = _char_visible(ext, line_frags)
            if visible and has_ink is not None and not ch.isspace():
                visible = has_ink(ext)
            if visible:
                add_text(ch)
                if not ch.isspace():
                    kept_non_newline = True
            elif not ch.isspace():
                add_gap()

        if kept_non_newline and line_text.endswith("\n"):
            add_text("\n")

    return chunks


def _clip_text_with_geometry(geometry: TextGeometry, fragments: List[Rect]) -> str:
    return render_clip_chunks(_clip_chunks(geometry, fragments), marker="")


def _geometry_char_fragments(
    geometry: TextGeometry,
    *,
    visible_regions: Optional[List[Rect]] = None,
) -> List[Rect]:
    rects: List[Rect] = []
    for offset, ch in enumerate(geometry.text):
        if ch in "\r\n" or ch == OBJECT_REPLACEMENT:
            continue
        ext = geometry.char_extents.get(offset)
        if ext is None or rect_area(ext) <= 0:
            continue
        if visible_regions is not None and not _char_visible(ext, visible_regions):
            continue
        rects.append(ext)

    if rects:
        return normalize_fragments(rects)

    line_rects: List[Rect] = []
    for line in geometry.lines:
        if not is_valid_rect(line.rect):
            continue
        if visible_regions is None:
            line_rects.append(line.rect)
            continue
        intersections = [
            inter
            for frag in visible_regions
            if (inter := intersect_rect(line.rect, frag)) is not None
        ]
        line_rects.extend(intersections)
    return normalize_fragments(line_rects)


def _same_fragment_sets(a: List[Rect], b: List[Rect]) -> bool:
    norm_a = normalize_fragments(a)
    norm_b = normalize_fragments(b)
    if len(norm_a) != len(norm_b):
        return False
    return all(_same_rect(left, right) for left, right in zip(norm_a, norm_b))


def name_can_be_rendered(text: str, rect: Rect) -> bool:
    """Could this box be showing `text` as glyphs at all?

    An accessible name is not a statement about pixels. GTK and Electron use it
    for the tooltip of an icon-only button, for the alt text of a picture, for a
    window's title and for a dock tile's app name - none of which draw the
    string inside the element's own box. Measured on v224, 1513 elements
    serialized a name as `visible_text`; the smallest of them were a 20x20
    "Match Case (Alt+C)" check box, a 48x48 launcher carrying "Calculator\\nPerform
    arithmetic..." and a 199x24 search field whose name is "Search: Type Search
    Term and press Enter to search" while the screen draws "Search".

    So the name is admitted as rendered text only where it *could* be rendered.
    This is a proof of absence, not of presence: a box big enough does not mean
    the glyphs are there, and short names in square icon buttons ("Back" in
    32x28) still pass. It removes the cases that are impossible, which are the
    ones that put a sentence into ground truth over a 20px square.

    Multi-line names are judged on their longest line, since that is what has to
    fit; the height test then rejects the box if it cannot stack the lines.
    """
    stripped = text.strip()
    if not stripped:
        return False
    if not is_valid_rect(rect):
        # No geometry to judge against; leave the decision to the caller rather
        # than invent one.
        return True
    lines = [line for line in stripped.splitlines() if line.strip()] or [stripped]
    width = int(rect["w"])
    height = int(rect["h"])
    if height < MIN_NAME_BOX_HEIGHT_PX:
        return False
    if height > MAX_NAME_BOX_HEIGHT_PX * max(1, len(lines)):
        return False
    if height < MIN_NAME_BOX_HEIGHT_PX * len(lines):
        return False
    longest = max(len(line.strip()) for line in lines)
    return width >= longest * MIN_NAME_CHAR_ADVANCE_PX



def box_has_ink_for_text(gray, boxes: List[Rect], text: str, *, dark: int = 170) -> bool:
    """Is there enough drawn on screen for this box to be showing this text?

    The last guard before a string is serialized. Relaxing when occlusion
    withholds text recovered a great deal of real content, but it also let
    through Chromium page tokens whose boxes are displaced into the gaps
    *between* glyphs - measured on a python.org listing, 22 `static` elements
    of one or two characters ("=", "[", ",") sitting on blank pixels while the
    text was drawn a few pixels away. Withheld text is a gap; text on the wrong
    box is a wrong answer, and this catches the second without reopening the
    first, because a correctly placed box always has its own glyphs behind it.
    """
    import numpy as np

    glyphs = [c for c in text if not c.isspace() and c != OBJECT_REPLACEMENT]
    if not glyphs or gray is None:
        return True
    needed = max(MIN_INK_ABSOLUTE, int(MIN_INK_PER_CHAR * len(glyphs)))
    total = 0
    height, width = gray.shape
    for box in boxes:
        if not is_valid_rect(box):
            continue
        x0, y0 = max(0, int(box["x"])), max(0, int(box["y"]))
        x1 = min(width, x0 + int(box["w"]))
        y1 = min(height, y0 + int(box["h"]))
        if x1 <= x0 or y1 <= y0:
            continue
        total += int((np.asarray(gray[y0:y1, x0:x1]) < dark).sum())
        if total >= needed:
            return True
    return total >= needed


def occlusion_reaches_text(
    geometry: "TextGeometry",
    *,
    source_rect: Rect,
    fragments: List[Rect],
    margin: int = MAX_CHAR_GEOMETRY_ERROR_PX,
) -> bool:
    """Does what covers this element cover any of its glyphs?

    Being partially occluded is what turns visible text into a character-level
    question, and when the reported glyph boxes do not line up with the ink
    there is no honest way to answer it. But most of the time the occluder never
    reaches the text at all: a heading whose box is 397px wide with 12px clipped
    off the right, holding "Latest News" in its first 100px, was withheld
    entirely at confidence 0.659 while every glyph of it was on screen.

    Ink cannot answer this - the element's own border, rule or background is ink
    too, and it does run to the boundary - so the glyph boxes have to. They are
    allowed to be wrong: every box is grown by `margin`, the largest
    box-vs-glyph displacement measured in this repo, before being tested, so the
    answer stays "yes, it reaches" for anything close to the cut.

    Returns True when the question is real (or cannot be settled), False when
    the occluder demonstrably misses every glyph.
    """
    import numpy as np

    boxes = [
        geometry.char_extents[offset]
        for offset, ch in enumerate(geometry.text)
        if offset in geometry.char_extents
        and not ch.isspace()
        and ch != OBJECT_REPLACEMENT
        and is_valid_rect(geometry.char_extents[offset])
    ]
    if not boxes or not is_valid_rect(source_rect):
        return True
    visible = [f for f in fragments if is_valid_rect(f)]
    if not visible:
        return True

    sx0, sy0 = int(source_rect["x"]), int(source_rect["y"])
    sx1 = sx0 + int(source_rect["w"])
    sy1 = sy0 + int(source_rect["h"])
    if sx1 <= sx0 or sy1 <= sy0:
        return True

    # A line cut down to a sliver is unreadable however wide it still is.
    #
    # The mask below is built over `source_rect`, which for a viewport-clipped
    # cell is *already the clipped rect* - so a HomeBank row cut to 5px of its
    # 17px line has its glyph boxes clamped into those 5 rows, every one of them
    # covered, and the occluder is judged not to reach the text. 28 such cells
    # serialized their full text over a 5px box, which the pixel audit counts as
    # a phantom and a reader would call invented. Height is the honest test:
    # below this fraction of a line, nothing can be read off the screen.
    glyph_heights = [int(b["h"]) for b in boxes if int(b.get("h", 0)) > 0]
    if glyph_heights:
        line_h = sorted(glyph_heights)[len(glyph_heights) // 2]
        visible_h = max((int(f["h"]) for f in visible), default=0)
        if line_h > 0 and visible_h < line_h * MIN_READABLE_LINE_FRACTION:
            return True

    covered = np.zeros((sy1 - sy0, sx1 - sx0), dtype=bool)
    for frag in visible:
        fx0 = max(sx0, int(frag["x"]))
        fy0 = max(sy0, int(frag["y"]))
        fx1 = min(sx1, fx0 + int(frag["w"]) + (int(frag["x"]) - fx0))
        fy1 = min(sy1, fy0 + int(frag["h"]) + (int(frag["y"]) - fy0))
        if fx1 > fx0 and fy1 > fy0:
            covered[fy0 - sy0:fy1 - sy0, fx0 - sx0:fx1 - sx0] = True

    for box in boxes:
        bx0 = max(sx0, int(box["x"]) - margin)
        by0 = max(sy0, int(box["y"]))
        bx1 = min(sx1, int(box["x"]) + int(box["w"]) + margin)
        by1 = min(sy1, int(box["y"]) + int(box["h"]))
        if bx1 <= bx0 or by1 <= by0:
            # The glyph sits outside the widget's own box entirely; that is the
            # geometry being untrustworthy, which is not something to resolve
            # here.
            return True
        if not covered[by0 - sy0:by1 - sy0, bx0 - sx0:bx1 - sx0].all():
            return True
    return False


#: Roles that are drawn as a surface, not as a run of text.
WINDOW_LIKE_ROLES_FOR_TEXT = frozenset(
    {"frame", "window", "dialog", "alert", "desktop frame"}
)


def _preserve_widget_geometry_for_visible_text(elem: Dict[str, Any]) -> bool:
    """Whose box survives its text being unreadable.

    When no glyph of an element passes the pixel test, this module concludes the
    element draws no text and clears its geometry - correct for a label, whose
    only ink *is* its text. It is wrong for a window: a dialog is a drawn
    surface, and VS Code gives its notification dialogs an `inner_text` of
    U+FFFC, the object-replacement character, which has no glyphs to find. The
    dialog was therefore stripped of its fragments, stamped `hidden` by
    `annotate_occlusion_state`, and - because
    `check_visible_fragments_present` is all-or-nothing over a capture -
    rejected 4.8% of the corpus while sitting plainly on screen.
    """
    attrs = elem.get("attrs") or {}
    states = attrs.get("states") or {}
    interfaces = attrs.get("interfaces") or {}
    role = (elem.get("role") or "").strip().lower()
    return bool(
        interfaces.get("editable_text")
        or states.get("editable")
        or elem.get("type") == "Text Input"
        or role in WINDOW_LIKE_ROLES_FOR_TEXT
    )


def populate_visible_text(
    elements: List[Dict[str, Any]],
    *,
    geometry_cache: Optional[Dict[Tuple[str, Tuple[int, ...]], Optional[TextGeometry]]] = None,
    gray: Any = None,
    offset_cache: Optional[Dict[Tuple[str, Tuple[int, ...]], int]] = None,
    hierarchy: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """`hierarchy` supplies ancestors for re-homing.

    The leaf export contains only leaves, so an element's containers are not in
    the list being annotated and cannot be found from it. Pass the unfiltered
    tree here and ancestor lookups resolve for every pass.
    """
    geometry_cache = geometry_cache if geometry_cache is not None else {}
    offset_cache = offset_cache if offset_cache is not None else {}
    ink_oracle = make_ink_oracle(gray)
    num_offset_corrected = 0
    num_rehomed = 0
    num_low_confidence = 0
    hierarchy_nodes = hierarchy if hierarchy is not None else elements
    num_text = 0
    num_clipped = 0
    num_hidden = 0
    num_unsupported = 0
    num_name_only = 0
    num_occluder_misses_text = 0

    for elem in elements:
        text = str(elem.get("inner_text") or "")
        if not text:
            elem["visible_text"] = ""
            elem["visible_text_status"] = "no_text"
            elem["visible_text_confidence"] = 1.0
            continue

        num_text += 1
        fragments = normalize_fragments(
            [clone_rect(f) for f in (elem.get("visible_fragments") or []) if isinstance(f, dict)]
        )
        rect = clone_rect(elem.get("rect", {}) or {})
        source_rect = clone_rect(
            elem.get("_occlusion_original_rect")
            or elem.get("_visibility_source_rect")
            or elem.get("rect", {})
            or {}
        )
        attrs = elem.get("attrs") or {}
        if attrs.get("text_source") == "name" and not name_can_be_rendered(
            text, source_rect
        ):
            # `visible_text` is the glyphs this element draws. This string is
            # its accessible name and the box is too small - or far too big - to
            # be showing it, so no glyphs of it are on screen. The name itself
            # is kept in `name`; only the claim that it is rendered is dropped.
            elem["visible_text"] = ""
            elem["visible_text_status"] = "name_only"
            elem["visible_text_confidence"] = 1.0
            num_name_only += 1
            continue

        if text_overflows_box(text, source_rect):
            # More lines of text than the widget is tall: an app mirroring a
            # document into a small offscreen control for screen readers. The
            # text is real and visible; only the box is wrong. Move it onto the
            # container that does show it, and withhold only if there is none.
            adopted = rehome_overflowing_text(elem, text, hierarchy_nodes)
            if adopted is not None:
                elem["rect"] = clone_rect(adopted)
                set_visible_fragments(elem, [clone_rect(adopted)])
                elem["_visibility_source_rect"] = clone_rect(adopted)
                elem["visible_text"] = text
                elem["visible_text_status"] = "rehomed"
                elem["visible_text_confidence"] = 1.0
                elem["text_rehomed_from"] = clone_rect(source_rect)
                num_rehomed += 1
                continue
            elem["visible_text"] = None
            elem["visible_text_status"] = "unsupported_overflow"
            elem["visible_text_confidence"] = 0.0
            num_unsupported += 1
            continue
        if len(fragments) == 1 and _same_rect(source_rect, fragments[0]):
            elem["visible_text"] = text
            elem["visible_text_status"] = "full_visible"
            elem["visible_text_confidence"] = 1.0
            continue
        if not fragments:
            elem["visible_text"] = ""
            elem["visible_text_status"] = "hidden"
            elem["visible_text_confidence"] = 1.0
            num_hidden += 1
            continue

        app_name = str(elem.get("_atspi_app_name") or "")
        path = tuple(int(x) for x in (elem.get("_atspi_path") or []) if isinstance(x, int))
        if not app_name or not path or attrs.get("text_source") != "text_iface":
            elem["visible_text"] = None
            elem["visible_text_status"] = "unsupported_partial"
            elem["visible_text_confidence"] = 0.0
            num_unsupported += 1
            continue

        cache_key = (app_name, path)
        if cache_key not in geometry_cache:
            geometry_cache[cache_key] = _collect_text_geometry(_resolve_node(app_name, list(path)))
        geometry = geometry_cache[cache_key]
        if geometry is None:
            elem["visible_text"] = None
            elem["visible_text_status"] = "unsupported_partial"
            elem["visible_text_confidence"] = 0.0
            num_unsupported += 1
            continue

        # Correct AT-SPI text geometry that is offset from the rendered glyphs
        # before any clipping decisions are made. The visible region here comes
        # from the element's own occlusion fragments measured against its
        # component rect, which is the geometry we do trust.
        #
        # This used to run only for elements an overlapping window had occluded.
        # That was the wrong condition: what makes the correction necessary is
        # that character geometry is about to be *used*, not what narrowed the
        # element. An element clipped by an ancestor viewport - a table cell in a
        # tree view scrolled past its right edge - reaches here with
        # `is_occluded` false, and skipping the correction let its uncorrected
        # boxes become the element rect. A measured HomeBank amount cell was
        # annotated at 349..385 with the full text "¥ 560" and no occlusion,
        # while the screen draws "¥ 5" at 398..421, cut off by the viewport:
        # a box roughly its own width away from the glyphs it claims.
        if gray is not None:
            geometry = repair_char_advances(geometry)
            if cache_key in offset_cache:
                dx, conf = offset_cache[cache_key]
            elif len(geometry.lines) > 1:
                # A document, not a cell. Confirm the reported boxes rather than
                # search for a shift, and judge each line on its own band.
                dx = 0
                conf = validate_char_extents_by_line(
                    gray,
                    geometry,
                    cell_rect=source_rect,
                    visible_regions=fragments,
                )
            else:
                dx, conf = estimate_char_extent_offset(
                    gray,
                    [clone_rect(r) for r in geometry.char_extents.values()],
                    cell_rect=source_rect,
                    visible_regions=fragments,
                )
                offset_cache[cache_key] = (dx, conf)
            if dx:
                geometry = shift_char_extents(geometry, dx)
                num_offset_corrected += 1
                elem["text_geometry_offset_px"] = dx
            elem["text_geometry_confidence"] = round(conf, 3)
            # The ink gate may only be trusted where the layout was actually
            # aligned to the pixels. Applying it to uncorrected geometry drops
            # characters whose boxes simply sit in the wrong place.
            required = (
                MULTILINE_GEOMETRY_MIN_CONFIDENCE
                if len(geometry.lines) > 1
                else CHAR_GEOMETRY_MIN_CONFIDENCE
            )
            geometry_validated = conf >= required
            if os.environ.get("DESKSHOT_DEBUG_TEXT_GEOMETRY"):
                print(
                    f"[textgeom] app={elem.get('app_name')} role={elem.get('role')} "
                    f"rect={(elem.get('rect') or {}).get('w')}x{(elem.get('rect') or {}).get('h')} "
                    f"src={source_rect.get('w')}x{source_rect.get('h')} "
                    f"lines={text.count(chr(10)) + 1} chars={len(text)} "
                    f"dx={dx} conf={conf:.3f} validated={geometry_validated} "
                    f"occluded={bool(elem.get('is_occluded'))}",
                    file=sys.stderr,
                )
            if not geometry_validated and elem.get("is_occluded"):
                # An occluder hides part of this element, so the visible text is
                # a character-level question and the boxes cannot answer it -
                # *if* the occluder reaches the text at all. Usually it does
                # not: what was clipped is the padding, the border or the empty
                # tail of a wide box. Then the question is not character-level
                # and the answer is the same as for an unoccluded element with
                # untrustworthy geometry - keep the text, keep the widget rect.
                if not occlusion_reaches_text(
                    geometry, source_rect=source_rect, fragments=fragments
                ):
                    elem["visible_text"] = text
                    elem["visible_text_status"] = "full_visible"
                    elem["visible_text_confidence"] = 1.0
                    elem["text_geometry_unvalidated"] = True
                    num_low_confidence += 1
                    num_occluder_misses_text += 1
                    continue
                # The glyph boxes do not line up with the ink even after
                # correction, so any character-level answer would be a guess.
                # Emitting no text is correct here: the occlusion state and the
                # marker still record that content was cut, and ground truth
                # never gains a string the screen does not show.
                elem["visible_text"] = None
                elem["visible_text_status"] = "unsupported_partial"
                elem["visible_text_confidence"] = 0.0
                num_unsupported += 1
                num_low_confidence += 1
                continue
            if not geometry_validated:
                # Nothing overlaps this element, so no character is hidden by an
                # occluder and the full text is still the honest answer. Only the
                # text-derived *geometry* is untrustworthy, so the widget rect is
                # kept rather than replaced by boxes the pixels do not confirm.
                # Cells that mix an icon with a label land here: their visible
                # region holds ink no glyph box can explain, which is not a
                # reason to withhold the label.
                elem["visible_text"] = text
                elem["visible_text_status"] = "full_visible"
                elem["visible_text_confidence"] = 1.0
                elem["text_geometry_unvalidated"] = True
                num_low_confidence += 1
                continue

        preserve_widget_geometry = _preserve_widget_geometry_for_visible_text(elem)
        full_text_fragments = _geometry_char_fragments(geometry)
        visible_text_fragments = _geometry_char_fragments(geometry, visible_regions=fragments)
        full_text_rect = union_rect(full_text_fragments)
        visible_text_rect = union_rect(visible_text_fragments)
        if full_text_rect is not None and not preserve_widget_geometry:
            elem["_visibility_source_rect"] = clone_rect(full_text_rect)
            source_rect = clone_rect(full_text_rect)

        # The ink test is a statement about pixels, so it holds regardless of
        # whether the offset correction ran. Scoping it to corrected geometry
        # looked right until a cell that renders nothing at all was found still
        # reporting "$ 5": the gate had been catching a phantom, not regressing.
        clip_chunks = _clip_chunks(geometry, fragments, has_ink=ink_oracle)
        visible_text = render_clip_chunks(clip_chunks, marker="")
        all_text_visible = bool(full_text_fragments) and _same_fragment_sets(
            full_text_fragments,
            visible_text_fragments,
        )
        if all_text_visible:
            elem["visible_text"] = text
            elem["visible_text_status"] = "full_visible"
            elem["visible_text_confidence"] = 1.0
            if full_text_rect is not None and not preserve_widget_geometry:
                elem["rect"] = clone_rect(full_text_rect)
                set_visible_fragments(elem, [full_text_rect])
                elem["_is_occluded_by_overlap"] = False
                elem["is_occluded"] = False
            continue

        elem["visible_text"] = visible_text
        elem["visible_text_confidence"] = 1.0
        if visible_text and not visible_text.strip():
            # Every inked glyph failed the pixel test, so the element draws no
            # text here. Emitting the surviving whitespace would be a phantom.
            visible_text = ""
            elem["visible_text"] = ""
        if not visible_text or not visible_text_fragments:
            elem["visible_text_status"] = "hidden"
            if not preserve_widget_geometry:
                set_visible_fragments(elem, [])
                elem["_is_occluded_by_overlap"] = True
                elem["is_occluded"] = True
            num_hidden += 1
        else:
            elem["visible_text_status"] = "clipped"
            # Keep visible_text as the literal visible characters, and expose the
            # marked form separately so consumers choose. Serialization uses the
            # marked form so a truncated string is not read as complete content.
            elem["visible_text_marked"] = render_clip_chunks(
                clip_chunks, marker=TEXT_CLIP_MARKER
            )
            if visible_text_rect is not None and not preserve_widget_geometry:
                elem["_occlusion_original_rect"] = source_rect
                elem["rect"] = clone_rect(visible_text_rect)
                set_visible_fragments(elem, visible_text_fragments)
                elem["_is_occluded_by_overlap"] = True
                elem["is_occluded"] = True
            num_clipped += 1

    # Recover text we refused only because character positions could not be
    # confirmed.
    #
    # Withholding is right when occlusion makes visibility a character-level
    # question. It is wrong when the element is fully visible and the only
    # missing thing is *where each glyph sits* - the string is still the string.
    # Measured on v235: Chromium's address bar and its page tabs came out empty
    # this way, and a tab's accessible name ("Download Python | Python.org") is
    # exactly what it draws. Restricted to unoccluded elements, because a
    # clipped tab would otherwise over-claim the part that is cut off, and still
    # subject to the ink and size tests below.
    num_recovered = 0
    if gray is not None:
        for elem in elements:
            if elem.get("visible_text_status") not in ("unsupported_partial",):
                continue
            if elem.get("is_occluded"):
                continue
            text = str(elem.get("inner_text") or elem.get("name") or "")
            if not text.strip():
                continue
            rect = elem.get("rect")
            if not isinstance(rect, dict) or not name_can_be_rendered(text, rect):
                continue
            if not box_has_ink_for_text(gray, [rect], text):
                continue
            elem["visible_text"] = text
            elem["visible_text_status"] = "unverified_full"
            elem["visible_text_confidence"] = 0.5
            num_recovered += 1

    # Last guard, applied to every path that emitted text rather than to the one
    # that happened to be noticed: a box claiming text must have ink behind it.
    num_no_ink = 0
    if gray is not None:
        height, width = gray.shape
        for elem in elements:
            text = elem.get("visible_text")
            if not text or not str(text).strip():
                continue
            frags = elem.get("visible_fragments")
            boxes = [f for f in frags if isinstance(f, dict)] if isinstance(frags, list) else []
            if not boxes:
                rect = elem.get("rect")
                boxes = [rect] if isinstance(rect, dict) else []
            if not boxes:
                continue
            if box_has_ink_for_text(gray, boxes, str(text)):
                continue
            elem["visible_text"] = None
            elem["visible_text_status"] = "no_ink"
            elem["visible_text_confidence"] = 0.0
            num_no_ink += 1

    return {
        "num_text_elements": num_text,
        "num_no_ink": num_no_ink,
        "num_recovered_unverified": num_recovered,
        "num_clipped": num_clipped,
        "num_hidden": num_hidden,
        "num_unsupported": num_unsupported,
        "num_name_only": num_name_only,
        "num_occluder_misses_text": num_occluder_misses_text,
        "num_rehomed": num_rehomed,
        "num_offset_corrected": num_offset_corrected,
        "num_low_confidence_text": num_low_confidence,
        "cache_entries": len(geometry_cache),
    }


def _element_uid(app_name: str, path: List[Any]) -> str:
    """Stable per-element id from the AT-SPI accessible path."""
    import hashlib

    key = app_name + ":" + ".".join(str(p) for p in path)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def strip_internal_text_visibility_state(elements: List[Dict[str, Any]]) -> None:
    for elem in elements:
        # Hash the accessible path into a stable uid before dropping the raw
        # fields. This is what lets an element be matched across observations,
        # which every temporal/transition label depends on.
        if "uid" not in elem:
            app = elem.get("_atspi_app_name")
            path = elem.get("_atspi_path")
            if app and isinstance(path, list):
                elem["uid"] = _element_uid(str(app), path)
        elem.pop("_atspi_path", None)
        elem.pop("_atspi_app_name", None)
