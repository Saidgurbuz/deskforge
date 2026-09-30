"""Derive per-element interaction state for computer-use consumers.

The AT-SPI walker already records raw `action_names`, `states` and `interfaces`
under `attrs`, but a downstream agent needs the answer, not the evidence: can
this element be acted on right now, and where should the click land.

Two things are worth deriving here rather than downstream:

*Actionable* is not "has an action". A greyed-out button still advertises its
actions, so sensitivity has to be folded in, otherwise an agent is taught to
click controls that cannot respond.

*Click point* is not the box centre. Once occlusion is resolved, an element can
be visible only as a strip down one side, and its bbox centre can sit under the
window covering it. Set-of-Marks agents click centres, so a centre that lands on
the occluder is a silently wrong action. The visible fragments give a point that
is actually on the element.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from deskshot.extraction.visibility_fragments import Rect, best_fragment, rect_area

# Action names that mean "a pointer click does something", normalized across
# toolkits. GTK reports "click"/"press"/"activate" for the same intent.
_CLICK_ACTIONS = {"click", "press", "activate", "jump", "open", "toggle", "select"}

_NON_INTERACTIVE_ROLES = {
    "label",
    "text",
    "static",
    "separator",
    "filler",
    "panel",
    "image",
    "heading",
    "paragraph",
}


def _states(elem: Dict[str, Any]) -> Dict[str, bool]:
    attrs = elem.get("attrs") or {}
    states = attrs.get("states")
    return states if isinstance(states, dict) else {}


def _action_names(elem: Dict[str, Any]) -> List[str]:
    attrs = elem.get("attrs") or {}
    names = attrs.get("action_names")
    if not isinstance(names, list):
        return []
    return [str(n).strip().lower() for n in names if str(n).strip()]


def _visible_fragments(elem: Dict[str, Any]) -> List[Rect]:
    fragments = elem.get("visible_fragments")
    if not isinstance(fragments, list):
        fragments = elem.get("_visible_fragments")
    if not isinstance(fragments, list):
        return []
    return [f for f in fragments if isinstance(f, dict) and rect_area(f) > 0]


def visible_click_point(elem: Dict[str, Any]) -> Optional[List[int]]:
    """A click point guaranteed to sit on the visible part of the element.

    Falls back to the bbox centre only when no fragments are recorded, which is
    the un-occluded case where the two agree anyway.
    """
    fragments = _visible_fragments(elem)
    if not fragments:
        rect = elem.get("rect")
        if not isinstance(rect, dict) or rect_area(rect) <= 0:
            return None
        return [
            int(rect["x"] + rect["w"] / 2),
            int(rect["y"] + rect["h"] / 2),
        ]
    target = best_fragment(fragments)
    if target is None:
        return None
    return [
        int(target["x"] + target["w"] / 2),
        int(target["y"] + target["h"] / 2),
    ]


def annotate_interaction_state(elements: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Stamp a normalized `interaction` block on each element."""
    num_actionable = 0
    num_disabled_with_actions = 0
    num_click_point_moved = 0

    for elem in elements:
        states = _states(elem)
        actions = _action_names(elem)
        role = str(elem.get("role") or "").strip().lower()

        # Only true flags are recorded, so absence is meaningful *when we have
        # state information at all*. Measured on a GTK menu: an enabled item
        # carries {"enabled": True, "sensitive": True, ...}, while a greyed-out
        # one carries {"selectable": True} and simply omits them. Defaulting a
        # missing flag to enabled therefore reports greyed-out controls as
        # operable. Fall back to enabled only when no states were exposed.
        if states:
            enabled = bool(states.get("sensitive", states.get("enabled", False)))
        else:
            enabled = True
        clickable_actions = [a for a in actions if a in _CLICK_ACTIONS]

        actionable = bool(clickable_actions) and enabled
        if not actions and role not in _NON_INTERACTIVE_ROLES:
            # Some toolkits expose no Action interface but are still operable
            # when focusable (entries, list items).
            actionable = bool(states.get("focusable")) and enabled

        click_point = visible_click_point(elem)
        rect = elem.get("rect")
        if (
            click_point
            and isinstance(rect, dict)
            and rect_area(rect) > 0
            and elem.get("is_occluded")
        ):
            centre = [int(rect["x"] + rect["w"] / 2), int(rect["y"] + rect["h"] / 2)]
            if centre != click_point:
                num_click_point_moved += 1

        elem["interaction"] = {
            "actionable": actionable,
            "actions": actions,
            "enabled": bool(enabled),
            "focusable": bool(states.get("focusable", False)),
            "editable": bool(states.get("editable", False)),
            "checkable": bool(states.get("checkable", False)),
            "checked": bool(states.get("checked", False)),
            "expandable": bool(states.get("expandable", False)),
            "expanded": bool(states.get("expanded", False)),
            # Which tab is showing, which row is highlighted: visible state that
            # nothing else in the record captures.
            "selected": bool(states.get("selected", False)),
            "click_point": click_point,
        }

        if actionable:
            num_actionable += 1
        if actions and not enabled:
            num_disabled_with_actions += 1

    return {
        "num_elements": len(elements),
        "num_actionable": num_actionable,
        "num_disabled_with_actions": num_disabled_with_actions,
        "num_click_point_off_center": num_click_point_moved,
    }
