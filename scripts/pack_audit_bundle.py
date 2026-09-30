#!/usr/bin/env python3
"""Pack a campaign into a self-contained folder you can label offline.

    PYTHONPATH=src python scripts/pack_audit_bundle.py --campaign audit/pilot40

Then, on a laptop:

    rsync -a <server>:<bundle> ./
    cd <bundle> && python3 -m http.server 8010
    open http://localhost:8010

The captures live on the server and the person labelling them does not. A live
session over an SSH tunnel is fine - the item payload is about 130KB - but a
bundle removes the tunnel from the loop entirely, which matters for two reasons
beyond speed: a second rater can work without an account on this machine, and
that is what makes an agreement number possible at all.

Answers are held in the browser and exported as one `labels.jsonl`, which
`scripts/merge_audit_labels.py` folds back into the campaign. The bundle carries
each item's SHA-256, and the merge refuses a file whose items do not match, so
labels cannot be attached to different pixels than they were made against.

`file://` cannot fetch, which is why the README says to run a local server.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from deskshot.inspector import audit as audit_mod  # noqa: E402
from deskshot.inspector import samples as samples_mod  # noqa: E402

STATIC = Path(__file__).resolve().parents[1] / "src" / "deskshot" / "inspector" / "static"
ASSETS = ("app.css", "audit.css", "audit.js", "audit_offline.js")

DEFAULT_OUT = Path(os.environ.get("TMPDIR") or "/tmp") / "audit_bundles"

README = """# {campaign} — offline audit bundle

{items} items, rubric `{rubric}`, packed {at} from `{corpus}`.

    python3 -m http.server 8010      # from this directory
    open http://localhost:8010

`file://` cannot fetch, so it has to be served, even locally.

Answers are saved in the browser as you go (IndexedDB, per browser profile).
Press **export** in the header when you are done - or at any point, it is
incremental - and you get `labels-{campaign}-<rater>.jsonl`. Send that back and:

    PYTHONPATH=src python scripts/merge_audit_labels.py \\
        --campaign audit/{campaign} --labels labels-{campaign}-<rater>.jsonl

The merge checks every item's SHA-256 against this bundle's, so an answer can
never be attached to a different capture than the one it was made against.

