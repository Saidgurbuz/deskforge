#!/usr/bin/env python
"""Does the window-control detector find the right buttons, on every theme?

Server-side-decorated windows have no accessible objects for close/minimise/
maximise - xfwm4 draws them and publishes nothing - so they can only be found in
pixels, and a pixel detector is only worth wiring in if it is right. Emitting a
partial or mis-ordered set puts false positives into ground truth, which is
worse than the known gap.

So this measures rather than asserts. It gathers every synthesized title bar in
a set of runs, runs the detector over each, and writes:

- `controls.json` - one row per bar: theme, style, app, band, detected rects
- `controls.png`  - the bands stacked, with each detected cell outlined, so the
  result can be *looked at* instead of trusted

Read the sheet before believing the counts. Both prior attempts at this detector
passed their own numbers and were wrong on the pixels.

Usage:
    PYTHONPATH=src python scripts/measure_window_controls.py \
        --root incremental_checks/v226_geometry/batch --output <dir>
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
from PIL import Image, ImageDraw

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from deskshot.extraction.window_controls import detect_controls, name_controls  # noqa: E402

#: Bands wider than this are cropped for the sheet - only the ends matter.
SHEET_END_WIDTH = 320


def collect_bands(roots: List[Path]) -> List[Dict[str, Any]]:
    bands: List[Dict[str, Any]] = []
    for root in roots:
        for leaf in sorted(root.rglob("*.elements.leaf.json")):
            png = leaf.with_name(leaf.name.replace(".elements.leaf.json", ".png"))
            meta = leaf.with_name(leaf.name.replace(".elements.leaf.json", ".meta.json"))
            if not png.exists() or not meta.exists():
                continue
            try:
                elements = json.loads(leaf.read_text(encoding="utf-8"))
                theme = json.loads(meta.read_text(encoding="utf-8")).get("theme", {})
            except Exception:
                continue
            for elem in elements:
                if str(elem.get("role")) != "title bar":
                    continue
                if (elem.get("attrs") or {}).get("synthesized") != "title_bar":
                    continue
                bands.append({
                    "png": str(png),
                    "app": elem.get("app_name"),
                    "band": elem.get("rect"),
                    "gtk_theme": theme.get("gtk_theme"),
                    "desktop_style": theme.get("desktop_style"),
                })
    return bands


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", action="append", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--limit-per-theme", type=int, default=6)
    args = ap.parse_args()

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    bands = collect_bands([Path(r) for r in args.root])

    rows: List[Dict[str, Any]] = []
    for entry in bands:
        band = entry["band"]
        if not isinstance(band, dict) or band.get("w", 0) < 40 or band.get("h", 0) < 8:
            continue
        with Image.open(entry["png"]) as img:
            gray = np.array(img.convert("L"))
        # `detect_controls` decides the side from the pixels rather than from a
        # style name; this script kept passing the style it no longer takes and
        # crashed on its first row, so the measurement had quietly stopped
        # running at all.
        rects, side = detect_controls(gray, band)
        rows.append({
            **entry,
            "num_controls": len(rects),
            "side": side,
            "rects": rects,
            "names": name_controls(len(rects), side),
        })

    (out_dir / "controls.json").write_text(
        json.dumps(rows, indent=2), encoding="utf-8"
    )

    by_style: Dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        by_style[f"{row['desktop_style']} / {row['gtk_theme']}"][row["num_controls"]] += 1
    print(f"{'style / gtk theme':44}{'bars':>6}  counts detected")
    for key in sorted(by_style):
        counts = by_style[key]
        total = sum(counts.values())
        spread = " ".join(f"{n}x{c}" for n, c in sorted(counts.items()))
        print(f"{key:44}{total:>6}  {spread}")
    found = sum(1 for r in rows if r["num_controls"])
    print(f"\nbars with controls detected: {found}/{len(rows)}")

    _write_sheet(rows, out_dir / "controls.png", args.limit_per_theme)
    print(f"sheet: {out_dir / 'controls.png'}")
    return 0


def _write_sheet(rows: List[Dict[str, Any]], path: Path, per_theme: int) -> None:
    """Stack the bands with their detections drawn, ends only."""
    picked: List[Dict[str, Any]] = []
    seen: Counter = Counter()
    for row in rows:
        key = (row["desktop_style"], row["gtk_theme"], row["app"])
        if seen[key] >= 1 or seen[(row["desktop_style"], row["gtk_theme"])] >= per_theme:
            continue
        seen[key] += 1
        seen[(row["desktop_style"], row["gtk_theme"])] += 1
        picked.append(row)
    if not picked:
        return

    crops = []
    for row in picked:
        band = row["band"]
        with Image.open(row["png"]) as img:
            full = img.convert("RGB")
        bx, by, bw, bh = band["x"], band["y"], band["w"], band["h"]
        keep_left = row["side"] != "right"
        x0 = bx if keep_left else max(bx, bx + bw - SHEET_END_WIDTH)
        x1 = min(full.width, x0 + min(bw, SHEET_END_WIDTH))
        crop = full.crop((max(0, x0), max(0, by), x1, min(full.height, by + bh)))
        draw = ImageDraw.Draw(crop)
        for rect in row["rects"]:
            draw.rectangle(
                [rect["x"] - x0, rect["y"] - by,
                 rect["x"] - x0 + rect["w"] - 1, rect["y"] - by + rect["h"] - 1],
                outline=(255, 0, 0),
            )
        crops.append((row, crop))

    scale = 2
    width = max(c.width for _r, c in crops) * scale + 460
    height = sum((c.height * scale) + 10 for _r, c in crops) + 10
    sheet = Image.new("RGB", (width, height), (32, 32, 32))
    draw = ImageDraw.Draw(sheet)
    y = 5
    for row, crop in crops:
        big = crop.resize((crop.width * scale, crop.height * scale), Image.NEAREST)
        sheet.paste(big, (5, y))
        label = (
            f"{row['app']} | {row['desktop_style']} | {row['gtk_theme']} | "
            f"{row['side'] or '-'} {row['names']}"
        )
        draw.text((big.width + 15, y + 4), label, fill=(230, 230, 230))
        y += big.height + 10
    sheet.save(path)


if __name__ == "__main__":
    raise SystemExit(main())
