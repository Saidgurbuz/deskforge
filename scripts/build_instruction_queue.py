#!/usr/bin/env python3
"""Draw a human-audit queue of instruction/target samples, once, and freeze it.

    PYTHONPATH=src python scripts/build_instruction_queue.py \
        --campaign instr300 \
        --samples <grounding_samples_dir> \
        --corpus <corpus_root> \
        --release <release_root> \
        --items 300 --seed 20260909 --dry-run

The dense audit asks whether every element on a screen is annotated correctly.
This asks a different question about a different artefact: for one action sample
- a screen, the natural-language instruction generated for it, and the single
target the model is trained to click - is the instruction a legitimate thing to
ask, and is the marked target the thing it asks for.

The sample file is the training view itself (`train.jsonl` plus the
`train.sources.txt` that says which capture each row came from), so what gets
audited is exactly what was trained on rather than a reconstruction of it. The
target geometry is joined from the release's own transition index by
`transition_id`, so the box a rater sees is the recorded one and not one
inferred from the point.

Needs pyarrow for that join. Run it from an interpreter that has it.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from deskshot.inspector import audit as audit_mod  # noqa: E402
from deskshot.inspector import samples as samples_mod  # noqa: E402

from build_audit_queue import (  # noqa: E402
    RARE_APPS, _Reservoir, _rank, release_member_hashes, sha256_file,
)

#: What a sample is stratified over. Style and target class are the axes an
#: instruction audit actually varies along; the split is here because a training
#: view can hold more than one and pooling them would hide it.
STRATUM_FIELDS = ("split", "style", "target_kind", "app_class", "target_size")

#: Where a target stops being a widget and starts being a container. Measured
#: against the screen, because a 400px box is a button on a 4K panel and half
#: the window on a 1366x768 one.
BIG_TARGET_SHARE = 0.05
SMALL_TARGET_PX = 32 * 32


def target_size_band(bbox: Optional[Sequence[float]], width: int, height: int) -> str:
    if not bbox or len(bbox) != 4 or not width or not height:
        return "unknown"
    area = max(0.0, (bbox[2] - bbox[0])) * max(0.0, (bbox[3] - bbox[1]))
    if area <= 0:
        return "empty"
    if area / float(width * height) >= BIG_TARGET_SHARE:
        return "container"
    if area <= SMALL_TARGET_PX:
        return "tiny"
    return "widget"


def stratum(row: Dict[str, Any], bbox, width, height) -> Tuple[str, ...]:
    app = str(row.get("target_app") or "?")
    return (
        str(row.get("split") or "?"),
        str(row.get("style") or "?"),
        str(row.get("target_kind") or row.get("target_role") or "?"),
        "rare_app" if app in RARE_APPS else "common_app",
        target_size_band(bbox, width, height),
    )


def read_samples(root: Path, splits: Sequence[str]) -> Iterable[Tuple[Dict[str, Any], str]]:
    """Every row of the training view, paired with the capture it came from.

    `<split>.sources.txt` is line-aligned with `<split>.jsonl`; a mismatch in
    length means one of them was regenerated without the other and the pairing
    would be silently wrong, so it is checked rather than assumed.
    """
    for split in splits:
        rows_path = root / ("%s.jsonl" % split)
        sources_path = root / ("%s.sources.txt" % split)
        if not rows_path.is_file():
            continue
        if not sources_path.is_file():
            raise SystemExit("no %s beside %s" % (sources_path.name, rows_path))
        with rows_path.open("r", encoding="utf-8") as rows, \
                sources_path.open("r", encoding="utf-8") as sources:
            for line, source in zip(rows, sources):
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line), source.strip()
        with rows_path.open("r", encoding="utf-8") as handle:
            n_rows = sum(1 for _ in handle)
        with sources_path.open("r", encoding="utf-8") as handle:
            n_sources = sum(1 for _ in handle)
        if n_rows != n_sources:
            raise SystemExit("%s has %d rows and %s has %d; they are not aligned"
                             % (rows_path.name, n_rows, sources_path.name, n_sources))


def transition_targets(release: Path, wanted: set, splits: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """The recorded target for each transition: box, point, uid, role, effect."""
    try:
        import pyarrow.parquet as pq
    except ImportError:
        raise SystemExit(
            "this needs pyarrow for the transition join; run it with "
            "an interpreter that has pyarrow installed")
    columns = ["transition_id", "action_type", "action_target_uid", "action_target_role",
               "action_target_kind", "action_target_text", "action_target_app",
               "action_point_px", "action_target_bbox_px", "before_key", "after_key",
               "effect"]
    out: Dict[str, Dict[str, Any]] = {}
    for split in splits:
        path = release / "index" / "transitions" / ("%s.parquet" % split)
        if not path.is_file():
            continue
        handle = pq.ParquetFile(str(path))
        for batch in handle.iter_batches(batch_size=200000, columns=columns):
            table = batch.to_pydict()
            for position, key in enumerate(table["transition_id"]):
                if key in wanted and key not in out:
                    out[key] = {column: table[column][position] for column in columns}
            if len(out) >= len(wanted):
                return out
    return out


def visible_fragments(elements: Sequence[Dict[str, Any]], uid: Optional[str],
                      bbox: Optional[Sequence[float]]) -> List[Dict[str, int]]:
    """The target's visible pixels, so "is it actually clickable" is answerable.

    By uid where the capture records one; otherwise by the element whose own
    rectangle matches the recorded box, which is what the box was derived from.
    """
    if uid:
        for element in elements:
            if element.get("uid") == uid:
                return [dict(fragment) for fragment in
                        (element.get("visible_fragments") or [])]
    if bbox and len(bbox) == 4:
        for element in elements:
            rect = element.get("rect") or {}
            if not rect:
                continue
            same = (abs(rect.get("x", -1) - bbox[0]) <= 1
                    and abs(rect.get("y", -1) - bbox[1]) <= 1
                    and abs(rect.get("x", 0) + rect.get("w", 0) - bbox[2]) <= 1
                    and abs(rect.get("y", 0) + rect.get("h", 0) - bbox[3]) <= 1)
            if same:
                return [dict(fragment) for fragment in
                        (element.get("visible_fragments") or [])]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--samples", required=True, type=Path,
                        help="the training view: <split>.jsonl plus <split>.sources.txt")
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--release", type=Path, default=None,
                        help="release root; the target geometry is joined from its "
                             "transition index and each capture is hash-checked")
    parser.add_argument("--audit-root", type=Path, default=Path("audit"))
    parser.add_argument("--split", action="append", default=[],
                        help="which <split>.jsonl to read (default: train and val)")
    parser.add_argument("--items", type=int, default=300)
    parser.add_argument("--seed", type=int, default=20260909)
    parser.add_argument("--min-per-level", type=int, default=4)
    parser.add_argument("--with-grounding", action="store_true",
                        help="add a first pass in which the target is hidden and "
                             "the rater clicks from the instruction alone, scored "
                             "by the server. Off by default: it makes every item "
                             "cost a click that has to be right, and a misclick "
                             "becomes a data point nobody meant to give.")
    parser.add_argument("--rubric", type=Path, default=None)
    parser.add_argument("--title", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    name = audit_mod.safe_name(args.campaign, "campaign")
    corpus = args.corpus.resolve()
    root = args.samples.resolve()
    splits = args.split or ["train", "val"]
    rubric = (audit_mod.load_rubric(args.rubric) if args.rubric
              else audit_mod.validate_rubric(
                  json.loads(json.dumps(audit_mod.INSTRUCTION_RUBRIC))))

    campaign = audit_mod.Campaign(args.audit_root / name)
    if campaign.exists() and not args.dry_run:
        raise SystemExit("%s already exists; campaigns are frozen once drawn" % campaign.root)

    # One pass: a uniform reservoir over everything, plus a floor per stratum
    # level. The strata need the target box, which is not in the sample row, so
    # the size band is filled in after the join and the floor is taken over the
    # axes that are knowable up front.
    population = _Reservoir(args.items)
    floors: Dict[Tuple[str, str], _Reservoir] = {}
    counts: Counter = Counter()
    rows: Dict[str, Tuple[Dict[str, Any], str]] = {}
    scanned = 0
    for row, source in read_samples(root, splits):
        scanned += 1
        key = str(row.get("sample_id") or row.get("transition_id"))
        rank = _rank(args.seed, key)
        population.offer(rank, key)
        rows[key] = (row, source)
        app = str(row.get("target_app") or "?")
        for field, level in (
            ("split", str(row.get("split") or "?")),
            ("style", str(row.get("style") or "?")),
            ("target_kind", str(row.get("target_kind") or row.get("target_role") or "?")),
            ("app_class", "rare_app" if app in RARE_APPS else "common_app"),
            ("app", app),
        ):
            counts[(field, level)] += 1
            reservoir = floors.get((field, level))
            if reservoir is None:
                reservoir = floors[(field, level)] = _Reservoir(args.min_per_level)
            reservoir.offer(rank, key)
    if not scanned:
        raise SystemExit("no samples under %s for splits %s" % (root, ", ".join(splits)))

    chosen: "OrderedDict[str, str]" = OrderedDict()
    if args.min_per_level:
        for field_level in sorted(floors):
            for key in floors[field_level].paths():
                chosen.setdefault(key, "floor:%s=%s" % field_level)
    from_floor = len(chosen)
    for key in population.paths():
        if len(chosen) >= args.items:
            break
        chosen.setdefault(key, "population")
    picked = list(chosen.items())
    random.Random(args.seed).shuffle(picked)
    print("samples: %s scanned, %s selected (%s uniform, %s to floor a stratum at >=%d)"
          % ("{:,}".format(scanned), "{:,}".format(len(picked)),
             "{:,}".format(sum(1 for _, how in picked if how == "population")),
             "{:,}".format(from_floor), args.min_per_level))

    targets: Dict[str, Dict[str, Any]] = {}
    if args.release:
        wanted = {str(rows[key][0].get("transition_id")) for key, _ in picked}
        targets = transition_targets(args.release.resolve(), wanted, splits)
        print("transition index: %s of %s targets joined"
              % ("{:,}".format(len(targets)), "{:,}".format(len(wanted))))
        missing = len(wanted) - len(targets)
        if missing:
            print("  %d samples have no row in the transition index and are dropped"
                  % missing)

    release_hashes = None
    if args.release:
        keys = {str(rows[key][0].get("observation_key")) for key, _ in picked}
        release_hashes = release_member_hashes(args.release.resolve(), sorted(keys))

    items: List[Dict[str, Any]] = []
    problems: List[str] = []
    verified = mismatched = 0
    for key, draw in picked:
        row, source = rows[key]
        base = corpus / source
        png = Path(str(base) + ".png")
        leaf = Path(str(base) + samples_mod.VIEWS["leaf"])
        if not png.is_file() or not leaf.is_file():
            problems.append("%s: missing png or leaf at %s" % (key, source))
            continue
        transition = targets.get(str(row.get("transition_id"))) if targets else None
        if targets and transition is None:
            continue
        width, height = samples_mod.png_size(png)
        bbox = (transition or {}).get("action_target_bbox_px")
        point_px = (transition or {}).get("action_point_px")
        if point_px is None and isinstance(row.get("point"), list) and width and height:
            point_px = [int(round(row["point"][0] * width)),
                        int(round(row["point"][1] * height))]
        try:
            elements = samples_mod.load_elements(leaf)
        except (OSError, ValueError) as error:
            problems.append("%s: %s" % (key, error))
            continue
        digests: Dict[str, Dict[str, Any]] = {}
        for kind, path in (("image", png), ("leaf", leaf)):
            digest, size = sha256_file(path)
            entry: Dict[str, Any] = {"sha256": digest, "bytes": size}
            if release_hashes is not None:
                expected = release_hashes.get((str(row.get("observation_key")), kind))
                if expected:
                    entry["release_sha256_match"] = expected == digest
                    if expected == digest:
                        verified += 1
                    else:
                        mismatched += 1
            digests[kind] = entry

        after_key = (transition or {}).get("after_key")
        after_source = None
        if after_key:
            # Same episode directory, one step on: the after screen is a sibling
            # of the before one, so it needs no second index to find.
            candidate = Path(str(base).rsplit("-step", 1)[0] +
                             "-step%02d" % (int(str(after_key).rsplit("step", 1)[-1])))
            if Path(str(candidate) + ".png").is_file():
                after_source = os.path.relpath(str(candidate), str(corpus))

        cell = dict(zip(STRATUM_FIELDS, stratum(row, bbox, width, height)))
        items.append({
            "schema": audit_mod.QUEUE_SCHEMA,
            "kind": "instruction",
            "sample_id": key,
            "transition_id": row.get("transition_id"),
            "observation_key": row.get("observation_key"),
            "source_path": source,
            "after_source_path": after_source,
            "width": width,
            "height": height,
            "n_elements": len(elements),
            "instruction": row.get("instruction"),
            "style": row.get("style"),
            "apps": [str(row.get("target_app") or "?")],
            "draw": draw,
            "stratum": cell,
            "target": {
                "uid": (transition or {}).get("action_target_uid"),
                "role": row.get("target_role") or (transition or {}).get("action_target_role"),
                "kind": row.get("target_kind") or (transition or {}).get("action_target_kind"),
                "text": (transition or {}).get("action_target_text"),
                "app": row.get("target_app"),
                "bbox_px": list(bbox) if bbox else None,
                "point_px": list(point_px) if point_px else None,
                "point_norm": row.get("point"),
                "visible_fragments": visible_fragments(
                    elements, (transition or {}).get("action_target_uid"), bbox),
                "area_share": (None if not (bbox and width and height) else
                               round(max(0.0, bbox[2] - bbox[0]) *
                                     max(0.0, bbox[3] - bbox[1]) /
                                     float(width * height), 5)),
            },
            "effect": (transition or {}).get("effect"),
            "digests": digests,
        })

    if problems:
        print("skipped %d unreadable samples; first few:" % len(problems))
        for problem in problems[:5]:
            print("  %s" % problem)
    if release_hashes is not None:
        print("release verification: %d members matched, %d mismatched"
              % (verified, mismatched))
        if mismatched:
            raise SystemExit("a local capture differs from its published member; refusing")
    if not items:
        raise SystemExit("nothing readable was selected")
    without_box = [item for item in items if not item["target"]["bbox_px"]]
    if without_box:
        # The grounding pass scores a click against the recorded box. Without
        # one there is nothing to score against, and a queue whose first and
        # strongest question cannot be answered is not worth labelling. The box
        # lives in the release's transition index, so this is a missing
        # `--release` rather than a missing capture.
        raise SystemExit(
            "%d of %d selected samples have no recorded target box. Pass "
            "--release <release root> so the geometry can be joined from "
            "index/transitions/<split>.parquet; deriving a box from the point "
            "would be inventing the thing the audit is checking."
            % (len(without_box), len(items)))

    for field in STRATUM_FIELDS:
        tally: Counter = Counter(item["stratum"][field] for item in items)
        print("%-12s %s" % (field, "  ".join("%s=%d" % pair
                                             for pair in tally.most_common(9))))
    with_box = sum(1 for item in items if item["target"]["bbox_px"])
    print("targets: %d of %d have a recorded box; %d have an after-screen"
          % (with_box, len(items),
             sum(1 for item in items if item["after_source_path"])))

    if args.dry_run:
        print("dry run: nothing written")
        return 0

    manifest = {
        "schema": audit_mod.SCHEMA,
        "name": name,
        "kind": "instruction",
        "mode": "sample",
        "grounding": bool(args.with_grounding),
        "title": args.title or "Instruction and target audit %s" % name,
        "created_at": audit_mod.now_stamp(),
        "created_by": os.environ.get("USER") or "unknown",
        "corpus": str(corpus),
        "samples": str(root),
        "splits": splits,
        "release": str(args.release.resolve()) if args.release else None,
        "seed": args.seed,
        "requested_items": args.items,
        "items": len(items),
        "population_total": scanned,
        "population_items": sum(1 for item in items if item["draw"] == "population"),
        "stratum_fields": list(STRATUM_FIELDS),
        "strata": {field: dict(Counter(item["stratum"][field] for item in items))
                   for field in STRATUM_FIELDS},
        "rubric": rubric,
        "rubric_ref": audit_mod.rubric_ref(rubric),
    }
    campaign.root.mkdir(parents=True, exist_ok=True)
    campaign.write_queue(items)
    campaign.write_manifest(manifest)
    print("wrote %s (%d items) and %s"
          % (campaign.queue_path, len(items), campaign.manifest_path))
    print("rubric %s" % manifest["rubric_ref"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
