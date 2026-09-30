"""A plan must fully determine the scenes a batch will capture.

`scene-plan --force-app-count 1 --uniform-pages` wrote 80,000 single-window
Chromium scenes; `scene-batch` rebuilt each one from its seed alone, with
default parameters, and produced ordinary multi-app scenes with different
scene ids. The run completed cleanly - 300/300 markers, 240 of 266 accepted per
shard - while capturing none of what was planned.

The fix is that the plan records what it was composed with. These tests hold
that contract from both ends.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from deskshot.generation.scene_composer import compose_scene

_spec = importlib.util.spec_from_file_location(
    "dscli", Path(__file__).resolve().parents[1] / "src/deskshot/cli.py"
)


def _read(tmp_path: Path, rows) -> list:
    from deskshot.cli import _read_plan_shard
    p = tmp_path / "plan.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return _read_plan_shard(str(p), "0/1")


def test_the_parameters_survive_the_round_trip(tmp_path: Path) -> None:
    got = _read(tmp_path, [
        {"seed": 100, "steps": 0, "force_app_count": 1, "uniform_pages": True},
    ])
    assert got == [(100, 0, 1, True)]


def test_a_plan_without_the_fields_keeps_the_old_defaults(tmp_path: Path) -> None:
    """Plans written before this change must still load and mean what they did."""
    got = _read(tmp_path, [{"seed": 7, "steps": 3}])
    assert got == [(7, 3, None, False)]


def test_force_app_count_zero_is_not_confused_with_absent(tmp_path: Path) -> None:
    """0 windows is a real request; `or` would have turned it into None."""
    got = _read(tmp_path, [{"seed": 5, "steps": 0, "force_app_count": 0}])
    assert got[0][2] == 0


def test_rebuilding_with_the_parameters_reproduces_the_planned_scene() -> None:
    """The end-to-end property the run violated."""
    seed = 9_200_514
    planned = compose_scene(seed, available_apps=["chromium-browser"],
                            force_app_count=1, uniform_pages=True)
    rebuilt = compose_scene(seed, available_apps=["chromium-browser"],
                            force_app_count=1, uniform_pages=True)
    assert rebuilt.scene_id == planned.scene_id
    assert [a.app_name for a in rebuilt.apps] == ["chromium-browser"]


def test_rebuilding_without_them_produces_a_different_scene() -> None:
    """Demonstrates the failure, so a regression is unambiguous."""
    seed = 9_200_514
    targeted = compose_scene(seed, available_apps=["chromium-browser", "mousepad"],
                             force_app_count=1, uniform_pages=True)
    generic = compose_scene(seed, available_apps=["chromium-browser", "mousepad"])
    assert targeted.scene_id != generic.scene_id or \
        len(targeted.apps) != len(generic.apps)


def test_striding_preserves_the_parameters(tmp_path: Path) -> None:
    rows = [{"seed": i, "steps": 0, "force_app_count": 0, "uniform_pages": False}
            for i in range(10)]
    from deskshot.cli import _read_plan_shard
    p = tmp_path / "plan.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    shard = _read_plan_shard(str(p), "1/3")
    assert [s for s, _, _, _ in shard] == [1, 4, 7]
    assert all(f == 0 and u is False for _, _, f, u in shard)
