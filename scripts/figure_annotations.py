#!/usr/bin/env python
"""Render a capture's annotations as a figure for a paper or a project page.

`render_annotations.py` is the audit view: fast, ugly, one pixel per box, and
built to answer "is this box on that widget". This is the other job - a figure
somebody else looks at - and it makes different trade-offs: a fixed
colour-blind-safe palette, occlusion drawn as occlusion, labels that do not
collide, one resample at the end so nothing aliases, and SVG output so a
reviewer can zoom into the PDF without hitting a wall of JPEG.

    # what the corpus adds to a screenshot
    figure_annotations.py <stem> --mode pair --out fig1.png

    # that a 12-pixel table cell really does get its own box
    figure_annotations.py <stem> --mode zoom --out fig2.png --zoom-factor 4

    # image beside the markup a model is trained to emit
    figure_annotations.py <stem> --mode tag --out teaser.png

    # a diversity plate
    figure_annotations.py <corpus-dir> --mode grid --limit 9 --out plate.png

`<stem>` is a capture path with or without a suffix: pass the .png, the
.elements.leaf.json, or the bare stem. A directory is searched for captures.
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from deskshot.figures.palette import FAMILIES  # noqa: E402
from deskshot.figures.render import (  # noqa: E402
    STYLES, Capture, figure, load_capture,
)
from deskshot.figures.svg import write_svg  # noqa: E402


def find_captures(target: Path, limit: int, seed: int) -> List[Path]:
    """Capture stems under `target`, without walking a 1.2M-sample corpus flat.

    A directory of captures is listed directly. A corpus root is descended one
    level at a time and stops as soon as `limit` stems are in hand, because
    `os.walk` over `shards/` is 11 million files.
    """
    if target.is_file() or (target.parent.is_dir() and not target.is_dir()):
        return [target]

    found: List[Path] = []
    stack = [target]
    rng = random.Random(seed)
    while stack and len(found) < limit * 4:
        here = stack.pop(0)
        try:
            entries = sorted(p for p in here.iterdir())
        except OSError:
            continue
        stems = [p for p in entries if p.name.endswith(".elements.leaf.json")]
        if stems:
            found.extend(stems)
            continue
        dirs = [p for p in entries if p.is_dir() and not p.name.startswith(".")]
        if seed:
            rng.shuffle(dirs)
        stack.extend(dirs[:24])
    if seed:
        rng.shuffle(found)
    return found[:limit]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", type=Path, help="a capture stem, or a directory of them")
    ap.add_argument("--out", required=True, type=Path, help=".png, or .svg for vector")
    ap.add_argument("--mode", default="overlay",
                    choices=["overlay", "pair", "zoom", "tag", "grid"])
    ap.add_argument("--style", default="paper", choices=sorted(STYLES))
    ap.add_argument("--view", default="leaf",
                    choices=["leaf", "filtered", "unfiltered", "amodal"])
    ap.add_argument("--width", type=int, default=1600, help="figure width in px")
    ap.add_argument("--labels", default="auto", choices=["none", "auto", "all"])
    ap.add_argument("--label-text", default="both", choices=["type", "text", "both"])
    ap.add_argument("--max-labels", type=int, default=40)
    ap.add_argument("--no-occlusion", action="store_true",
                    help="do not draw the hidden part of an occluded element")
    ap.add_argument("--family", action="append", choices=sorted(FAMILIES) + ["other"],
                    help="restrict to a family; repeatable")
    ap.add_argument("--type", action="append", dest="types",
                    help="restrict to an element type; repeatable")
    ap.add_argument("--no-legend", action="store_true")
    ap.add_argument("--brief-legend", action="store_true",
                    help="families only, without the per-type breakdown")
    ap.add_argument("--title", default=None, help="'' to omit")
    ap.add_argument("--caption", default=None, help="'' to omit")
    ap.add_argument("--zoom", default=None, metavar="X,Y,W,H",
                    help="region to call out; the densest region is picked if omitted")
    ap.add_argument("--zoom-factor", type=float, default=3.0)
    ap.add_argument("--columns", type=int, default=3)
    ap.add_argument("--limit", type=int, default=1, help="captures, for --mode grid")
    ap.add_argument("--seed", type=int, default=0, help="non-zero shuffles the search")
    ap.add_argument("--dpi", type=int, default=300, help="stamped into the PNG")
    args = ap.parse_args()

    paths = find_captures(args.target, max(1, args.limit if args.mode == "grid" else 1), args.seed)
    if not paths:
        print("no captures under %s" % args.target, file=sys.stderr)
        return 1

    captures: List[Capture] = []
    for path in paths:
        try:
            captures.append(load_capture(path, view=args.view))
        except (FileNotFoundError, OSError) as exc:
            print("skipping %s: %s" % (path, exc), file=sys.stderr)
    if not captures:
        print("no capture could be loaded", file=sys.stderr)
        return 1

    zoom = None
    if args.zoom:
        try:
            x, y, w, h = (int(v) for v in args.zoom.split(","))
            zoom = (x, y, x + w, y + h)
        except ValueError:
            print("--zoom wants X,Y,W,H", file=sys.stderr)
            return 2

    kwargs = dict(
        mode=args.mode,
        style=STYLES[args.style],
        width=args.width,
        labels=args.labels,
        label_text=args.label_text,
        max_labels=args.max_labels,
        show_occluded=not args.no_occlusion,
        families=args.family,
        types=args.types,
        legend=not args.no_legend,
        legend_detail=not args.brief_legend,
        title=args.title,
        caption=args.caption,
        zoom=zoom,
        zoom_factor=args.zoom_factor,
        columns=args.columns,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.out.suffix.lower() == ".svg":
        write_svg(captures[0], args.out, style=STYLES[args.style], width=args.width,
                  labels=args.labels, label_text=args.label_text,
                  max_labels=args.max_labels, show_occluded=not args.no_occlusion,
                  families=args.family, types=args.types)
        print("%s  (vector, %d elements)" % (args.out, len(captures[0].elements)))
        return 0

    image = figure(captures, **kwargs)
    image.save(args.out, dpi=(args.dpi, args.dpi))
    print("%s  %dx%d  %d capture(s)  %d elements"
          % (args.out, image.width, image.height, len(captures),
             sum(len(c.elements) for c in captures)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
