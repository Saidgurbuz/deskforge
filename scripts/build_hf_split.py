#!/usr/bin/env python
"""Assign every capture to train or test, stratified, without leaking.

Two rules drive the design.

**Split whole scenes, never frames.** An episode's frames differ by one click;
putting frame 3 in test while frames 2 and 4 are in train is not a held-out
sample, it is a memorisation check with the answer next door. The unit of
assignment is therefore the scene, and every frame of an episode follows its
scene.

**Stratify on what a reader would slice by.** A test set that happens to hold
no 4K captures, or none of the dark themes, cannot answer "does it work on 4K"
or "does it work in the dark". The stratum is
`theme x resolution x window-count bucket x episode/static`, and each stratum
contributes the same *fraction* of its scenes, so the test set reproduces the
corpus mix rather than a uniform sample of it.

Applications are not in the stratum key: a scene has between zero and eight of
them, so they do not partition. They are checked afterwards instead - the
report prints train and test share per application, and the split is only
useful if those track.

    build_hf_split.py --root <corpus> --test-size 10000 --out splits.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List

#: Window counts are bucketed rather than used raw: eight-window scenes are 2.5%
#: of the corpus, and a stratum that small produces a rounding-dominated share.
WINDOW_BUCKETS = ((0, 0, "0"), (1, 1, "1"), (2, 3, "2-3"), (4, 5, "4-5"), (6, 8, "6-8"))


def window_bucket(n: int) -> str:
    for low, high, label in WINDOW_BUCKETS:
        if low <= n <= high:
            return label
    return "6-8"


def scan_shard(args) -> List[Dict[str, Any]]:
    """One row per capture, carrying everything the stratum key needs."""
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
                names = os.listdir(dd)
            except OSError:
                continue
            for name in names:
                if not name.endswith(".meta.json"):
                    continue
                stem = name[: -len(".meta.json")]
                if f"{stem}.png" not in names:
                    continue  # not a finished sample
                try:
                    meta = json.loads(open(os.path.join(dd, name), encoding="utf-8").read())
                except (OSError, ValueError):
                    continue
                scene = meta.get("scene") or {}
                launched = meta.get("launched_apps") or []
                rows.append({
                    "key": stem,
                    "shard": shard,
                    "group": sub,
                    "scene_id": scene.get("scene_id") or d,
                    "step": int(stem.rsplit("step", 1)[-1]) if "step" in stem else 0,
                    "theme": scene.get("theme_preset") or "?",
                    "resolution": scene.get("display_preset") or "?",
                    "profile": (meta.get("desktop_fixture") or {}).get("profile") or "?",
                    "apps": sorted(launched),
                    "n_windows": len(launched),
                    "n_elements": meta.get("num_elements_leaf") or 0,
                    "path": os.path.join(sub, d, stem),
                })
    return rows


def allocate(scenes, strata, test_size: int, total_caps: int, seed: int) -> set:
    """Choose the test scenes: proportional per stratum, rounded to nearest.

    Allocation is by *captures*, not scenes, because an episode carries up to
    sixteen frames and a scene-proportional split would size the test set
    wrongly. Two details matter:

    **Round to nearest, do not fill past the target.** A greedy "add until the
    quota is met" overshoots badly on small strata - a nine-frame episode
    dropped into a stratum wanting half a frame overshoots eighteen-fold, and
    summed over several hundred strata the test set came out 2.3x too large.
    A scene joins only if adding it moves the running total *closer* to what the
    stratum is owed.

    **Every stratum contributes at least one scene.** Rounding to nearest alone
    would empty the rare corners - the 4K dark-theme eight-window ones - and a
    test set missing them cannot answer whether the model handles them, which is
    the whole reason for stratifying.
    """
    rng = random.Random(seed)
    chosen: set = set()
    for key in sorted(strata):
        ids = sorted(strata[key])
        rng.shuffle(ids)
        caps_here = sum(len(scenes[i]) for i in ids)
        want = test_size * caps_here / max(total_caps, 1)
        taken = 0
        for scene_id in ids:
            size = len(scenes[scene_id])
            closer = abs(taken + size - want) < abs(taken - want)
            if not closer and taken > 0:
                break          # past the target, and we already have some
            chosen.add(scene_id)   # closer, or this stratum is still empty
            taken += size
    return chosen


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--test-size", type=int, default=10000,
                    help="target captures in test; strata contribute proportionally")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--seed", type=int, default=20260823)
    ap.add_argument("--threads", type=int, default=48)
    args = ap.parse_args()

    shards_dir = args.root / "shards"
    shards = sorted(p.name for p in shards_dir.iterdir() if p.is_dir())
    print(f"scanning {len(shards)} shards", flush=True)
    rows: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        for got in pool.map(scan_shard, [(str(shards_dir), s) for s in shards]):
            rows.extend(got)
    print(f"{len(rows)} captures", flush=True)
    if not rows:
        print("nothing to split", file=sys.stderr)
        return 1

    # Group frames under their scene: the unit of assignment.
    scenes: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        scenes[row["scene_id"]].append(row)

    def stratum_of(frames: List[Dict[str, Any]]) -> tuple:
        head = min(frames, key=lambda r: r["step"])
        return (head["theme"], head["resolution"],
                window_bucket(head["n_windows"]),
                "episode" if len(frames) > 1 else "static")

    strata: Dict[tuple, List[str]] = defaultdict(list)
    for scene_id, frames in scenes.items():
        strata[stratum_of(frames)].append(scene_id)

    total_caps = len(rows)
    test_scenes = allocate(scenes, strata, args.test_size, total_caps, args.seed)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    n_test = 0
    with open(args.out, "w", encoding="utf-8") as fh:
        for row in sorted(rows, key=lambda r: (r["shard"], r["path"])):
            row["split"] = "test" if row["scene_id"] in test_scenes else "train"
            n_test += row["split"] == "test"
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    print(f"\ntest {n_test} captures ({100*n_test/total_caps:.2f}%) "
          f"from {len(test_scenes)} scenes; train {total_caps - n_test}")
    print(f"-> {args.out}")

    def share(field: str, pick) -> None:
        tr, te = Counter(), Counter()
        for row in rows:
            (te if row["split"] == "test" else tr)[pick(row)] += 1
        print(f"\n{field:14s} {'train %':>9s} {'test %':>9s} {'delta pp':>9s}")
        for k in sorted(set(tr) | set(te)):
            a = 100 * tr[k] / max(sum(tr.values()), 1)
            b = 100 * te[k] / max(sum(te.values()), 1)
            print(f"  {str(k):12s} {a:8.2f}% {b:8.2f}% {b - a:+8.2f}")

    share("theme", lambda r: r["theme"])
    share("resolution", lambda r: r["resolution"])
    share("windows", lambda r: window_bucket(r["n_windows"]))
    share("kind", lambda r: r["group"])
    share("profile", lambda r: r["profile"])

    # Applications are not part of the key, so this is the real check.
    tr_apps, te_apps = Counter(), Counter()
    tr_n = sum(1 for r in rows if r["split"] == "train")
    te_n = len(rows) - tr_n
    for row in rows:
        for app in row["apps"]:
            (te_apps if row["split"] == "test" else tr_apps)[app] += 1
    print(f"\n{'application':22s} {'train/cap':>10s} {'test/cap':>10s} {'delta':>8s}")
    worst = 0.0
    for app in sorted(set(tr_apps) | set(te_apps)):
        a = tr_apps[app] / max(tr_n, 1)
        b = te_apps[app] / max(te_n, 1)
        worst = max(worst, abs(b - a))
        print(f"  {app:20s} {a:10.4f} {b:10.4f} {b - a:+8.4f}")
    print(f"\nlargest per-capture application deviation: {worst:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
