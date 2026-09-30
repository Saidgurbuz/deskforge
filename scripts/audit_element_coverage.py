#!/usr/bin/env python
"""Find visible content that no annotation covers, per window.

The rule the dataset is built on is that every visible UI element is annotated.
Nothing in the pipeline checked it. `audit_annotation_pixels` asks the opposite
question - is there an annotation with no content behind it - and its whole-screen
"uncovered ink" number is dominated by wallpaper, so it never caught a missing
widget.

Scoping the same idea to one window makes it sharp. A window's own pixels are
almost entirely UI, so inked pixels inside it that no element covers are a
missing annotation, not scenery. Three known failures all show up this way:

- FileZilla's directory tree, which AT-SPI never exposes at all
- dock icons for apps that are running but not in a hard-coded list
- popup windows that arrive with no accessible contents

Reported per app window rather than per screen, so an app that cannot be
annotated properly is named instead of being averaged away.

Thin rules - window borders, pane dividers, toolbar edges, table grid lines -
are counted separately as `decoration_ink` and kept out of the headline number.
Measured over the 12-scene batch in `incremental_checks/v213_combined`, they
were 50-99% of every app's flagged ink (baobab 94%, gnome-logs 99%, nautilus
90%), so the metric was mostly reporting them and nothing else. They are not
widgets: they are how a container draws its own edge, they change with the style
pack, they carry no label, action or state, and the container they belong to is
already annotated - a window's own 1px border is flagged only because this
script deliberately excludes the frame rect from coverage in order to ask about
the window's contents. Discounting them is therefore not a blind spot for
missing widgets: rendering the discounted mask over HomeBank shows it lands on
the window border, the pane dividers and the transaction grid and on nothing
else, and the tree-row expanders and folder icons that this audit found in
v214 (11x11 and 16x16) are far too blocky to be discounted by it.

They are reported rather than dropped, so the decision stays visible.

Usage:
    PYTHONPATH=src python scripts/audit_element_coverage.py \
        --root incremental_checks/v209_parallel --output <out>
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

#: Gradient magnitude above which a pixel counts as drawn content.
INK_GRADIENT = 18

#: Ignore slivers: a window edge or a scrollbar groove is not a missing widget.
MIN_REGION_SIDE = 12

#: Windows smaller than this are tooltips and menus whose geometry we may not
#: have; they are reported but not scored, to keep the headline number about
#: real application windows.
MIN_WINDOW_SIDE = 120

WINDOW_ROLES = {"frame", "window", "dialog", "alert", "file chooser"}

#: A rule is at most this thick across and at least this long along. Measured on
#: the captures: window borders are 1-2px, GTK pane dividers and toolbar edges
#: 1px, table grid lines 1px; the thinnest thing that is a real widget in the
#: same captures is a 6px scrollbar groove, and the smallest missing widget the
#: audit has actually caught is an 11x11 expander triangle.
RULE_MAX_THICKNESS = 3
RULE_MIN_LENGTH = 20


def _run_lengths(mask: np.ndarray, axis: int) -> np.ndarray:
    """For every set pixel, the length of the contiguous run it belongs to.

    Vectorised: runs are found from the transitions of a padded copy and their
    lengths written into a difference array, so one cumulative sum fills every
    pixel. A per-run Python loop takes minutes on a 2560x1440 mask.
    """
    m = mask if axis == 1 else mask.T
    height, width = m.shape
    padded = np.zeros((height, width + 2), dtype=np.int8)
    padded[:, 1:-1] = m
    diff = np.diff(padded, axis=1)
    starts = np.argwhere(diff == 1)
    ends = np.argwhere(diff == -1)
    out = np.zeros(height * (width + 1), dtype=np.int32)
    if starts.size:
        lengths = ends[:, 1] - starts[:, 1]
        np.add.at(out, starts[:, 0] * (width + 1) + starts[:, 1], lengths)
        np.add.at(out, ends[:, 0] * (width + 1) + ends[:, 1], -lengths)
    filled = np.cumsum(out).reshape(height, width + 1)[:, :width]
    return filled if axis == 1 else filled.T


def rule_mask(mask: np.ndarray) -> np.ndarray:
    """Which pixels of `mask` are part of a thin, long rule."""
    across = _run_lengths(mask, 1)
    along = _run_lengths(mask, 0)
    return (
        ((along <= RULE_MAX_THICKNESS) & (across >= RULE_MIN_LENGTH))
        | ((across <= RULE_MAX_THICKNESS) & (along >= RULE_MIN_LENGTH))
    )


def ink_mask(gray: np.ndarray) -> np.ndarray:
    gx = np.zeros_like(gray, dtype=np.int32)
    gy = np.zeros_like(gray, dtype=np.int32)
    gx[:, 1:] = np.abs(np.diff(gray.astype(np.int32), axis=1))
    gy[1:, :] = np.abs(np.diff(gray.astype(np.int32), axis=0))
    return (gx + gy) > INK_GRADIENT


def _rect(elem: Dict[str, Any]) -> Optional[Tuple[int, int, int, int]]:
    r = elem.get("rect")
    if not isinstance(r, dict):
        return None
    x, y = int(r.get("x", 0)), int(r.get("y", 0))
    w, h = int(r.get("w", 0)), int(r.get("h", 0))
    if w <= 0 or h <= 0:
        return None
    return x, y, w, h


def coverage_rects(elem: Dict[str, Any]) -> List[Tuple[int, int, int, int]]:
    """Every region this element annotates.

    `rect` is not the whole answer. A partially occluded element has its rect
    clipped to its *largest* visible fragment, while the rest of what it covers
    lives in `visible_fragments` - and ScreenTag serializes those fragments, so
    they are part of the annotation. Counting only `rect` blamed the pipeline
    for ink its own output describes: an eog image view split into four
    fragments had 219k px of annotated area attributed to no element, which is
    most of why eog measured 14.5% uncovered.

    Both are serialized, so coverage is their union: taking fragments *instead*
    of the rect goes wrong the other way, because an element that keeps its full
    rect on partial occlusion annotates that whole rect while its fragments are
    only the exposed pieces. Using the union means the metric never blames the
    pipeline for area its own output already describes, in either direction.
    """
    boxes: List[Tuple[int, int, int, int]] = []
    box = _rect(elem)
    if box is not None:
        boxes.append(box)
    frags = elem.get("visible_fragments")
    if isinstance(frags, list):
        for frag in frags:
            if isinstance(frag, dict):
                fbox = _rect({"rect": frag})
                if fbox is not None:
                    boxes.append(fbox)
    return boxes


def _largest_gaps(
    uncovered: np.ndarray, box: Tuple[int, int, int, int], limit: int = 5
) -> List[Dict[str, int]]:
    """Bounding boxes of the biggest uncovered clusters, for a human to look at.

    Deliberately crude - a coarse grid rather than connected components. The
    output is a pointer to where to zoom in, not a segmentation.
    """
    x0, y0, x1, y1 = box
    cell = 24
    hits: List[Tuple[int, int, int]] = []
    for gy in range(y0, y1, cell):
        for gx in range(x0, x1, cell):
            patch = uncovered[gy:min(gy + cell, y1), gx:min(gx + cell, x1)]
            count = int(patch.sum())
            if count > cell * 2:
                hits.append((count, gx, gy))
    hits.sort(reverse=True)
    return [{"x": gx, "y": gy, "w": cell, "h": cell, "ink": c} for c, gx, gy in hits[:limit]]


def _stack_index(elem: Dict[str, Any]) -> int:
    value = elem.get("_window_stack_index")
    return value if isinstance(value, int) else -1


def _own_region(win: Dict[str, Any], windows: List[Dict[str, Any]], shape) -> np.ndarray:
    """Mask of the pixels `win` is the topmost window over.

    Windows above it in the stack are subtracted. Windows with no stack index
    are left alone rather than guessed at - unknown order is not evidence that
    something covers this one.
    """
    mask = np.zeros(shape, dtype=bool)
    x0, y0, x1, y1 = win["box"]
    mask[y0:y1, x0:x1] = True
    mine = _stack_index(win["elem"])
    if mine < 0:
        return mask
    for other in windows:
        if other is win:
            continue
        theirs = _stack_index(other["elem"])
        if theirs <= mine:
            continue
        ox0, oy0, ox1, oy1 = other["box"]
        mask[oy0:oy1, ox0:ox1] = False
    return mask


def audit_capture(png: Path, elements: List[Dict[str, Any]]) -> Dict[str, Any]:
    with Image.open(png) as img:
        gray = np.array(img.convert("L"))
    ink = ink_mask(gray)
    height, width = ink.shape

    covered = np.zeros_like(ink, dtype=bool)
    windows: List[Dict[str, Any]] = []
    for elem in elements:
        role = str(elem.get("role") or "").strip().lower()
        if role in WINDOW_ROLES:
            box = _rect(elem)
            if box is None:
                continue
            x, y, w, h = box
            x0, y0 = max(0, x), max(0, y)
            x1, y1 = min(width, x + w), min(height, y + h)
            if x1 <= x0 or y1 <= y0:
                continue
            windows.append({"elem": elem, "box": (x0, y0, x1, y1), "role": role})
            # A window's own rect is not "coverage" - it is the area whose
            # contents we are asking about.
            continue
        for x, y, w, h in coverage_rects(elem):
            x0, y0 = max(0, x), max(0, y)
            x1, y1 = min(width, x + w), min(height, y + h)
            if x1 <= x0 or y1 <= y0:
                continue
            covered[y0:y1, x0:x1] = True

    flagged = ink & ~covered
    # Split the flagged ink before scoring: rules are decoration we decided not
    # to annotate, and leaving them in the headline number would keep the metric
    # measuring something the pipeline is not going to fix.
    decoration = flagged & rule_mask(flagged)
    uncovered = flagged & ~decoration
    rows: List[Dict[str, Any]] = []
    for win in windows:
        x0, y0, x1, y1 = win["box"]
        if (x1 - x0) < MIN_WINDOW_SIDE or (y1 - y0) < MIN_WINDOW_SIDE:
            continue
        # Score only the pixels this window actually owns.
        #
        # Containment is not ownership. A window on top of another paints over
        # it, and charging those pixels to the window underneath makes one app's
        # gap look like another's: HomeBank measured 10.4% uncovered in a scene
        # where the red was entirely FileZilla's unannotated file panes, drawn
        # over HomeBank and scored against it because they fell inside its rect.
        # Every overlapping-window scene mixed the two, so per-app numbers moved
        # with the layout rather than with the annotations.
        own = _own_region(win, windows, ink.shape)
        win_ink = int((ink & own).sum())
        win_unc = int((uncovered & own).sum())
        win_dec = int((decoration & own).sum())
        if win_ink <= 0:
            continue
        rows.append({
            "app_name": win["elem"].get("app_name"),
            "role": win["role"],
            "title": (win["elem"].get("visible_text") or win["elem"].get("name") or "")[:60],
            "rect": {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0},
            "ink": win_ink,
            "uncovered_ink": win_unc,
            "uncovered_ratio": round(win_unc / win_ink, 4),
            "decoration_ink": win_dec,
            "decoration_ratio": round(win_dec / win_ink, 4),
            "gaps": _largest_gaps(uncovered, (x0, y0, x1, y1)),
        })
    return {"capture": png.name, "windows": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", required=True)
    ap.add_argument("--output", default=None)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    by_app: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"windows": 0, "ink": 0, "uncovered": 0, "decoration": 0}
    )
    captures: List[Dict[str, Any]] = []
    seen = 0
    for leaf in sorted(Path(args.root).rglob("*.elements.leaf.json")):
        png = leaf.with_name(leaf.name.replace(".elements.leaf.json", ".png"))
        if not png.is_file():
            continue
        data = json.loads(leaf.read_text(encoding="utf-8"))
        elements = data if isinstance(data, list) else data.get("elements", [])
        try:
            row = audit_capture(png, elements)
        except Exception as exc:
            print(f"  !! {png.name}: {exc}")
            continue
        captures.append(row)
        for win in row["windows"]:
            bucket = by_app[str(win["app_name"])]
            bucket["windows"] += 1
            bucket["ink"] += win["ink"]
            bucket["uncovered"] += win["uncovered_ink"]
            bucket["decoration"] += win["decoration_ink"]
        seen += 1
        if args.limit and seen >= args.limit:
            break

    print(f"captures={len(captures)}\n")
    print(f"{'app':22}{'windows':>9}{'uncovered ink':>16}{'decoration':>13}")
    print("-" * 60)
    ranked = sorted(
        by_app.items(), key=lambda kv: -(kv[1]["uncovered"] / max(1.0, kv[1]["ink"]))
    )
    for app, stats in ranked:
        ratio = stats["uncovered"] / max(1.0, stats["ink"])
        decor = stats["decoration"] / max(1.0, stats["ink"])
        print(
            f"{app:22}{int(stats['windows']):>9}{ratio * 100:>15.1f}%{decor * 100:>12.1f}%"
        )

    if args.output:
        out = Path(args.output)
        out.mkdir(parents=True, exist_ok=True)
        (out / "element_coverage.json").write_text(
            json.dumps({"by_app": {k: dict(v) for k, v in by_app.items()},
                        "captures": captures}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
