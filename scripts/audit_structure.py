#!/usr/bin/env python
"""Invariants that must hold, so a violation is a bug rather than an opinion.

The pixel audits answer "does this box have content behind it", which needs
thresholds, and thresholds mean judgment calls in both directions - a dock icon
called blank, a calculator's "." called a phantom. This file asks a different
kind of question: things the annotation set must satisfy *by construction*, no
matter the theme, the toolkit or the layout. There is no threshold to tune and
no correct case to punish, so these can be strict.

Four invariants, each with a failure this project has actually shipped:

`covered_but_visible` - an element is annotated as fully visible while a window
above it in the stack completely covers where it sits. Both cannot be true. This
is the shape of the z-order bug that annotated a buried Chromium window as if it
were on top while dropping 165 of HomeBank's elements.

`escapes_parent` - a child's box is not inside its parent's. Real containment
can be violated legitimately by popups and tooltips, which are excluded, but a
button that sticks out of the toolbar holding it is a geometry error.

`duplicate_leaf` - two leaves with the same role and the same rect. One widget,
annotated twice: it inflates counts and teaches a model to predict a box twice.

`missing_window_controls` - a window with a title bar but no close/minimise/
maximise annotated. They are interactive elements a person uses constantly, and
they go missing per-toolkit rather than uniformly, so a per-window count is the
only way to see it.

Usage:
    PYTHONPATH=src python scripts/audit_structure.py --root <run> [--output <dir>]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

WINDOW_ROLES = {"frame", "window", "dialog", "alert", "file chooser"}

#: Roles that legitimately leave their parent's box: menus and tooltips are
#: drawn in their own X windows and are routinely larger than the widget that
#: owns them.
ESCAPE_ALLOWED_ROLES = {
    "menu", "menu item", "check menu item", "radio menu item", "popup menu",
    "tool tip", "window", "dialog", "frame", "alert", "combo box", "list box",
    # A GtkNotebook tab is its *label*, a 57x24 box, while its children are the
    # page's contents, drawn in the notebook body a couple of hundred pixels
    # away. The AT-SPI parent link really does say so, so those children escape
    # their parent by construction and there is nothing to fix in the capture.
    # This accounted for 36 of 50 reported escapes in one run, all in eog's
    # preferences dialog, and made the metric unreadable.
    "page tab", "page tab list",
}

#: A child may stick out by this many pixels before it counts as escaping. GTK
#: draws focus rings and shadows a pixel or two outside the allocation.
ESCAPE_SLACK = 3

#: Names window controls go by across the toolkits in the pool.
CONTROL_NAMES = {
    "close", "minimize", "minimise", "maximize", "maximise", "restore",
    "unmaximize", "fullscreen", "shade", "roll up",
}

#: Only windows at least this big are expected to carry controls; popups and
#: tooltips have none by design.
MIN_DECORATED_WINDOW = 200

#: How far below a window's top edge the controls can sit.
TITLE_BAR_BAND = 64


def _rect(elem: Dict[str, Any]) -> Optional[Tuple[int, int, int, int]]:
    r = elem.get("rect")
    if not isinstance(r, dict):
        return None
    try:
        x, y, w, h = int(r["x"]), int(r["y"]), int(r["w"]), int(r["h"])
    except (KeyError, TypeError, ValueError):
        return None
    return (x, y, w, h) if w > 0 and h > 0 else None


def _covers(outer: Tuple[int, int, int, int], inner: Tuple[int, int, int, int]) -> bool:
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return ox <= ix and oy <= iy and ox + ow >= ix + iw and oy + oh >= iy + ih


def _stack(elem: Dict[str, Any]) -> Optional[int]:
    value = elem.get("_window_stack_index")
    return value if isinstance(value, int) else None


def _is_visible(elem: Dict[str, Any]) -> bool:
    """Annotated as showing something: not occluded, or with visible fragments."""
    if elem.get("is_occluded"):
        frags = elem.get("visible_fragments")
        return bool(frags) if isinstance(frags, list) else True
    return True


def check_capture(elements: List[Dict[str, Any]]) -> Dict[str, Any]:
    windows = []
    for elem in elements:
        if str(elem.get("role") or "").strip().lower() in WINDOW_ROLES:
            box, stack = _rect(elem), _stack(elem)
            if box and stack is not None:
                windows.append({"box": box, "stack": stack, "elem": elem})

    covered_but_visible: List[Dict[str, Any]] = []
    for elem in elements:
        if str(elem.get("role") or "").strip().lower() in WINDOW_ROLES:
            continue
        box, stack = _rect(elem), _stack(elem)
        if box is None or stack is None:
            continue
        if elem.get("is_occluded"):
            continue  # already says it is covered; nothing to contradict
        for win in windows:
            if win["stack"] <= stack:
                continue
            if _covers(win["box"], box):
                covered_but_visible.append({
                    "app_name": elem.get("app_name"),
                    "role": elem.get("role"),
                    "rect": dict(elem["rect"]),
                    "stack": stack,
                    "covered_by": {
                        "name": win["elem"].get("name"),
                        "stack": win["stack"],
                    },
                })
                break

    # Resolve parents in the index space the leaf export actually preserves.
    #
    # Leaves are re-indexed when the export is built, so `_dom_index` and
    # `_parent_dom_index` are leaf-local and a parent link read from them lands
    # on an unrelated element: this check reported a mousepad text area whose
    # "parent" was a desktop icon, and a split pane parented to another icon -
    # 27 impossible escapes. `_source_*` are the links into the tree the
    # hierarchy came from, so use those and skip when neither resolves rather
    # than compare rectangles that were never related.
    by_source = {int(e["_source_dom_index"]): e for e in elements
                 if isinstance(e.get("_source_dom_index"), int)}
    escapes: List[Dict[str, Any]] = []
    for elem in elements:
        role = str(elem.get("role") or "").strip().lower()
        if role in ESCAPE_ALLOWED_ROLES:
            continue
        parent_idx = elem.get("_source_parent_dom_index")
        parent = by_source.get(parent_idx) if isinstance(parent_idx, int) else None
        if parent is None:
            continue
        if str(parent.get("role") or "").strip().lower() in ESCAPE_ALLOWED_ROLES:
            continue
        box, pbox = _rect(elem), _rect(parent)
        if box is None or pbox is None:
            continue
        px, py, pw, ph = pbox
        grown = (px - ESCAPE_SLACK, py - ESCAPE_SLACK,
                 pw + 2 * ESCAPE_SLACK, ph + 2 * ESCAPE_SLACK)
        if not _covers(grown, box):
            escapes.append({
                "app_name": elem.get("app_name"),
                "role": elem.get("role"),
                "rect": dict(elem["rect"]),
                "parent_role": parent.get("role"),
                "parent_rect": dict(parent["rect"]),
            })

    seen: Counter = Counter()
    for elem in elements:
        box = _rect(elem)
        if box:
            seen[(str(elem.get("role") or "").strip().lower(), box)] += 1
    duplicates = [{"role": k[0], "rect": dict(zip("xywh", k[1])), "count": n}
                  for k, n in seen.items() if n > 1]

    missing_controls: List[Dict[str, Any]] = []
    window_control_counts: List[Dict[str, Any]] = []
    for win in windows:
        wx, wy, ww, wh = win["box"]
        if ww < MIN_DECORATED_WINDOW or wh < MIN_DECORATED_WINDOW:
            continue
        found = set()
        for elem in elements:
            box = _rect(elem)
            if box is None:
                continue
            ex, ey, _ew, _eh = box
            if not (wx <= ex <= wx + ww and wy <= ey <= wy + TITLE_BAR_BAND):
                continue
            name = str(elem.get("name") or elem.get("visible_text") or "").strip().lower()
            for control in CONTROL_NAMES:
                if control == name or name.startswith(control + " "):
                    found.add("close" if control == "close" else
                              "minimize" if control.startswith("mini") else
                              "maximize")
        row = {
            "app_name": win["elem"].get("app_name"),
            "title": str(win["elem"].get("name") or "")[:40],
            "rect": {"x": wx, "y": wy, "w": ww, "h": wh},
            "controls": sorted(found),
        }
        window_control_counts.append(row)
        if not found:
            missing_controls.append(row)

    return {
        "covered_but_visible": covered_but_visible,
        "escapes_parent": escapes,
        "duplicate_leaf": duplicates,
        "missing_window_controls": missing_controls,
        "windows_checked": window_control_counts,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--output", default=None)
    args = ap.parse_args()

    totals: Counter = Counter()
    per_app: Dict[str, Counter] = defaultdict(Counter)
    captures: List[Dict[str, Any]] = []
    control_rows: List[Dict[str, Any]] = []

    for leaf in sorted(Path(args.root).rglob("*.elements.leaf.json")):
        data = json.loads(leaf.read_text(encoding="utf-8"))
        elements = data if isinstance(data, list) else data.get("elements", [])
        row = check_capture(elements)
        row["capture"] = leaf.name
        captures.append(row)
        control_rows.extend(row["windows_checked"])
        for key in ("covered_but_visible", "escapes_parent",
                    "duplicate_leaf", "missing_window_controls"):
            totals[key] += len(row[key])
            for entry in row[key]:
                per_app[str(entry.get("app_name"))][key] += 1

    print(f"captures={len(captures)}\n")
    for key in ("covered_but_visible", "escapes_parent",
                "duplicate_leaf", "missing_window_controls"):
        print(f"  {key:26}{totals[key]:6}")

    windows_total = len(control_rows)
    with_any = sum(1 for r in control_rows if r["controls"])
    print(f"\nwindow controls: {with_any}/{windows_total} decorated windows have any")
    by_app_controls: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    for row in control_rows:
        by_app_controls[str(row["app_name"])][1] += 1
        if row["controls"]:
            by_app_controls[str(row["app_name"])][0] += 1
    print(f"{'app':22}{'with':>6}{'total':>7}")
    print("-" * 36)
    for app, (has, total) in sorted(by_app_controls.items(), key=lambda kv: kv[1][0] / max(1, kv[1][1])):
        print(f"{app:22}{has:>6}{total:>7}")

    if per_app:
        print(f"\n{'app':22}" + "".join(f"{k[:12]:>14}" for k in
              ("covered_vis", "escapes", "duplicate", "no_controls")))
        print("-" * 78)
        for app, counts in sorted(per_app.items(), key=lambda kv: -sum(kv[1].values())):
            print(f"{app:22}"
                  f"{counts['covered_but_visible']:>14}"
                  f"{counts['escapes_parent']:>14}"
                  f"{counts['duplicate_leaf']:>14}"
                  f"{counts['missing_window_controls']:>14}")

    if args.output:
        out = Path(args.output)
        out.mkdir(parents=True, exist_ok=True)
        (out / "structure.json").write_text(
            json.dumps({"totals": dict(totals), "captures": captures}, indent=2),
            encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
