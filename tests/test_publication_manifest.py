"""A release manifest must not count rejects or duplicates as training data.

Three lines get drawn and each is a judgement, so each is pinned here:

- a capture whose *planned* application never launched is still correct about
  what is on screen, and is the only evidence for three applications dropped
  from the pool mid-project, so it stays publishable;
- a near-duplicate scene and a no-op episode frame are both correct and both
  repeated, so they stay publishable but leave the training set;
- too few elements, no coverage, a missing element list, or a box claiming
  pixels it cannot show is wrong, and leaves entirely.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "bpm", Path(__file__).resolve().parents[1] / "scripts/build_publication_manifest.py"
)
bpm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bpm)


def _capture(d: Path, stem: str, *, failed=(), missing=(), near_dup="",
             apps=("mousepad",), verdict=True, png=True, leaf=42):
    if png:
        (d / f"{stem}.png").write_bytes(b"px")
    (d / f"{stem}.meta.json").write_text(json.dumps({
        "scene": {"scene_id": "sc1", "seed": 7, "theme_preset": "linux_classic",
                  "display_preset": "fhd_1920x1080"},
        "launched_apps": list(apps), "num_elements_leaf": leaf,
        "desktop_fixture": {"profile": "balanced"},
    }))
    if verdict:
        (d / f"{stem}.verdict.json").write_text(json.dumps({
            "failed_checks": list(failed), "missing_apps": list(missing),
            "near_duplicate_of": near_dup,
        }))


def _scan(tmp_path: Path):
    root = tmp_path / "shards" / "shard-0000" / "st" / "aa"
    root.mkdir(parents=True)
    return root


def _rows(tmp_path: Path):
    return bpm.scan_shard((str(tmp_path / "shards"), "shard-0000"))


def test_a_clean_capture_is_publishable_and_trainable(tmp_path: Path) -> None:
    _capture(_scan(tmp_path), "scene-a-step00")
    r = _rows(tmp_path)[0]
    assert r["publishable"] and r["train_eligible"]
    assert r["hard_failures"] == []


def test_a_planned_app_that_never_launched_stays_publishable(tmp_path: Path) -> None:
    """The scene has fewer windows than planned; nothing is mislabelled."""
    _capture(_scan(tmp_path), "scene-a-step00", missing=["xarchiver"])
    r = _rows(tmp_path)[0]
    assert r["publishable"] and r["train_eligible"]
    assert r["missing_apps"] == ["xarchiver"]


def test_a_degenerate_capture_is_excluded(tmp_path: Path) -> None:
    _capture(_scan(tmp_path), "scene-a-step00",
             failed=["min_elements", "coverage"], leaf=1)
    r = _rows(tmp_path)[0]
    assert not r["publishable"] and not r["train_eligible"]
    assert set(r["hard_failures"]) == {"min_elements", "coverage"}


def test_a_box_claiming_pixels_it_cannot_show_is_excluded(tmp_path: Path) -> None:
    _capture(_scan(tmp_path), "scene-a-step00", failed=["visible_fragments_present"])
    assert not _rows(tmp_path)[0]["publishable"]


def test_a_near_duplicate_is_publishable_but_not_trainable(tmp_path: Path) -> None:
    _capture(_scan(tmp_path), "scene-a-step00", near_dup="scene-other-step00")
    r = _rows(tmp_path)[0]
    assert r["publishable"] and not r["train_eligible"]
    assert r["near_duplicate"]


def test_a_no_op_frame_is_publishable_but_not_trainable(tmp_path: Path) -> None:
    d = tmp_path / "shards" / "shard-0000" / "ep" / "sc1"
    d.mkdir(parents=True)
    for i in range(3):
        _capture(d, f"scene-sc1-step{i:02d}")
    (d / "episode.json").write_text(json.dumps({"steps": [
        {"step_index": 0, "diff": None},
        {"step_index": 1, "diff": {"changed": True}},
        {"step_index": 2, "diff": {"changed": False}},   # the no-op
    ]}))
    rows = {r["step"]: r for r in _rows(tmp_path)}
    assert rows[1]["train_eligible"] and not rows[1]["no_op_frame"]
    assert rows[2]["no_op_frame"]
    assert rows[2]["publishable"] and not rows[2]["train_eligible"]


def test_a_sample_with_no_verdict_cannot_be_published(tmp_path: Path) -> None:
    """Unjudged is not the same as passing."""
    _capture(_scan(tmp_path), "scene-a-step00", verdict=False)
    r = _rows(tmp_path)[0]
    assert not r["has_verdict"] and not r["publishable"]


def test_an_uncommitted_capture_is_not_counted(tmp_path: Path) -> None:
    """No PNG means the sample never committed."""
    _capture(_scan(tmp_path), "scene-a-step00", png=False)
    assert _rows(tmp_path) == []


def test_malformed_json_does_not_crash_the_scan(tmp_path: Path) -> None:
    d = _scan(tmp_path)
    _capture(d, "scene-a-step00")
    (d / "scene-b-step00.png").write_bytes(b"px")
    (d / "scene-b-step00.meta.json").write_text("{not json")
    rows = _rows(tmp_path)
    assert [r["key"] for r in rows] == ["scene-a-step00"]
