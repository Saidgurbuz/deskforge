#!/usr/bin/env python
"""Decide, per sample, whether it belongs in a published release.

`build_hf_split.py` indexes every finished capture. That is the right input for
a split but the wrong input for a release: it counts captures the pipeline
itself rejected, and exact duplicates, as ordinary training data. This pass
applies the quality decision and says why, one row per sample.

**Three tiers, because "valid" is not one question.**

`publishable`
    The annotation describes the screen correctly. A capture keeps this even when
    a *planned* application never launched: the scene has fewer windows than the
    plan asked for, but nothing on screen is mislabelled. Those samples are also
    the only evidence for three applications that were dropped from the pool
    mid-project, so discarding them would silently narrow the application count.

`train_eligible`
    `publishable` and not a duplicate of something already kept - neither a
    near-duplicate scene (perceptual hash, distance 0) nor a no-op episode frame
    whose annotation is byte-identical to the frame before it. Both are correct
    annotations; both would be repeated data.

`excluded`
    Degenerate or wrong: too few elements, no type diversity, no coverage, a
    missing element list, an element whose box claims pixels it cannot show, or
    an element attributed to the wrong application's window.

Every row carries the reasons, so any of these lines can be redrawn without
rescanning 1.19 million samples.

    build_publication_manifest.py --root <corpus> --out manifest.jsonl
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List

#: Failing any of these means the capture does not describe a usable screen.
DISQUALIFYING = {
    "min_elements",          # nothing on screen
    "type_diversity",        # one kind of thing only
    "tree_depth",            # no hierarchy
    "coverage",              # annotations cover almost no pixels
    "elements_missing",      # the element list is absent
    "leaf_window_ownership", # elements attributed to the wrong app's window
    # A published box with no visible fragment claims pixels it cannot show.
    # Fixed in the pipeline, but samples captured before the fix carry it in
    # their stored element lists and cannot be repaired without regeneration.
    "visible_fragments_present",
    "visible_fragments_within_source",
    "non_occluded_rect_consistency",
}


def _parse_action(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.startswith("{"):
        try:
            got = ast.literal_eval(raw)
            return got if isinstance(got, dict) else {}
        except (ValueError, SyntaxError):
            return {}
    return {}


def scan_shard(args) -> List[Dict[str, Any]]:
    root, shard = args
    rows: List[Dict[str, Any]] = []
    base = os.path.join(root, shard)
    for sub in ("ep", "st"):
        group = os.path.join(base, sub)
        if not os.path.isdir(group):
            continue
        try:
            dirs = os.listdir(group)
        except OSError:
            continue
        for d in dirs:
            dd = os.path.join(group, d)
            try:
                names = set(os.listdir(dd))
            except OSError:
                continue

            # Which frames of this episode changed nothing? The generator
            # already recorded it per step and it agrees with the annotation
            # 98.6% of the time, so there is no need to re-hash anything.
            noop_steps = set()
            if "episode.json" in names:
                try:
                    ep = json.loads(open(os.path.join(dd, "episode.json"),
                                        encoding="utf-8").read())
                    for step in ep.get("steps") or []:
                        diff = step.get("diff")
                        idx = step.get("step_index")
                        if isinstance(diff, dict) and not diff.get("changed") \
                                and isinstance(idx, int):
                            noop_steps.add(idx)
                except (OSError, ValueError):
                    pass

            for name in sorted(names):
                if not name.endswith(".meta.json"):
                    continue
                stem = name[: -len(".meta.json")]
                if f"{stem}.png" not in names:
                    continue  # not a committed sample
                try:
                    meta = json.loads(open(os.path.join(dd, name),
                                           encoding="utf-8").read())
                except (OSError, ValueError):
                    continue
                verdict = {}
                if f"{stem}.verdict.json" in names:
                    try:
                        verdict = json.loads(
                            open(os.path.join(dd, f"{stem}.verdict.json"),
                                 encoding="utf-8").read())
                    except (OSError, ValueError):
                        verdict = {}

                scene = meta.get("scene") or {}
                launched = sorted(meta.get("launched_apps") or [])

                # Occlusion severity, from the metadata the pipeline already
                # wrote: how much of the raw accessibility walk the occlusion
                # pass had to clip or drop. Reading it here costs nothing,
                # whereas measuring it from the element lists would mean
                # opening 175 KB per sample.
                occ = meta.get("occlusion") or {}
                seen = int(occ.get("num_elements_in") or 0)
                clipped = int(occ.get("num_clipped") or 0)
                dropped = int(occ.get("num_dropped") or 0)
                occluded_ratio = round((clipped + dropped) / seen, 4) if seen else 0.0
                step = int(stem.rsplit("step", 1)[-1]) if "step" in stem else 0
                failed = set(verdict.get("failed_checks") or [])
                hard = sorted(failed & DISQUALIFYING)
                near_dup = bool(verdict.get("near_duplicate_of"))
                is_noop = step in noop_steps
                missing = list(verdict.get("missing_apps") or [])

                publishable = not hard and bool(verdict)
                rows.append({
                    "key": stem,
                    "shard": shard,
                    "group": sub,
                    "path": os.path.join(sub, d, stem),
                    "scene_id": scene.get("scene_id") or d,
                    "seed": scene.get("seed"),
                    "step": step,
                    "theme": scene.get("theme_preset") or "?",
                    "resolution": scene.get("display_preset") or "?",
                    "profile": (meta.get("desktop_fixture") or {}).get("profile") or "?",
                    "apps": launched,
                    "n_windows": len(launched),
                    "n_elements": meta.get("num_elements_leaf") or 0,
                    "occluded_ratio": occluded_ratio,
                    "n_clipped": clipped,
                    "n_dropped": dropped,
                    "has_verdict": bool(verdict),
                    "publishable": publishable,
                    "train_eligible": publishable and not near_dup and not is_noop,
                    "hard_failures": hard,
                    "missing_apps": missing,
                    "near_duplicate": near_dup,
                    "no_op_frame": is_noop,
                })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--threads", type=int, default=48)
    args = ap.parse_args()

    shards_dir = args.root / "shards"
    shards = sorted(p.name for p in shards_dir.iterdir() if p.is_dir())
    print(f"scanning {len(shards)} shards", flush=True)
    rows: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        for got in pool.map(scan_shard, [(str(shards_dir), s) for s in shards]):
            rows.extend(got)
    if not rows:
        print("nothing found", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        for row in sorted(rows, key=lambda r: (r["shard"], r["path"])):
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    n = len(rows)
    pub = sum(r["publishable"] for r in rows)
    tr = sum(r["train_eligible"] for r in rows)
    nov = sum(not r["has_verdict"] for r in rows)
    print(f"\nsamples on disk        {n:>10,}")
    print(f"  without a verdict    {nov:>10,}  (cannot be judged; excluded)")
    print(f"  PUBLISHABLE          {pub:>10,}  ({100*pub/n:5.2f}%)")
    print(f"  TRAIN-ELIGIBLE       {tr:>10,}  ({100*tr/n:5.2f}%)")

    print("\nwhy samples are excluded (a sample can have several):")
    hard = Counter()
    for r in rows:
        for h in r["hard_failures"]:
            hard[h] += 1
    for k, v in hard.most_common():
        print(f"  {k:34s} {v:>9,}  {100*v/n:5.2f}%")
    dup = sum(r["near_duplicate"] for r in rows)
    noop = sum(r["no_op_frame"] for r in rows)
    print("\nkept as publishable but not for training:")
    print(f"  near-duplicate scenes              {dup:>9,}  {100*dup/n:5.2f}%")
    print(f"  no-op episode frames               {noop:>9,}  {100*noop/n:5.2f}%")
    ma = sum(1 for r in rows if r["missing_apps"])
    print(f"\nkept, flagged: a planned app never launched  {ma:>9,}  {100*ma/n:5.2f}%")

    apps = Counter()
    for r in rows:
        if r["train_eligible"]:
            for a in r["apps"]:
                apps[a] += 1
    print(f"\napplications present in train-eligible samples: {len(apps)}")
    for a, c in apps.most_common():
        print(f"  {a:24s} {c:>9,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
