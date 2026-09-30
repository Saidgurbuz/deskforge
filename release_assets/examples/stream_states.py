#!/usr/bin/env python
"""Stream the States view: a screenshot and its dense annotation.

    python examples/stream_states.py --root . --split train --limit 2000

The recommended training filter is `record["flags"]["state_train_eligible"]`,
which drops near-duplicate scenes and no-op episode frames. Every publishable
observation is in the payload, so an evaluation that wants them can have them.

With `webdataset` installed the same thing is four lines:

    import webdataset as wds
    url = "data/train/part-{00000..00903}.tar"
    dataset = (wds.WebDataset(url, shardshuffle=True)
                 .decode("pil")
                 .to_tuple("png", "leaf.json", "screentag.txt", "record.json"))
"""

from __future__ import annotations

import argparse
import collections
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import decode, iter_tar, shards_for  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--split", default="train")
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--eligible-only", action="store_true",
                    help="the recommended training filter")
    ap.add_argument("--decode-images", action="store_true")
    args = ap.parse_args()

    shards = shards_for(args.root, args.split)
    if not shards:
        print("no shards under %s/data/%s" % (args.root, args.split), file=sys.stderr)
        return 1

    seen = kept = 0
    elements = 0
    apps: collections.Counter = collections.Counter()
    started = time.time()
    for shard in shards:
        for sample in iter_tar(shard):
            seen += 1
            item = decode(sample, with_image=args.decode_images)
            record = item["record"]
            if args.eligible_only and not record["flags"]["state_train_eligible"]:
                continue
            kept += 1
            elements += len(item["elements"])
            apps.update(record["apps"])
            if kept >= args.limit:
                break
        if kept >= args.limit:
            break

    seconds = time.time() - started
    print("%s samples read, %s kept in %.1fs  (%.0f samples/s)"
          % ("{:,}".format(seen), "{:,}".format(kept), seconds, kept / max(seconds, 1e-9)))
    print("mean elements per sample: %.1f" % (elements / max(kept, 1)))
    print("applications: %s" % ", ".join("%s %d" % pair for pair in apps.most_common(8)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
