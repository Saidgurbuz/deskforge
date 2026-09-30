"""The conversion rules that are easy to get backwards.

Every test here is one of the checks the release procedure calls for, and most
of them exist because the mistake they catch would be invisible downstream: an
action paired with the screen it was not taken on still trains, it just trains
the wrong thing.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from PIL import Image

from deskshot.release import splits as split_rules
from deskshot.release.keys import (
    episode_id, member_name, observation_key, transition_id,
)
from deskshot.release.schema import assert_sanitized, build_record, viewport_of
from deskshot.release.transitions import (
    NORM_GRID, SCREENTAG_GRID, box_in_viewport, box_px, build_transitions,
    denormalize_point, normalize_box, normalize_point, point_in_viewport,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------- keys

def test_a_key_is_shard_qualified() -> None:
    """152 stems and 128 scene ids repeat across shards; bare ones collide."""
    a = observation_key("shard-0004", "scene-2d392a5f0d35fb1e-step00")
    b = observation_key("shard-0112", "scene-2d392a5f0d35fb1e-step00")
    assert a != b
    assert episode_id("shard-0004", "ep", "x") != episode_id("shard-0112", "ep", "x")


def test_a_key_carries_no_dot() -> None:
    """WebDataset takes the key to be everything before the first dot."""
    key = observation_key("shard-0000", "scene-abc123-step07")
    assert "." not in key
    assert member_name(key, "image") == key + ".png"
    assert member_name(key, "leaf").split(".", 1)[0] == key


# ------------------------------------------------------------------ splits

def test_an_extended_scene_follows_the_held_out_priority() -> None:
    held_app = sorted(split_rules.HELDOUT_APPS)[0]
    assert split_rules.assign("new", frozen={}, apps=[held_app],
                              theme=split_rules.HELDOUT_THEME,
                              resolution=split_rules.HELDOUT_RESOLUTION) == "test_app"
    assert split_rules.assign("new", frozen={}, apps=["vscode"],
                              theme=split_rules.HELDOUT_THEME,
                              resolution=split_rules.HELDOUT_RESOLUTION) == "test_theme"
    assert split_rules.assign("new", frozen={}, apps=["vscode"], theme="ubuntu_like",
                              resolution=split_rules.HELDOUT_RESOLUTION) == "test_resolution"
    assert split_rules.assign("new", frozen={}, apps=["vscode"], theme="ubuntu_like",
                              resolution="fhd_1920x1080") == "train"


def test_a_frozen_assignment_always_wins() -> None:
    """v3 is finalized; a scene it placed is never moved by the extension rules."""
    held_app = sorted(split_rules.HELDOUT_APPS)[0]
    assert split_rules.assign("s", frozen={"s": "val"}, apps=[held_app],
                              theme=split_rules.HELDOUT_THEME,
                              resolution=split_rules.HELDOUT_RESOLUTION) == "val"


def test_val_and_test_id_are_never_reached_by_extension() -> None:
    for split in split_rules.FROZEN_SPLITS:
        assert split not in {
            split_rules.assign("s%d" % i, frozen={}, apps=["vscode"],
                               theme="ubuntu_like", resolution="fhd_1920x1080")
            for i in range(20)
        }


def test_leakage_is_detected_over_the_union_of_frames() -> None:
    held_app = sorted(split_rules.HELDOUT_APPS)[0]
    clean = {"a": {"split": "train", "apps": {"vscode"}, "theme": "t", "resolution": "r"}}
    assert split_rules.leaks(clean) == {}
    dirty = {"a": {"split": "train", "apps": {"vscode", held_app},
                   "theme": "t", "resolution": "r"}}
    assert "app:" + held_app in split_rules.leaks(dirty)


# ------------------------------------------------------------- coordinates

@pytest.mark.parametrize("width,height", [(1366, 768), (1920, 1080), (3840, 2160)])
def test_a_point_round_trips_within_one_grid_cell(width: int, height: int) -> None:
    for point in ([0, 0], [width - 1, height - 1], [width // 3, height // 7]):
        norm = normalize_point(point, width, height, NORM_GRID)
        back = denormalize_point(norm, width, height, NORM_GRID)
        assert abs(back[0] - point[0]) <= width / NORM_GRID + 1
        assert abs(back[1] - point[1]) <= height / NORM_GRID + 1


def test_the_screentag_grid_is_the_annotation_grid() -> None:
    assert SCREENTAG_GRID == 500
    assert normalize_point([960, 540], 1920, 1080, SCREENTAG_GRID) == [250, 250]


def test_a_box_normalizes_as_x0y0x1y1() -> None:
    box = normalize_box({"x": 0, "y": 0, "w": 960, "h": 540}, 1920, 1080, NORM_GRID)
    assert box == [0, 0, 500, 500]
    assert box_px({"x": 10, "y": 20, "w": 30, "h": 40}) == [10, 20, 40, 60]


def test_a_point_outside_the_viewport_is_rejected() -> None:
    assert point_in_viewport([0, 0], 100, 100)
    assert not point_in_viewport([100, 50], 100, 100)
    assert not point_in_viewport([-1, 5], 100, 100)
    assert not point_in_viewport(None, 100, 100)
    assert not box_in_viewport([0, 0, 101, 10], 100, 100)


def test_a_resolution_preset_names_its_own_viewport() -> None:
    assert viewport_of("uhd_3840x2160") == (3840, 2160)
    assert viewport_of("wxga_1366x768") == (1366, 768)
    assert viewport_of("nonsense") is None


# ------------------------------------------------------------- transitions

def _episode(steps):
    return {"scene_id": "abc", "steps": steps}


def _step(index, *, uid="u1", point=(10, 10), changed=True, rect=None):
    step = {"step_index": index, "observation_stem": "scene-abc-step%02d" % index}
    if index:
        step["action"] = {
            "type": "click", "target_uid": uid, "point": list(point),
            "metadata": {"role": "push button", "text": "OK"},
            "target": {"role": "push button", "kind": "button", "name": "OK",
                       "visible_text": "OK", "app_name": "homebank",
                       "rect": rect or {"x": 5, "y": 5, "w": 20, "h": 12}},
        }
        step["diff"] = {"changed": changed, "magnitude": 0.01 if changed else 0.0,
                        "appeared": [], "changes": [{"uid": "z"}] if changed else []}
    else:
        step["action"] = None
    return step


def _build(steps, *, flags=None, width=100, height=100):
    present = flags or {}

    def key_of(stem):
        return observation_key("shard-0000", stem) if stem else None

    def flags_of(key):
        return present.get(key, {"publishable": True, "near_duplicate": False,
                                 "no_op_frame": False})

    return build_transitions(_episode(steps), episode_key="E", split="train",
                             width=width, height=height,
                             observation_key_of=key_of, observation_flags=flags_of)


def test_step_k_action_maps_observation_k_minus_1_to_k() -> None:
    """The off-by-one that would pair every action with the wrong screen."""
    rows, _ = _build([_step(0), _step(1), _step(2)])
    assert [r["action_index"] for r in rows] == [1, 2]
    assert rows[0]["before_key"].endswith("step00")
    assert rows[0]["after_key"].endswith("step01")
    assert rows[1]["before_key"].endswith("step01")
    assert rows[1]["after_key"].endswith("step02")


def test_step_zero_produces_no_transition() -> None:
    rows, _ = _build([_step(0)])
    assert rows == []


def test_a_no_op_transition_is_kept() -> None:
    """"This click changed nothing" is supervision, not a defect."""
    rows, _ = _build([_step(0), _step(1, changed=False)])
    assert len(rows) == 1
    assert rows[0]["transition_train_eligible"] is True
    assert rows[0]["effect"]["changed"] is False
    assert rows[0]["exclusion_reasons"] == []


def test_a_no_op_state_is_still_state_ineligible() -> None:
    """The two eligibility flags are independent and must stay that way."""
    record = build_record(
        observation_key="k", scene_id="s", episode_id="e", step_index=1,
        split="train", group="ep", meta={},
        flags={"publishable": True, "state_train_eligible": False,
               "near_duplicate": False, "no_op_frame": True})
    assert record["flags"]["no_op_frame"] is True
    assert record["flags"]["state_train_eligible"] is False
    assert record["flags"]["publishable"] is True


def test_an_action_outside_the_viewport_is_excluded() -> None:
    rows, reasons = _build([_step(0), _step(1, point=(500, 500))])
    assert rows[0]["transition_train_eligible"] is False
    assert "action_point_outside_viewport" in rows[0]["exclusion_reasons"]
    assert reasons["action_point_outside_viewport"] == 1


def test_an_unresolved_target_is_excluded() -> None:
    steps = [_step(0), _step(1)]
    steps[1]["action"]["target"] = {}
    rows, _ = _build(steps)
    assert "target_uid_did_not_resolve" in rows[0]["exclusion_reasons"]


def test_a_near_duplicate_endpoint_is_excluded_but_still_listed() -> None:
    key = observation_key("shard-0000", "scene-abc-step00")
    rows, _ = _build([_step(0), _step(1)], flags={
        key: {"publishable": True, "near_duplicate": True, "no_op_frame": False}})
    assert rows[0]["transition_train_eligible"] is False
    assert "before_near_duplicate" in rows[0]["exclusion_reasons"]
    assert rows[0]["before_key"] is not None, "an excluded row still names its endpoints"


def test_a_transition_id_is_stable_and_unique() -> None:
    ids = {transition_id("E", i) for i in range(10)}
    assert len(ids) == 10
    assert transition_id("E", 3) == transition_id("E", 3)


# ----------------------------------------------------------------- records

def test_a_record_never_carries_a_host_path() -> None:
    """meta.json stores the wallpaper as an absolute path on a shared machine."""
    meta = {"theme": {"gtk_theme": "Tahoe-Light",
                      "wallpaper": "/proj/example/users/alice/tools/w/blue.png"},
            "viewport": {"width": 800, "height": 600}}
    record = build_record(observation_key="k", scene_id="s", episode_id=None,
                          step_index=None, split="train", group="st", meta=meta,
                          flags={"publishable": True})
    assert record["theme"]["wallpaper"] == "blue.png"
    assert_sanitized(record)


def test_a_record_that_grew_a_field_is_refused() -> None:
    record = build_record(observation_key="k", scene_id="s", episode_id=None,
                          step_index=None, split="train", group="st", meta={},
                          flags={"publishable": True})
    record["internal_debug"] = "whatever"
    with pytest.raises(ValueError, match="unapproved"):
        assert_sanitized(record)


def test_a_record_that_leaks_a_path_is_refused() -> None:
    record = build_record(observation_key="k", scene_id="s", episode_id=None,
                          step_index=None, split="train", group="st", meta={},
                          flags={"publishable": True})
    record["scene"]["layout"] = "/proj/example/secret"
    with pytest.raises(ValueError, match="host path"):
        assert_sanitized(record)


# -------------------------------------------------------------- packing

@pytest.fixture()
def staged(tmp_path: Path):
    """A two-scene corpus, its release index, and a pack plan over both."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    corpus = tmp_path / "corpus"
    release = tmp_path / "release"
    rows = []
    for scene in ("aaa", "bbb"):
        directory = corpus / "shards" / "shard-0000" / "ep" / scene
        directory.mkdir(parents=True)
        steps = []
        for step in range(2):
            stem = "scene-%s-step%02d" % (scene, step)
            Image.new("RGB", (100, 100), (20, 30, 40)).save(directory / (stem + ".png"))
            (directory / (stem + ".elements.leaf.json")).write_text(
                json.dumps([{"type": "Button", "rect": {"x": 1, "y": 1, "w": 9, "h": 9}}]))
            (directory / (stem + ".screentag.txt")).write_text("<screentag></screentag>")
            (directory / (stem + ".meta.json")).write_text(json.dumps(
                {"viewport": {"width": 100, "height": 100}, "launched_apps": ["homebank"],
                 "num_elements_leaf": 1, "scene": {"seed": 1}}))
            steps.append(_step(step))
            rows.append({
                "observation_key": observation_key("shard-0000", stem),
                "split": "train", "group": "ep", "step_index": step,
                "width": 100, "height": 100, "publishable": True,
                "state_train_eligible": True, "near_duplicate": False,
                "no_op_frame": False, "missing_apps": [],
                "scene_id": scene,
                "episode_id": episode_id("shard-0000", "ep", scene),
                "source_path": "shards/shard-0000/ep/%s/%s" % (scene, stem),
            })
        (directory / "episode.json").write_text(json.dumps(_episode(steps)))

    (release / "index" / "observations").mkdir(parents=True)
    (release / "index" / "transitions").mkdir(parents=True)
    (release / "_work").mkdir(parents=True)
    pq.write_table(pa.table({k: [r[k] for r in rows] for k in rows[0]}),
                   release / "index" / "observations" / "train.parquet")
    pq.write_table(pa.table({k: [r[k] for r in rows] for k in
                             ("observation_key", "split", "scene_id", "episode_id",
                              "source_path")} | {"tar_path": ["data/train/part-00000.tar"] * len(rows),
                                                 "est_bytes": [1000] * len(rows),
                                                 "member_index": list(range(len(rows)))}),
                   release / "_work" / "pack_plan.parquet")
    return corpus, release


