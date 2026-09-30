#!/usr/bin/env python
"""Everything that can only be written once the tars exist.

Byte offsets, checksums and the tar each observation ended up in are all facts
about the packed payload, so they are collected from the completion markers the
packer wrote while it verified each finished tar - the one moment they were
known to be true - rather than recomputed from a plan that might not match.

    finalize_hf_release.py --release <staging> [--preview-per-cell 3]

Safe to re-run: it rewrites its outputs from whatever tars are complete now,
which is what makes it usable against a pilot and again against the full pack.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from deskshot.release.keys import MEMBER_SUFFIXES  # noqa: E402

PREVIEW_LIMIT_BYTES = int(3.5e9)

#: The Dataset Viewer thumbnails every row it shows. At full resolution a
#: preview row is ~850KB and `first-rows` times out; the canonical images are in
#: `data/` and this file exists to be browsed. Downscaling is safe for what the
#: preview carries: ScreenTag coordinates are on a 0-500 grid normalized to the
#: viewport, so they still describe a resized image, and the original
#: `width`/`height` are columns in the row.
PREVIEW_MAX_PX = 1024


def load_markers(release: Path) -> List[Dict[str, Any]]:
    markers = sorted((release / "_work" / "markers").glob("*.json"))
    out = []
    for path in markers:
        try:
            marker = json.loads(path.read_text())
        except ValueError:
            continue
        if (release / marker["tar_path"]).is_file():
            out.append(marker)
    return out


def write_member_index(release: Path, markers: List[Dict[str, Any]]) -> int:
    rows: Dict[str, List[Any]] = defaultdict(list)
    for marker in markers:
        for entry in marker["member_index"]:
            rows["observation_key"].append(entry["observation_key"])
            rows["kind"].append(entry["kind"])
            rows["tar_path"].append(marker["tar_path"])
            rows["tar_member"].append(entry["tar_member"])
            rows["byte_offset"].append(entry["byte_offset"])
            rows["byte_size"].append(entry["byte_size"])
            rows["sha256"].append(entry["sha256"])
    table = pa.table({
        "observation_key": pa.array(rows["observation_key"], pa.string()),
        "kind": pa.array(rows["kind"], pa.string()),
        "tar_path": pa.array(rows["tar_path"], pa.string()),
        "tar_member": pa.array(rows["tar_member"], pa.string()),
        "byte_offset": pa.array(rows["byte_offset"], pa.int64()),
        "byte_size": pa.array(rows["byte_size"], pa.int64()),
        "sha256": pa.array(rows["sha256"], pa.string()),
    })
    (release / "index").mkdir(parents=True, exist_ok=True)
    pq.write_table(table, release / "index" / "shard_members.parquet", compression="zstd")
    return table.num_rows


def write_checksums(release: Path, markers: List[Dict[str, Any]]) -> int:
    (release / "checksums").mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({
        "tar_path": [m["tar_path"] for m in markers],
        "split": [m["split"] for m in markers],
        "observations": [m["observations"] for m in markers],
        "members": [m["members"] for m in markers],
        "bytes": [m["bytes"] for m in markers],
        "sha256": [m["sha256"] for m in markers],
    }), release / "checksums" / "shard_stats.parquet", compression="zstd")

    lines = ["%s  %s" % (m["sha256"], m["tar_path"]) for m in sorted(
        markers, key=lambda m: m["tar_path"])]
    # Everything that is not a tar, hashed here so the whole upload is covered.
    for path in sorted(release.rglob("*")):
        if not path.is_file() or path.suffix == ".tar":
            continue
        relative = path.relative_to(release)
        # `_work` is scratch and `.cache` is the uploader's own resume state -
        # neither is published, and hashing 1,186 state files made this file
        # three times longer than the release it describes.
        if str(relative).startswith(("_work", ".cache")) \
                or relative.name == "sha256sums.txt":
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(8 << 20), b""):
                digest.update(chunk)
        lines.append("%s  %s" % (digest.hexdigest(), relative))
    (release / "checksums" / "sha256sums.txt").write_text("\n".join(lines) + "\n")
    return len(lines)


def join_tar_paths(release: Path, markers: List[Dict[str, Any]]) -> Dict[str, int]:
    """Give every observation row the tar and member names it was packed as."""
    where: Dict[str, str] = {}
    for marker in markers:
        for entry in marker["member_index"]:
            where[entry["observation_key"]] = marker["tar_path"]

    counts: Dict[str, int] = {}
    for path in sorted((release / "index" / "observations").glob("*.parquet")):
        table = pq.read_table(path)
        keys = table["observation_key"].to_pylist()
        tar_paths = [where.get(key) for key in keys]
        columns = {name: table[name] for name in table.column_names
                   if name not in ("tar_path",) + tuple(
                       "%s_member" % kind for kind in MEMBER_SUFFIXES)}
        columns["tar_path"] = pa.array(tar_paths, pa.string())
        for kind, suffix in MEMBER_SUFFIXES.items():
            columns["%s_member" % kind] = pa.array(
                [key + suffix if tar else None for key, tar in zip(keys, tar_paths)],
                pa.string())
        pq.write_table(pa.table(columns), path, compression="zstd")
        counts[path.stem] = sum(1 for tar in tar_paths if tar)
    return counts


def _downscale(blob: bytes, max_px: int) -> bytes:
    """Fit the long edge to `max_px`, keeping PNG. Returns the original if smaller."""
    import io as _io

    from PIL import Image
    with Image.open(_io.BytesIO(blob)) as image:
        if max(image.size) <= max_px:
            return blob
        scale = max_px / float(max(image.size))
        resized = image.convert("RGB").resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.LANCZOS)
        buffer = _io.BytesIO()
        # `optimize=True` costs seconds per image and saves a few percent
        # on an already-resized screenshot.
        resized.save(buffer, format="PNG", compress_level=6)
    return buffer.getvalue()


def build_preview(release: Path, per_cell: int, seed: int,
                  max_px: int = PREVIEW_MAX_PX) -> Dict[str, Any]:
    """A stratified sample with images inline, for the Hub's Dataset Viewer.

    Stratified over the axes a reader would want to see vary - split, theme,
    resolution, density, occlusion, static against episode, changed against
    no-op, common against rare application - so the viewer's first page is not
    eight hundred screenshots of the same desktop.
    """
    rng = random.Random(seed)
    observations = {}
    for path in sorted((release / "index" / "observations").glob("*.parquet")):
        observations[path.stem] = pq.read_table(path).to_pydict()

    rare = {"gnome-system-monitor", "pluma", "xarchiver", "eog", "transmission-gtk"}
    cells: Dict[tuple, List[tuple]] = defaultdict(list)
    for split, columns in observations.items():
        for index, tar in enumerate(columns.get("tar_path") or []):
            if not tar:
                continue
            occ = columns["occluded_ratio"][index] or 0.0
            elements = columns["n_elements"][index] or 0
            apps = columns["apps"][index] or []
            cell = (
                split,
                columns["theme"][index],
                columns["resolution"][index],
                "dense" if elements >= 200 else ("sparse" if elements < 60 else "medium"),
                "occ_high" if occ >= 0.5 else ("occ_low" if occ < 0.15 else "occ_mid"),
                columns["group"][index],
                "noop" if columns["no_op_frame"][index] else "changed",
                "rare_app" if set(apps) & rare else "common_app",
            )
            cells[cell].append((split, index))

    chosen: List[tuple] = []
    for cell in sorted(cells):
        members = cells[cell]
        rng.shuffle(members)
        chosen.extend(members[:per_cell])
    rng.shuffle(chosen)
    print("preview: %s strata, %s candidate rows"
          % ("{:,}".format(len(cells)), "{:,}".format(len(chosen))))

    wanted: Dict[str, List[tuple]] = defaultdict(list)
    for split, index in chosen:
        wanted[observations[split]["tar_path"][index]].append((split, index))

    # Seek to each member instead of walking the tar. Iterating every member of
    # 1,151 shards to find a handful of keys reads 1.1 TB of headers; the
    # offsets are already in the member index and the shards are uncompressed.
    ranges: Dict[tuple, tuple] = {}
    members = pq.read_table(release / "index" / "shard_members.parquet",
                            columns=["observation_key", "kind", "tar_path",
                                     "byte_offset", "byte_size"]).to_pydict()
    for position, kind in enumerate(members["kind"]):
        if kind in ("image", "screentag"):
            ranges[(members["observation_key"][position], kind)] = (
                members["tar_path"][position], members["byte_offset"][position],
                members["byte_size"][position])

    rows: Dict[str, List[Any]] = defaultdict(list)
    total_bytes = 0
    started = time.time()
    for tar_path in sorted(wanted):
        keys = {observations[s]["observation_key"][i]: (s, i) for s, i in wanted[tar_path]}
        with (release / tar_path).open("rb") as handle:
            for key in list(keys):
                blobs = {}
                for kind in ("image", "screentag"):
                    entry = ranges.get((key, kind))
                    if entry is None:
                        continue
                    _, offset, size = entry
                    handle.seek(offset)
                    blobs[kind] = handle.read(size)
                if len(blobs) != 2:
                    continue
                image = _downscale(blobs["image"], max_px) if max_px else blobs["image"]
                if total_bytes + len(image) > PREVIEW_LIMIT_BYTES:
                    continue
                total_bytes += len(image)
                keys[key] = keys[key] + (image, blobs["screentag"].decode("utf-8"))
        for key, value in keys.items():
            if len(value) != 4:
                continue
            split, index, image, screentag = value
            columns = observations[split]
            rows["observation_key"].append(key)
            rows["image"].append({"bytes": image, "path": key + ".png"})
            rows["screentag"].append(screentag)
            for name in ("split", "scene_id", "episode_id", "step_index", "group",
                         "width", "height", "apps", "theme", "resolution",
                         "n_windows", "n_elements", "occluded_ratio",
                         "state_train_eligible", "no_op_frame", "tar_path"):
                rows[name].append(columns[name][index])

    schema = pa.schema(
        [pa.field("observation_key", pa.string()),
         pa.field("image", pa.struct([pa.field("bytes", pa.binary()),
                                      pa.field("path", pa.string())])),
         pa.field("screentag", pa.string()),
         pa.field("split", pa.string()), pa.field("scene_id", pa.string()),
         pa.field("episode_id", pa.string()), pa.field("step_index", pa.int32()),
         pa.field("group", pa.string()), pa.field("width", pa.int32()),
         pa.field("height", pa.int32()), pa.field("apps", pa.list_(pa.string())),
         pa.field("theme", pa.string()), pa.field("resolution", pa.string()),
         pa.field("n_windows", pa.int32()), pa.field("n_elements", pa.int32()),
         pa.field("occluded_ratio", pa.float32()),
         pa.field("state_train_eligible", pa.bool_()),
         pa.field("no_op_frame", pa.bool_()), pa.field("tar_path", pa.string())],
        metadata={b"huggingface": json.dumps(
            {"info": {"features": {
                "image": {"_type": "Image"},
                "observation_key": {"dtype": "string", "_type": "Value"},
            }}}).encode("utf-8")})
    demo = release / "demo"
    demo.mkdir(parents=True, exist_ok=True)
    for stale in demo.glob("preview*.parquet"):
        stale.unlink()
    table = pa.table({name: rows[name] for name in schema.names}, schema=schema)
    parts = write_preview_shards(table, demo)
    size = sum(part.stat().st_size for part in parts)
    print("preview: %s rows in %d shards, %.2f GB, %.0fs"
          % ("{:,}".format(table.num_rows), len(parts), size / 1e9, time.time() - started))
    return {"rows": table.num_rows, "bytes": size, "shards": len(parts),
            "strata": len(cells)}


#: The Hub's viewer refuses to scan a single large Parquet file - a 1.72 GB
#: preview came back "Scan size limit exceeded: attempted to read 1720367642
#: bytes". Shards of a few hundred megabytes are the convention and the viewer
#: reads them happily.
PREVIEW_SHARD_BYTES = int(300e6)


def write_preview_shards(table: pa.Table, demo: Path) -> List[Path]:
    """Split the preview into viewer-sized parts, by accumulated row bytes."""
    per_row = max(1, table.nbytes // max(1, table.num_rows))
    rows_per_shard = max(1, PREVIEW_SHARD_BYTES // per_row)
    count = max(1, (table.num_rows + rows_per_shard - 1) // rows_per_shard)
    parts: List[Path] = []
    for index in range(count):
        chunk = table.slice(index * rows_per_shard, rows_per_shard)
        if chunk.num_rows == 0:
            continue
        path = demo / ("preview-%05d-of-%05d.parquet" % (index, count))
        pq.write_table(chunk, path, compression="zstd")
        parts.append(path)
    return parts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--release", required=True, type=Path)
    ap.add_argument("--preview-per-cell", type=int, default=1)
    ap.add_argument("--seed", type=int, default=31)
    ap.add_argument("--preview-max-px", type=int, default=PREVIEW_MAX_PX,
                    help="fit preview images to this long edge; 0 keeps full size")
    ap.add_argument("--skip-preview", action="store_true")
    args = ap.parse_args()

    markers = load_markers(args.release)
    print("%s completed tars" % "{:,}".format(len(markers)))
    if not markers:
        raise SystemExit("nothing packed yet")

    members = write_member_index(args.release, markers)
    print("index/shard_members.parquet: %s members" % "{:,}".format(members))

    joined = join_tar_paths(args.release, markers)
    print("observations joined to their tar: %s"
          % {k: "{:,}".format(v) for k, v in sorted(joined.items())})

    summary = {"tars": len(markers), "members": members, "joined": joined}
    if not args.skip_preview:
        summary["preview"] = build_preview(args.release, args.preview_per_cell,
                                           args.seed, args.preview_max_px)

    lines = write_checksums(args.release, markers)
    print("checksums/sha256sums.txt: %s lines" % "{:,}".format(lines))

    (args.release / "_work" / "finalize_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
