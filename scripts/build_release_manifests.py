#!/usr/bin/env python
"""Scene, observation, transition and episode indexes for the release.

Reads the frozen corpus manifests and writes Parquet. Nothing here opens a
screenshot or an element list: the manifest already carries the per-observation
facts, and episode files are reached directly from the scene ids in it rather
than by walking eleven million files.

    build_release_manifests.py --corpus <corpus root> --out <staging dir>

Stages are separate and restartable; `--stage` runs one. Every count it reports
is derived, never assumed, and every excluded transition carries the reason it
was excluded so a difference in the totals can be accounted for.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from deskshot.release import splits as split_rules  # noqa: E402
from deskshot.release.keys import episode_id, observation_key  # noqa: E402
from deskshot.release.schema import viewport_of  # noqa: E402
from deskshot.release.transitions import build_transitions  # noqa: E402

DATASET_VERSION = "1.0.0"
CHUNK = 200_000

# Flag bits, packed one byte per step so 158k episodes cost megabytes and not
# a dict-of-dicts.
PRESENT, PUBLISHABLE, NEAR_DUPLICATE, NO_OP = 1, 2, 4, 8


def rows(path: Path) -> Iterator[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                yield json.loads(line)
            except ValueError:
                continue


# ------------------------------------------------------------------- scenes

def build_scenes(corpus: Path, out: Path, splits_name: str) -> Dict[str, Any]:
    started = time.time()
    frozen: Dict[str, str] = {}
    clashes = 0
    for record in rows(corpus / "plan" / splits_name):
        scene_id, split = record.get("scene_id"), record.get("split")
        if not scene_id or not split:
            continue
        if frozen.setdefault(scene_id, split) != split:
            clashes += 1
    if clashes:
        raise SystemExit("%d scenes carry two different splits in %s" % (clashes, splits_name))
    print("frozen splits: %s scenes from %s" % ("{:,}".format(len(frozen)), splits_name))

    scenes: Dict[str, Dict[str, Any]] = {}
    total = 0
    for record in rows(corpus / "plan" / "manifest.jsonl"):
        total += 1
        scene_id = record["scene_id"]
        scene = scenes.get(scene_id)
        if scene is None:
            scene = scenes[scene_id] = {
                "scene_id": scene_id, "apps": set(), "apps_any_frame": set(),
                "theme": record.get("theme"),
                "resolution": record.get("resolution"), "profile": record.get("profile"),
                "n_observations": 0, "n_publishable": 0, "n_state_train_eligible": 0,
                "groups": set(), "shards": set(),
            }
        scene["apps_any_frame"].update(record.get("apps") or [])
        scene["groups"].add(record.get("group"))
        scene["shards"].add(record.get("shard"))
        scene["n_observations"] += 1
        if record.get("train_eligible"):
            scene["n_state_train_eligible"] += 1
        if not record.get("publishable"):
            continue
        scene["n_publishable"] += 1
        # The union over every *publishable* frame.
        #
        # Union over every frame, because one capture of a scene can miss an
        # application another capture of it saw - that is how three held-out
        # applications leaked into train the first time this was built.
        #
        # Publishable only, because a frame the release drops cannot leak. Scene
        # c7b3c63caaea03b2 is the case that forced the distinction: it was
        # captured twice, xarchiver failed to launch in the copy v3 kept, and
        # launched in a second copy that is not publishable. Counting the
        # unpublished copy would have moved a legitimately clean train scene
        # into test_app over an application that never ships.
        scene["apps"].update(record.get("apps") or [])
        if scene["theme"] is None:
            scene["theme"] = record.get("theme")
        if scene["resolution"] is None:
            scene["resolution"] = record.get("resolution")
    print("manifest: %s observations over %s scenes  (%.0fs)"
          % ("{:,}".format(total), "{:,}".format(len(scenes)), time.time() - started))

    # A scene with nothing publishable ships nothing, so it is not in the
    # release and does not need - or deserve - a split.
    empty = [scene_id for scene_id, scene in scenes.items() if not scene["n_publishable"]]
    for scene_id in empty:
        del scenes[scene_id]
    print("scenes with no publishable observation: %s (not in the release)"
          % "{:,}".format(len(empty)))

    extended = Counter()
    for scene_id, scene in scenes.items():
        before = split_rules.assign
        scene["split"] = before(scene_id, frozen=frozen, apps=scene["apps"],
                                theme=scene["theme"], resolution=scene["resolution"])
        scene["split_source"] = "v3" if scene_id in frozen else "extended"
        if scene["split_source"] == "extended":
            extended[scene["split"]] += 1

    for scene_id, split in frozen.items():
        if scene_id in scenes and scenes[scene_id]["split"] != split:
            raise SystemExit("scene %s lost its frozen split" % scene_id)

    leaked = split_rules.leaks(scenes)
    if leaked:
        raise SystemExit("held-out attributes reached train: %s"
                         % {k: len(v) for k, v in leaked.items()})

    table = pa.table({
        "scene_id": pa.array([s["scene_id"] for s in scenes.values()], pa.string()),
        "split": pa.array([s["split"] for s in scenes.values()], pa.string()),
        "split_source": pa.array([s["split_source"] for s in scenes.values()], pa.string()),
        "apps": pa.array([sorted(s["apps"]) for s in scenes.values()], pa.list_(pa.string())),
        "apps_any_frame": pa.array([sorted(s["apps_any_frame"]) for s in scenes.values()],
                                   pa.list_(pa.string())),
        "theme": pa.array([s["theme"] for s in scenes.values()], pa.string()),
        "resolution": pa.array([s["resolution"] for s in scenes.values()], pa.string()),
        "profile": pa.array([s["profile"] for s in scenes.values()], pa.string()),
        "groups": pa.array([sorted(x for x in s["groups"] if x) for s in scenes.values()],
                           pa.list_(pa.string())),
        "shards": pa.array([sorted(x for x in s["shards"] if x) for s in scenes.values()],
                           pa.list_(pa.string())),
        "n_observations": pa.array([s["n_observations"] for s in scenes.values()], pa.int32()),
        "n_publishable": pa.array([s["n_publishable"] for s in scenes.values()], pa.int32()),
        "n_state_train_eligible": pa.array(
            [s["n_state_train_eligible"] for s in scenes.values()], pa.int32()),
    })
    (out / "index").mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out / "index" / "scenes.parquet", compression="zstd")

    summary = {
        "scenes": len(scenes),
        "scenes_without_publishable_observations": len(empty),
        "observations_in_manifest": total,
        "frozen_scenes": len(frozen),
        "extended_scenes": dict(extended),
        "by_split": dict(Counter(s["split"] for s in scenes.values())),
        "leakage": {},
    }
    print("\nscenes by split")
    for split in split_rules.ALL_SPLITS:
        print("  %-16s %8s" % (split, "{:,}".format(summary["by_split"].get(split, 0))))
    print("extended (absent from v3): %s" % dict(extended))
    print("no held-out attribute reached train")
    return summary


# -------------------------------------------------------------- observations

OBSERVATION_FIELDS = [
    ("dataset_version", pa.string()), ("observation_key", pa.string()),
    ("scene_id", pa.string()), ("episode_id", pa.string()),
    ("step_index", pa.int32()), ("split", pa.string()), ("group", pa.string()),
    ("shard", pa.string()), ("source_path", pa.string()),
    ("width", pa.int32()), ("height", pa.int32()),
    ("apps", pa.list_(pa.string())), ("theme", pa.string()),
    ("resolution", pa.string()), ("profile", pa.string()),
    ("n_windows", pa.int32()), ("n_elements", pa.int32()),
    ("occluded_ratio", pa.float32()),
    ("publishable", pa.bool_()), ("state_train_eligible", pa.bool_()),
    ("near_duplicate", pa.bool_()), ("no_op_frame", pa.bool_()),
    ("missing_apps", pa.list_(pa.string())), ("seed", pa.int64()),
]
OBSERVATION_SCHEMA = pa.schema([pa.field(name, kind) for name, kind in OBSERVATION_FIELDS])


class SplitWriters(object):
    """One Parquet writer per split, opened lazily, closed together."""

    def __init__(self, directory: Path, schema: pa.Schema):
        self.directory = directory
        self.schema = schema
        self.writers: Dict[str, pq.ParquetWriter] = {}
        self.counts: Counter = Counter()
        directory.mkdir(parents=True, exist_ok=True)

    def write(self, split: str, batch: List[Dict[str, Any]]) -> None:
        if not batch:
            return
        writer = self.writers.get(split)
        if writer is None:
            writer = self.writers[split] = pq.ParquetWriter(
                str(self.directory / ("%s.parquet" % split)), self.schema, compression="zstd")
        columns = {field.name: [row.get(field.name) for row in batch] for field in self.schema}
        writer.write_table(pa.table(columns, schema=self.schema))
        self.counts[split] += len(batch)

    def close(self) -> None:
        for writer in self.writers.values():
            writer.close()


def build_observations(corpus: Path, out: Path) -> Dict[str, Any]:
    started = time.time()
    scenes = pq.read_table(out / "index" / "scenes.parquet",
                           columns=["scene_id", "split"]).to_pydict()
    split_of = dict(zip(scenes["scene_id"], scenes["split"]))

    writers = SplitWriters(out / "index" / "observations", OBSERVATION_SCHEMA)
    pending: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    episode_flags: Dict[str, bytearray] = {}
    seen_keys = set()
    duplicates = 0
    kept = skipped = 0

    for record in rows(corpus / "plan" / "manifest.jsonl"):
        if not record.get("publishable"):
            skipped += 1
            continue
        shard = record["shard"]
        stem = record["path"].rsplit("/", 1)[-1]
        key = observation_key(shard, stem)
        if key in seen_keys:
            duplicates += 1
            continue
        seen_keys.add(key)

        group = record.get("group")
        scene_id = record["scene_id"]
        split = split_of.get(scene_id)
        if split is None:
            raise SystemExit("scene %s has no split" % scene_id)
        episode = episode_id(shard, group, scene_id) if group == "ep" else None
        viewport = viewport_of(record.get("resolution")) or (0, 0)
        step = record.get("step")

        if episode is not None and isinstance(step, int):
            packed = episode_flags.setdefault(episode, bytearray())
            while len(packed) <= step:
                packed.append(0)
            packed[step] = (PRESENT
                            | (PUBLISHABLE if record.get("publishable") else 0)
                            | (NEAR_DUPLICATE if record.get("near_duplicate") else 0)
                            | (NO_OP if record.get("no_op_frame") else 0))

        pending[split].append({
            "dataset_version": DATASET_VERSION,
            "observation_key": key,
            "scene_id": scene_id,
            "episode_id": episode,
            "step_index": step,
            "split": split,
            "group": group,
            "shard": shard,
            "source_path": "shards/%s/%s" % (shard, record["path"]),
            "width": viewport[0], "height": viewport[1],
            "apps": sorted(record.get("apps") or []),
            "theme": record.get("theme"),
            "resolution": record.get("resolution"),
            "profile": record.get("profile"),
            "n_windows": record.get("n_windows"),
            "n_elements": record.get("n_elements"),
            "occluded_ratio": record.get("occluded_ratio"),
            "publishable": True,
            "state_train_eligible": bool(record.get("train_eligible")),
            "near_duplicate": bool(record.get("near_duplicate")),
            "no_op_frame": bool(record.get("no_op_frame")),
            "missing_apps": sorted(record.get("missing_apps") or []),
            "seed": record.get("seed"),
        })
        kept += 1
        if len(pending[split]) >= CHUNK:
            writers.write(split, pending[split])
            pending[split] = []

    for split, batch in pending.items():
        writers.write(split, batch)
    writers.close()

    if duplicates:
        raise SystemExit("%d duplicate observation keys" % duplicates)

    with (out / "_work" / "episode_flags.json").open("w") as handle:
        json.dump({key: list(value) for key, value in episode_flags.items()}, handle)

    print("\nobservations kept %s   not publishable %s   (%.0fs)"
          % ("{:,}".format(kept), "{:,}".format(skipped), time.time() - started))
    for split in split_rules.ALL_SPLITS:
        print("  %-16s %8s" % (split, "{:,}".format(writers.counts.get(split, 0))))
    return {"published_observations": kept, "not_publishable": skipped,
            "by_split": dict(writers.counts)}


# ------------------------------------------------- episodes and transitions

TRANSITION_SCHEMA = pa.schema([
    pa.field("transition_id", pa.string()), pa.field("episode_id", pa.string()),
    pa.field("action_index", pa.int32()), pa.field("split", pa.string()),
    pa.field("before_key", pa.string()), pa.field("after_key", pa.string()),
    pa.field("transition_train_eligible", pa.bool_()),
    pa.field("action_type", pa.string()), pa.field("action_target_uid", pa.string()),
    pa.field("action_target_role", pa.string()), pa.field("action_target_kind", pa.string()),
    pa.field("action_target_text", pa.string()), pa.field("action_target_app", pa.string()),
    pa.field("action_point_px", pa.list_(pa.int32())),
    pa.field("action_point_norm_1000", pa.list_(pa.int32())),
    pa.field("action_point_screentag_500", pa.list_(pa.int32())),
    pa.field("action_target_bbox_px", pa.list_(pa.int32())),
    pa.field("action_target_bbox_norm_1000", pa.list_(pa.int32())),
    pa.field("effect", pa.struct([
        pa.field("changed", pa.bool_()), pa.field("magnitude", pa.float32()),
        pa.field("appeared", pa.int32()), pa.field("disappeared", pa.int32()),
        pa.field("moved", pa.int32()), pa.field("text_changed", pa.int32()),
        pa.field("state_changed", pa.int32()), pa.field("newly_occluded", pa.int32()),
        pa.field("revealed", pa.int32()), pa.field("semantic_changes", pa.int32()),
        pa.field("persisted", pa.int32()),
    ])),
    pa.field("exclusion_reasons", pa.list_(pa.string())),
])

EPISODE_SCHEMA = pa.schema([
    pa.field("episode_id", pa.string()), pa.field("scene_id", pa.string()),
    pa.field("split", pa.string()), pa.field("shard", pa.string()),
    pa.field("observation_keys", pa.list_(pa.string())),
    pa.field("transition_ids", pa.list_(pa.string())),
    pa.field("n_observations", pa.int32()), pa.field("n_transitions", pa.int32()),
    pa.field("n_transitions_eligible", pa.int32()),
    pa.field("episode_complete", pa.bool_()),
    pa.field("episode_status", pa.string()),
    pa.field("apps", pa.list_(pa.string())), pa.field("theme", pa.string()),
    pa.field("resolution", pa.string()),
])


def _episode_rows(job):
    """One episode.json, read and turned into rows. Runs on a worker thread."""
    corpus, shard, scene_id, split, resolution, theme, apps, flags = job
    key = episode_id(shard, "ep", scene_id)
    path = corpus / "shards" / shard / "ep" / scene_id / "episode.json"
    def bare(status):
        """An episode whose action log did not survive still has observations.

        994 runs died before writing `episode.json` and 6 wrote a truncated
        one, across 183 shards. Their captures are fine and ship; they just
        contribute no transitions. Dropping the episode row entirely left those
        observations pointing at an `episode_id` with no row to resolve to, so
        the row is emitted with the reason instead.
        """
        observations = [observation_key(shard, "scene-%s-step%02d" % (scene_id, i))
                        for i, packed in enumerate(flags) if packed & PRESENT]
        return key, {
            "episode_id": key, "scene_id": scene_id, "split": split, "shard": shard,
            "observation_keys": observations, "transition_ids": [],
            "n_observations": len(observations), "n_transitions": 0,
            "n_transitions_eligible": 0, "episode_complete": False,
            "episode_status": status,
            "apps": apps, "theme": theme, "resolution": resolution,
        }, [], {status: 1}

    if not path.is_file():
        return bare("no_episode_json")
    try:
        with path.open("r", encoding="utf-8") as handle:
            episode = json.load(handle)
    except OSError:
        return bare("episode_json_unreadable")
    except ValueError:
        return bare("episode_json_truncated")

    width, height = viewport_of(resolution) or (0, 0)

    def key_of(stem: Optional[str]) -> Optional[str]:
        if not stem:
            return None
        index = _step_of(stem)
        if index is None or index >= len(flags) or not (flags[index] & PRESENT):
            return None
        return observation_key(shard, stem)

    def flags_of(observation: Optional[str]) -> Optional[Dict[str, Any]]:
        if not observation:
            return None
        index = _step_of(observation)
        if index is None or index >= len(flags):
            return None
        packed = flags[index]
        return {"publishable": bool(packed & PUBLISHABLE),
                "near_duplicate": bool(packed & NEAR_DUPLICATE),
                "no_op_frame": bool(packed & NO_OP)}

    transitions, reasons = build_transitions(
        episode, episode_key=key, split=split, width=width, height=height,
        observation_key_of=key_of, observation_flags=flags_of,
    )
    observations = [observation_key(shard, "scene-%s-step%02d" % (scene_id, i))
                    for i, packed in enumerate(flags) if packed & PRESENT]
    row = {
        "episode_id": key, "scene_id": scene_id, "split": split, "shard": shard,
        "observation_keys": observations,
        "transition_ids": [t["transition_id"] for t in transitions],
        "n_observations": len(observations),
        "n_transitions": len(transitions),
        "n_transitions_eligible": sum(1 for t in transitions if t["transition_train_eligible"]),
        "episode_complete": len(observations) == len(flags),
        "episode_status": "complete",
        "apps": apps, "theme": theme, "resolution": resolution,
    }
    return key, row, transitions, reasons


def _step_of(stem: str) -> Optional[int]:
    tail = stem.rsplit("-step", 1)
    if len(tail) != 2:
        return None
    try:
        return int(tail[1])
    except ValueError:
        return None


def build_episodes(corpus: Path, out: Path, workers: int) -> Dict[str, Any]:
    started = time.time()
    with (out / "_work" / "episode_flags.json").open() as handle:
        episode_flags = {key: bytes(value) for key, value in json.load(handle).items()}

    scenes = pq.read_table(out / "index" / "scenes.parquet").to_pydict()
    facts = {scene: (split, theme, resolution, apps) for scene, split, theme, resolution, apps
             in zip(scenes["scene_id"], scenes["split"], scenes["theme"],
                    scenes["resolution"], scenes["apps"])}

    jobs = []
    for key, flags in episode_flags.items():
        shard, _group, scene_id = key.split("__", 2)
        split, theme, resolution, apps = facts[scene_id]
        jobs.append((corpus, shard, scene_id, split, resolution, theme, list(apps), flags))
    print("\nreading %s episode files with %d workers"
          % ("{:,}".format(len(jobs)), workers))

    episodes = SplitWriters(out / "index" / "episodes", EPISODE_SCHEMA)
    transitions = SplitWriters(out / "index" / "transitions", TRANSITION_SCHEMA)
    pending_e: Dict[str, List] = defaultdict(list)
    pending_t: Dict[str, List] = defaultdict(list)
    reasons: Counter = Counter()
    eligible = 0
    total = 0
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for key, row, rows_out, why in pool.map(_episode_rows, jobs, chunksize=64):
            done += 1
            reasons.update(why)
            if row is None:
                continue
            pending_e[row["split"]].append(row)
            for transition in rows_out:
                pending_t[transition["split"]].append(transition)
                total += 1
                if transition["transition_train_eligible"]:
                    eligible += 1
            for split in list(pending_t):
                if len(pending_t[split]) >= CHUNK:
                    transitions.write(split, pending_t[split])
                    pending_t[split] = []
            for split in list(pending_e):
                if len(pending_e[split]) >= CHUNK:
                    episodes.write(split, pending_e[split])
                    pending_e[split] = []
            if done % 25000 == 0:
                print("  %s episodes  %.0fs" % ("{:,}".format(done), time.time() - started))

    for split, batch in pending_e.items():
        episodes.write(split, batch)
    for split, batch in pending_t.items():
        transitions.write(split, batch)
    episodes.close()
    transitions.close()

    print("\nepisodes %s   transitions %s   eligible %s   (%.0fs)"
          % ("{:,}".format(sum(episodes.counts.values())), "{:,}".format(total),
             "{:,}".format(eligible), time.time() - started))
    for split in split_rules.ALL_SPLITS:
        print("  %-16s episodes %7s   transitions %8s"
              % (split, "{:,}".format(episodes.counts.get(split, 0)),
                 "{:,}".format(transitions.counts.get(split, 0))))
    if reasons:
        print("\nwhy a transition is not training-eligible")
        for reason, count in reasons.most_common():
            print("  %-34s %8s" % (reason, "{:,}".format(count)))
    lost = reasons.get("no_episode_json", 0) + reasons.get("episode_json_truncated", 0) \
        + reasons.get("episode_json_unreadable", 0)
    return {
        "episodes": dict(episodes.counts),
        "transitions": dict(transitions.counts),
        "transitions_total": total,
        "transitions_eligible": eligible,
        "episodes_without_action_log": lost,
        "exclusion_reasons": dict(reasons),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--splits", default="splits_v3.jsonl")
    ap.add_argument("--stage", action="append",
                    choices=["scenes", "observations", "episodes"],
                    help="repeatable; all three in order by default")
    ap.add_argument("--workers", type=int, default=min(32, (os.cpu_count() or 8) * 2))
    args = ap.parse_args()

    stages = args.stage or ["scenes", "observations", "episodes"]
    (args.out / "_work").mkdir(parents=True, exist_ok=True)
    summary_path = args.out / "_work" / "manifest_summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.is_file() else {}

    if "scenes" in stages:
        summary["scenes"] = build_scenes(args.corpus, args.out, args.splits)
    if "observations" in stages:
        summary["observations"] = build_observations(args.corpus, args.out)
    if "episodes" in stages:
        summary["episodes"] = build_episodes(args.corpus, args.out, args.workers)

    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print("\nwrote %s" % summary_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