def _pack(corpus: Path, release: Path, extra=()):
    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "pack_hf_release.py"),
         "--corpus", str(corpus), "--release", str(release), "--workers", "2", *extra],
        capture_output=True, text=True)


def test_packing_writes_a_verified_tar_and_a_marker(staged) -> None:
    corpus, release = staged
    result = _pack(corpus, release)
    assert result.returncode == 0, result.stdout + result.stderr
    tar_path = release / "data" / "train" / "part-00000.tar"
    assert tar_path.is_file()
    assert not list((release / "data" / "train").glob("*.partial"))

    with tarfile.open(str(tar_path)) as tar:
        names = tar.getnames()
    assert len(names) == 16, "four members for each of four observations"
    # WebDataset groups by key, so a sample's four members must be consecutive
    # and a key must never reappear after another key has started.
    keys = [name.split(".", 1)[0] for name in names]
    for start in range(0, len(keys), 4):
        assert len(set(keys[start:start + 4])) == 1
    runs = [key for index, key in enumerate(keys) if index == 0 or key != keys[index - 1]]
    assert len(runs) == len(set(runs)), "a key resumes after another key began"

    marker = json.loads((release / "_work" / "markers" /
                         "data__train__part-00000.tar.json").read_text())
    assert marker["observations"] == 4 and marker["members"] == 16
    assert len(marker["sha256"]) == 64
    assert all(entry["byte_offset"] > 0 for entry in marker["member_index"])


