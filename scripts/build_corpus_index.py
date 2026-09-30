#!/usr/bin/env python
"""Turn the corpus manifest into something a browser can query.

`plan/manifest.jsonl` is 746MB and 1.27M rows. Reading it to answer "what is
this capture" costs a full scan; holding it in memory costs a few gigabytes.
SQLite is 200MB on disk, answers by primary key instantly, and lets the
inspector ask real questions - "a test_app sample with three windows and heavy
occlusion" - instead of only offering a directory tree.

    build_corpus_index.py --corpus /path/to/v1
    build_corpus_index.py --corpus /path/to/v1 --splits plan/splits_v3.jsonl

Rebuild it whenever the manifest or the splits change. It is derived data:
delete it and the inspector falls back to plain browsing.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    path            TEXT PRIMARY KEY,
    stem            TEXT NOT NULL,
    shard           TEXT NOT NULL,
    scene_id        TEXT,
    step            INTEGER,
    "group"         TEXT,
    split           TEXT,
    apps            TEXT,
    theme           TEXT,
    resolution      TEXT,
    profile         TEXT,
    seed            INTEGER,
    n_elements      INTEGER,
    n_windows       INTEGER,
    occluded_ratio  REAL,
    publishable     INTEGER,
    train_eligible  INTEGER,
    near_duplicate  INTEGER,
    no_op_frame     INTEGER
);
CREATE INDEX IF NOT EXISTS samples_split ON samples(split);
CREATE INDEX IF NOT EXISTS samples_shard ON samples(shard);
CREATE INDEX IF NOT EXISTS samples_scene ON samples(scene_id);
CREATE INDEX IF NOT EXISTS samples_theme ON samples(theme);
CREATE INDEX IF NOT EXISTS samples_resolution ON samples(resolution);
CREATE INDEX IF NOT EXISTS samples_occlusion ON samples(occluded_ratio);
"""


def _row(record: Dict[str, Any], split: Optional[str]) -> tuple:
    #: `|a|b|` so `LIKE '%|eog|%'` cannot match `gnome-logs` inside `logs`.
    apps = "|%s|" % "|".join(str(a) for a in (record.get("apps") or []))
    shard = str(record.get("shard") or "")
    path = str(record.get("path") or "")
    full = "shards/%s/%s" % (shard, path) if shard and path else path
    return (
        full,
        full.rsplit("/", 1)[-1],
        shard,
        record.get("scene_id"),
        record.get("step"),
        record.get("group"),
        split or record.get("split"),
        apps,
        record.get("theme"),
        record.get("resolution"),
        record.get("profile"),
        record.get("seed"),
        record.get("n_elements"),
        record.get("n_windows"),
        record.get("occluded_ratio"),
        1 if record.get("publishable") else 0,
        1 if record.get("train_eligible") else 0,
        1 if record.get("near_duplicate") else 0,
        1 if record.get("no_op_frame") else 0,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", required=True, type=Path)
    ap.add_argument("--manifest", default="plan/manifest.jsonl")
    ap.add_argument("--splits", default="plan/splits_v3.jsonl",
                    help="optional; its `split` overrides the manifest's")
    ap.add_argument("--out", default="plan/index.sqlite")
    ap.add_argument("--batch", type=int, default=20000)
    args = ap.parse_args()

    manifest = args.corpus / args.manifest
    if not manifest.is_file():
        print("no manifest at %s" % manifest, file=sys.stderr)
        return 1
    splits = args.corpus / args.splits
    if not splits.is_file():
        print("no splits at %s - indexing the manifest alone" % splits)
        splits = None

    out = args.corpus / args.out
    tmp = out.with_name(out.name + ".partial")
    if tmp.exists():
        tmp.unlink()
    out.parent.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(str(tmp))
    db.executescript(SCHEMA)
    db.execute("PRAGMA journal_mode = OFF")
    db.execute("PRAGMA synchronous = OFF")

    started = time.time()
    insert = "INSERT OR REPLACE INTO samples VALUES (%s)" % ",".join("?" * 19)

    # The manifest is the base, not the splits file: the splits only cover the
    # train-eligible 1.07M, and the ~199k samples it leaves out - the rejected
    # and the near-duplicate - are exactly the ones somebody opens the inspector
    # to look at. Indexing splits alone left those browsable but factless.
    total = skipped = 0
    batch = []
    with manifest.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            batch.append(_row(record, record.get("split")))
            if len(batch) >= args.batch:
                db.executemany(insert, batch)
                total += len(batch)
                batch = []
                if total % 200000 == 0:
                    print("  %s manifest rows  %.0fs"
                          % ("{:,}".format(total), time.time() - started))
    if batch:
        db.executemany(insert, batch)
        total += len(batch)
    db.commit()

    # Second pass by primary key rather than a path->split dict, which would be
    # a few hundred megabytes of Python strings for no gain.
    assigned = 0
    if splits is not None:
        updates = []
        with splits.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                split = record.get("split")
                shard, path = record.get("shard"), record.get("path")
                if not split or not shard or not path:
                    continue
                updates.append((split, "shards/%s/%s" % (shard, path)))
                if len(updates) >= args.batch:
                    db.executemany("UPDATE samples SET split = ? WHERE path = ?", updates)
                    assigned += len(updates)
                    updates = []
        if updates:
            db.executemany("UPDATE samples SET split = ? WHERE path = ?", updates)
            assigned += len(updates)
        db.commit()

    db.execute("ANALYZE")
    db.commit()
    db.close()
    os.replace(str(tmp), str(out))

    print("%s\n  %s rows from %s  %s unreadable  %.0f MB  %.0fs"
          % (out, "{:,}".format(total), manifest.name, "{:,}".format(skipped),
             out.stat().st_size / 1e6, time.time() - started))
    if splits is not None:
        print("  %s carry a split from %s; the rest are on disk but not train-eligible"
              % ("{:,}".format(assigned), splits.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
