#!/usr/bin/env python
"""Fetch one observation by key, without reading the shard that holds it.

    python examples/get_by_key.py --root . --key shard-0106__scene-002ad3e5b23cbfd9-step00

`index/shard_members.parquet` carries a byte offset and length for every
member. The canonical shards are **uncompressed** tars, so those offsets are
real file positions: one seek and one read returns the PNG, whether the file is
local or behind an HTTP range request.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pyarrow.compute as pc
import pyarrow.parquet as pq


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--key", required=True)
    ap.add_argument("--kind", default="record",
                    choices=["image", "leaf", "screentag", "record"])
    ap.add_argument("--out", default=None, help="write the bytes here")
    args = ap.parse_args()

    root = Path(args.root)
    members = pq.read_table(root / "index" / "shard_members.parquet")
    rows = members.filter(
        pc.and_(pc.equal(members["observation_key"], args.key),
                pc.equal(members["kind"], args.kind))).to_pydict()
    if not rows["observation_key"]:
        print("no member %r for key %r" % (args.kind, args.key), file=sys.stderr)
        return 1

    tar_path = root / rows["tar_path"][0]
    offset, size = rows["byte_offset"][0], rows["byte_size"][0]
    with tar_path.open("rb") as handle:
        handle.seek(offset)
        blob = handle.read(size)

    print("%s  %s  %d bytes at offset %d in %s"
          % (args.key, args.kind, size, offset, rows["tar_path"][0]))
    if args.out:
        Path(args.out).write_bytes(blob)
        print("wrote %s" % args.out)
    elif args.kind == "record":
        print(json.dumps(json.loads(blob), indent=2)[:1200])
    elif args.kind == "screentag":
        print(blob.decode("utf-8")[:600])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
