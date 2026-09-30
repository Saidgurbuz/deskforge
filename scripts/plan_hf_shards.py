#!/usr/bin/env python
"""Decide which observation goes in which tar, before anything is packed.

Packing is the expensive, restartable part, so the decision is made once and
written down. `pack_plan.parquet` is the contract: a worker owns one tar, reads
its rows, and never has to agree with another worker about anything.

    plan_hf_shards.py --corpus <corpus> --release <staging> [--target-gb 1.0]

Two rules the plan must not break:

* **A whole scene lands in one tar.** The split unit is the scene, and an
  episode is inside a scene, so scene-level assignment also keeps every episode
  in one tar - which is what makes a pair-then-shuffle transition loader able to
  read both endpoints from the shard it already has open.
* **Assignment is deterministic.** Scenes are taken in sorted order and filled
  greedily, so re-running the planner on the same index reproduces the same
  plan, and a half-finished pack can be resumed rather than restarted.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Tuple

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from deskshot.release.keys import SOURCE_SUFFIXES  # noqa: E402

#: A tar header is 512 bytes and every member is padded to a 512-byte boundary,
#: so a four-member observation costs about 3KB of tar overhead. Counting it
#: keeps the finished shards inside the size band instead of just above it.
TAR_BLOCK = 512
RECORD_ESTIMATE = 4096


def _member_overhead(size: int) -> int:
    return TAR_BLOCK + ((size + TAR_BLOCK - 1) // TAR_BLOCK) * TAR_BLOCK


def measure(corpus: Path, source_paths: List[str], workers: int) -> Dict[str, int]:
    """Bytes each observation costs in a tar, by listing directories not files.

    One `scandir` per source directory answers for every capture in it. Statting
    three files per observation would be 3.6M round trips on a shared
    filesystem; this is about 210,000.
    """
    directories: Dict[str, List[str]] = defaultdict(list)
    for path in source_paths:
        head, _, stem = path.rpartition("/")
        directories[head].append(stem)

    wanted_suffixes = tuple(SOURCE_SUFFIXES.values())

    def one(directory: str) -> Tuple[str, Dict[str, int]]:
        sizes: Dict[str, int] = {}
        try:
            with os.scandir(str(corpus / directory)) as entries:
                for entry in entries:
                    if not entry.name.endswith(wanted_suffixes):
                        continue
                    try:
                        sizes[entry.name] = entry.stat().st_size
                    except OSError:
                        continue
        except OSError:
            pass
        return directory, sizes

    started = time.time()
    out: Dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for done, (directory, sizes) in enumerate(
                pool.map(one, sorted(directories), chunksize=32), start=1):
            for stem in directories[directory]:
                total = _member_overhead(RECORD_ESTIMATE)
                for suffix in wanted_suffixes:
                    total += _member_overhead(sizes.get(stem + suffix, 0))
                out["%s/%s" % (directory, stem)] = total
            if done % 40000 == 0:
                print("  measured %s directories  %.0fs"
                      % ("{:,}".format(done), time.time() - started))
    print("measured %s directories for %s observations  (%.0fs)"
          % ("{:,}".format(len(directories)), "{:,}".format(len(out)), time.time() - started))
    return out


PLAN_SCHEMA = pa.schema([
    pa.field("observation_key", pa.string()), pa.field("split", pa.string()),
    pa.field("scene_id", pa.string()), pa.field("episode_id", pa.string()),
    pa.field("source_path", pa.string()), pa.field("tar_path", pa.string()),
    pa.field("est_bytes", pa.int64()), pa.field("member_index", pa.int32()),
])


def plan_split(split: str, table: pa.Table, sizes: Dict[str, int],
               target_bytes: int) -> Tuple[List[dict], List[dict]]:
    keys = table["observation_key"].to_pylist()
    scenes = table["scene_id"].to_pylist()
    episodes = table["episode_id"].to_pylist()
    paths = table["source_path"].to_pylist()

    by_scene: Dict[str, List[int]] = defaultdict(list)
    for index, scene in enumerate(scenes):
        by_scene[scene].append(index)

    rows: List[dict] = []
    shards: List[dict] = []
    shard_index, used, members = 0, 0, 0
    # Sorted so the plan is a function of the index, not of row order.
    for scene in sorted(by_scene):
        indices = sorted(by_scene[scene], key=lambda i: keys[i])
        cost = sum(sizes.get(paths[i], 0) for i in indices)
        if used and used + cost > target_bytes:
            shards.append({"split": split, "tar_path": _tar_path(split, shard_index),
                           "est_bytes": used, "observations": members})
            shard_index += 1
            used, members = 0, 0
        tar = _tar_path(split, shard_index)
        for i in indices:
            rows.append({
                "observation_key": keys[i], "split": split, "scene_id": scene,
                "episode_id": episodes[i], "source_path": paths[i], "tar_path": tar,
                "est_bytes": sizes.get(paths[i], 0), "member_index": members,
            })
            members += 1
        used += cost
    if members:
        shards.append({"split": split, "tar_path": _tar_path(split, shard_index),
                       "est_bytes": used, "observations": members})
    return rows, shards


def _tar_path(split: str, index: int) -> str:
    return "data/%s/part-%05d.tar" % (split, index)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", required=True, type=Path)
    ap.add_argument("--release", required=True, type=Path)
    ap.add_argument("--target-gb", type=float, default=1.0,
                    help="target uncompressed tar size; the band is 0.75-1.25 GB")
    ap.add_argument("--workers", type=int, default=min(32, (os.cpu_count() or 8) * 2))
    args = ap.parse_args()

    target = int(args.target_gb * 1e9)
    observations = args.release / "index" / "observations"
    tables = {p.stem: pq.read_table(
        p, columns=["observation_key", "scene_id", "episode_id", "source_path"])
        for p in sorted(observations.glob("*.parquet"))}
    if not tables:
        raise SystemExit("no observation index in %s - run build_release_manifests.py" % observations)

    every_path: List[str] = []
    for table in tables.values():
        every_path.extend(table["source_path"].to_pylist())
    sizes = measure(args.corpus, every_path, args.workers)
    unmeasured = sum(1 for path in every_path if path not in sizes)
    if unmeasured:
        raise SystemExit("%d observations have no source files on disk" % unmeasured)

    rows: List[dict] = []
    shards: List[dict] = []
    for split in sorted(tables):
        split_rows, split_shards = plan_split(split, tables[split], sizes, target)
        rows.extend(split_rows)
        shards.extend(split_shards)

    work = args.release / "_work"
    work.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({field.name: [row[field.name] for row in rows] for field in PLAN_SCHEMA},
                 schema=PLAN_SCHEMA),
        work / "pack_plan.parquet", compression="zstd")
    pq.write_table(
        pa.table({
            "split": [s["split"] for s in shards],
            "tar_path": [s["tar_path"] for s in shards],
            "est_bytes": [s["est_bytes"] for s in shards],
            "observations": [s["observations"] for s in shards],
        }), work / "shard_plan.parquet", compression="zstd")

    total = sum(s["est_bytes"] for s in shards)
    print("\nplanned %s tars over %s observations, %.2f TB"
          % ("{:,}".format(len(shards)), "{:,}".format(len(rows)), total / 1e12))
    print("\n  %-16s %6s %12s %10s %10s" % ("split", "tars", "observations", "TB", "mean GB"))
    for split in sorted(tables):
        mine = [s for s in shards if s["split"] == split]
        size = sum(s["est_bytes"] for s in mine)
        print("  %-16s %6d %12s %10.3f %10.2f"
              % (split, len(mine), "{:,}".format(sum(s["observations"] for s in mine)),
                 size / 1e12, size / max(1, len(mine)) / 1e9))

    oversize = [s for s in shards if s["est_bytes"] > 1.25e9]
    undersize = [s for s in shards if s["est_bytes"] < 0.75e9]
    print("\noutside the 0.75-1.25 GB band: %d over, %d under" % (len(oversize), len(undersize)))
    if oversize:
        worst = max(oversize, key=lambda s: s["est_bytes"])
        print("  largest %s at %.2f GB - a single scene bigger than the target"
              % (worst["tar_path"], worst["est_bytes"] / 1e9))
    if undersize:
        print("  under-band tars are the last of each split, plus splits smaller than one tar")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