def test_a_whole_episode_lands_in_one_tar(staged) -> None:
    corpus, release = staged
    assert _pack(corpus, release).returncode == 0
    import pyarrow.parquet as pq
    plan = pq.read_table(release / "_work" / "pack_plan.parquet").to_pydict()
    by_episode = {}
    for episode, tar in zip(plan["episode_id"], plan["tar_path"]):
        by_episode.setdefault(episode, set()).add(tar)
    assert all(len(tars) == 1 for tars in by_episode.values())


def test_a_completed_tar_is_not_packed_again(staged) -> None:
    corpus, release = staged
    assert _pack(corpus, release).returncode == 0
    before = (release / "data" / "train" / "part-00000.tar").stat().st_mtime_ns
    second = _pack(corpus, release)
    assert "0 to pack" in second.stdout
    assert (release / "data" / "train" / "part-00000.tar").stat().st_mtime_ns == before


def test_an_interrupted_pack_leaves_no_tar_and_is_redone(staged) -> None:
    """A `.partial` is not a shard. The next run overwrites it."""
    corpus, release = staged
    tar_dir = release / "data" / "train"
    tar_dir.mkdir(parents=True)
    (tar_dir / "part-00000.tar.partial").write_bytes(b"half a tar")
    assert _pack(corpus, release).returncode == 0
    assert (tar_dir / "part-00000.tar").is_file()
    assert not (tar_dir / "part-00000.tar.partial").exists()


