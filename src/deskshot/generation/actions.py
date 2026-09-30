"""Scene actions and the state diff between two observations.

The unit of temporal supervision is `(S_t, a, S_t+1)` where S is a dense parsed
state. Two choices matter here.

Actions target an *element*, not a raw pixel. Grounding the action in an
annotated element is what makes the resulting triple useful: the subject is
typed and located, rather than a bare coordinate. Clicks resolve through
`interaction.click_point`, which lands on the visible part of a partially
covered element instead of its bbox centre.

The diff is computed on element uids, so it says what actually changed rather
than how the pixels moved: appeared, disappeared, moved, text or value changed,
newly occluded, revealed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class SceneAction:
    """One action applied between two observations."""

    type: str                                   # click | type | key | scroll | wait
    target_uid: Optional[str] = None
    point: Optional[Tuple[int, int]] = None
    text: str = ""
    value: str = ""                             # key combo, e.g. "ctrl+s"
    dy: int = 0
    seconds: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"type": self.type}
        for key in ("target_uid", "text", "value", "dy", "seconds"):
            val = getattr(self, key)
            if val:
                out[key] = val
        if self.point:
            out["point"] = list(self.point)
        if self.metadata:
            out["metadata"] = dict(self.metadata)
        return out


def _by_uid(elements: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {e["uid"]: e for e in elements if isinstance(e.get("uid"), str)}


def _rect_tuple(elem: Dict[str, Any]) -> Optional[Tuple[int, int, int, int]]:
    rect = elem.get("rect")
    if not isinstance(rect, dict):
        return None
    return (
        int(rect.get("x", 0)),
        int(rect.get("y", 0)),
        int(rect.get("w", 0)),
        int(rect.get("h", 0)),
    )


def _text_of(elem: Dict[str, Any]) -> str:
    for key in ("visible_text", "inner_text", "name"):
        val = elem.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


#: Interaction fields whose change is invisible in geometry and text. Ticking a
#: check box moves nothing and renames nothing, so without these a toggle is
#: recorded as an action that did nothing - which is the opposite of true, and
#: exactly the transition a computer-use model most needs to learn.
_STATE_FIELDS = ("checked", "expanded", "enabled", "editable")


def _interaction_state(elem: Dict[str, Any]) -> Tuple:
    interaction = elem.get("interaction") or {}
    return tuple(interaction.get(field) for field in _STATE_FIELDS)


def diff_states(
    before: List[Dict[str, Any]],
    after: List[Dict[str, Any]],
    *,
    move_tolerance: int = 2,
    detail_limit: int = 12,
) -> Dict[str, Any]:
    """Element-level diff between two observations, keyed by uid.

    `move_tolerance` absorbs sub-pixel jitter so a redraw is not reported as a
    move; anything larger is a real layout change.
    """
    a, b = _by_uid(before), _by_uid(after)
    a_ids, b_ids = set(a), set(b)

    appeared = sorted(b_ids - a_ids)
    disappeared = sorted(a_ids - b_ids)

    moved: List[str] = []
    text_changed: List[str] = []
    state_changed: List[str] = []
    newly_occluded: List[str] = []
    revealed: List[str] = []

    for uid in sorted(a_ids & b_ids):
        old, new = a[uid], b[uid]
        r_old, r_new = _rect_tuple(old), _rect_tuple(new)
        if r_old and r_new and any(
            abs(o - n) > move_tolerance for o, n in zip(r_old, r_new)
        ):
            moved.append(uid)
        if _text_of(old) != _text_of(new):
            text_changed.append(uid)
        if _interaction_state(old) != _interaction_state(new):
            state_changed.append(uid)

        old_state = old.get("occlusion_state", "none")
        new_state = new.get("occlusion_state", "none")
        if old_state == "none" and new_state in ("partial", "hidden"):
            newly_occluded.append(uid)
        elif old_state in ("partial", "hidden") and new_state == "none":
            revealed.append(uid)

    # The uid lists say *that* something changed; this says what, so an episode
    # can be read without joining it back against two element files. Bounded,
    # because a window opening changes a hundred elements and the detail is only
    # useful while it is small enough to look at.
    changes: List[Dict[str, Any]] = []
    for uid in text_changed[:detail_limit]:
        changes.append({
            "uid": uid, "kind": "text", "role": a[uid].get("role"),
            "before": _text_of(a[uid]), "after": _text_of(b[uid]),
        })
    for uid in state_changed[:detail_limit]:
        changes.append({
            "uid": uid, "kind": "state", "role": a[uid].get("role"),
            "label": _text_of(b[uid]),
            "before": dict(zip(_STATE_FIELDS, _interaction_state(a[uid]))),
            "after": dict(zip(_STATE_FIELDS, _interaction_state(b[uid]))),
        })
    for uid in moved[:detail_limit]:
        changes.append({
            "uid": uid, "kind": "moved", "role": a[uid].get("role"),
            "label": _text_of(b[uid]),
            "before": _rect_tuple(a[uid]), "after": _rect_tuple(b[uid]),
        })

    churn = len(appeared) + len(disappeared) + len(moved) + len(text_changed) + len(state_changed)

    return {
        "num_before": len(before),
        "num_after": len(after),
        "appeared": appeared,
        "disappeared": disappeared,
        "moved": moved,
        "text_changed": text_changed,
        "state_changed": state_changed,
        "newly_occluded": newly_occluded,
        "revealed": revealed,
        "persisted": len(a_ids & b_ids),
        "changes": changes,
        # One number for "how much did this action move the screen", so an
        # episode can be filtered or curriculum-ordered without reading diffs.
        "magnitude": round(churn / max(1, max(len(before), len(after))), 4),
        "changed": bool(
            appeared or disappeared or moved or text_changed or state_changed
        ),
    }


def resolve_action_point(
    action: SceneAction,
    elements: List[Dict[str, Any]],
) -> Optional[Tuple[int, int]]:
    """Where the action should land on screen.

    An explicit point wins; otherwise the target element's visible click point,
    which is not the bbox centre when the element is partially covered.
    """
    if action.point:
        return action.point
    if not action.target_uid:
        return None
    target = _by_uid(elements).get(action.target_uid)
    if target is None:
        return None
    point = (target.get("interaction") or {}).get("click_point")
    if isinstance(point, list) and len(point) == 2:
        return (int(point[0]), int(point[1]))
    rect = target.get("rect")
    if isinstance(rect, dict):
        return (
            int(rect.get("x", 0) + rect.get("w", 0) / 2),
            int(rect.get("y", 0) + rect.get("h", 0) / 2),
        )
    return None


# Roles whose activation usually changes the screen, and roles that are large
# but inert. Sorting purely by area picks the latter: measured on a first
# episode, two of three random clicks landed on a "drawing area" or an unnamed
# "text" region and produced no state change at all.
#
# The membership below is no longer a guess. Over 2,644 real transitions in the
# corpus, comparing each step's ScreenTag against the one before it, the share
# of clicks that changed nothing at all came out as:
#
#     check box   0.0%   list item  0.0%   menu item   0.6%   page tab   1.2%
#     menu        5.2%   toggle     6.2%   link       11.0%   push btn  13.5%
#     combo box  17.2%   table column header 30.2%   radio button 31.0%
#     spin button 52.9%
#
# Three roles that were nominated as high-yield are measurably not: clicking a
# column header re-sorts a table that is often already in that order, clicking
# an already-selected radio button does nothing, and clicking the body of a spin
# button misses the increment arrows entirely. Together they produced 31% of all
# no-op transitions. They stay selectable - a computer-use dataset needs them,
# and "this click does nothing" is itself worth learning - but they no longer
# outrank the roles that actually move the screen.
_HIGH_YIELD_ROLES = {
    "push button", "menu item", "menu", "check menu item", "radio menu item",
    "check box", "toggle button", "tab", "page tab", "link",
    "list item", "combo box", "slider", "expander", "tree item",
}
# Measured no-op rate of 30% or worse. They fall through to the middle tier
# with every unclassified role, which is exactly where the measurement puts
# them; `tests/test_action_yield.py` pins them out of the high tier.
_MEASURED_LOW_YIELD_ROLES = frozenset(
    {"table column header", "radio button", "spin button"}
)
_LOW_YIELD_ROLES = {
    "drawing area", "panel", "filler", "separator", "scroll pane", "viewport",
    "layered pane", "frame", "window", "image", "label",
}


def _target_priority(elem: Dict[str, Any]) -> Tuple[int, int]:
    role = str(elem.get("role") or "").strip().lower()
    has_text = bool(
        (elem.get("inner_text") or elem.get("name") or "").strip()
    )
    if role in _HIGH_YIELD_ROLES:
        tier = 0
    elif role in _LOW_YIELD_ROLES:
        tier = 2
    else:
        tier = 1
    if not has_text and tier != 0:
        tier += 1
    rect = elem.get("rect") or {}
    area = max(0, int(rect.get("w", 0))) * max(0, int(rect.get("h", 0)))
    return (tier, -area)


def sample_actionable_targets(
    elements: List[Dict[str, Any]],
    *,
    limit: int = 8,
) -> List[Dict[str, Any]]:
    """Elements worth acting on, most likely to change the screen first.

    Ranked by role first and size second: a transition sample is only useful if
    the action actually did something, and the biggest element on screen is
    usually an inert container.
    """
    candidates = [
        e
        for e in elements
        if isinstance(e.get("uid"), str)
        and (e.get("interaction") or {}).get("actionable")
        and (e.get("interaction") or {}).get("click_point")
    ]
    candidates.sort(key=_target_priority)
    return candidates[:limit]
