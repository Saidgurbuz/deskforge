"""Carrying the finalized split forward, and only extending it where it stops.

`plan/splits_v3.jsonl` covers the 1,067,799 state-train-eligible observations.
The release carries every *publishable* observation, which is 1,207,368, so
roughly 140k of them belong to scenes v3 never had to place. Those scenes get a
split here, by the same rules and the same priority `build_eval_splits.py` uses,
and every v3 assignment is preserved exactly.

`val` and `test_id` are never redrawn. They are a finalized random sample; a
scene that was not in v3 has no claim on them, and enlarging them after the
fact would change what a number measured against them means.
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional, Set

# Imported rather than restated: if the held-out axes ever move, they move once.
import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "_build_eval_splits",
    Path(__file__).resolve().parents[3] / "scripts" / "build_eval_splits.py",
)
_EVAL = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("_build_eval_splits", _EVAL)
_SPEC.loader.exec_module(_EVAL)

HELDOUT_APPS: Dict[str, str] = dict(_EVAL.HELDOUT_APPS)
HELDOUT_THEME: str = _EVAL.HELDOUT_THEME
HELDOUT_RESOLUTION: str = _EVAL.HELDOUT_RESOLUTION

#: Priority order, and the two splits that are a finalized random sample.
AXIS_ORDER = ("test_app", "test_theme", "test_resolution")
FROZEN_SPLITS = ("val", "test_id")
ALL_SPLITS = ("train",) + FROZEN_SPLITS + AXIS_ORDER


def axis_for(apps: Iterable[str], theme: Optional[str],
             resolution: Optional[str]) -> Optional[str]:
    """The held-out axis this scene falls on, in priority order, or None."""
    if set(apps or ()) & set(HELDOUT_APPS):
        return "test_app"
    if theme == HELDOUT_THEME:
        return "test_theme"
    if resolution == HELDOUT_RESOLUTION:
        return "test_resolution"
    return None


def assign(
    scene_id: str,
    *,
    frozen: Dict[str, str],
    apps: Iterable[str],
    theme: Optional[str],
    resolution: Optional[str],
) -> str:
    """The split for one scene. A frozen assignment always wins."""
    existing = frozen.get(scene_id)
    if existing:
        return existing
    return axis_for(apps, theme, resolution) or "train"


def leaks(scenes: Dict[str, dict]) -> Dict[str, Set[str]]:
    """Held-out attributes that reached train. Must be empty, always.

    Checked over the union of applications across every frame of a scene: one
    capture of a scene can miss an application another capture of it saw, and
    keying on a single frame is exactly how three held-out applications leaked
    into train the first time this was built.
    """
    found: Dict[str, Set[str]] = {}
    for scene_id, scene in scenes.items():
        if scene.get("split") != "train":
            continue
        for app in set(scene.get("apps") or ()) & set(HELDOUT_APPS):
            found.setdefault("app:" + app, set()).add(scene_id)
        if scene.get("theme") == HELDOUT_THEME:
            found.setdefault("theme:" + HELDOUT_THEME, set()).add(scene_id)
        if scene.get("resolution") == HELDOUT_RESOLUTION:
            found.setdefault("resolution:" + HELDOUT_RESOLUTION, set()).add(scene_id)
    return found
