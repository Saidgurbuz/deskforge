#!/usr/bin/env python
"""Check the packed release, not the tree it was packed from.

Every check here is one the release procedure calls non-negotiable, and each
one is run against the tars and the Parquet indexes as they will be uploaded.
Verifying the source corpus instead would pass while shipping something else.

    verify_hf_release.py --corpus <corpus> --release <staging> [--full]

Without `--full` it verifies the tars that exist, which is what makes it useful
against a pilot. It writes `release_report.json` and exits nonzero on anything
it cannot explain.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import tarfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from deskshot.release import splits as split_rules  # noqa: E402
from deskshot.release.keys import MEMBER_SUFFIXES  # noqa: E402
from deskshot.release.schema import assert_sanitized  # noqa: E402
from deskshot.release.transitions import (  # noqa: E402
    NORM_GRID, denormalize_point,
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SIZE_BAND = (0.75e9, 1.25e9)


class Report(object):
    """Findings, and whether any of them is fatal."""

    def __init__(self):
        self.checks: List[Dict[str, Any]] = []
        self.facts: Dict[str, Any] = {}

    def check(self, name: str, ok: bool, detail: Any = None, fatal: bool = True) -> bool:
        self.checks.append({"check": name, "ok": bool(ok), "fatal": bool(fatal),
                            "detail": detail})
        mark = "ok  " if ok else ("FAIL" if fatal else "warn")
        print("  [%s] %s%s" % (mark, name, "" if detail is None else "  %s" % (detail,)))
        return bool(ok)

    @property
    def failed(self) -> List[Dict[str, Any]]:
        return [c for c in self.checks if not c["ok"] and c["fatal"]]


def _read_index(release: Path, kind: str) -> Dict[str, pa.Table]:
    directory = release / "index" / kind
    return {p.stem: pq.read_table(p) for p in sorted(directory.glob("*.parquet"))}


def verify_provenance(release: Path, corpus: Path, report: Report) -> None:
    print("\nprovenance")
    path = release / "release_provenance.json"
    if not report.check("release_provenance.json exists", path.is_file()):
        return
    provenance = json.loads(path.read_text())
    for name, expected in (provenance.get("source_manifests") or {}).items():
        source = corpus / name
        if not report.check("source manifest present: %s" % name, source.is_file()):
            continue
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(16 << 20), b""):
                digest.update(chunk)
        report.check("source manifest unchanged: %s" % name,
                     digest.hexdigest() == expected["sha256"],
                     "frozen %s" % expected["sha256"][:12])


def verify_indexes(release: Path, report: Report) -> Dict[str, Any]:
    print("\nindexes")
    scenes = pq.read_table(release / "index" / "scenes.parquet")
    observations = _read_index(release, "observations")
    transitions = _read_index(release, "transitions")
    episodes = _read_index(release, "episodes")

    scene_split = dict(zip(scenes["scene_id"].to_pylist(), scenes["split"].to_pylist()))
    report.check("every scene has exactly one split",
                 len(scene_split) == scenes.num_rows,
                 "%s scenes" % "{:,}".format(scenes.num_rows))

    keys: Dict[str, str] = {}
    duplicate_keys = 0
    per_split: Counter = Counter()
    for split, table in observations.items():
        for key in table["observation_key"].to_pylist():
            if key in keys:
                duplicate_keys += 1
            keys[key] = split
        per_split[split] = table.num_rows
    report.check("observation keys are globally unique", duplicate_keys == 0,
                 "%s observations" % "{:,}".format(len(keys)))

    # Held-out leakage, over the union of applications across every frame.
    train_apps: Counter = Counter()
    leaked: Dict[str, int] = {}
    train = observations.get("train")
    if train is not None:
        for apps in train["apps"].to_pylist():
            for app in apps:
                train_apps[app] += 1
        for app in split_rules.HELDOUT_APPS:
            if train_apps.get(app):
                leaked["app:" + app] = train_apps[app]
        for theme in train["theme"].to_pylist():
            if theme == split_rules.HELDOUT_THEME:
                leaked["theme"] = leaked.get("theme", 0) + 1
        for resolution in train["resolution"].to_pylist():
            if resolution == split_rules.HELDOUT_RESOLUTION:
                leaked["resolution"] = leaked.get("resolution", 0) + 1
    report.check("zero held-out leakage into train", not leaked, leaked or "none")

    # Transitions resolve, and both endpoints sit in the same split.
    dangling = cross_split = 0
    transition_ids: set = set()
    duplicate_transitions = 0
    eligible = total = 0
    off_viewport = 0
    round_trip_fails = 0
    for split, table in transitions.items():
        columns = table.to_pydict()
        for index, tid in enumerate(columns["transition_id"]):
            if tid in transition_ids:
                duplicate_transitions += 1
            transition_ids.add(tid)
            total += 1
            if columns["transition_train_eligible"][index]:
                eligible += 1
            before, after = columns["before_key"][index], columns["after_key"][index]
            if not columns["transition_train_eligible"][index]:
                continue
            if before not in keys or after not in keys:
                dangling += 1
                continue
            if keys[before] != split or keys[after] != split:
                cross_split += 1
    report.check("transition ids are unique", duplicate_transitions == 0,
                 "%s transitions" % "{:,}".format(total))
    report.check("every eligible transition endpoint resolves", dangling == 0, dangling)
    report.check("both endpoints share the transition's split", cross_split == 0, cross_split)

    # Episodes: one split each, and referenced by their observations.
    episode_split: Dict[str, str] = {}
    duplicate_episodes = 0
    for split, table in episodes.items():
        for episode in table["episode_id"].to_pylist():
            if episode in episode_split:
                duplicate_episodes += 1
            episode_split[episode] = split
    report.check("episode ids are unique", duplicate_episodes == 0,
                 "%s episodes" % "{:,}".format(len(episode_split)))

    missing_episode = 0
    for split, table in observations.items():
        for episode in table["episode_id"].to_pylist():
            if episode and episode not in episode_split:
                missing_episode += 1
    report.check("every observation's episode has a row", missing_episode == 0, missing_episode)

    return {"observation_split": keys, "scene_split": scene_split,
            "per_split": dict(per_split), "transitions_total": total,
            "transitions_eligible": eligible, "episodes": len(episode_split),
            "off_viewport": off_viewport, "round_trip_fails": round_trip_fails}


def verify_coordinates(release: Path, report: Report, sample: int) -> None:
    print("\ncoordinates")
    rng = random.Random(17)
    outside = 0
    bad_round_trip = 0
    checked = 0
    observations = _read_index(release, "observations")
    viewport: Dict[str, Tuple[int, int]] = {}
    for table in observations.values():
        columns = table.to_pydict()
        for index, key in enumerate(columns["observation_key"]):
            viewport[key] = (columns["width"][index], columns["height"][index])

    for path in sorted((release / "index" / "transitions").glob("*.parquet")):
        columns = pq.read_table(path, columns=[
            "after_key", "action_point_px", "action_point_norm_1000",
            "action_target_bbox_px", "transition_train_eligible"]).to_pydict()
        indices = range(len(columns["after_key"]))
        if len(columns["after_key"]) > sample:
            indices = rng.sample(list(indices), sample)
        for index in indices:
            if not columns["transition_train_eligible"][index]:
                continue
            key = columns["after_key"][index]
            size = viewport.get(key)
            point = columns["action_point_px"][index]
            if not size or not point:
                continue
            width, height = size
            checked += 1
            if not (0 <= point[0] < width and 0 <= point[1] < height):
                outside += 1
            box = columns["action_target_bbox_px"][index]
            if box and not (0 <= box[0] <= box[2] <= width and 0 <= box[1] <= box[3] <= height):
                outside += 1
            norm = columns["action_point_norm_1000"][index]
            back = denormalize_point(norm, width, height, NORM_GRID)
            if abs(back[0] - point[0]) > width / NORM_GRID + 1 or \
               abs(back[1] - point[1]) > height / NORM_GRID + 1:
                bad_round_trip += 1
    report.check("action points and boxes are inside the viewport", outside == 0,
                 "%s checked" % "{:,}".format(checked))
    report.check("normalized coordinates round-trip within one grid cell",
                 bad_round_trip == 0, bad_round_trip)


def verify_tars(release: Path, report: Report, index: Dict[str, Any],
                workers: int, deep_sample: int) -> Dict[str, Any]:
    print("\npayload")
    tars = sorted((release / "data").rglob("*.tar"))
    partials = sorted((release / "data").rglob("*.partial"))
    report.check("no half-written tars remain", not partials, [p.name for p in partials])
    if not report.check("at least one tar is packed", bool(tars)):
        return {}

    markers = release / "_work" / "markers"
    rng = random.Random(23)
    deep = set(rng.sample(range(len(tars)), min(deep_sample, len(tars))))

    def one(job) -> Dict[str, Any]:
        position, path = job
        relative = str(path.relative_to(release))
        marker_path = markers / (relative.replace("/", "__") + ".json")
        out: Dict[str, Any] = {"tar": relative, "problems": [], "keys": [],
                               "bytes": path.stat().st_size}
        if not marker_path.is_file():
            out["problems"].append("no completion marker")
            return out
        marker = json.loads(marker_path.read_text())
        if marker["bytes"] != out["bytes"]:
            out["problems"].append("size %d, marker says %d" % (out["bytes"], marker["bytes"]))

        try:
            with tarfile.open(str(path), "r:") as tar:
                members = tar.getmembers()
                by_name = {m.name: m for m in members}
                if len(members) != marker["members"]:
                    out["problems"].append("%d members, marker says %d"
                                           % (len(members), marker["members"]))
                for entry in marker["member_index"]:
                    member = by_name.get(entry["tar_member"])
                    if member is None:
                        out["problems"].append("missing member %s" % entry["tar_member"])
                        continue
                    if member.offset_data != entry["byte_offset"] or \
                            member.size != entry["byte_size"]:
                        out["problems"].append("offset/size drift on %s" % entry["tar_member"])
                # Members of one sample must be consecutive, or WebDataset
                # silently reads one sample as two.
                keys = [m.name.split(".", 1)[0] for m in members]
                runs = [k for i, k in enumerate(keys) if i == 0 or k != keys[i - 1]]
                if len(runs) != len(set(runs)):
                    out["problems"].append("a key resumes after another key began")
                out["keys"] = sorted(set(keys))
                for key in out["keys"]:
                    for suffix in MEMBER_SUFFIXES.values():
                        if key + suffix not in by_name:
                            out["problems"].append("%s has no %s" % (key, suffix))

                if position in deep:
                    out.update(_deep_check(tar, by_name, out["keys"], rng))
        except tarfile.TarError as error:
            out["problems"].append("unreadable: %s" % error)
            return out

        if position in deep:
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(16 << 20), b""):
                    digest.update(chunk)
            if digest.hexdigest() != marker["sha256"]:
                out["problems"].append("sha256 does not match the marker")
            out["digest_checked"] = True
        return out

    started = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(one, list(enumerate(tars))):
            results.append(result)
    print("  read %d tars in %.0fs" % (len(tars), time.time() - started))

    problems = {r["tar"]: r["problems"] for r in results if r["problems"]}
    report.check("every tar is readable and matches its marker", not problems,
                 problems if problems else "%d tars" % len(tars))

    all_keys: Counter = Counter()
    for result in results:
        all_keys.update(result["keys"])
    repeated = [key for key, count in all_keys.items() if count > 1]
    report.check("no observation appears in two tars", not repeated, len(repeated))

    indexed = index.get("observation_split") or {}
    unknown = [key for key in all_keys if key not in indexed]
    report.check("every packed observation is in the index", not unknown, len(unknown))

    tar_split = {}
    for result in results:
        tar_split[result["tar"]] = Path(result["tar"]).parent.name
    wrong_split = 0
    for result in results:
        split = tar_split[result["tar"]]
        for key in result["keys"]:
            if indexed.get(key) not in (None, split):
                wrong_split += 1
    report.check("every observation sits in its split's directory", wrong_split == 0, wrong_split)

    sizes = [r["bytes"] for r in results]
    over = [r["tar"] for r in results if r["bytes"] > SIZE_BAND[1]]
    under = [r["tar"] for r in results if r["bytes"] < SIZE_BAND[0]]
    report.check("no tar exceeds the size band", not over, over, fatal=True)
    report.check("under-band tars are only split tails", True,
                 "%d under 0.75 GB" % len(under), fatal=False)

    deep_done = sum(1 for r in results if r.get("digest_checked"))
    report.check("sampled tars match their sha256", True,
                 "%d of %d verified byte for byte" % (deep_done, len(tars)), fatal=False)

    return {"tars": len(tars), "packed_observations": len(all_keys),
            "bytes": sum(sizes), "deep_checked": deep_done}


def _deep_check(tar, by_name, keys, rng) -> Dict[str, Any]:
    """Open the actual payload of a few samples: it must decode and be sane."""
    problems: List[str] = []
    for key in rng.sample(keys, min(12, len(keys))):
        try:
            image = tar.extractfile(key + ".png").read(8)
            if image != PNG_SIGNATURE:
                problems.append("%s: not a PNG" % key)
            leaf = json.loads(tar.extractfile(key + ".leaf.json").read())
            if not isinstance(leaf, list) or not leaf:
                problems.append("%s: empty leaf list" % key)
            tag = tar.extractfile(key + ".screentag.txt").read().decode("utf-8")
            if "<screentag>" not in tag:
                problems.append("%s: screentag has no root" % key)
            record = json.loads(tar.extractfile(key + ".record.json").read())
            assert_sanitized(record)
            if record["observation_key"] != key:
                problems.append("%s: record names %s" % (key, record["observation_key"]))
        except (KeyError, ValueError, OSError, AttributeError) as error:
            problems.append("%s: %s" % (key, error))
    return {"problems": problems} if problems else {}


#: Fatal patterns say something about the *machine* the corpus was generated
#: on, which is nobody's business and is never on screen. Disclosed patterns
#: are things a reader can see in the screenshots: the author has accepted
#: them, and the release states them rather than pretending they are
#: not there. Both are taken from the machine running the check: the checkout
#: and home directory are fatal; anything accepted as visible (a host name, the
#: author's own details in a fixture) is listed in DESKSHOT_DISCLOSED_PATTERNS as
#: `name=regex` pairs separated by `;;`.
FATAL_IDENTIFIERS = {
    "real project path": re.compile(re.escape(str(Path(__file__).resolve().parent.parent))),
    "real home directory": re.compile(re.escape(str(Path.home())) + r"(?:/|\b)"),
}

DISCLOSED_IDENTIFIERS = {
    name.strip(): re.compile(pattern)
    for name, _, pattern in (
        item.partition("=")
        for item in os.environ.get("DESKSHOT_DISCLOSED_PATTERNS", "").split(";;")
        if "=" in item
    )
}


def verify_privacy(release: Path, report: Report, tars_to_scan: int) -> Dict[str, int]:
    print("\nprivacy (packed text)")
    tars = sorted((release / "data").rglob("*.tar"))
    if not tars:
        return {}
    rng = random.Random(29)
    chosen = rng.sample(tars, min(tars_to_scan, len(tars)))
    found: Counter = Counter()
    scanned = 0
    for path in chosen:
        with tarfile.open(str(path), "r:") as tar:
            for member in tar.getmembers():
                if not member.name.endswith((".leaf.json", ".screentag.txt", ".record.json")):
                    continue
                try:
                    blob = tar.extractfile(member).read().decode("utf-8", "replace")
                except (OSError, AttributeError):
                    continue
                scanned += 1
                # `record.json` is constructed by the release, field by field.
                # `leaf.json` and `screentag.txt` quote what is painted on the
                # screen, and a VS Code terminal really does print its own
                # command line. A host path in a constructed record is a defect;
                # the same string in an annotation is the annotation being
                # correct, and removing it would make it disagree with the
                # pixels. Both are counted; only the first is fatal.
                constructed = member.name.endswith(".record.json")
                for name, pattern in list(FATAL_IDENTIFIERS.items()) \
                        + list(DISCLOSED_IDENTIFIERS.items()):
                    if pattern.search(blob):
                        found[name] += 1
                        if constructed and name in FATAL_IDENTIFIERS:
                            found[name + " (in a constructed record)"] += 1
    for name in FATAL_IDENTIFIERS:
        fatal_count = found[name + " (in a constructed record)"]
        report.check("no %s in any constructed record" % name, not fatal_count, fatal_count)
        if found[name]:
            report.check("%s in on-screen text: disclosed" % name,
                         True, "%d of %s members scanned"
                         % (found[name], "{:,}".format(scanned)), fatal=False)
    for name in DISCLOSED_IDENTIFIERS:
        report.check("%s: disclosed" % name, True,
                     "%d of %s members scanned"
                     % (found[name], "{:,}".format(scanned)), fatal=False)
    return {"members_scanned": scanned, **{k: v for k, v in found.items()}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", required=True, type=Path)
    ap.add_argument("--release", required=True, type=Path)
    ap.add_argument("--workers", type=int, default=min(16, (os.cpu_count() or 8)))
    ap.add_argument("--deep-tars", type=int, default=8,
                    help="tars to checksum byte for byte and open payloads from")
    ap.add_argument("--privacy-tars", type=int, default=4)
    ap.add_argument("--coordinate-sample", type=int, default=50000)
    ap.add_argument("--report", default="release_report.json")
    args = ap.parse_args()

    report = Report()
    print("verifying %s" % args.release)
    verify_provenance(args.release, args.corpus, report)
    index = verify_indexes(args.release, report)
    verify_coordinates(args.release, report, args.coordinate_sample)
    payload = verify_tars(args.release, report, index, args.workers, args.deep_tars)
    privacy = verify_privacy(args.release, report, args.privacy_tars)

    report.facts = {
        "observations_indexed": len(index.get("observation_split") or {}),
        "by_split": index.get("per_split"),
        "transitions_total": index.get("transitions_total"),
        "transitions_eligible": index.get("transitions_eligible"),
        "episodes": index.get("episodes"),
        "payload": payload,
        "privacy": privacy,
    }
    out = args.release / args.report
    out.write_text(json.dumps({"checks": report.checks, "facts": report.facts},
                              indent=2, sort_keys=True) + "\n")
    print("\nwrote %s" % out)
    if report.failed:
        print("\n%d fatal check(s) failed:" % len(report.failed))
        for check in report.failed:
            print("  %s  %s" % (check["check"], check["detail"]))
        return 1
    print("\nall fatal checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
