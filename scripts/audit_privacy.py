#!/usr/bin/env python3
"""Find identifying strings in a run before it is published.

Three surfaces, and they are not equally serious:

- **drawn** - `*.elements.leaf.json` and `*.screentag.txt`. The leaf list is the
  published annotation and ScreenTag is generated from it, so both mirror what
  the screenshot shows: a hit here is a hit *in the pixels*, and it cannot be
  fixed afterwards because editing the annotation only makes it disagree with
  the image. This is the bucket that blocks a run.
- **pixels** - what OCR reads off the screenshot (`--ocr`). Catches text no
  annotation covers, which is a third of on-screen text today, so a leak can be
  visible without appearing in any annotation. Also blocking.
- **sidecar** - `*.meta.json` and the intermediate element lists
  (`unfiltered`, filtered, `amodal`). Provenance genuinely records where the run
  came from, and the unfiltered list is the raw AT-SPI walk, which by design
  keeps nodes that were never drawn - a hidden desktop link naming the account,
  for instance. Reported so nobody forgets it exists, but rewritable at publish
  time without touching a single sample, so it does not block.

Usage:
    PYTHONPATH=src python scripts/audit_privacy.py --root <run>
    PYTHONPATH=src python scripts/audit_privacy.py --root <run> --ocr --limit 8
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from deskshot.privacy import Leak, describe_watchlist, scan_text  # noqa: E402

#: Element fields that carry text a reader could see. `value` is included
#: because spin buttons and entries put their contents there rather than in
#: `inner_text`.
TEXT_FIELDS = ("name", "inner_text", "visible_text", "description", "value", "title")


def _iter_strings(obj: Any, path: str = "") -> Iterable[Tuple[str, str]]:
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for key, val in obj.items():
            yield from _iter_strings(val, f"{path}.{key}" if path else str(key))
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_strings(item, path)


def _scan_elements(path: Path, include_brand: bool) -> List[Tuple[str, Leak]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    elements = data.get("elements", []) if isinstance(data, dict) else data
    hits: List[Tuple[str, Leak]] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        for field in TEXT_FIELDS:
            value = element.get(field)
            if isinstance(value, str):
                for leak in scan_text(value, include_brand=include_brand):
                    hits.append((f"{element.get('app_name') or '?'}:{field}", leak))
    return hits


def _scan_text_file(path: Path, include_brand: bool) -> List[Tuple[str, Leak]]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return []
    return [("screentag", leak) for leak in scan_text(text, include_brand=include_brand)]


def _scan_meta(path: Path, include_brand: bool) -> List[Tuple[str, Leak]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    hits: List[Tuple[str, Leak]] = []
    for key, value in _iter_strings(data):
        for leak in scan_text(value, include_brand=include_brand):
            hits.append((key, leak))
    return hits


def _ocr_reader():
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        print("  (--ocr needs `pip install --user rapidocr_onnxruntime`)", file=sys.stderr)
        return None
    return RapidOCR()


def _scan_pixels(reader, path: Path, include_brand: bool) -> List[Tuple[str, Leak]]:
    result, _elapsed = reader(str(path))
    hits: List[Tuple[str, Leak]] = []
    for entry in result or []:
        text = str(entry[1])
        for leak in scan_text(text, include_brand=include_brand):
            hits.append(("ocr", leak))
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=0, help="scan at most N captures")
    parser.add_argument("--ocr", action="store_true", help="also read the screenshots")
    parser.add_argument(
        "--allow-brand",
        action="store_true",
        help="do not report the project's own name (it is published under it)",
    )
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument(
        "--list-affected",
        action="store_true",
        help="print the stem of every capture with a drawn identifier, one per "
             "line, so a finished corpus can be filtered. Nothing is deleted.",
    )
    args = parser.parse_args()

    include_brand = not args.allow_brand
    # rglob: a corpus run nests captures under st/<xx>/ and ep/<scene_id>/.
    metas = sorted(args.root.rglob("*.meta.json"))
    by_stem = {m.name.split(".")[0]: m.parent for m in metas}
    stems = sorted(by_stem)
    if args.limit:
        stems = stems[: args.limit]
    if not stems:
        print(f"no captures under {args.root}")
        return 2

    reader = _ocr_reader() if args.ocr else None
    buckets: Dict[str, collections.Counter] = {
        "drawn": collections.Counter(),
        "pixels": collections.Counter(),
        "sidecar": collections.Counter(),
    }
    examples: Dict[Tuple[str, str], str] = {}
    dirty: Dict[str, set] = {k: set() for k in buckets}

    for stem in stems:
        here = by_stem[stem]
        for element_file in sorted(here.glob(f"{stem}.elements*.json")):
            # Only the leaf list is published; the others are intermediates the
            # pipeline keeps so decisions can be recomputed later, and the raw
            # walk legitimately contains nodes that were never on screen.
            bucket = "drawn" if element_file.name.endswith(".elements.leaf.json") else "sidecar"
            for where, leak in _scan_elements(element_file, include_brand):
                buckets[bucket][leak.literal] += 1
                dirty[bucket].add(stem)
                if bucket == "drawn":
                    examples.setdefault((leak.literal, where), leak.context)
        screentag = here / f"{stem}.screentag.txt"
        if screentag.is_file():
            for where, leak in _scan_text_file(screentag, include_brand):
                buckets["drawn"][leak.literal] += 1
                dirty["drawn"].add(stem)
                examples.setdefault((leak.literal, where), leak.context)
        meta = here / f"{stem}.meta.json"
        if meta.is_file():
            for where, leak in _scan_meta(meta, include_brand):
                buckets["sidecar"][leak.literal] += 1
                dirty["sidecar"].add(stem)
        if reader is not None:
            for shot in sorted(here.glob(f"{stem}.png"))[:1]:
                for where, leak in _scan_pixels(reader, shot, include_brand):
                    buckets["pixels"][leak.literal] += 1
                    dirty["pixels"].add(stem)
                    examples.setdefault((leak.literal, where), leak.context)

    print(f"privacy audit: {len(stems)} captures under {args.root}")
    print(f"watching {len(describe_watchlist())} identifiers from this environment\n")
    for bucket in ("drawn", "pixels", "sidecar"):
        counts = buckets[bucket]
        if bucket == "pixels" and reader is None:
            print("pixels   : not scanned (pass --ocr)")
            continue
        n_dirty = len(dirty[bucket])
        verdict = "CLEAN" if not counts else f"{n_dirty}/{len(stems)} captures affected"
        print(f"{bucket:9s}: {verdict}")
        for literal, count in counts.most_common(12):
            print(f"    {count:5d}  {literal!r}")
    if examples:
        print("\nexamples:")
        for (literal, where), context in list(examples.items())[:10]:
            print(f"    [{where}] {context!r}")

    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "root": str(args.root),
                    "num_captures": len(stems),
                    "buckets": {k: dict(v) for k, v in buckets.items()},
                    "captures_affected": {k: sorted(v) for k, v in dirty.items()},
                    "watchlist": describe_watchlist(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    if args.list_affected:
        # Deliberately just a list. A capture that draws an identifier cannot be
        # repaired - the string is in the pixels - so the only remedy is to drop
        # the sample, and that is a decision to make deliberately rather than a
        # side effect of running an audit.
        print("\naffected capture stems:")
        for stem in sorted(dirty["drawn"] | dirty["pixels"]):
            print(stem)

    blocking = bool(buckets["drawn"]) or bool(buckets["pixels"])
    print("\nverdict:", "FAIL - do not publish" if blocking else "PASS")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
