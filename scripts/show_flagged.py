#!/usr/bin/env python
"""Render whatever an audit flagged, so it can be looked at before it is acted on.

The most expensive mistakes in this project were all trusting a number. A drift
metric reported 65% by comparing against neighbours' ink. A phantom metric
called a calculator's "." button a phantom. A gradient-based blank test called a
Plank dock icon blank, and acting on it would have deleted a correct
annotation. Every one was caught the same way - by rendering the flagged region
and looking - and every time that meant writing a throwaway crop script that was
then lost.

    show_flagged.py blank     --root <run> --out sheet.png
    show_flagged.py uncovered --root <run> --out dir/
    show_flagged.py phantom   --root <run> --out sheet.png
    show_flagged.py withheld  --root <run> --out sheet.png [--bucket <name>]

`blank`, `phantom` and `withheld` produce a contact sheet: one tile per flagged
element with its box in red and enough surrounding context to judge it. A tile
showing a visible widget means the audit is wrong; a tile showing flat
background means the annotation is.

`withheld` renders the elements whose text the pipeline refused to emit, split
into the buckets that produced them, because "text was withheld" has several
causes and they need opposite fixes. `--bucket` picks one; without it the split
is printed and every bucket is sampled.

`uncovered` paints the worst window per app: red is inked pixels no annotation
covers, with the thin-rule decoration the coverage audit discounts already
excluded. That is the view that shows a missing widget as a shape you recognise.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

TILE_W, TILE_H = 420, 300
CONTEXT_PAD = 60
COLUMNS = 4


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _captures(root: Path):
    for leaf in sorted(root.rglob("*.elements.leaf.json")):
        png = leaf.with_name(leaf.name.replace(".elements.leaf.json", ".png"))
        if png.is_file():
            yield png, leaf


def _elements(leaf: Path) -> List[Dict[str, Any]]:
    data = json.loads(leaf.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data.get("elements", [])


def _tile(png: Path, rect: Dict[str, int], caption: str, subcaption: str) -> Image.Image:
    with Image.open(png) as img:
        rgb = img.convert("RGB")
        x0 = max(0, int(rect["x"]) - CONTEXT_PAD)
        y0 = max(0, int(rect["y"]) - CONTEXT_PAD)
        x1 = min(rgb.width, int(rect["x"]) + int(rect["w"]) + CONTEXT_PAD)
        y1 = min(rgb.height, int(rect["y"]) + int(rect["h"]) + CONTEXT_PAD)
        crop = rgb.crop((x0, y0, x1, y1))
    draw = ImageDraw.Draw(crop)
    draw.rectangle(
        [int(rect["x"]) - x0, int(rect["y"]) - y0,
         int(rect["x"]) - x0 + int(rect["w"]) - 1,
         int(rect["y"]) - y0 + int(rect["h"]) - 1],
        outline=(255, 0, 0), width=2,
    )
    crop.thumbnail((TILE_W, TILE_H - 26))
    tile = Image.new("RGB", (TILE_W, TILE_H), (250, 250, 250))
    tile.paste(crop, ((TILE_W - crop.width) // 2, 26))
    d = ImageDraw.Draw(tile)
    d.text((6, 4), caption[:62], fill=(0, 0, 0))
    d.text((6, 15), subcaption[:70], fill=(90, 90, 90))
    return tile


def _sheet(tiles: List[Image.Image], out: Path) -> None:
    if not tiles:
        print("nothing flagged - no sheet written")
        return
    rows = (len(tiles) + COLUMNS - 1) // COLUMNS
    sheet = Image.new("RGB", (COLUMNS * TILE_W, rows * TILE_H), (255, 255, 255))
    for i, t in enumerate(tiles):
        sheet.paste(t, ((i % COLUMNS) * TILE_W, (i // COLUMNS) * TILE_H))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    print(f"{len(tiles)} flagged -> {out} ({sheet.width}x{sheet.height})")


def show_blank(root: Path, out: Path, limit: int) -> None:
    audit = _load("abw", "audit_blank_widgets.py")
    tiles = []
    for png, leaf in _captures(root):
        row = audit.audit_capture(png, _elements(leaf))
        for entry in row["blank"]:
            tiles.append(_tile(
                png, entry["rect"],
                f"{entry['app_name']} {entry['role']} "
                f"{entry['rect']['w']}x{entry['rect']['h']} std={entry.get('std')}",
                f"name={entry.get('name', '')!r} occluded={int(bool(entry.get('is_occluded')))}",
            ))
            if limit and len(tiles) >= limit:
                _sheet(tiles, out)
                return
    _sheet(tiles, out)


def show_phantom(root: Path, out: Path, limit: int) -> None:
    audit = _load("aap", "audit_annotation_pixels.py")
    tiles = []
    for png, leaf in _captures(root):
        row = audit.audit_capture(png, _elements(leaf))
        for kind in ("phantoms", "drifted"):
            for entry in row.get(kind, []):
                tiles.append(_tile(
                    png, entry["rect"],
                    f"{kind[:-1]} {entry.get('role')} "
                    f"{entry['rect']['w']}x{entry['rect']['h']} ink={entry.get('ink_pixels')}",
                    f"text={entry.get('text', '')!r}",
                ))
                if limit and len(tiles) >= limit:
                    _sheet(tiles, out)
                    return
    _sheet(tiles, out)


def withheld_bucket(elem: Dict[str, Any]) -> Optional[str]:
    """Why this element's text was withheld, or None if it was not.

    The four causes have nothing in common but their symptom, and three of them
    are bugs while one is correct behaviour, so any judgment about the withheld
    rate has to be made per bucket.
    """
    status = str(elem.get("visible_text_status") or "")
    if status not in ("unsupported_partial", "unsupported_overflow", "name_only"):
        return None
    if status == "unsupported_overflow":
        return "overflow"
    if status == "name_only":
        # Not a failure: the string is an accessible name the box cannot be
        # rendering. Rendered here so that claim can be checked by eye.
        return "name_only"
    source = (elem.get("attrs") or {}).get("text_source")
    if source != "text_iface":
        return "name_sourced"
    if elem.get("source") == "desktop_chrome":
        return "desktop_chrome"
    if elem.get("text_geometry_confidence") is None:
        return "no_geometry"
    return "low_confidence"


def show_withheld(root: Path, out: Path, limit: int, bucket: Optional[str]) -> None:
    from collections import Counter

    counts: Counter = Counter()
    picked: List[Tuple[Path, Dict[str, Any], str]] = []
    for png, leaf in _captures(root):
        for elem in _elements(leaf):
            name = withheld_bucket(elem)
            if name is None:
                continue
            counts[name] += 1
            if bucket and name != bucket:
                continue
            picked.append((png, elem, name))

    total = sum(counts.values())
    print(f"withheld text: {total} elements")
    for name, n in counts.most_common():
        print(f"  {name:16}{n:5}  ({n / max(1, total):.1%})")

    step = max(1, len(picked) // limit) if limit else 1
    tiles = []
    for png, elem, name in picked[::step][:limit or None]:
        rect = elem.get("rect") or {}
        if not rect.get("w") or not rect.get("h"):
            continue
        tiles.append(_tile(
            png, rect,
            f"{name} {elem.get('app_name')} {elem.get('role')} "
            f"{rect['w']}x{rect['h']} conf={elem.get('text_geometry_confidence')}",
            f"text={str(elem.get('inner_text') or '')[:52]!r}",
        ))
    _sheet(tiles, out)


def show_uncovered(root: Path, out: Path, apps: Optional[List[str]]) -> None:
    coverage = _load("aec", "audit_element_coverage.py")
    out.mkdir(parents=True, exist_ok=True)
    best: Dict[str, Tuple[Path, Dict[str, Any]]] = {}

    for png, leaf in _captures(root):
        row = coverage.audit_capture(png, _elements(leaf))
        for win in row["windows"]:
            app = str(win["app_name"])
            if apps and app not in apps:
                continue
            if app not in best or win["uncovered_ratio"] > best[app][1]["uncovered_ratio"]:
                best[app] = (png, win)

    for app, (png, win) in sorted(best.items(), key=lambda kv: -kv[1][1]["uncovered_ratio"]):
        leaf = png.with_name(png.name[:-4] + ".elements.leaf.json")
        elements = _elements(leaf)
        with Image.open(png) as img:
            rgb = np.array(img.convert("RGB"))
            gray = np.array(img.convert("L"))
        ink = coverage.ink_mask(gray)
        height, width = ink.shape
        covered = np.zeros_like(ink, dtype=bool)
        for elem in elements:
            if str(elem.get("role") or "").strip().lower() in coverage.WINDOW_ROLES:
                continue
            for bx, by, bw, bh in coverage.coverage_rects(elem):
                x0, y0 = max(0, bx), max(0, by)
                x1, y1 = min(width, bx + bw), min(height, by + bh)
                if x1 > x0 and y1 > y0:
                    covered[y0:y1, x0:x1] = True
        flagged = ink & ~covered
        uncovered = flagged & ~(flagged & coverage.rule_mask(flagged))

        r = win["rect"]
        sub = (rgb[r["y"]:r["y"] + r["h"], r["x"]:r["x"] + r["w"]] * 0.55).astype(np.uint8)
        sub[uncovered[r["y"]:r["y"] + r["h"], r["x"]:r["x"] + r["w"]]] = [255, 0, 0]
        image = Image.fromarray(sub)
        if image.width > 1500:
            image = image.resize((1500, int(image.height * 1500 / image.width)))
        path = out / f"{app}_{win['uncovered_ratio'] * 100:.1f}pct.png"
        image.save(path)
        print(f"{app:20}{win['uncovered_ratio'] * 100:6.1f}%  {r['w']}x{r['h']}  {path.name}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["blank", "uncovered", "phantom", "withheld"])
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=48,
                    help="stop after this many tiles (contact sheets only)")
    ap.add_argument("--apps", default=None,
                    help="comma-separated app filter (uncovered only)")
    ap.add_argument("--bucket", default=None,
                    help="one withheld-text bucket (withheld only)")
    args = ap.parse_args()

    root, out = Path(args.root), Path(args.out)
    if args.what == "blank":
        show_blank(root, out, args.limit)
    elif args.what == "phantom":
        show_phantom(root, out, args.limit)
    elif args.what == "withheld":
        show_withheld(root, out, args.limit, args.bucket)
    else:
        show_uncovered(root, out, args.apps.split(",") if args.apps else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
