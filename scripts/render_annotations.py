#!/usr/bin/env python
"""Draw a capture's annotations onto its screenshot, so they can be looked at.

Every audit in this project reports a number. A number tells you a box is
suspicious; it does not tell you whether the box is on the widget. This draws
the published leaf boxes over the pixels they claim, colour-coded by type, and
writes a side-by-side of the raw screenshot and the overlay.

    render_annotations.py <stem-or-capture-dir> --out overlay.png
    render_annotations.py <dir> --out sheet.png --grid   # several captures

The legend maps colour to type, from the shared palette in
`deskshot.figures.palette`. A box that hugs its widget is right; a box floating
over background, or a widget with no box at all, is the failure this is meant
to expose.

This is the *audit* view: one pixel per box, everything drawn, no layout. For a
figure someone else will look at - occlusion drawn as occlusion, labels that do
not collide, vector output - use `figure_annotations.py`.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any, Dict, List

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from deskshot.figures.palette import colour_of, rgb  # noqa: E402


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _elements(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("elements"), list):
        return payload["elements"]
    return []


def _colour_for(kind: str) -> tuple:
    """The project palette, shared with `figure_annotations.py`.

    This used to be `hash(kind) % 997` turned into a hue. Python salts string
    hashes per process, so the same capture came out in different colours on
    every run and an audit sheet could not be compared with the one before it.
    """
    return rgb(colour_of(kind))


def _font(size: int):
    for name in ("DejaVuSans.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def stems_in(target: Path) -> List[Path]:
    if target.is_dir():
        return sorted(p for p in target.glob("*.png") if p.with_suffix("").name)
    return [target]


def render(png: Path, out: Path, label_every: bool = False) -> Dict[str, Any]:
    stem = png.name[: -len(".png")]
    here = png.parent
    leaf = _elements(_load(here / f"{stem}.elements.leaf.json"))
    meta = _load(here / f"{stem}.meta.json") or {}

    base = Image.open(png).convert("RGB")
    overlay = base.copy()
    draw = ImageDraw.Draw(overlay, "RGBA")
    font = _font(11)

    kinds: Dict[str, int] = {}
    for elem in leaf:
        rect = elem.get("rect") or {}
        x, y = int(rect.get("x", 0)), int(rect.get("y", 0))
        w, h = int(rect.get("w", 0)), int(rect.get("h", 0))
        if w <= 0 or h <= 0:
            continue
        kind = str(elem.get("type") or "unknown")
        kinds[kind] = kinds.get(kind, 0) + 1
        colour = _colour_for(kind)
        draw.rectangle([x, y, x + w - 1, y + h - 1], outline=colour + (255,), width=1)
        draw.rectangle([x, y, x + w - 1, y + h - 1], fill=colour + (28,))

    legend_h = 18 * (len(kinds) + 1) + 10
    sheet = Image.new("RGB", (base.width * 2 + 24, max(base.height, legend_h) + 30), (24, 24, 28))
    sheet.paste(base, (8, 24))
    sheet.paste(overlay, (base.width + 16, 24))
    d2 = ImageDraw.Draw(sheet)
    d2.text((8, 6), f"{stem}   |   raw", fill=(230, 230, 230), font=font)
    d2.text((base.width + 16, 6),
            f"annotated: {len(leaf)} leaf elements, {len(kinds)} types", fill=(230, 230, 230), font=font)
    y = 30
    for kind, n in sorted(kinds.items(), key=lambda kv: -kv[1]):
        d2.rectangle([base.width * 2 + 2, y, base.width * 2 + 14, y + 12], fill=_colour_for(kind))
        y += 18
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return {"stem": stem, "leaf": len(leaf), "types": kinds,
            "apps": meta.get("launched_apps"), "viewport": meta.get("viewport")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("target", type=Path, help="a capture .png, or a directory of them")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--limit", type=int, default=1)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    pngs = stems_in(args.target)
    if args.seed:
        random.Random(args.seed).shuffle(pngs)
    pngs = pngs[: args.limit]
    if not pngs:
        print("no captures found")
        return 1
    for i, png in enumerate(pngs):
        out = args.out if len(pngs) == 1 else args.out.with_name(f"{args.out.stem}_{i:02d}{args.out.suffix}")
        info = render(png, out)
        print(f"{out}  {info['leaf']} elements  apps={info['apps']}")
        top = sorted(info["types"].items(), key=lambda kv: -kv[1])[:8]
        print("   " + ", ".join(f"{k}={v}" for k, v in top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
