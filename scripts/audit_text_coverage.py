#!/usr/bin/env python
"""Read the screen, then ask whether the annotations account for what it says.

Every other audit here reasons about *boxes*. That leaves one blind spot they
cannot see between them: a large element whose box covers text it does not
describe. Coverage says the pixels are inside an annotation, the blank-widget
check says something is drawn, the phantom check only looks at elements that
already claim text - so gucharmap's character table, dozens of plainly legible
glyphs, passed all three while being annotated as a single `drawing area` with
`no_text` and measured 1.0% uncovered ink.

OCR is the only instrument that notices, because it starts from the screen
rather than from the annotation list.

Every detected string lands in one of three buckets:

`matched`     some element overlapping it carries that text. The good case.
`unlabelled`  an element covers the pixels but no element carries the words -
              the gucharmap case, and the one worth acting on.
`uncovered`   nothing covers it at all. Usually also caught by the coverage
              audit; reported here so the two can be cross-checked.

Text drawn *inside* a picture is separated out rather than counted against the
pipeline: a photograph of a sign is content, not UI, and annotating its words
would be wrong. That is decided by the covering element's kind, not by guessing.

Needs `rapidocr_onnxruntime` (pip, self-contained ONNX, no system packages).
It is optional on purpose - this reports on ground truth, it does not produce
any, so a missing dependency must not silently change what is emitted.

Usage:
    PYTHONPATH=src python scripts/audit_text_coverage.py --root <run> [--output <dir>]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

#: Below this OCR confidence a reading is too unreliable to hold the pipeline to.
MIN_CONFIDENCE = 0.75

#: Very short strings are mostly OCR noise off borders and icons.
MIN_CHARS = 2

#: Kinds whose job is to show pixels. Text inside one of these is content the
#: screen is displaying, not a control the pipeline failed to annotate.
PICTURE_KINDS = frozenset({"image", "icon", "canvas"})


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text).lower())


def _rect(elem: Dict[str, Any]) -> Optional[Tuple[int, int, int, int]]:
    r = elem.get("rect")
    if not isinstance(r, dict):
        return None
    try:
        x, y, w, h = int(r["x"]), int(r["y"]), int(r["w"]), int(r["h"])
    except (KeyError, TypeError, ValueError):
        return None
    return (x, y, w, h) if w > 0 and h > 0 else None


def _overlaps(a, b, *, min_ratio: float = 0.5) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    return (ix * iy) >= min_ratio * max(1, aw * ah)


def load_ocr():
    try:
        from rapidocr_onnxruntime import RapidOCR
    except Exception as exc:  # pragma: no cover - environment dependent
        raise SystemExit(
            "audit_text_coverage needs rapidocr_onnxruntime:\n"
            "    pip install --user rapidocr_onnxruntime\n"
            f"({exc})"
        )
    return RapidOCR()


def audit_capture(ocr, png: Path, elements: List[Dict[str, Any]]) -> Dict[str, Any]:
    result, _ = ocr(str(png))
    boxes = []
    for entry in (result or []):
        quad, text, score = entry[0], entry[1], float(entry[2])
        if score < MIN_CONFIDENCE or len(str(text).strip()) < MIN_CHARS:
            continue
        xs = [p[0] for p in quad]; ys = [p[1] for p in quad]
        boxes.append((
            (int(min(xs)), int(min(ys)), int(max(xs) - min(xs)), int(max(ys) - min(ys))),
            str(text), score,
        ))

    annotated = []
    for elem in elements:
        box = _rect(elem)
        if box is None:
            continue
        annotated.append((box, elem))

    rows: List[Dict[str, Any]] = []
    for box, text, score in boxes:
        wanted = _norm(text)
        covering = [(b, e) for b, e in annotated if _overlaps(box, b)]
        # The smallest covering element is the one that should be describing it.
        covering.sort(key=lambda be: be[0][2] * be[0][3])
        matched = any(wanted and wanted in _norm(e.get("visible_text") or "")
                      for _b, e in covering)
        if matched:
            bucket = "matched"
        elif not covering:
            bucket = "uncovered"
        elif any(str(e.get("kind")) in PICTURE_KINDS for _b, e in covering[:1]):
            bucket = "in_picture"
        else:
            bucket = "unlabelled"
        owner = covering[0][1] if covering else None
        rows.append({
            "text": text[:60], "score": round(score, 3), "bucket": bucket,
            "rect": {"x": box[0], "y": box[1], "w": box[2], "h": box[3]},
            "app_name": (owner or {}).get("app_name"),
            "owner_kind": (owner or {}).get("kind"),
            "owner_role": (owner or {}).get("role"),
        })
    return {"capture": png.name, "regions": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--output", default=None)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    ocr = load_ocr()
    buckets: Counter = Counter()
    by_app: Dict[str, Counter] = defaultdict(Counter)
    captures: List[Dict[str, Any]] = []

    leaves = sorted(Path(args.root).rglob("*.elements.leaf.json"))
    if args.limit:
        leaves = leaves[:args.limit]
    for leaf in leaves:
        png = leaf.with_name(leaf.name.replace(".elements.leaf.json", ".png"))
        if not png.is_file():
            continue
        data = json.loads(leaf.read_text(encoding="utf-8"))
        elements = data if isinstance(data, list) else data.get("elements", [])
        row = audit_capture(ocr, png, elements)
        captures.append(row)
        for region in row["regions"]:
            buckets[region["bucket"]] += 1
            by_app[str(region["app_name"])][region["bucket"]] += 1

    total = sum(buckets.values())
    print(f"captures={len(captures)}  text regions read={total}\n")
    for name in ("matched", "unlabelled", "uncovered", "in_picture"):
        n = buckets[name]
        print(f"  {name:12}{n:6}  {n / max(1, total) * 100:5.1f}%")

    print(f"\n{'app':22}{'read':>7}{'matched':>9}{'unlabelled':>12}{'uncovered':>11}")
    print("-" * 62)
    ranked = sorted(by_app.items(), key=lambda kv: -(kv[1]["unlabelled"] + kv[1]["uncovered"]))
    for app, counts in ranked[:16]:
        read = sum(counts.values())
        print(f"{app[:21]:22}{read:>7}{counts['matched']:>9}"
              f"{counts['unlabelled']:>12}{counts['uncovered']:>11}")

    if args.output:
        out = Path(args.output)
        out.mkdir(parents=True, exist_ok=True)
        (out / "text_coverage.json").write_text(
            json.dumps({"buckets": dict(buckets),
                        "by_app": {k: dict(v) for k, v in by_app.items()},
                        "captures": captures}, indent=2, ensure_ascii=False),
            encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