Set your name in the header before you start; it is stamped into every answer
and is how a second rater's labels are told from the first's.
"""


def load_elements_projected(path: Path) -> List[Dict[str, Any]]:
    return audit_mod.project_elements(samples_mod.load_elements(path))


def encode_image(source: Path, cap: int, fmt: str, quality: int) -> Tuple[bytes, str]:
    from PIL import Image
    with Image.open(str(source)) as handle:
        image = handle.convert("RGB")
    if cap:
        longest = max(image.size)
        if longest > cap:
            scale = float(cap) / longest
            image = image.resize((max(1, int(image.size[0] * scale)),
                                  max(1, int(image.size[1] * scale))), Image.LANCZOS)
    buffer = io.BytesIO()
    if fmt == "webp":
        image.save(buffer, "WEBP", quality=quality, method=4)
        return buffer.getvalue(), "webp"
    image.save(buffer, "PNG", compress_level=6)
    return buffer.getvalue(), "png"


def crop_wanted(rect: Dict[str, Any], packed_scale: float, below: int) -> bool:
    """Whether a native-resolution crop of this element earns its bytes.

    Two conditions, and both matter. If the frame was not downscaled there is
    nothing a crop could add. And if the element is large, the downscaled frame
    already shows its edges to within a pixel or two of where they are - the
    judgement that needs real pixels is "is this 14px box on the ink", not "is
    this window the right size". Cropping everything made the bundle 596KB an
    item, most of it whole-window crops nobody would zoom into.
    """
    if packed_scale >= 0.999:
        return False
    longest = max(int(rect.get("w") or 0), int(rect.get("h") or 0))
    return 0 < longest <= below


def crop_for(source: Path, rect: Dict[str, Any], pad: float, minimum: int,
             maximum: int) -> Optional[bytes]:
    """Native pixels around one element, for judging a 12px box honestly.

    A downscaled frame is enough to see that a box is roughly right and not
    enough to see that it is two pixels short, so small sampled elements get a
    lossless crop at full resolution. The span is capped: the crop is the
    element plus context, and past a few hundred pixels the context stops
    being context.
    """
    from PIL import Image
    with Image.open(str(source)) as handle:
        image = handle.convert("RGB")
    width, height = image.size
    w = max(int(rect.get("w") or 0), 1)
    h = max(int(rect.get("h") or 0), 1)
    cx = int(rect.get("x") or 0) + w / 2.0
    cy = int(rect.get("y") or 0) + h / 2.0
    span_w = min(max(int(w * pad), minimum), maximum)
    span_h = min(max(int(h * pad), minimum), maximum)
    x0 = max(0, min(int(cx - span_w / 2), width - 1))
    y0 = max(0, min(int(cy - span_h / 2), height - 1))
    x1 = max(x0 + 1, min(int(cx + span_w / 2), width))
    y1 = max(y0 + 1, min(int(cy + span_h / 2), height))
    buffer = io.BytesIO()
    image.crop((x0, y0, x1, y1)).save(buffer, "WEBP", lossless=True, method=1)
    return json.dumps({"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}).encode(), buffer.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--campaign", required=True, type=Path)
    parser.add_argument("--out", type=Path, default=None,
                        help="bundle directory (default: %s/<campaign>)" % DEFAULT_OUT)
    parser.add_argument("--corpus", type=Path, default=None,
                        help="override the corpus recorded in the manifest")
    parser.add_argument("--max-px", type=int, default=1600,
                        help="long edge of each packed frame (default 1600)")
    parser.add_argument("--quality", type=int, default=86)
    parser.add_argument("--format", choices=("webp", "png"), default="webp")
    parser.add_argument("--crops", action="store_true", default=True,
                        help="pack a lossless native-resolution crop per sampled element")
    parser.add_argument("--no-crops", dest="crops", action="store_false")
    parser.add_argument("--crop-pad", type=float, default=3.0)
    parser.add_argument("--crop-min", type=int, default=224)
    parser.add_argument("--crop-max", type=int, default=512,
                        help="largest crop span, per side")
    parser.add_argument("--crop-below", type=int, default=240,
                        help="only crop elements whose longest side is at most this")
    parser.add_argument("--limit", type=int, default=0, help="pack only the first N items")
    parser.add_argument("--include-labels", action="store_true",
                        help="seed the bundle with the answers already recorded")
    args = parser.parse_args()

    campaign = audit_mod.Campaign(args.campaign)
    if not campaign.exists():
        raise SystemExit("no campaign at %s" % args.campaign)
    corpus = (args.corpus or Path(campaign.manifest["corpus"])).resolve()
    if not corpus.is_dir():
        raise SystemExit("no corpus at %s" % corpus)
    out = (args.out or (DEFAULT_OUT / campaign.name)).resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / "items").mkdir(exist_ok=True)

    queue = campaign.queue[:args.limit] if args.limit else campaign.queue
    index_rows: List[Dict[str, Any]] = []
    total_bytes = 0
    for position, item in enumerate(queue):
        base = corpus / str(item["source_path"])
        png = Path(str(base) + ".png")
        leaf = Path(str(base) + samples_mod.VIEWS["leaf"])
        if not png.is_file() or not leaf.is_file():
            print("  skipping %s: missing png or leaf" % item["observation_key"])
            continue
        elements = load_elements_projected(leaf)
        blob, extension = encode_image(png, args.max_px, args.format, args.quality)
        name = "%04d" % position
        (out / "items" / ("%s.%s" % (name, extension))).write_bytes(blob)
        total_bytes += len(blob)

        crops: Dict[str, Any] = {}
        if args.crops:
            longest_side = max(int(item.get("width") or 1), int(item.get("height") or 1))
            packed_scale = min(1.0, float(args.max_px) / longest_side) if args.max_px else 1.0
            by_key = {element["key"]: element for element in elements}
            for key in item.get("element_keys") or []:
                element = by_key.get(key)
                if not element:
                    continue
                rects = element.get("visible_fragments") or (
                    [element["rect"]] if element.get("rect") else [])
                if not rects or not crop_wanted(rects[0], packed_scale, args.crop_below):
                    continue
                geometry, crop = crop_for(png, rects[0], args.crop_pad, args.crop_min,
                                          args.crop_max)
                crop_name = "%s-%02d.webp" % (name, len(crops))
                (out / "items" / crop_name).write_bytes(crop)
                total_bytes += len(crop)
                crops[key] = dict(json.loads(geometry.decode()), file="items/" + crop_name)

        payload = {
            "index": position,
            "item": item,
            "elements": elements,
            "sampled": [key for key in (item.get("element_keys") or []) if key in
                        {element["key"] for element in elements}],
            "image": "items/%s.%s" % (name, extension),
            "crops": crops,
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        (out / "items" / ("%s.json" % name)).write_bytes(body)
        total_bytes += len(body)
        index_rows.append({
            "index": position,
            "observation_key": item["observation_key"],
            "stratum": item.get("stratum"),
            "apps": item.get("apps"),
            "n_elements": item.get("n_elements"),
            "occluded_ratio": item.get("occluded_ratio"),
            "draw": item.get("draw"),
            "width": item.get("width"),
            "height": item.get("height"),
            # so an offline element row can say whether it was a sampled
            # element or one the rater stopped on during a sweep
            "element_keys": item.get("element_keys") or [],
            "sha256": ((item.get("digests") or {}).get("leaf") or {}).get("sha256"),
            "image_sha256": ((item.get("digests") or {}).get("image") or {}).get("sha256"),
            "payload": "items/%s.json" % name,
            "image": "items/%s.%s" % (name, extension),
        })
        if (position + 1) % 50 == 0:
            print("  packed %d/%d (%.0f MB)" % (position + 1, len(queue), total_bytes / 1e6))

    manifest = {
        "schema": "deskshot.audit.bundle/1",
        "campaign": campaign.name,
        "packed_at": audit_mod.now_stamp(),
        "corpus": str(corpus),
        "rubric": campaign.rubric,
        "rubric_ref": audit_mod.rubric_ref(campaign.rubric),
        "mode": campaign.mode,
        "element_sample": campaign.manifest.get("element_sample"),
        "source_manifest": campaign.manifest,
        "items": index_rows,
    }
    (out / "bundle.json").write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8")
    if args.include_labels and campaign.labels_path.is_file():
        shutil.copy2(str(campaign.labels_path), str(out / "seed-labels.jsonl"))

    for asset in ASSETS:
        source = STATIC / asset
        if source.is_file():
            shutil.copy2(str(source), str(out / asset))
    shutil.copy2(str(STATIC / "audit.html"), str(out / "audit-live.html.txt"))
    (out / "index.html").write_text(offline_page(), encoding="utf-8")
    (out / "README.md").write_text(README.format(
        campaign=campaign.name, items=len(index_rows),
        rubric=manifest["rubric_ref"], at=manifest["packed_at"], corpus=corpus),
        encoding="utf-8")

    print("bundle %s  (mode: %s)" % (out, campaign.mode))
    print("  %d items, %.1f MB, %.0f KB per item"
          % (len(index_rows), total_bytes / 1e6,
             (total_bytes / max(1, len(index_rows))) / 1e3))
    print("  rsync -a %s <laptop>:  then `python3 -m http.server` inside it" % out)
    return 0


def offline_page() -> str:
    """The bundle's own page: the live one with its fetches redirected.

    Written here rather than shipped as a fourth copy of the UI so the offline
    page cannot drift from the live one: it loads the same `audit.js` and only
    replaces the four functions that talk to a server.
    """
    live = (STATIC / "audit.html").read_text(encoding="utf-8")
    live = live.replace(
        '<script src="/static/audit.js"></script>',
        # The shim has to install its overrides before anything fetches, so the
        # page defers bootstrap and audit_offline.js calls window.auditStart().
        '<script>window.AUDIT_DEFER = true;</script>\n'
        '<script src="audit.js"></script>\n'
        '<script src="audit_offline.js"></script>')
    live = live.replace('href="/static/', 'href="')
    live = live.replace('<a class="plain" href="/corpus">corpus →</a>',
                        '<button id="export" title="download the answers so far">'
                        'export answers</button>')
    return live


if __name__ == "__main__":
    raise SystemExit(main())
