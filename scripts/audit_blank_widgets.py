#!/usr/bin/env python
"""Find annotated widgets with nothing drawn behind them.

`audit_annotation_pixels` only checks elements that serialize *text*: it asks
whether the glyphs an element claims are actually on screen. An annotated push
button, icon or check box carrying no text is never examined, so a widget that
the accessibility tree reports as present and visible while the screen shows
nothing there passes silently. That is a false positive in the strictest sense -
a box the model would be trained to predict over blank pixels - and it is the
class the dock icons fell into.

The check is deliberately narrow, because "no ink" is *correct* for plenty of
elements: an empty table cell, a blank region of a text view, a filler, a
separator, the transparent part of a container. Only roles that cannot be both
visible and blank are examined - a button has a border or a label, an icon has
pixels, a scroll bar has a groove. If one of those is drawn at all, it inks.

Ink is measured inside the element's `visible_fragments`, not its full rect, so
a widget correctly annotated as occluded is not blamed for the pixels somebody
else is drawing over it.

Two cheaper classes are reported alongside, both pure bookkeeping errors:

- `offscreen`: a rect with no on-screen area at all.
- `duplicate`: leaves sharing an identical rect, which double-count one widget.

Usage:
    PYTHONPATH=src python scripts/audit_blank_widgets.py \
        --root incremental_checks/v220_fnfp_baseline/batch --output <out>
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

#: "Nothing is drawn here" is flatness, not absence of gradient.
#:
#: The gradient detector the other audits use answers a different question -
#: whether there are *edges* - and it reported a Plank dock icon as blank when
#: the icon was plainly there, dark art on a dark dock: 24 distinct grey levels,
#: std 8.15, but no adjacent-pixel step above 18. Flagging that would have
#: deleted a correct annotation. A region where nothing is painted is uniform,
#: so measure uniformity directly. The real blanks measure std 0.00 over a
#: single grey value; the softest true content seen measures std 0.32.
BLANK_STD = 1.5
BLANK_RANGE = 4

#: Shrink each fragment before measuring. A widget flush against a window edge
#: includes a pixel or two of whatever is behind the window, and that boundary
#: step alone made four flat scrollbars look like content.
EDGE_EROSION = 1

#: Roles that cannot be visible and blank at once. Containers, cells, text
#: bodies and separators are all legitimately blank and are excluded.
MUST_DRAW_ROLES = {
    "push button", "toggle button", "check box", "radio button", "check menu item",
    "radio menu item", "combo box", "menu item", "page tab", "icon", "image",
    "slider", "spin button", "scroll bar", "link", "tree item",
}

#: Widgets thinner than this (after erosion) are slivers whose pixels are hard
#: to attribute; they are counted separately rather than scored.
MIN_WIDGET_SIDE = 4


def region_is_flat(gray: np.ndarray, boxes: List[Tuple[int, int, int, int]]) -> Tuple[bool, float, int]:
    """Is nothing painted across all of `boxes`?

    Returns (flat, std, range) over the union of the eroded fragments. Measuring
    the fragments together rather than one at a time matters for a widget split
    by an overlapping window: each piece may be uniform on its own while the
    pieces differ from each other, which is still content.
    """
    values: List[np.ndarray] = []
    for x0, y0, x1, y1 in boxes:
        ex0, ey0 = x0 + EDGE_EROSION, y0 + EDGE_EROSION
        ex1, ey1 = x1 - EDGE_EROSION, y1 - EDGE_EROSION
        if ex1 - ex0 < MIN_WIDGET_SIDE or ey1 - ey0 < MIN_WIDGET_SIDE:
            continue
        values.append(gray[ey0:ey1, ex0:ex1].reshape(-1))
    if not values:
        return False, -1.0, -1
    joined = np.concatenate(values).astype(np.int32)
    std = float(joined.std())
    rng = int(joined.max() - joined.min())
    return (std < BLANK_STD and rng <= BLANK_RANGE), std, rng


def _clip(rect: Dict[str, Any], width: int, height: int) -> Optional[Tuple[int, int, int, int]]:
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
    """On-screen rectangles the element actually claims to be showing."""
    frags = elem.get("visible_fragments")
    boxes: List[Tuple[int, int, int, int]] = []
    if isinstance(frags, list):
        # An empty list means nothing visible: return empty rather than falling
        # back to the rect, which would measure the covering window's pixels.
        for frag in frags:
            if isinstance(frag, dict):
                box = _clip(frag, width, height)
                if box is not None:
                    boxes.append(box)
        return boxes
    rect = elem.get("rect")
    if isinstance(rect, dict):
        box = _clip(rect, width, height)
        if box is not None:
            boxes.append(box)
    return boxes


def audit_capture(png: Path, elements: List[Dict[str, Any]]) -> Dict[str, Any]:
    with Image.open(png) as img:
        gray = np.array(img.convert("L"))
    height, width = gray.shape

    blank: List[Dict[str, Any]] = []
    offscreen: List[Dict[str, Any]] = []
    slivers = 0
    checked = 0
    seen_rects: Dict[Tuple[Any, ...], int] = Counter()

    for elem in elements:
        role = str(elem.get("role") or "").strip().lower()
        rect = elem.get("rect") or {}
        key = (role, rect.get("x"), rect.get("y"), rect.get("w"), rect.get("h"))
        seen_rects[key] += 1

        box = _clip(rect, width, height)
        if box is None:
            offscreen.append({
                "role": role, "app_name": elem.get("app_name"),
                "rect": dict(rect) if isinstance(rect, dict) else None,
                "name": (elem.get("name") or "")[:40],
            })
            continue

        if role not in MUST_DRAW_ROLES:
            continue
        boxes = visible_boxes(elem, width, height)
        if not boxes:
            # Nothing visible claimed: correctly annotated as hidden.
            continue
        flat, std, rng = region_is_flat(gray, boxes)
        if std < 0:
            # Every fragment was too thin to measure once eroded.
            slivers += 1
            continue

        checked += 1
        if flat:
            blank.append({
                "role": role,
                "app_name": elem.get("app_name"),
                "name": (elem.get("name") or elem.get("visible_text") or "")[:40],
                "rect": dict(rect),
                "visible_area": sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in boxes),
                "std": round(std, 3),
                "range": rng,
                "is_occluded": bool(elem.get("is_occluded")),
                "occlusion_state": elem.get("occlusion_state"),
            })

    duplicates = [{"role": k[0], "rect": {"x": k[1], "y": k[2], "w": k[3], "h": k[4]},
                   "count": n} for k, n in seen_rects.items() if n > 1]

    return {
        "capture": png.name,
        "num_elements": len(elements),
        "num_checked": checked,
        "num_blank": len(blank),
        "num_offscreen": len(offscreen),
        "num_duplicate_rects": len(duplicates),
        "num_slivers": slivers,
        "blank": blank[:40],
        "offscreen": offscreen[:20],
        "duplicates": sorted(duplicates, key=lambda d: -d["count"])[:20],
    }


def iter_captures(root: Path, limit: Optional[int]) -> Iterable[Tuple[Path, Path]]:
    seen = 0
    for leaf in sorted(root.rglob("*.elements.leaf.json")):
        png = leaf.with_name(leaf.name.replace(".elements.leaf.json", ".png"))
        if not png.is_file():
            continue
        yield png, leaf
        seen += 1
        if limit and seen >= limit:
            return


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--output", default=None)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    captures: List[Dict[str, Any]] = []
    by_app: Dict[str, Dict[str, int]] = defaultdict(lambda: {"checked": 0, "blank": 0})
    by_role: Counter = Counter()
    totals = Counter()

    for png, leaf in iter_captures(Path(args.root), args.limit):
        data = json.loads(leaf.read_text(encoding="utf-8"))
        elements = data if isinstance(data, list) else data.get("elements", [])
        try:
            row = audit_capture(png, elements)
        except Exception as exc:
            print(f"  !! {png.name}: {exc}")
            continue
        captures.append(row)
        for key in ("num_checked", "num_blank", "num_offscreen",
                    "num_duplicate_rects", "num_slivers"):
            totals[key] += row[key]
        for entry in row["blank"]:
            by_app[str(entry["app_name"])]["blank"] += 1
            by_role[entry["role"]] += 1
        for elem in elements:
            role = str(elem.get("role") or "").strip().lower()
            if role in MUST_DRAW_ROLES:
                by_app[str(elem.get("app_name"))]["checked"] += 1

    checked = totals["num_checked"]
    blank = totals["num_blank"]
    print(f"captures={len(captures)}  must-draw widgets checked={checked}")
    print(f"blank (annotated, nothing drawn): {blank}"
          f"  ({blank / max(1, checked) * 100:.2f}%)")
    print(f"offscreen rects: {totals['num_offscreen']}   "
          f"duplicate rects: {totals['num_duplicate_rects']}   "
          f"slivers skipped: {totals['num_slivers']}\n")

    if by_role:
        print("blank by role:")
        for role, n in by_role.most_common(15):
            print(f"  {role:22}{n:>6}")
        print()
    ranked = sorted(by_app.items(), key=lambda kv: -kv[1]["blank"])
    print(f"{'app':22}{'checked':>9}{'blank':>8}{'rate':>9}")
    print("-" * 50)
    for app, stats in ranked[:20]:
        if not stats["checked"] and not stats["blank"]:
            continue
        rate = stats["blank"] / max(1, stats["checked"]) * 100
        print(f"{app:22}{stats['checked']:>9}{stats['blank']:>8}{rate:>8.1f}%")

    if args.output:
        out = Path(args.output)
        out.mkdir(parents=True, exist_ok=True)
        (out / "blank_widgets.json").write_text(
            json.dumps({"totals": dict(totals),
                        "by_app": {k: dict(v) for k, v in by_app.items()},
                        "by_role": dict(by_role),
                        "captures": captures}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
