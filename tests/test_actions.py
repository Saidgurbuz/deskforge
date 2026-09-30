"""Tests for scene actions and state diffing."""

from __future__ import annotations

from deskshot.generation.actions import (
    SceneAction,
    diff_states,
    resolve_action_point,
    sample_actionable_targets,
)


def _el(uid, x=0, y=0, w=10, h=10, text="", occl="none", actionable=True, click=None,
        role="push button"):
    return {
        "uid": uid,
        "role": role,
        "rect": {"x": x, "y": y, "w": w, "h": h},
        "inner_text": text,
        "occlusion_state": occl,
        "interaction": {"actionable": actionable, "click_point": click or [x + w // 2, y + h // 2]},
    }


def test_diff_reports_appeared_and_disappeared() -> None:
    before = [_el("a"), _el("b")]
    after = [_el("a"), _el("c")]

    d = diff_states(before, after)

    assert d["appeared"] == ["c"]
    assert d["disappeared"] == ["b"]
    assert d["persisted"] == 1
    assert d["changed"] is True


def test_diff_reports_move_beyond_tolerance_only() -> None:
    before = [_el("a", x=0), _el("b", x=100)]
    after = [_el("a", x=1), _el("b", x=140)]     # 1px jitter vs a real move

    d = diff_states(before, after)

    assert d["moved"] == ["b"]


def test_diff_reports_occlusion_transitions() -> None:
    """Newly occluded / revealed is the label the amodal work needs."""
    before = [_el("a", occl="none"), _el("b", occl="partial")]
    after = [_el("a", occl="partial"), _el("b", occl="none")]

    d = diff_states(before, after)

    assert d["newly_occluded"] == ["a"]
    assert d["revealed"] == ["b"]


def test_diff_reports_text_change() -> None:
    d = diff_states([_el("a", text="0")], [_el("a", text="42")])

    assert d["text_changed"] == ["a"]


def test_diff_of_identical_states_is_unchanged() -> None:
    state = [_el("a"), _el("b")]

    d = diff_states(state, list(state))

    assert d["changed"] is False
    assert d["moved"] == [] and d["appeared"] == [] and d["disappeared"] == []


def test_resolve_action_point_uses_visible_click_point() -> None:
    """Not the bbox centre: that can sit under the covering window."""
    elements = [_el("a", x=0, y=0, w=100, h=20, click=[10, 10])]

    point = resolve_action_point(SceneAction(type="click", target_uid="a"), elements)

    assert point == (10, 10)


def test_resolve_action_point_prefers_explicit_point() -> None:
    elements = [_el("a")]

    point = resolve_action_point(
        SceneAction(type="click", target_uid="a", point=(7, 9)), elements
    )

    assert point == (7, 9)


def test_resolve_action_point_returns_none_for_unknown_target() -> None:
    assert resolve_action_point(SceneAction(type="click", target_uid="zz"), [_el("a")]) is None


def test_sample_actionable_targets_prefers_larger_and_skips_inert() -> None:
    elements = [
        _el("small", w=10, h=10, text="a"),
        _el("big", w=200, h=40, text="b"),
        _el("inert", w=300, h=80, actionable=False, text="c"),
    ]

    targets = sample_actionable_targets(elements, limit=5)

    assert [t["uid"] for t in targets] == ["big", "small"]


def test_sample_ranks_interactive_roles_above_big_inert_containers() -> None:
    """Sorting by area alone picks the canvas; measured 2/3 no-op clicks."""
    elements = [
        _el("canvas", w=900, h=600, role="drawing area"),
        _el("button", w=80, h=24, role="push button", text="Save"),
        _el("blank_text", w=400, h=300, role="text"),
    ]

    targets = sample_actionable_targets(elements, limit=3)

    assert targets[0]["uid"] == "button"
    assert targets[-1]["uid"] == "canvas"


def test_action_to_dict_omits_empty_fields() -> None:
    assert SceneAction(type="key", value="ctrl+s").to_dict() == {
        "type": "key",
        "value": "ctrl+s",
    }