def test_a_marker_without_its_tar_is_repacked(staged) -> None:
    corpus, release = staged
    assert _pack(corpus, release).returncode == 0
    (release / "data" / "train" / "part-00000.tar").unlink()
    result = _pack(corpus, release)
    assert result.returncode == 0
    assert "1 to pack" in result.stdout
    assert (release / "data" / "train" / "part-00000.tar").is_file()


def test_no_member_name_repeats_across_the_release(staged) -> None:
    corpus, release = staged
    assert _pack(corpus, release).returncode == 0
    seen = set()
    for tar_path in (release / "data").rglob("*.tar"):
        with tarfile.open(str(tar_path)) as tar:
            for name in tar.getnames():
                assert name not in seen, "duplicate tar member %s" % name
                seen.add(name)


def test_a_packed_record_is_sanitized_and_carries_its_action(staged) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq
    corpus, release = staged
    key = observation_key("shard-0000", "scene-aaa-step01")
    pq.write_table(pa.table({
        "after_key": [key], "transition_id": ["E__t01"], "action_type": ["click"],
        "action_target_uid": ["u1"], "action_target_role": ["push button"],
        "action_target_kind": ["button"], "action_target_text": ["OK"],
        "action_target_app": ["homebank"], "action_point_px": [[10, 10]],
        "action_point_norm_1000": [[100, 100]], "action_point_screentag_500": [[50, 50]],
        "action_target_bbox_px": [[5, 5, 25, 17]],
        "action_target_bbox_norm_1000": [[50, 50, 250, 170]],
        "effect": [{"changed": True, "magnitude": 0.01}],
        "transition_train_eligible": [True],
    }), release / "index" / "transitions" / "train.parquet")
    assert _pack(corpus, release, extra=["--repack"]).returncode == 0
    with tarfile.open(str(release / "data" / "train" / "part-00000.tar")) as tar:
        blob = tar.extractfile(key + ".record.json").read()
    record = json.loads(blob)
    assert_sanitized(record)
    assert record["action_into_this_state"]["target_uid"] == "u1"
    assert record["effect_of_that_action"]["changed"] is True
    other = observation_key("shard-0000", "scene-aaa-step00")
    with tarfile.open(str(release / "data" / "train" / "part-00000.tar")) as tar:
        first = json.loads(tar.extractfile(other + ".record.json").read())
    assert "action_into_this_state" not in first, "step 0 has no action into it"


