#!/usr/bin/env python
"""Stream the Transitions view: before, action, after.

    python examples/stream_transitions.py --root . --split train --limit 2000

A screenshot is stored **once**. A transition names its two endpoints by key,
so the loader pairs them; it never stores both images in a row. Because a whole
episode is packed into one shard, both endpoints of every transition are in the
shard already open - pair first, then shuffle at sample level, and the pairing
costs no extra I/O.

`transition_train_eligible` is not the same flag as `state_train_eligible`. A
transition whose action changed nothing is **kept** - it is negative
supervision - while the resulting state is excluded from the States view.
"""

from __future__ import annotations

import argparse
import collections
import sys
import time
from pathlib import Path

import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import decode, iter_tar, shards_for  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--split", default="train")
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--eligible-only", action="store_true", default=True)
    ap.add_argument("--decode-images", action="store_true")
    args = ap.parse_args()

    root = Path(args.root)
    table = pq.read_table(
        root / "index" / "transitions" / ("%s.parquet" % args.split),
        columns=["before_key", "after_key", "action_type", "action_point_px",
                 "action_target_role", "action_target_text", "effect",
                 "transition_train_eligible"]).to_pydict()
    by_before: dict = collections.defaultdict(list)
    for index, before in enumerate(table["before_key"]):
        if args.eligible_only and not table["transition_train_eligible"][index]:
            continue
        by_before[before].append(index)
    print("%s eligible transitions in %s" % ("{:,}".format(sum(
        len(v) for v in by_before.values())), args.split))

    pairs = 0
    changed = 0
    roles: collections.Counter = collections.Counter()
    started = time.time()
    for shard in shards_for(root, args.split):
        # One pass per shard: hold only the states this shard's transitions need.
        held: dict = {}
        wanted_after: dict = collections.defaultdict(list)
        for sample in iter_tar(shard):
            key = sample["__key__"]
            item = decode(sample, with_image=args.decode_images)
            if key in by_before:
                held[key] = item
                for index in by_before[key]:
                    wanted_after[table["after_key"][index]].append((key, index))
            for before_key, index in wanted_after.pop(key, []):
                before = held.get(before_key)
                if before is None:
                    continue
                pairs += 1
                changed += bool(table["effect"][index]["changed"])
                roles[table["action_target_role"][index]] += 1
                if pairs >= args.limit:
                    break
            if pairs >= args.limit:
                break
        if pairs >= args.limit:
            break

    seconds = time.time() - started
    print("%s transitions paired in %.1fs  (%.0f/s)"
          % ("{:,}".format(pairs), seconds, pairs / max(seconds, 1e-9)))
    print("changed something: %.1f%%   no-op: %.1f%%"
          % (100.0 * changed / max(pairs, 1), 100.0 * (pairs - changed) / max(pairs, 1)))
    print("clicked roles: %s" % ", ".join("%s %d" % pair for pair in roles.most_common(8)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
