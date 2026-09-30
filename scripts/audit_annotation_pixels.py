#!/usr/bin/env python
"""Check saved annotations against the pixels they claim to describe.

Accessibility trees describe what a widget *believes*, not what it *drew*. That
gap produced a real defect: a HomeBank cell reported the text `$ 5` at a location
where the screenshot is completely blank. It was found by eye, on one element, by
accident - which is a bad way to learn that ground truth is wrong.

This audits a whole corpus of existing captures. Three checks, chosen because
each has a clear failure mode and low false-positive rate:

*Text phantoms* - an element serializes visible text but its box contains no ink.
Whatever it claims, nothing is drawn there.

*Unclaimed ink beside a text box* - inked pixels next to an element that no
element box covers. A box displaced from its content leaves its own glyphs
unclaimed, whereas a merely dense neighbourhood does not, because the neighbours
have boxes of their own. Comparing against a padded neighbourhood instead was
tried first and reported 65% of elements as drifted: in a table the neighbours'
ink dominates, so the measure said nothing.

*Uncovered ink* - the fraction of inked pixels no element box contains. High
values mean content is on screen that the annotations never mention.

Ink is detected by local gradient rather than darkness, so light-on-dark themes
are handled the same as dark-on-light.

Usage:
    PYTHONPATH=src python scripts/audit_annotation_pixels.py \
        --root incremental_checks --output incremental_checks/vXXX/audit
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# A box smaller than this is mostly border and its ink test is unreliable.
MIN_AUDIT_SIDE = 4
# Gradient magnitude above which a pixel counts as inked.
INK_GRADIENT = 18
# Inked pixels an element must hold per character before it counts as drawn, and
# the floor below which any box is blank whatever it claims. Judging ink as a
# fraction of box area instead called a calculator's "." button a phantom: a dot
# is a handful of pixels in a 105x28 box, so the ratio is tiny and the button is
# perfectly real. Every genuine glyph contributes far more than one inked pixel,
# so counting per character stays sensitive to a blank box while never faulting a
# small glyph in a large one.
MIN_INK_PER_CHAR = 1.0
MIN_INK_ABSOLUTE = 4
# Unclaimed ink beside a text box, relative to the ink inside it, above which the
# box looks displaced from its content.
DRIFT_UNCLAIMED_RATIO = 1.0


def ink_mask(gray: np.ndarray) -> np.ndarray:
    """Inked pixels, via local gradient so themes of either polarity work."""
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
    if w < MIN_AUDIT_SIDE or h < MIN_AUDIT_SIDE:
        return None
    return x, y, w, h


def _serialized_text(elem: Dict[str, Any]) -> str:
    """The text this element would put into ScreenTag.

    Mirrors `run_extraction._serialized_text`: an empty `visible_text` is an
    answer ("this element draws no glyphs"), not missing data, so it must not
    fall through to `inner_text`. Falling through made the audit score strings
    the pipeline does not emit - an icon button whose name is withheld was still
    being checked for the ink of that name.
    """
    # Any decision counts, including "None". Listing the statuses that mean
    # "withheld" was the bug: `no_ink` and `name_only` were added later, set
    # `visible_text` to None like the others, fell through to `inner_text`, and
    # the audit went on scoring the accessible name the pipeline had just
    # refused to emit - phantom_rate did not move when the emission stopped.
    # The presence of the key is the signal; only a genuinely unset one falls
    # back.
    if "visible_text" in elem:
        val = elem["visible_text"]
        return val if isinstance(val, str) else ""
    val = elem.get("inner_text")
    return val if isinstance(val, str) else ""


def audit_capture(png: Path, elements: List[Dict[str, Any]]) -> Dict[str, Any]:
    with Image.open(png) as img:
        gray = np.array(img.convert("L"))
    ink = ink_mask(gray)
    height, width = ink.shape
    covered = np.zeros_like(ink, dtype=bool)

    phantoms: List[Dict[str, Any]] = []
    drifted: List[Dict[str, Any]] = []
    text_boxes: List[Tuple[Dict[str, Any], Tuple[int, int, int, int], int]] = []
    n_text = 0

    for elem in elements:
        box = _rect(elem)
        if box is None:
            continue
        x, y, w, h = box
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(width, x + w), min(height, y + h)
        if x1 <= x0 or y1 <= y0:
            continue
        covered[y0:y1, x0:x1] = True

        text = _serialized_text(elem)
        if not text:
            continue
        n_text += 1

        patch = ink[y0:y1, x0:x1]
        inside = int(patch.sum())
        ratio = inside / max(1, patch.size)
        n_glyphs = len(text.strip())
        entry = {
            "role": elem.get("role"),
            "type": elem.get("type"),
            "rect": {"x": x, "y": y, "w": w, "h": h},
            "text": text[:40],
            "ink_ratio": round(ratio, 5),
            "ink_pixels": inside,
            "expected_ink": max(MIN_INK_ABSOLUTE, int(MIN_INK_PER_CHAR * n_glyphs)),
        }

        if inside < entry["expected_ink"]:
            phantoms.append(entry)
            continue
        text_boxes.append((entry, (x0, y0, x1, y1), inside))

    # Second pass, once every box is known: ink beside a text element that no
    # element claims. Neighbours with their own boxes do not count against it.
    unclaimed = ink & ~covered
    for entry, (x0, y0, x1, y1), inside in text_boxes:
        pad = max(20, (x1 - x0))
        nx0, nx1 = max(0, x0 - pad), min(width, x1 + pad)
        beside = int(unclaimed[y0:y1, nx0:nx1].sum())
        if inside > 0 and beside / inside > DRIFT_UNCLAIMED_RATIO:
            entry["unclaimed_beside"] = beside
            entry["ink_inside"] = inside
            drifted.append(entry)

    total_ink = int(ink.sum())
    uncovered = int((ink & ~covered).sum())
    return {
        "capture": png.name,
        "num_elements": len(elements),
        "num_text_elements": n_text,
        "num_text_phantoms": len(phantoms),
        "num_text_drifted": len(drifted),
        "ink_uncovered_ratio": round(uncovered / max(1, total_ink), 4),
        "phantoms": phantoms[:20],
        "drifted": drifted[:20],
    }


def _app_of(name: str) -> str:
    stem = name.split(".")[0]
    if stem.startswith("scene-"):
        return "scene"
    return stem.rsplit("-", 1)[0] if "-" in stem else stem


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
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="incremental_checks")
    ap.add_argument("--output", required=True)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    rows: List[Dict[str, Any]] = []
    by_app: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_role: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for png, leaf in iter_captures(Path(args.root), args.limit or None):
        try:
            data = json.loads(leaf.read_text(encoding="utf-8"))
        except Exception:
            continue
        elements = data if isinstance(data, list) else data.get("elements", [])
        if not isinstance(elements, list):
            continue
        try:
            row = audit_capture(png, elements)
        except Exception as exc:  # a corrupt capture must not stop the sweep
            print(f"  !! {png.name}: {exc}", flush=True)
            continue

        app = _app_of(png.name)
        row["app"] = app
        rows.append(row)
        by_app[app]["captures"] += 1
        by_app[app]["text_elements"] += row["num_text_elements"]
        by_app[app]["phantoms"] += row["num_text_phantoms"]
        by_app[app]["drifted"] += row["num_text_drifted"]
        for entry in row["phantoms"]:
            by_role[str(entry.get("role"))]["phantoms"] += 1
        for entry in row["drifted"]:
            by_role[str(entry.get("role"))]["drifted"] += 1

        if row["num_text_phantoms"] or row["num_text_drifted"]:
            print(
                f"  {png.name[:52]:54} phantoms={row['num_text_phantoms']:3} "
                f"drift={row['num_text_drifted']:3} uncovered_ink={row['ink_uncovered_ratio']}",
                flush=True,
            )

    tot_text = sum(r["num_text_elements"] for r in rows)
    tot_ph = sum(r["num_text_phantoms"] for r in rows)
    tot_dr = sum(r["num_text_drifted"] for r in rows)
    uncovered = [r["ink_uncovered_ratio"] for r in rows]

    report = {
        "captures": len(rows),
        "text_elements": tot_text,
        "text_phantoms": tot_ph,
        "text_drifted": tot_dr,
        "phantom_rate": round(tot_ph / max(1, tot_text), 5),
        "drift_rate": round(tot_dr / max(1, tot_text), 5),
        "ink_uncovered_mean": round(float(np.mean(uncovered)), 4) if uncovered else None,
        "ink_uncovered_p90": round(float(np.percentile(uncovered, 90)), 4) if uncovered else None,
        "by_app": {k: dict(v) for k, v in sorted(by_app.items())},
        "by_role": {k: dict(v) for k, v in sorted(by_role.items())},
        "rows": rows,
    }
    (out / "pixel_audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(
        f"\ncaptures={report['captures']} text_elements={tot_text} "
        f"phantoms={tot_ph} ({report['phantom_rate']:.2%}) "
        f"drifted={tot_dr} ({report['drift_rate']:.2%}) "
        f"uncovered_ink mean={report['ink_uncovered_mean']} p90={report['ink_uncovered_p90']}"
    )
    worst = sorted(by_app.items(), key=lambda kv: -(kv[1]["phantoms"] + kv[1]["drifted"]))[:10]
    print("\nworst apps (phantoms + drift):")
    for app, stats in worst:
        if not (stats["phantoms"] or stats["drifted"]):
            continue
        print(
            f"  {app:24} captures={stats['captures']:3} text={stats['text_elements']:5} "
            f"phantoms={stats['phantoms']:4} drifted={stats['drifted']:4}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
