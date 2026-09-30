"""Transitions, and the off-by-one that makes them mean anything.

`episode.json` stores an action on the step it *produced*, not the step it was
taken from. Step 0 has `action: null` because nothing led to it. So

    observation[k - 1]  --  steps[k].action  -->  observation[k]

and the diff on step `k` describes that same transition. Reading it the other
way round pairs every action with the screen it was not taken on, and nothing
downstream would notice.

The eligibility rules are the release procedure's, with one that is easy to get
backwards: **a no-op transition is kept.** "This click changed nothing" is
supervision, not a defect. What is excluded is the resulting *state* from the
states view, which is a different flag on a different table.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from deskshot.release.keys import transition_id

#: The ScreenTag coordinate grid, and the normalized integer grid used by the
#: convenience fields. Pixels stay authoritative; both of these are derived.
SCREENTAG_GRID = 500
NORM_GRID = 1000


def _norm(value: int, extent: int, grid: int) -> int:
    if not extent:
        return 0
    return max(0, min(grid, int(round(value * grid / float(extent)))))


def normalize_point(point, width: int, height: int, grid: int) -> Optional[List[int]]:
    if not point or len(point) < 2:
        return None
    return [_norm(int(point[0]), width, grid), _norm(int(point[1]), height, grid)]


def denormalize_point(point, width: int, height: int, grid: int) -> List[int]:
    """The inverse the round-trip test checks against."""
    return [int(round(point[0] * width / float(grid))),
            int(round(point[1] * height / float(grid)))]


def normalize_box(rect: Optional[Dict[str, Any]], width: int, height: int,
                  grid: int) -> Optional[List[int]]:
    if not isinstance(rect, dict):
        return None
    try:
        x, y = int(rect["x"]), int(rect["y"])
        w, h = int(rect["w"]), int(rect["h"])
    except (KeyError, TypeError, ValueError):
        return None
    return [_norm(x, width, grid), _norm(y, height, grid),
            _norm(x + w, width, grid), _norm(y + h, height, grid)]


def box_px(rect: Optional[Dict[str, Any]]) -> Optional[List[int]]:
    if not isinstance(rect, dict):
        return None
    try:
        x, y = int(rect["x"]), int(rect["y"])
        return [x, y, x + int(rect["w"]), y + int(rect["h"])]
    except (KeyError, TypeError, ValueError):
        return None


def point_in_viewport(point, width: int, height: int) -> bool:
    if not point or len(point) < 2:
        return False
    try:
        x, y = int(point[0]), int(point[1])
    except (TypeError, ValueError):
        return False
    return 0 <= x < width and 0 <= y < height


def box_in_viewport(box: Optional[List[int]], width: int, height: int) -> bool:
    if not box:
        return False
    x0, y0, x1, y1 = box
    return 0 <= x0 <= x1 <= width and 0 <= y0 <= y1 <= height


def _effect(diff: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    diff = diff or {}
    return {
        "changed": bool(diff.get("changed")),
        "magnitude": float(diff.get("magnitude") or 0.0),
        "appeared": len(diff.get("appeared") or []),
        "disappeared": len(diff.get("disappeared") or []),
        "moved": len(diff.get("moved") or []),
        "text_changed": len(diff.get("text_changed") or []),
        "state_changed": len(diff.get("state_changed") or []),
        "newly_occluded": len(diff.get("newly_occluded") or []),
        "revealed": len(diff.get("revealed") or []),
        "semantic_changes": len(diff.get("changes") or []),
        "persisted": int(diff.get("persisted") or 0),
    }


def build_transitions(
    episode: Dict[str, Any],
    *,
    episode_key: str,
    split: str,
    width: int,
    height: int,
    observation_key_of,
    observation_flags,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Every adjacent step pair in one episode, eligible or not.

    Ineligible transitions are still returned, carrying `exclusion_reasons`, so
    a count table can say why something was dropped instead of leaving a
    difference nobody can account for.

    `observation_key_of(stem)` maps a step's `observation_stem` to a release
    key, or None when that capture is not in the release at all.
    `observation_flags(key)` returns the manifest flags for an endpoint.
    """
    steps = episode.get("steps") or []
    by_index: Dict[int, Dict[str, Any]] = {}
    for step in steps:
        if isinstance(step, dict) and isinstance(step.get("step_index"), int):
            by_index[step["step_index"]] = step

    rows: List[Dict[str, Any]] = []
    reasons: Dict[str, int] = {}

    for index in sorted(by_index):
        if index == 0:
            continue  # nothing led to the first observation
        step = by_index[index]
        previous = by_index.get(index - 1)
        excluded: List[str] = []

        if previous is None:
            excluded.append("no_adjacent_predecessor")

        before_key = observation_key_of((previous or {}).get("observation_stem"))
        after_key = observation_key_of(step.get("observation_stem"))
        if before_key is None or after_key is None:
            excluded.append("endpoint_not_in_release")

        before = observation_flags(before_key) if before_key else None
        after = observation_flags(after_key) if after_key else None
        for label, flags in (("before", before), ("after", after)):
            if flags is None:
                continue
            if not flags.get("publishable"):
                excluded.append("%s_not_publishable" % label)
            if flags.get("near_duplicate"):
                excluded.append("%s_near_duplicate" % label)

        action = step.get("action") if isinstance(step.get("action"), dict) else None
        if not action:
            excluded.append("no_action_record")
        elif not action.get("type"):
            excluded.append("action_has_no_type")

        point = (action or {}).get("point")
        if action and not point_in_viewport(point, width, height):
            excluded.append("action_point_outside_viewport")

        target = (action or {}).get("target") or {}
        target_box = box_px(target.get("rect"))
        if action:
            # The pipeline resolves the target against the *before* state when it
            # clicks, and writes what it resolved. A uid with no resolved target,
            # or a target box outside the screen, means that resolution failed.
            if not action.get("target_uid"):
                excluded.append("action_has_no_target_uid")
            elif target_box is None:
                excluded.append("target_uid_did_not_resolve")
            elif not box_in_viewport(target_box, width, height):
                excluded.append("target_box_outside_viewport")

        for reason in excluded:
            reasons[reason] = reasons.get(reason, 0) + 1

        metadata = (action or {}).get("metadata") or {}
        rows.append({
            "transition_id": transition_id(episode_key, index),
            "episode_id": episode_key,
            "action_index": index,
            "split": split,
            "before_key": before_key,
            "after_key": after_key,
            "transition_train_eligible": not excluded,
            "action_type": (action or {}).get("type"),
            "action_target_uid": (action or {}).get("target_uid"),
            "action_target_role": target.get("role") or metadata.get("role"),
            "action_target_kind": target.get("kind"),
            "action_target_text": target.get("visible_text") or metadata.get("text"),
            "action_target_app": target.get("app_name"),
            "action_point_px": [int(point[0]), int(point[1])] if point and len(point) >= 2 else None,
            "action_point_norm_1000": normalize_point(point, width, height, NORM_GRID),
            "action_point_screentag_500": normalize_point(point, width, height, SCREENTAG_GRID),
            "action_target_bbox_px": target_box,
            "action_target_bbox_norm_1000": normalize_box(target.get("rect"), width, height, NORM_GRID),
            "effect": _effect(step.get("diff")),
            "exclusion_reasons": sorted(set(excluded)),
        })
    return rows, reasons
