"""Annotate the close/minimise/maximise buttons the window manager draws.

STATUS: not wired into the pipeline. The detector below finds the title bar and
the right side of it, but does not yet segment the glyphs reliably - on measured
bands it found 2 of 4 controls on xarchiver and 0 on bluefish, because xfwm4
draws them as thin outlines whose column runs merge and split with the gap
threshold, and whose heights (24, 14, 7, 9 px on one bar) fail a consistency
check tuned for uniform glyphs. Emitting a partial, possibly mis-ordered set
would put false positives into ground truth, which is worse than the known gap,
so `run_extraction` does not call this yet. The approach that should replace
glyph segmentation: detect the button *cells* by
their periodicity against the window's right edge, which is uniform even when
the glyphs inside them are not.

An app with client-side decorations draws its own controls as real widgets, so
AT-SPI publishes them and they are annotated like anything else. An app with
server-side decorations does not: xfwm4 draws them, and xfwm4 publishes nothing
to AT-SPI. Measured across 34 captures, that left **30 of 93** decorated windows
with any control annotated - complete for chromium, vscode, eog, file-roller;
zero for mousepad, bluefish, xarchiver, homebank, thunar, baobab, pluma and the
rest. They are among the most-used controls on any screen, so the gap matters.

They cannot be walked for, so they are located the only way left: in the
pixels of the title bar this pipeline already synthesizes for exactly these
windows.

Detection, and why each step is the way it is:

- Ink is a horizontal *step*, not distance from a background value. Title bars
  are commonly painted as a vertical gradient, so every pixel differs from any
  single background estimate and the whole band reads as one 564px-wide "glyph";
  differencing along x separates a gradient from a glyph edge without needing to
  know the theme's colours or polarity.
- Glyphs are found by projecting ink onto x and splitting on gaps. Connected
  components would split a single glyph into pieces (an unmaximise icon is two
  overlapping outlines); a column projection keeps a glyph whole.
- Which clusters are controls is decided by *shape*, not position: controls are
  a run of similar-width, similar-height, evenly-spaced clusters at one end of
  the band. Title text fails all three - its clusters are letters of varying
  width with uneven gaps.

Naming comes from the layout this project configures rather than from guessing
what a glyph depicts. `xfce_config` writes xfwm4's `button_layout` as `CHM|`
for the macOS-like style - close, hide, maximise, on the left - and leaves the
default `O|SHMC` otherwise - menu on the left, then shade, hide, maximise,
close on the right. So the side and the count identify the buttons.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

Rect = Dict[str, int]

#: xfwm4 `SHMC`: the right-hand group in the default layout, in draw order.
RIGHT_GROUP = ("Shade", "Minimize", "Maximize", "Close")

#: xfwm4 `CHM`: the left-hand group in the macOS-like layout, in draw order.
LEFT_GROUP_MACOS = ("Close", "Minimize", "Maximize")

#: A control glyph is a small mark. Anything wider is title text or an app icon.
MIN_GLYPH_SIDE = 3
MAX_GLYPH_SIDE = 30

#: Columns with no ink this wide separate one glyph from the next.
GLYPH_GAP = 4

#: Plausible distance between the centres of two neighbouring control cells.
#: Measured across the styles this project configures: macOS traffic lights sit
#: on a ~20px pitch, xfwm4's outline glyphs on ~25-40px.
MIN_CELL_PITCH = 13
MAX_CELL_PITCH = 64

#: How far a spacing may stray from the run's median and still be the same
#: pitch. Cells are laid out by the window manager, so real ones are regular;
#: lettering is not.
PITCH_TOLERANCE = 0.22

#: The outermost control butts against the window edge. Allow it to sit this
#: many pitches in, which covers the frame border and the cell's own padding.
EDGE_SLACK_PITCHES = 1.6

#: How far in from an edge a control group can start. A fixed reach beats a
#: fraction: on a 1500px title bar a fraction of the width reaches deep into
#: the title text, and on a 400px one it excludes the buttons.
EDGE_REACH_PX = 240

#: How alike a run has to be to read as a button group rather than lettering.
MAX_WIDTH_RATIO = 2.2
MAX_SPACING_RATIO = 1.6

#: Strokes closer together than this belong to one glyph.
#:
#: A column projection sees strokes, not glyphs: xfwm4's minimise `_` is a
#: horizontal line whose only vertical edges are its two ends, so it arrives as
#: two 1px clusters, and the maximise square arrives as its two 3px sides.
#: Measured on three ubuntu_like title bars, strokes within a button are <=10px
#: apart while the buttons themselves are 33px apart, so merging under 12px
#: recovers exactly the four glyphs.
#:
#: One value cannot serve both themes: ubuntu_like's strokes sit <=10px apart
#: inside a button that is 33px from the next, while the macOS discs are only
#: 9px apart from each other. Merging at 12 recovers ubuntu's four glyphs and
#: fuses macOS's three discs into one 78px blob. So every gap is tried and the
#: most regular run wins, which needs no per-theme knowledge at all.
GLYPH_MERGE_GAPS = (0, 6, 12)

#: A control group starts at least this far in from the band's edge. The
#: window's own border and its shadow hug the edge and arrive as a run of
#: similar, evenly spaced strokes - which is exactly what a button group looks
#: like to the test above, and on the macOS-like theme it was being picked
#: instead of the three discs sitting 16px further in.
EDGE_INSET_PX = 10

#: Ink must differ from the band background by at least this much.
INK_DELTA = 24


def _ink_columns(gray, band: Rect) -> Optional[Tuple[Any, int]]:
    """Per-column ink counts for the band, plus its left edge."""
    import numpy as np

    x0, y0 = max(0, int(band["x"])), max(0, int(band["y"]))
    x1 = min(int(gray.shape[1]), x0 + int(band["w"]))
    y1 = min(int(gray.shape[0]), y0 + int(band["h"]))
    if x1 - x0 < 8 or y1 - y0 < 6:
        return None
    # Trim the frame's own border rows, which run the full width and would make
    # every column look inked.
    inset = 2
    if (y1 - inset) - (y0 + inset) < 6:
        inset = 0
    patch = np.asarray(gray[y0 + inset:y1 - inset, x0:x1], dtype=np.int32)
    if patch.shape[0] < 4 or patch.shape[1] < 8:
        return None
    # Horizontal steps, not distance from a background value. Title bars are
    # commonly painted as a vertical gradient, so every pixel differs from any
    # single background estimate and the whole band reads as ink - measured on a
    # bluefish bar, that produced one 564px-wide "glyph". A gradient varies
    # smoothly across x, while a glyph edge is a step, so differencing along x
    # separates them without needing to know the theme's colours or polarity.
    step = np.abs(np.diff(patch, axis=1)) > INK_DELTA
    ink = np.zeros(patch.shape, dtype=bool)
    ink[:, 1:] = step
    return ink, x0


def _clusters(ink, x_offset: int) -> List[Tuple[int, int, int]]:
    """(x_start, x_end, height) for each run of inked columns."""
    import numpy as np

    columns = ink.any(axis=0)
    out: List[Tuple[int, int, int]] = []
    start = None
    gap = 0
    for i, filled in enumerate(columns):
        if filled:
            if start is None:
                start = i
            gap = 0
        elif start is not None:
            gap += 1
            if gap >= GLYPH_GAP:
                end = i - gap + 1
                rows = np.flatnonzero(ink[:, start:end].any(axis=1))
                if rows.size:
                    out.append((x_offset + start, x_offset + end,
                                int(rows[-1] - rows[0]) + 1))
                start = None
                gap = 0
    if start is not None:
        rows = np.flatnonzero(ink[:, start:].any(axis=1))
        if rows.size:
            out.append((x_offset + start, x_offset + ink.shape[1],
                        int(rows[-1] - rows[0]) + 1))
    return out


def _best_comb(
    glyph_cols,
    *,
    edge: int,
    inward: int,
    max_len: int,
) -> Optional[Tuple[int, int, int]]:
    """Fit a comb of equal cells to the glyph columns at one end of the band.

    Glyph *shape* is not usable here: xfwm4 draws the four controls as outlines
    of quite different sizes - on one measured bar the heights were 24, 14, 7
    and 9 px - and a single glyph does not even give a single run of columns.
    An unmaximise square is two vertical strokes 7px apart and a minimise dash
    is two 2px ends 9px apart, so the *nearest neighbouring run* is not the next
    button; taking it as the pitch put every cell a third of a cell out.

    The cells, though, are laid out by the window manager and are identical, so
    the thing to fit is the comb: the pitch and phase whose boundaries all fall
    in blank columns while every cell holds ink. On the xarchiver bar above,
    pitch 23 separates the four glyph groups cleanly and pitch 19 - the distance
    to the nearest run - cuts two glyphs in half, which is exactly the signal.

    Returns (pitch, outermost_boundary, num_cells) in band-relative columns.
    """
    import numpy as np

    width = len(glyph_cols)
    inked = np.flatnonzero(glyph_cols)
    if inked.size == 0:
        return None
    best: Optional[Tuple[Tuple[int, float], Tuple[int, int, int]]] = None

    for pitch in range(MIN_CELL_PITCH, MAX_CELL_PITCH + 1):
        for start in range(pitch):
            outer = edge + inward * start
            cells: List[Tuple[int, int]] = []
            for k in range(max_len):
                a = outer + inward * k * pitch
                b = a + inward * pitch
                lo, hi = min(a, b), max(a, b)
                if lo < 0 or hi > width:
                    break
                if not bool(glyph_cols[lo:hi].any()):
                    break
                cells.append((lo, hi))
            if len(cells) < 2:
                continue
            # Every boundary between two accepted cells must fall in a gap: a
            # comb that slices through a glyph is the wrong comb.
            bounds = sorted({c[0] for c in cells} | {c[1] for c in cells})
            interior = bounds[1:-1]
            if any(glyph_cols[max(0, b - 1):min(width, b + 2)].any() for b in interior):
                continue
            # Among combs that fit, prefer the one whose cells hold their ink
            # centrally - the alternative is a comb shifted by a whole gap.
            offset = 0.0
            for lo, hi in cells:
                cols = inked[(inked >= lo) & (inked < hi)]
                offset += abs(float(cols.mean()) - (lo + hi - 1) / 2.0) / pitch
            key = (len(cells), -offset / len(cells))
            if best is None or key > best[0]:
                best = (key, (pitch, outer, len(cells)))
    return best[1] if best else None


def controls_side(style: str) -> str:
    """Which end of the title bar this session's window manager draws them on.

    Nothing is inferred from the pixels: `xfce_config` writes xfwm4's
    `button_layout` itself - `CHM|` for the macOS-like style, the default
    `O|SHMC` otherwise - so the style names the side, and the side plus the
    count names the buttons.
    """
    return "left" if (style or "").strip().lower() == "macos" else "right"


def _merge_strokes(clusters, gap):
    """Join the strokes of one glyph back into a single cluster."""
    if gap <= 0:
        return list(clusters)
    merged = []
    for start, end, height in clusters:
        if merged and start - merged[-1][1] < gap:
            prev_start, prev_end, prev_h = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end), max(prev_h, height))
        else:
            merged.append((start, end, height))
    return merged


def _best_run(clusters, *, want_max=4):
    """The longest run of 3-4 adjacent clusters that look like a button group.

    Buttons in a title bar are the same size and evenly spaced; title text is
    letters of varying width with uneven gaps. Searching for the *run* rather
    than filtering clusters one by one is what separates them, and it is why the
    earlier attempt - which took the last four clusters and tested them - found
    three of four buttons on one theme and none on another.
    """
    best = []
    for size in range(want_max, 2, -1):
        for i in range(0, max(0, len(clusters) - size + 1)):
            group = clusters[i:i + size]
            widths = [e - s for s, e, _h in group]
            heights = [h for _s, _e, h in group]
            centres = [(s + e) / 2 for s, e, _h in group]
            gaps = [b - a for a, b in zip(centres, centres[1:])]
            if min(widths) <= 0 or min(heights) <= 0 or min(gaps) <= 0:
                continue
            if max(widths) / min(widths) > MAX_WIDTH_RATIO:
                continue
            # Heights deliberately not compared: `^` `_` `[]` `X` measure
            # 7, 2, 9 and 9 px tall and are still one button group. Equal
            # widths and even spacing are what make it a group.
            if max(gaps) / min(gaps) > MAX_SPACING_RATIO:
                continue
            # Buttons sit next to each other; a gap far larger than the glyph
            # means the run has swallowed a word of the title.
            if max(gaps) > max(widths) * 4:
                continue
            if len(group) > len(best):
                best = group
        if best:
            break
    return best


def detect_controls(gray, band: Rect) -> Tuple[List[Rect], str]:
    """Control button rects inside `band`, and which side they sit on.

    Both edges are searched because the layout depends on the theme, and this
    project sets it: `xfce_config` writes xfwm4's `button_layout` as `CHM|` for
    the macOS-like style - three round buttons on the left - and leaves the
    default `O|SHMC` otherwise, four glyphs on the right. Verified against every
    title bar in a batch: ubuntu_like draws `^ _ [] X` at the right edge,
    macos_tahoe_like three discs at the left.
    """
    prepared = _ink_columns(gray, band)
    if prepared is None:
        return [], ""
    ink, x_offset = prepared
    raw = _clusters(ink, x_offset)
    bx, bw = int(band["x"]), int(band["w"])
    reach = min(EDGE_REACH_PX, max(80, bw // 3))

    best_group: List[Tuple[int, int, int]] = []
    best_side = ""
    for gap in GLYPH_MERGE_GAPS:
        # Width bounds only, plus a height *ceiling*. There is no useful height
        # floor: xfwm4's minimise glyph is a 2px horizontal line, and requiring
        # a minimum height dropped it and with it the whole run. The ceiling
        # excludes the window's own border, a 39px-tall stroke beside them.
        clusters = [c for c in _merge_strokes(raw, gap)
                    if MIN_GLYPH_SIDE <= (c[1] - c[0]) <= MAX_GLYPH_SIDE
                    and c[2] <= MAX_GLYPH_SIDE]
        # Capped by the layouts this project configures: the default
        # `O|SHMC` puts four on the right, the macOS-like `CHM|` three on the
        # left. Without the cap the left run swallows a fourth cluster - the
        # window border or the first title glyph - and then cannot be named.
        for side, candidates, want in (
            ("right", [c for c in clusters if c[0] >= bx + bw - reach], 4),
            ("left", [c for c in clusters if c[1] <= bx + reach], 3),
        ):
            group = _best_run(candidates, want_max=want)
            if group:
                first, last = group[0][0], group[-1][1]
                if first - bx < EDGE_INSET_PX or (bx + bw) - last < EDGE_INSET_PX:
                    group = []
            if len(group) > len(best_group):
                best_group, best_side = group, side

    for group, side in ((best_group, best_side),):
        if len(group) < 3:
            continue
        pad = max(2, min(e - s for s, e, _h in group) // 2)
        rects = [{
            "x": max(bx, s - pad),
            "y": int(band["y"]),
            "w": (e - s) + 2 * pad,
            "h": int(band["h"]),
        } for s, e, _h in group]
        return rects, side
    return [], ""


def name_controls(count: int, side: str) -> List[str]:
    """Map a detected run onto the layout this project configures.

    Right-hand groups come from xfwm4's default `O|SHMC` and are read from the
    close button backwards, so a theme that omits shade still names the rest
    correctly. Left-hand groups are the macOS-like `CHM|`.
    """
    if side == "left":
        return list(LEFT_GROUP_MACOS[:count])
    return list(RIGHT_GROUP[len(RIGHT_GROUP) - count:]) if count <= len(RIGHT_GROUP) else []


def synthesize_window_controls(
    elements: List[Dict[str, Any]], gray, *, screentag_class: Any = None
) -> Dict[str, Any]:
    """Add a button element per window control found in a synthesized title bar.

    Only synthesized title bars are considered. A window whose decorations are
    its own widgets already has real controls in the tree, and a second,
    pixel-derived copy would be a duplicate annotation.
    """
    meta = {"num_controls": 0, "num_title_bars_with_controls": 0, "by_side": {}}
    if gray is None or not elements:
        return meta

    indices = [e.get("_dom_index") for e in elements if isinstance(e.get("_dom_index"), int)]
    next_index = (max(indices) + 1) if indices else 0
    added: List[Dict[str, Any]] = []

    for bar in list(elements):
        if str(bar.get("role") or "") != "title bar":
            continue
        if (bar.get("attrs") or {}).get("synthesized") != "title_bar":
            continue
        band = bar.get("rect")
        if not isinstance(band, dict):
            continue
        rects, side = detect_controls(gray, band)
        names = name_controls(len(rects), side)
        if not names or len(names) != len(rects):
            continue

        meta["num_title_bars_with_controls"] += 1
        meta["by_side"][side] = meta["by_side"].get(side, 0) + 1
        for rect, name in zip(rects, names):
            added.append({
                "tag": "push button",
                "role": "push button",
                "name": name,
                "attrs": {"synthesized": "window_control"},
                "rect": dict(rect),
                "z": bar.get("z", 0),
                "position": "absolute",
                "inner_text": "",
                "visible_text": "",
                "visible_text_status": "no_text",
                "type": (screentag_class("push button") if screentag_class else None) or "Button",
                "vlm_label": "Button",
                "frame_index": bar.get("frame_index", 0),
                "source": bar.get("source", "app"),
                "app_name": bar.get("app_name"),
                "occlusion_state": "none",
                "visible_fragments": [dict(rect)],
                "is_occluded": False,
                "_window_stack_index": bar.get("_window_stack_index"),
                "_parent_dom_index": bar.get("_dom_index"),
                "_dom_index": next_index,
                "_children_dom_indices": [],
                "_depth": int(bar.get("_depth", 0)) + 1,
                "_atspi_path": [],
                "parent_index": None,
                "children_indices": [],
                "id": None,
                "classes": None,
                "reading_order_index": bar.get("reading_order_index"),
            })
            next_index += 1
            meta["num_controls"] += 1

    elements.extend(added)
    return meta
