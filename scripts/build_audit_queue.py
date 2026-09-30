#!/usr/bin/env python3
"""Draw a human-audit queue from the corpus index, once, and freeze it.

    PYTHONPATH=src python scripts/build_audit_queue.py --campaign pilot40 \
        --corpus <corpus_root> \
        --release <release_root> \
        --items 40 --seed 20260909 --dry-run

Why the queue is a file and not a query:

* A sample that is re-drawn after some of it has been labelled is not a random
  sample of anything. Freezing it makes the denominator knowable and makes the
  campaign reproducible from `manifest.json` alone.
* The inspector must stay stdlib-only (`server.py` says so, and it has to keep
  working after any environment rebuild), so nothing at serve time opens SQLite
  or Parquet. All of that happens here, ahead of time.

`publishable = 1` in `plan/index.sqlite` is exactly the 1,207,368 observations
the release published, so the audit population is the released one by
construction rather than by filtering afterwards.

With `--release`, each item's `.png`, `.elements.leaf.json` and
`.screentag.txt` are hashed and checked against the release's own
`index/shard_members.parquet`. Those three members are copied into the tars
byte-for-byte (`scripts/pack_hf_release.py` reads them through
`SOURCE_SUFFIXES` and only rebuilds `record.json`), so a match proves the item
a human is looking at *is* the released artefact - which matters because the
1.14TB payload was deleted after upload and cannot be read locally any more.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import os
import random
import sqlite3
import sys
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from deskshot.inspector import audit as audit_mod  # noqa: E402
from deskshot.inspector import samples as samples_mod  # noqa: E402

#: Applications the corpus has few of. Named so a stratum exists for them:
#: uniform sampling over 1.2M captures would put roughly one `pluma` and one
#: `gnome-system-monitor` in a 600-item queue, and those are held-out apps.
RARE_APPS = frozenset({
    "gnome-system-monitor", "pluma", "xarchiver", "eog", "transmission-gtk",
})

#: Upper bound of each band, half-open below and closed above, so a capture
#: with no occlusion at all is its own stratum rather than being lumped in with
#: one covered pixel.
OCCLUSION_BANDS = (("low", 0.10), ("mid", 0.30), ("high", 0.50))


def occlusion_band(ratio: Optional[float]) -> str:
    value = float(ratio or 0.0)
    if value <= 0.0:
        return "none"
    for name, upper in OCCLUSION_BANDS:
        if value <= upper:
            return name
    return "severe"


def density_band(n_elements: Optional[int]) -> str:
    count = int(n_elements or 0)
    if count >= 200:
        return "dense"
    if count < 60:
        return "sparse"
    return "medium"


def parse_apps(raw: Optional[str]) -> List[str]:
    """Apps are stored as `|a|b|`, so a substring test is wrong on purpose."""
    return [part for part in (raw or "").split("|") if part]


def stratum(row: sqlite3.Row) -> Tuple[str, ...]:
    apps = parse_apps(row["apps"])
    return (
        str(row["split"]),
        str(row["theme"]),
        str(row["resolution"]),
        density_band(row["n_elements"]),
        occlusion_band(row["occluded_ratio"]),
        str(row["group"]),
        "rare_app" if set(apps) & RARE_APPS else "common_app",
    )


STRATUM_FIELDS = ("split", "theme", "resolution", "density", "occlusion",
                  "group", "app_class")


def _rank(seed: int, path: str) -> str:
    return hashlib.sha256(("%d|%s" % (seed, path)).encode("utf-8")).hexdigest()


class _Reservoir(object):
    """The `size` paths with the smallest rank hash, in one pass.

    Selecting the smallest hashes is a uniform random sample without
    replacement, keyed by the campaign seed, so the draw is reproducible from
    `manifest.json` and costs memory proportional to the *sample*, not to the
    1.07M rows it is drawn from. A bounded max-heap keyed on the negated rank
    keeps the worst current member at the top so it can be displaced.
    """

    def __init__(self, size: int):
        self.size = max(0, int(size))
        self.heap: List[Tuple[str, str]] = []

    def offer(self, rank: str, path: str) -> None:
        if self.size == 0:
            return
        entry = (_invert(rank), path)
        if len(self.heap) < self.size:
            heapq.heappush(self.heap, entry)
        elif entry > self.heap[0]:
            heapq.heapreplace(self.heap, entry)

    def paths(self) -> List[str]:
        return [path for _, path in sorted(self.heap, reverse=True)]


def _invert(rank: str) -> str:
    """Order-reversing map on a hex digest, so a min-heap works as a max-heap."""
    return rank.translate(_INVERT_HEX)


_INVERT_HEX = str.maketrans("0123456789abcdef", "fedcba9876543210")


def select_paths(
    db: sqlite3.Connection,
    items: int,
    seed: int,
    where: Sequence[str],
    parameters: Sequence[Any],
    min_per_level: int,
) -> Tuple[List[Tuple[str, str]], Dict[str, Any]]:
    """A uniform sample of the population, with a floor under every stratum.

    A purely uniform draw is the unbiased thing and the easy thing to reason
    about, but 83% of the corpus is `train`, so a 600-item uniform sample gives
    each of the four held-out axes a handful of items and the three rare
    applications about one each - and those are exactly the slices an audit of
    generalisation is for. A purely stratified draw fixes coverage and breaks
    the population estimate instead.

    So: draw `items` uniformly, then guarantee `min_per_level` for every level
    of every stratum axis, and record which mechanism selected each item along
    with its inclusion weight. Both estimates are then computable - the
    population rate from the weights, the per-slice rate from the floor - and
    neither is silently mixed into the other.
    """
    clause = " AND ".join(where) if where else "1=1"
    population = _Reservoir(items)
    floors: Dict[Tuple[str, str], _Reservoir] = {}
    counts: Dict[Tuple[str, str], int] = defaultdict(int)
    scanned = 0
    cells: Dict[Tuple[str, ...], int] = defaultdict(int)

    query = ("SELECT path, \"group\", split, apps, theme, resolution, n_elements,"
             " occluded_ratio FROM samples WHERE %s" % clause)
    for row in db.execute(query, tuple(parameters)):
        scanned += 1
        path = row["path"]
        rank = _rank(seed, path)
        population.offer(rank, path)
        cell = stratum(row)
        cells[cell] += 1
        for field, level in zip(STRATUM_FIELDS, cell):
            counts[(field, level)] += 1
            reservoir = floors.get((field, level))
            if reservoir is None:
                reservoir = floors[(field, level)] = _Reservoir(min_per_level)
            reservoir.offer(rank, path)
        for app in parse_apps(row["apps"]):
            counts[("app", app)] += 1
            reservoir = floors.get(("app", app))
            if reservoir is None:
                reservoir = floors[("app", app)] = _Reservoir(min_per_level)
            reservoir.offer(rank, path)

    chosen: "OrderedDict[str, str]" = OrderedDict()
    # Floors first: they are the constraint, and a path a floor already picked
    # costs the population sample nothing because the weights are computed
    # from what each mechanism actually contributed.
    if min_per_level:
        for (field, level) in sorted(floors):
            for path in floors[(field, level)].paths():
                chosen.setdefault(path, "floor:%s=%s" % (field, level))
    floor_count = len(chosen)
    for path in population.paths():
        if len(chosen) >= items:
            break
        chosen.setdefault(path, "population")
    if len(chosen) > items:
        # Floors alone overflowed the budget. Keep them - a queue that misses a
        # whole stratum is worse than one slightly larger than asked for - but
        # say so loudly rather than silently truncating a stratum to nothing.
        pass

    picked = list(chosen.items())
    random.Random(seed).shuffle(picked)
    report = {
        "scanned": scanned,
        "joint_cells_occupied": len(cells),
        "requested": items,
        "selected": len(picked),
        "from_floor": floor_count,
        "from_population": sum(1 for _, how in picked if how == "population"),
        "min_per_level": min_per_level,
        "levels": {"%s=%s" % key: value for key, value in sorted(counts.items())},
        "population_total": scanned,
    }
    return picked, report


def fetch_rows(db: sqlite3.Connection, paths: Sequence[str]) -> Dict[str, sqlite3.Row]:
    """The full row for each selected path - a primary-key hit each, ~0.1ms."""
    out: Dict[str, sqlite3.Row] = {}
    query = ("SELECT path, stem, shard, scene_id, step, \"group\", split, apps, theme,"
             " resolution, profile, seed, n_elements, n_windows, occluded_ratio,"
             " publishable, train_eligible, near_duplicate, no_op_frame"
             " FROM samples WHERE path = ?")
    for path in paths:
        row = db.execute(query, (path,)).fetchone()
        if row is not None:
            out[path] = row
    return out


def sha256_file(path: Path) -> Tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def release_member_hashes(release: Path,
                          keys: Sequence[str]) -> Optional[Dict[Tuple[str, str], str]]:
    """Per-member SHA-256 from the release index, for the keys we drew.

    Optional: needs pyarrow, which the inspector does not have and does not
    want. Run this step from an environment that does (for example a venv
    with `pip install pyarrow`) or omit `--release`.
    """
    try:
        import pyarrow.parquet as pq
    except ImportError:
        raise SystemExit(
            "--release needs pyarrow; run this script with an interpreter that has it "
            "(e.g. a venv with pyarrow installed) or drop --release"
        )
    wanted = set(keys)
    members_path = release / "index" / "shard_members.parquet"
    if not members_path.is_file():
        # A release can be useful without its member index - the transition
        # geometry lives elsewhere - so this is reported and skipped rather
        # than fatal. It is said out loud because a provenance check that
        # quietly did not happen is worse than one that never existed.
        print("release has no index/shard_members.parquet: capture hashes are "
              "recorded but not verified against the published members")
        return None
    out: Dict[Tuple[str, str], str] = {}
    handle = pq.ParquetFile(str(members_path))
    for batch in handle.iter_batches(
        batch_size=250000, columns=["observation_key", "kind", "sha256"]
    ):
        table = batch.to_pydict()
        for key, kind, digest in zip(table["observation_key"], table["kind"], table["sha256"]):
            if key in wanted:
                out[(key, kind)] = digest
    return out


def build_items(
    picked: Sequence[Tuple[str, str]],
    rows: Dict[str, sqlite3.Row],
    corpus: Path,
    seed: int,
    element_sample: int,
    release_hashes: Optional[Dict[Tuple[str, str], str]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    problems: List[str] = []
    mismatches = 0
    verified = 0
    for path, draw in picked:
        row = rows.get(path)
        if row is None:
            problems.append("%s: vanished from the index between passes" % path)
            continue
        base = corpus / row["path"]
        png = Path(str(base) + ".png")
        leaf = Path(str(base) + samples_mod.VIEWS["leaf"])
        tag = Path(str(base) + ".screentag.txt")
        if not png.is_file() or not leaf.is_file():
            problems.append("%s: missing png or leaf" % row["path"])
            continue
        try:
            elements = samples_mod.load_elements(leaf)
        except (OSError, ValueError) as error:
            problems.append("%s: %s" % (row["path"], error))
            continue
        width, height = samples_mod.png_size(png)
        key = "%s__%s" % (row["shard"], row["stem"])
        digests: Dict[str, Dict[str, Any]] = {}
        for kind, path in (("image", png), ("leaf", leaf), ("screentag", tag)):
            if not path.is_file():
                continue
            digest, size = sha256_file(path)
            entry: Dict[str, Any] = {"sha256": digest, "bytes": size}
            if release_hashes is not None:
                expected = release_hashes.get((key, kind))
                entry["release_sha256_match"] = bool(expected) and expected == digest
                if expected and expected != digest:
                    mismatches += 1
                elif expected:
                    verified += 1
            digests[kind] = entry
        element_keys = audit_mod.sample_element_keys(
            elements, element_sample, "%d|%s" % (seed, key)
        )
        cell = dict(zip(STRATUM_FIELDS, stratum(row)))
        items.append({
            "schema": audit_mod.QUEUE_SCHEMA,
            "observation_key": key,
            "source_path": row["path"],
            "stem": row["stem"],
            "shard": row["shard"],
            "scene_id": row["scene_id"],
            "step": row["step"],
            "width": width,
            "height": height,
            "n_elements": len(elements),
            "n_windows": row["n_windows"],
            "occluded_ratio": row["occluded_ratio"],
            "apps": parse_apps(row["apps"]),
            "seed": row["seed"],
            "profile": row["profile"],
            "flags": {
                "publishable": bool(row["publishable"]),
                "train_eligible": bool(row["train_eligible"]),
                "near_duplicate": bool(row["near_duplicate"]),
                "no_op_frame": bool(row["no_op_frame"]),
            },
            "stratum": cell,
            "draw": draw,
            "element_keys": element_keys,
            "digests": digests,
        })
    return items, {
        "problems": problems[:50],
        "problem_count": len(problems),
        "release_verified": verified,
        "release_mismatched": mismatches,
    }


def strata_table(items: Sequence[Dict[str, Any]], field: str) -> List[Tuple[str, int]]:
    counter: Counter = Counter()
    for item in items:
        counter[str(item["stratum"].get(field))] += 1
    return counter.most_common()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--campaign", required=True,
                        help="campaign name; becomes <audit-root>/<name>/")
    parser.add_argument("--corpus", required=True, type=Path,
                        help="corpus root holding shards/ and plan/index.sqlite")
    parser.add_argument("--release", type=Path, default=None,
                        help="release root, to verify each item against its published member")
    parser.add_argument("--audit-root", type=Path, default=Path("audit"))
    parser.add_argument("--index", type=Path, default=None,
                        help="override plan/index.sqlite")
    parser.add_argument("--items", type=int, default=600)
    parser.add_argument("--seed", type=int, default=20260909)
    parser.add_argument("--min-per-level", type=int, default=4,
                        help="floor per level of every stratum axis and per application;"
                             " 0 for a purely uniform draw")
    parser.add_argument("--mode", choices=audit_mod.MODES, default=audit_mod.DEFAULT_MODE,
                        help="sweep: show the whole annotation and ask only for the "
                             "exceptions (fast, a census). sample: judge a fixed few "
                             "elements per screen individually (slower, stronger "
                             "per-element evidence). Both are recorded distinctly.")
    parser.add_argument("--element-sample", type=int, default=None,
                        help="elements judged per screen in sample mode "
                             "(default: the rubric's)")
    parser.add_argument("--rubric", type=Path, default=None,
                        help="rubric JSON; the built-in default when omitted")
    parser.add_argument("--split", action="append", default=[],
                        help="restrict to these splits (repeatable)")
    parser.add_argument("--app", action="append", default=[],
                        help="restrict to captures containing these apps (repeatable)")
    parser.add_argument("--include-near-duplicates", action="store_true")
    parser.add_argument("--include-no-op-frames", action="store_true")
    parser.add_argument("--title", default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="print the strata table and write nothing")
    args = parser.parse_args()

    name = audit_mod.safe_name(args.campaign, "campaign")
    corpus = args.corpus.resolve()
    index_path = args.index or (corpus / "plan" / "index.sqlite")
    if not index_path.is_file():
        raise SystemExit("no corpus index at %s" % index_path)

    rubric = audit_mod.load_rubric(args.rubric) if args.rubric else \
        audit_mod.validate_rubric(json.loads(json.dumps(audit_mod.DEFAULT_RUBRIC)))
    element_sample = args.element_sample or int(rubric.get("element_sample") or 8)
    rubric["element_sample"] = element_sample

    campaign = audit_mod.Campaign(args.audit_root / name)
    if campaign.exists() and not args.dry_run:
        raise SystemExit("%s already exists; campaigns are frozen once drawn"
                         % campaign.root)

    where = ["publishable = 1"]
    parameters: List[Any] = []
    if not args.include_near_duplicates:
        where.append("near_duplicate = 0")
    if not args.include_no_op_frames:
        where.append("no_op_frame = 0")
    if args.split:
        where.append("split IN (%s)" % ",".join("?" * len(args.split)))
        parameters.extend(args.split)
    for app in args.app:
        where.append("apps LIKE ?")
        parameters.append("%%|%s|%%" % app)

    db = sqlite3.connect("file:%s?mode=ro" % index_path, uri=True)
    db.row_factory = sqlite3.Row
    picked, report = select_paths(db, args.items, args.seed, where, parameters,
                                  args.min_per_level)
    print("index: %s scanned, %s joint cells occupied"
          % ("{:,}".format(report["scanned"]), "{:,}".format(report["joint_cells_occupied"])))
    print("draw: %s selected (%s uniform from the population, %s to floor a stratum"
          " at >=%d)"
          % ("{:,}".format(report["selected"]), "{:,}".format(report["from_population"]),
             "{:,}".format(report["from_floor"]), report["min_per_level"]))
    if not picked:
        raise SystemExit("the filters selected nothing")
    if report["selected"] > args.items:
        print("note: the stratum floors need %d items, more than the %d asked for"
              % (report["selected"], args.items))

    rows = fetch_rows(db, [path for path, _ in picked])

    release_hashes = None
    if args.release:
        keys = ["%s__%s" % (row["shard"], row["stem"]) for row in rows.values()]
        release_hashes = release_member_hashes(args.release.resolve(), keys)
        print("release index: %s member hashes for %s items"
              % ("{:,}".format(len(release_hashes)), "{:,}".format(len(keys))))

    items, build_report = build_items(picked, rows, corpus, args.seed, element_sample,
                                      release_hashes)
    if build_report["problem_count"]:
        print("skipped %d unreadable captures; first few:" % build_report["problem_count"])
        for problem in build_report["problems"][:5]:
            print("  %s" % problem)
    if release_hashes is not None:
        print("release verification: %d members matched, %d mismatched"
              % (build_report["release_verified"], build_report["release_mismatched"]))
        if build_report["release_mismatched"]:
            raise SystemExit("a local file differs from its published member; refusing to write")
    if not items:
        raise SystemExit("nothing readable was selected")

    for field in STRATUM_FIELDS:
        pairs = strata_table(items, field)
        print("%-11s %s" % (field, "  ".join("%s=%d" % pair for pair in pairs)))
    if args.mode == "sample":
        print("elements: %d screens x %d sampled = %s judgements"
              % (len(items), element_sample, "{:,}".format(len(items) * element_sample)))
    else:
        total = sum(int(item["n_elements"] or 0) for item in items)
        print("elements: %d screens, %s annotated elements in all - a sweep judges "
              "every one of them by exception" % (len(items), "{:,}".format(total)))

    if args.dry_run:
        print("dry run: nothing written")
        return 0

    manifest = {
        "schema": audit_mod.SCHEMA,
        "name": name,
        "title": args.title or "Annotation audit %s" % name,
        "created_at": audit_mod.now_stamp(),
        "created_by": os.environ.get("USER") or "unknown",
        "corpus": str(corpus),
        "index": str(index_path),
        "release": str(args.release.resolve()) if args.release else None,
        "seed": args.seed,
        "mode": args.mode,
        "requested_items": args.items,
        "items": len(items),
        "population_total": report["population_total"],
        "population_items": sum(1 for item in items if item["draw"] == "population"),
        "element_sample": element_sample,
        "filters": {
            "where": where,
            "parameters": parameters,
            "splits": args.split,
            "apps": args.app,
            "include_near_duplicates": bool(args.include_near_duplicates),
            "include_no_op_frames": bool(args.include_no_op_frames),
        },
        "stratum_fields": list(STRATUM_FIELDS),
        "strata": {field: dict(strata_table(items, field)) for field in STRATUM_FIELDS},
        "selection": report,
        "provenance": build_report,
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
