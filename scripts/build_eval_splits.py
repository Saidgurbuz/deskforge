#!/usr/bin/env python
"""Assign every train-eligible sample to train, validation, or one test split.

A single random test set answers one question - "does it work on data like the
training data" - and a reviewer discounts it accordingly. This builds the
in-distribution splits *and* three held-out axes, each of which asks something a
random split cannot.

**The rule that makes a held-out axis mean anything: it leaves training
entirely.** For the application axis, every *training* scene containing a
held-out application is removed, not only the scenes assigned to the test split.
Otherwise the model has met the application in the corner of some other
screenshot and the split measures nothing. Theme and resolution are automatic -
a scene has exactly one of each - but applications co-occur, so this costs real
data and the cost is measured before an axis is chosen.

**Occlusion is deliberately not an axis.** The corpus exists to argue that this
supervision improves occlusion robustness, and a model cannot learn that from
data with the occlusion removed. Occlusion and window count are reported as
slices of the in-distribution test set instead, which is where the degradation
curve comes from.

Assignment is by whole scene, never by frame: an episode's frames differ by one
click, so splitting them apart tests memorisation rather than generalisation.

    build_eval_splits.py --manifest manifest.jsonl --out splits_v3.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List

#: Applications held out of training entirely.
#:
#: Two are an unseen *instance* of a category training has seen - pluma against
#: mousepad/gnome-text-editor/bluefish, xarchiver against file-roller - and one
#: is a category with no representative in training at all. Reporting those two
#: groups separately is more informative than one averaged number, because they
#: are different questions.
#:
#: eog was a fourth candidate, the unseen category "image viewer". It is not
#: held out, because holding out an application removes every scene *containing*
#: it, and eog appears in 109,454 train-eligible samples against 11,352 for
#: gnome-system-monitor. It cost 94,326 training samples to make an unseen-
#: category claim that gnome-system-monitor already makes for a tenth of that.
#: Footprint, not interest, decides what this axis can afford.
HELDOUT_APPS = {
    "gnome-system-monitor": "unseen category (system monitor)",
    "pluma": "unseen instance (text editor)",
    "xarchiver": "unseen instance (archive manager)",
}

#: The cheapest theme to hold out, and the most distinct: a dark Nord palette.
HELDOUT_THEME = "quartz_night_nord"

#: Deliberately not the largest resolution, and the cheapest of what remains.
#:
#: uhd_3840x2160 is cheaper still (4.83% against 7.06%) but it is the densest
#: supervision in the corpus - smallest text, most elements per screen - and
#: training needs it more than the split does.
#:
#: 2880x1800 is the sharper question of the two candidates that leave UHD in
#: training. It is one of only two 16:10 resolutions here, so holding it out
#: leaves training a single 16:10 scale (1920x1200) and asks for an unseen scale
#: at an aspect ratio it has barely seen - while comparable pixel scales are
#: available at 16:9 (2560x1440, 3840x2160). hdplus_1600x900 would instead sit
#: bracketed on the crowded 16:9 ladder, an easier test costing 40,630 more
#: training samples.
HELDOUT_RESOLUTION = "retina_2880x1800"

#: Priority order. A scene lands in exactly one split, so a single failure mode
#: is never reported twice under different names.
AXIS_ORDER = ("test_app", "test_theme", "test_resolution")


def load(path: Path) -> List[Dict[str, Any]]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            if row.get("train_eligible"):
                rows.append(row)
    return rows


def window_bucket(n: int) -> str:
    if n == 0:
        return "0"
    if n == 1:
        return "1"
    if n <= 3:
        return "2-3"
    if n <= 5:
        return "4-5"
    return "6-8"


def occlusion_bin(ratio: float) -> str:
    if ratio <= 0.0001:
        return "none"
    if ratio < 0.10:
        return "0-10%"
    if ratio < 0.25:
        return "10-25%"
    return "25%+"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--val-size", type=int, default=10000)
    ap.add_argument("--test-size", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=20260824)
    args = ap.parse_args()

    rows = load(args.manifest)
    if not rows:
        print("no train-eligible rows in the manifest", file=sys.stderr)
        return 1
    print(f"train-eligible samples: {len(rows):,}")

    scenes: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        scenes[row["scene_id"]].append(row)
    print(f"scenes: {len(scenes):,}")

    # --- held-out axes, in priority order ------------------------------------
    axis_of: Dict[str, str] = {}
    for scene_id, frames in scenes.items():
        # The union over frames, not the first frame's list. Eight scene ids in
        # the corpus were captured twice by different runs and the runs disagree
        # on whether an application came up, so trusting one frame let three
        # held-out applications leak straight back into training. A scene is
        # held out if *any* of its captures shows the application.
        apps = {a for f in frames for a in f["apps"]}
        head = frames[0]
        if apps & set(HELDOUT_APPS):
            axis_of[scene_id] = "test_app"
        elif head["theme"] == HELDOUT_THEME:
            axis_of[scene_id] = "test_theme"
        elif head["resolution"] == HELDOUT_RESOLUTION:
            axis_of[scene_id] = "test_resolution"

    remaining = [s for s in scenes if s not in axis_of]
    rng = random.Random(args.seed)

    # --- in-distribution val/test, stratified over what a reader slices by ----
    strata: Dict[tuple, List[str]] = defaultdict(list)
    for scene_id in remaining:
        head = scenes[scene_id][0]
        strata[(head["theme"], head["resolution"],
                window_bucket(head["n_windows"]),
                "episode" if len(scenes[scene_id]) > 1 else "static")].append(scene_id)

    total_remaining = sum(len(scenes[s]) for s in remaining)

    def take(target: int, taken_already: set) -> set:
        chosen: set = set()
        for key in sorted(strata):
            ids = [s for s in strata[key] if s not in taken_already and s not in chosen]
            rng.shuffle(ids)
            here = sum(len(scenes[s]) for s in strata[key])
            want = target * here / max(total_remaining, 1)
            got = 0
            for scene_id in ids:
                size = len(scenes[scene_id])
                closer = abs(got + size - want) < abs(got - want)
                if not closer and got > 0:
                    break
                chosen.add(scene_id)
                got += size
        return chosen

    val = take(args.val_size, set())
    test_id = take(args.test_size, val)
    for s in val:
        axis_of[s] = "val"
    for s in test_id:
        axis_of[s] = "test_id"

    args.out.parent.mkdir(parents=True, exist_ok=True)
    counts = Counter()
    with open(args.out, "w", encoding="utf-8") as fh:
        for row in sorted(rows, key=lambda r: (r["shard"], r["path"])):
            split = axis_of.get(row["scene_id"], "train")
            row["split"] = split
            counts[split] += 1
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    n = len(rows)
    print(f"\n{'split':18s}{'samples':>12s}{'%':>8s}{'scenes':>10s}")
    scene_counts = Counter(axis_of.get(s, "train") for s in scenes)
    for split in ("train", "val", "test_id", "test_app", "test_theme", "test_resolution"):
        print(f"  {split:16s}{counts[split]:>10,}{100*counts[split]/n:>7.2f}%"
              f"{scene_counts[split]:>10,}")
    held = n - counts["train"]
    print(f"\nheld out of training: {held:,} ({100*held/n:.2f}%)")

    print("\nheld-out applications (each group asks a different question):")
    per = Counter()
    for row in rows:
        if row["split"] == "test_app":
            for a in row["apps"]:
                if a in HELDOUT_APPS:
                    per[a] += 1
    for a, why in HELDOUT_APPS.items():
        print(f"  {a:24s}{per[a]:>9,}   {why}")

    leak = [a for a in HELDOUT_APPS
            if any(a in r["apps"] for r in rows if r["split"] == "train")]
    print(f"\nheld-out applications leaking into train: {leak or 'none'}")

    print("\nID test set, sliced for the degradation curve:")
    idt = [r for r in rows if r["split"] == "test_id"]
    print(f"  {'occlusion':12s}{'samples':>10s}   {'windows':10s}{'samples':>10s}")
    ob = Counter(occlusion_bin(r["occluded_ratio"]) for r in idt)
    wb = Counter(window_bucket(r["n_windows"]) for r in idt)
    keys = ["none", "0-10%", "10-25%", "25%+"]
    wkeys = ["0", "1", "2-3", "4-5", "6-8"]
    for i in range(max(len(keys), len(wkeys))):
        a = f"  {keys[i]:12s}{ob[keys[i]]:>10,}" if i < len(keys) else " " * 24
        b = f"   {wkeys[i]:10s}{wb[wkeys[i]]:>10,}" if i < len(wkeys) else ""
        print(a + b)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