def test_an_on_screen_window_title_is_not_treated_as_a_leak() -> None:
    """A title bar quoting a path is annotation; it is painted in the image."""
    meta = {"viewport": {"width": 800, "height": 600},
            "occlusion": {"window_stack": [
                {"name": "landing_page.html (/tmp/session-6k9s307k/Users/iris/"
                         "Documents/landing_page.html) - Bluefish 2.2.12",
                 "rect": {"x": 0, "y": 0, "w": 800, "h": 600}, "stack_index": 0}]}}
    record = build_record(observation_key="k", scene_id="s", episode_id=None,
                          step_index=None, split="train", group="st", meta=meta,
                          flags={"publishable": True})
    assert_sanitized(record)
    assert "/tmp/session-" in record["window_stack"][0]["name"]


def test_a_clicked_widget_label_quoting_a_path_is_not_a_leak() -> None:
    """A file chooser's combo box really does read that path on screen."""
    record = build_record(
        observation_key="k", scene_id="s", episode_id="e", step_index=1,
        split="train", group="ep", meta={}, flags={"publishable": True},
        action={"type": "click", "target_app": "bluefish",
                "target_text": "/tmp/session-sxc_9evg/home/anika/Documents"})
    assert_sanitized(record)
    assert record["action_into_this_state"]["target_text"].startswith("/tmp/session-")


def test_a_host_path_in_another_action_field_is_still_refused() -> None:
    """The exemption is one leaf, not the whole action."""
    record = build_record(
        observation_key="k", scene_id="s", episode_id="e", step_index=1,
        split="train", group="ep", meta={}, flags={"publishable": True},
        action={"type": "click", "target_app": "/proj/example/leaked",
                "target_text": "fine"})
    with pytest.raises(ValueError, match="host path"):
        assert_sanitized(record)


def test_a_host_path_outside_on_screen_text_is_still_refused() -> None:
    meta = {"viewport": {"width": 800, "height": 600},
            "theme": {"wallpaper": "/proj/example/users/alice/w/blue.png"}}
    record = build_record(observation_key="k", scene_id="s", episode_id=None,
                          step_index=None, split="train", group="st", meta=meta,
                          flags={"publishable": True})
    assert record["theme"]["wallpaper"] == "blue.png"
    record["provenance"]["generator"] = "/proj/example/whatever"
    with pytest.raises(ValueError, match="host path"):
        assert_sanitized(record)
