#!/usr/bin/env python
"""Write the WebDataset tars, one owner per tar, resumable at any point.

Packing 1.09 TB takes hours and will be interrupted. The invariants that make
that survivable:

* **One worker owns one tar.** Two workers never append to the same file, so
  there is no interleaving to detect afterwards.
* **A tar is written to `<name>.partial`, verified, then renamed.** A `.tar`
  that exists is a `.tar` that was finished and read back; a crash leaves a
  `.partial` that the next run overwrites.
* **A completion marker records what the tar contains.** Byte offsets, sizes
  and digests are captured while the finished file is verified, which is the
  only moment they are known to be true.

    pack_hf_release.py --corpus <corpus> --release <staging> [--limit 10]

`--limit` packs the first N planned tars of each split, which is the pilot.
Re-running skips tars that already have a marker, so the full pack is the same
command without `--limit`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tarfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from deskshot.release.keys import SOURCE_SUFFIXES, member_name  # noqa: E402
from deskshot.release.schema import assert_sanitized, build_record  # noqa: E402

DATASET_VERSION = "1.0.0"
#: Fixed so a tar is byte-reproducible: the mtime of a file on a research
#: filesystem is when the job happened to run, which is not release metadata.
FIXED_MTIME = 0
KIND_ORDER = ("image", "leaf", "screentag", "record")


class PackError(Exception):
    pass


def _read_bytes(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read()


def _tarinfo(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = FIXED_MTIME
    info.mode = 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.type = tarfile.REGTYPE
    return info


def load_actions(release: Path, split: str) -> Dict[str, Tuple[dict, dict]]:
    """after_key -> (action, effect), the transition that produced that state.

    Loaded once per split rather than per tar: re-reading a 760,000-row index
    for each of ~900 tars would cost more than the packing.
    """
    path = release / "index" / "transitions" / ("%s.parquet" % split)
    if not path.is_file():
        return {}
    table = pq.read_table(path, columns=[
        "after_key", "transition_id", "action_type", "action_target_uid",
        "action_target_role", "action_target_kind", "action_target_text",
        "action_target_app", "action_point_px", "action_point_norm_1000",
        "action_point_screentag_500", "action_target_bbox_px",
        "action_target_bbox_norm_1000", "effect", "transition_train_eligible",
    ]).to_pydict()
    out: Dict[str, Tuple[dict, dict]] = {}
    for index, after in enumerate(table["after_key"]):
        if not after:
            continue
        out[after] = (
            {
                "transition_id": table["transition_id"][index],
                "type": table["action_type"][index],
                "target_uid": table["action_target_uid"][index],
                "target_role": table["action_target_role"][index],
                "target_kind": table["action_target_kind"][index],
                "target_text": table["action_target_text"][index],
                "target_app": table["action_target_app"][index],
                "point_px": table["action_point_px"][index],
                "point_norm_1000": table["action_point_norm_1000"][index],
                "point_screentag_500": table["action_point_screentag_500"][index],
                "target_bbox_px": table["action_target_bbox_px"][index],
                "target_bbox_norm_1000": table["action_target_bbox_norm_1000"][index],
                "train_eligible": table["transition_train_eligible"][index],
            },
            table["effect"][index],
        )
    return out


def pack_one(
    corpus: Path,
    release: Path,
    tar_path: str,
    rows: List[Dict[str, Any]],
    actions: Dict[str, Tuple[dict, dict]],
) -> Dict[str, Any]:
    """Write, verify and publish one tar. Returns its completion marker."""
    target = release / tar_path
    marker = release / "_work" / "markers" / (tar_path.replace("/", "__") + ".json")
    partial = target.with_name(target.name + ".partial")
    target.parent.mkdir(parents=True, exist_ok=True)
    marker.parent.mkdir(parents=True, exist_ok=True)

    started = time.time()
    written: List[Dict[str, Any]] = []
    if partial.exists():
        partial.unlink()

    with tarfile.open(str(partial), "w", format=tarfile.PAX_FORMAT) as tar:
        for row in rows:
            key = row["observation_key"]
            base = corpus / row["source_path"]
            payload: Dict[str, bytes] = {}
            for kind, suffix in SOURCE_SUFFIXES.items():
                source = Path(str(base) + suffix)
                try:
                    payload[kind] = _read_bytes(source)
                except OSError as error:
                    raise PackError("%s: %s" % (source, error))

            meta_path = Path(str(base) + ".meta.json")
            try:
                meta = json.loads(_read_bytes(meta_path).decode("utf-8"))
            except (OSError, ValueError) as error:
                raise PackError("%s: %s" % (meta_path, error))

            action, effect = actions.get(key, (None, None))
            record = build_record(
                observation_key=key, scene_id=row["scene_id"],
                episode_id=row["episode_id"], step_index=row["step_index"],
                split=row["split"], group=row["group"], meta=meta,
                flags={
                    "publishable": row["publishable"],
                    "state_train_eligible": row["state_train_eligible"],
                    "near_duplicate": row["near_duplicate"],
                    "no_op_frame": row["no_op_frame"],
                    "missing_apps": row["missing_apps"],
                },
                action=action, effect=effect, dataset_version=DATASET_VERSION,
            )
            assert_sanitized(record)

            declared = (record.get("width"), record.get("height"))
            if declared != (row["width"], row["height"]):
                raise PackError("%s: viewport %s in meta.json, %s in the index"
                                % (key, declared, (row["width"], row["height"])))
            payload["record"] = json.dumps(record, ensure_ascii=False,
                                           sort_keys=True).encode("utf-8")

            # All four members of a sample consecutively: WebDataset groups by
            # the key it sees, and a split group silently becomes two samples.
            for kind in KIND_ORDER:
                name = member_name(key, kind)
                blob = payload[kind]
                tar.addfile(_tarinfo(name, len(blob)), _BytesReader(blob))
                written.append({"observation_key": key, "kind": kind,
                                "tar_member": name, "byte_size": len(blob),
                                "sha256": hashlib.sha256(blob).hexdigest()})

    offsets, digest, size = _verify(partial, written)
    for entry, offset in zip(written, offsets):
        entry["byte_offset"] = offset
        entry["tar_path"] = tar_path

    os.replace(str(partial), str(target))
    payload = {
        "tar_path": tar_path,
        "split": rows[0]["split"],
        "observations": len(rows),
        "members": len(written),
        "bytes": size,
        "sha256": digest,
        "seconds": round(time.time() - started, 1),
        "dataset_version": DATASET_VERSION,
        "member_index": written,
    }
    tmp = marker.with_name(marker.name + ".partial")
    tmp.write_text(json.dumps(payload))
    os.replace(str(tmp), str(marker))
    return payload


class _BytesReader(object):
    """tarfile wants a file object; the payload is already in memory."""

    def __init__(self, blob: bytes):
        self._blob = blob
        self._at = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self._blob) - self._at
        chunk = self._blob[self._at:self._at + size]
        self._at += len(chunk)
        return chunk


def _verify(path: Path, expected: List[Dict[str, Any]]) -> Tuple[List[int], str, int]:
    """Read the finished tar back. Offsets are only true once it is written."""
    offsets: List[int] = []
    with tarfile.open(str(path), "r:") as tar:
        members = tar.getmembers()
        if len(members) != len(expected):
            raise PackError("%s holds %d members, expected %d"
                            % (path.name, len(members), len(expected)))
        for member, entry in zip(members, expected):
            if member.name != entry["tar_member"]:
                raise PackError("%s: member %r where %r was expected"
                                % (path.name, member.name, entry["tar_member"]))
            if member.size != entry["byte_size"]:
                raise PackError("%s: %s is %d bytes, expected %d"
                                % (path.name, member.name, member.size, entry["byte_size"]))
            offsets.append(member.offset_data)

    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(8 << 20)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return offsets, digest.hexdigest(), size


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", required=True, type=Path)
    ap.add_argument("--release", required=True, type=Path)
    ap.add_argument("--limit", type=int, default=None,
                    help="pack only the first N planned tars of each split (the pilot)")
    ap.add_argument("--split", action="append", help="restrict to a split; repeatable")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--repack", action="store_true",
                    help="ignore completion markers and write every selected tar again")
    args = ap.parse_args()

    plan = pq.read_table(args.release / "_work" / "pack_plan.parquet").to_pydict()
    observations: Dict[str, Dict[str, Any]] = {}
    for split_path in sorted((args.release / "index" / "observations").glob("*.parquet")):
        table = pq.read_table(split_path, columns=[
            "observation_key", "split", "group", "step_index", "width", "height",
            "publishable", "state_train_eligible", "near_duplicate", "no_op_frame",
            "missing_apps"]).to_pydict()
        for index, key in enumerate(table["observation_key"]):
            observations[key] = {name: table[name][index] for name in table}

    by_tar: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for index, tar_path in enumerate(plan["tar_path"]):
        key = plan["observation_key"][index]
        row = dict(observations[key])
        row.update({"observation_key": key, "scene_id": plan["scene_id"][index],
                    "episode_id": plan["episode_id"][index],
                    "source_path": plan["source_path"][index]})
        by_tar[tar_path].append(row)

    wanted = sorted(by_tar)
    if args.split:
        chosen = set(args.split)
        wanted = [t for t in wanted if by_tar[t][0]["split"] in chosen]
    if args.limit:
        per_split: Dict[str, int] = defaultdict(int)
        limited = []
        for tar_path in wanted:
            split = by_tar[tar_path][0]["split"]
            if per_split[split] < args.limit:
                per_split[split] += 1
                limited.append(tar_path)
        wanted = limited

    markers = args.release / "_work" / "markers"
    markers.mkdir(parents=True, exist_ok=True)
    todo = []
    for tar_path in wanted:
        marker = markers / (tar_path.replace("/", "__") + ".json")
        if marker.is_file() and (args.release / tar_path).is_file() and not args.repack:
            continue
        todo.append(tar_path)
    print("%d planned tars selected, %d already complete, %d to pack"
          % (len(wanted), len(wanted) - len(todo), len(todo)))
    if not todo:
        return 0

    actions_cache: Dict[str, Dict[str, Tuple[dict, dict]]] = {}
    for split in sorted({by_tar[t][0]["split"] for t in todo}):
        actions_cache[split] = load_actions(args.release, split)
        print("  %s: %s transitions carry an action into a packed state"
              % (split, "{:,}".format(len(actions_cache[split]))))

    started = time.time()
    packed_bytes = 0
    failures: List[str] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(pack_one, args.corpus, args.release, tar_path,
                        by_tar[tar_path], actions_cache[by_tar[tar_path][0]["split"]]): tar_path
            for tar_path in todo
        }
        for done, future in enumerate(as_completed(futures), start=1):
            tar_path = futures[future]
            try:
                marker = future.result()
            except Exception as error:  # a failed tar must not stop the others
                failures.append("%s: %s" % (tar_path, error))
                print("  FAILED %s: %s" % (tar_path, error))
                continue
            packed_bytes += marker["bytes"]
            if done % 10 == 0 or done == len(todo):
                rate = packed_bytes / max(1e-9, time.time() - started)
                print("  %d/%d tars  %.1f GB  %.0f MB/s"
                      % (done, len(todo), packed_bytes / 1e9, rate / 1e6))

    print("\npacked %d tars, %.2f GB, %.0fs"
          % (len(todo) - len(failures), packed_bytes / 1e9, time.time() - started))
    if failures:
        print("%d failed:" % len(failures))
        for line in failures[:20]:
            print("  " + line)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
