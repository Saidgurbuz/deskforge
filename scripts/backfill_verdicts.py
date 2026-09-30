#!/usr/bin/env python3
"""Write the accept/reject verdict for captures that never got one.

A shard normally judges its captures in an audit pass when it finishes. That
pass crashed on one run, so 242,700 samples sit on disk with no verdict - all
perfectly good captures that nothing has an opinion about. Re-capturing them to
recover an opinion would be absurd: the verdict is a **function of files that
already exist**, so it can simply be computed.

Two of the three judgements need no pixels at all:

- **missing_apps** - the scene's planned applications are in the capture's own
  metadata, and so are the ones that actually came up.
- **quality_checks** - element counts, type diversity, coverage and tree depth
  all read the element lists.

The third, **near_duplicate_capture**, needs a perceptual hash of every
screenshot compared against every other, which is a different and much more
expensive job; it is left out and recorded as not evaluated rather than quietly
assumed. That is honest and it keeps this pass fast enough to run over a corpus:
no image is decoded.

Verdicts written here carry `"source": "backfill"` so they are never confused
with one a shard produced while running.

    PYTHONPATH=src python scripts/backfill_verdicts.py --root <corpus root>
    PYTHONPATH=src python scripts/backfill_verdicts.py --root <root> --shard shard-0007
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
import threading
from concurrent.futures import (
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
)
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from deskshot.postprocessing.quality import run_quality_checks  # noqa: E402


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _elements(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        found = payload.get("elements")
        return found if isinstance(found, list) else []
    return []


def verdict_for(meta_path: Path) -> Dict[str, Any] | None:
    """Judge one capture from its metadata and element lists."""
    meta = _load(meta_path)
    if not isinstance(meta, dict):
        return None
    stem = str(meta.get("stem") or meta_path.name.split(".meta.json")[0])
    here = meta_path.parent

    scene = meta.get("scene") or {}
    planned = [a.get("app_name") for a in scene.get("apps", []) if a.get("app_name")]
    # Only apps that never came up count as missing. One that launched and is
    # hidden behind another window is a layout we generate deliberately, not a
    # fault - see `evaluate_scene_capture`.
    launched = {str(a) for a in (meta.get("launched_apps") or []) if str(a).strip()}
    missing = [app for app in planned if app not in launched]

    filtered = _elements(_load(here / f"{stem}.elements.json"))
    leaf = _elements(_load(here / f"{stem}.elements.leaf.json"))
    viewport = meta.get("viewport") or {}
    present = {
        (e.get("app_name") or "").strip()
        for e in filtered
        if e.get("source") == "app" and (e.get("app_name") or "").strip()
    }

    failed: List[str] = []
    quality_ok = True
    if filtered:
        for name, ok, _message in run_quality_checks(
            filtered,
            leaf_elements=leaf,
            viewport_w=int(viewport.get("width") or 1920),
            viewport_h=int(viewport.get("height") or 1080),
        ):
            if not ok:
                failed.append(name)
                quality_ok = False
    else:
        failed.append("elements_missing")
        quality_ok = False

    reasons: List[str] = []
    if missing:
        reasons.append("missing_apps")
    if not quality_ok:
        reasons.append("quality_checks_failed")

    # A verdict the run produced carries a perceptual hash and a near-duplicate
    # decision that this pass cannot recompute without decoding every PNG. An
    # earlier version overwrote those with nulls, which silently destroyed the
    # duplicate detection for every shard that already had a real verdict. Keep
    # them, and re-apply the duplicate rejection they encode.
    prior = _load(here / f"{stem}.verdict.json")
    prior = prior if isinstance(prior, dict) else {}
    near_duplicate_of = prior.get("near_duplicate_of") or ""
    if near_duplicate_of:
        reasons.append("near_duplicate_capture")

    verdict = {
        "stem": stem,
        "seed": scene.get("seed"),
        "scene_id": scene.get("scene_id", ""),
        "accepted": not reasons,
        "status": "accepted" if not reasons else "rejected_post",
        "reasons": reasons,
        "missing_apps": missing,
        # Recorded, never a rejection: a window can legitimately be covered.
        "occluded_apps": [a for a in planned if a in launched and a not in present],
        "quality_ok": quality_ok,
        "failed_checks": failed,
        "near_duplicate_of": near_duplicate_of,
        "source": "backfill",
    }
    for carried in ("screenshot_hash", "near_duplicate_distance"):
        if carried in prior:
            verdict[carried] = prior[carried]
    return verdict


def _judge_one(meta_path: Path, overwrite: bool) -> str:
    """Judge a single capture. Returns which counter to bump."""
    stem = meta_path.name.split(".meta.json")[0]
    target = meta_path.parent / f"{stem}.verdict.json"
    if target.exists() and not overwrite:
        return "skipped"
    verdict = verdict_for(meta_path)
    if verdict is None:
        return "unreadable"
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(verdict, sort_keys=True), encoding="utf-8")
    tmp.replace(target)
    return "written"


def _do_shard(shard: Path, overwrite: bool, limit: int, threads: int = 16) -> Dict[str, int]:
    """Judge every capture in one shard. Safe to run concurrently with others.

    Judging costs 5 ms of CPU and roughly 700 ms of cold GPFS latency, so the
    work inside a shard is spread over threads rather than done in order: a
    serial pass over one 4,340-capture shard took 55 minutes and the whole
    corpus would have taken 86 hours. Threads are the right tool precisely
    because the cost is waiting, not computing, and they share the page cache.
    """
    counts = Counter()
    metas = list(shard.rglob("*.meta.json"))
    if limit:
        metas = metas[:limit]
    if not metas:
        return {"written": 0, "skipped": 0, "unreadable": 0}
    with ThreadPoolExecutor(max_workers=threads) as pool:
        for outcome in pool.map(lambda m: _judge_one(m, overwrite), metas):
            counts[outcome] += 1
    return {"written": counts["written"], "skipped": counts["skipped"],
            "unreadable": counts["unreadable"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--shard", default="", help="one shard directory name")
    parser.add_argument("--overwrite", action="store_true",
                        help="also rewrite verdicts a shard already produced")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--jobs", type=int, default=1,
                        help="worker processes; this pass is I/O bound on GPFS, "
                             "so more workers help far past the core count")
    parser.add_argument("--threads", type=int, default=16,
                        help="concurrent captures judged inside one shard")
    parser.add_argument("--slice", default="",
                        help="'K/N' - take every Nth shard starting at K, so an "
                             "LSF array can cover the corpus without overlap")
    args = parser.parse_args()

    base = args.root / "shards"
    shards = [base / args.shard] if args.shard else sorted(
        p for p in base.iterdir() if p.is_dir()
    ) if base.is_dir() else [args.root]

    if args.slice:
        k, n = (int(x) for x in args.slice.split("/"))
        shards = [s for i, s in enumerate(shards) if i % n == k]

    totals = Counter()
    if args.jobs > 1 and len(shards) > 1:
        # Judging one capture reads three JSON files and writes one. That is
        # almost pure GPFS latency - a single process managed one shard an hour,
        # which is 180 hours for this corpus - so the pool is sized for
        # outstanding I/O, not for CPU.
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            futures = {
                pool.submit(_do_shard, shard, args.overwrite, args.limit, args.threads): shard
                for shard in shards
            }
            for future in as_completed(futures):
                shard = futures[future]
                try:
                    result = future.result()
                except Exception as exc:  # a bad shard must not sink the rest
                    print(f"{shard.name}: FAILED {exc!r}", flush=True)
                    totals["failed_shards"] += 1
                    continue
                totals.update(result)
                print(f"{shard.name}: wrote {result['written']} "
                      f"skipped {result['skipped']} unreadable {result['unreadable']}",
                      flush=True)
    else:
        for shard in shards:
            result = _do_shard(shard, args.overwrite, args.limit, args.threads)
            totals.update(result)
            print(f"{shard.name}: wrote {result['written']} "
                  f"skipped {result['skipped']} unreadable {result['unreadable']}",
                  flush=True)

    print(f"TOTAL wrote {totals['written']} verdicts, "
          f"skipped {totals['skipped']} that already had one, "
          f"{totals['unreadable']} unreadable, "
          f"{totals['failed_shards']} shards failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
