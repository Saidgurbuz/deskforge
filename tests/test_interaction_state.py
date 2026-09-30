"""Tests for derived per-element interaction state."""

from __future__ import annotations

from deskshot.extraction.interaction_state import (
    annotate_interaction_state,
    visible_click_point,
)


def _elem(**kw):
    base = {
        "role": "push button",
        "rect": {"x": 0, "y": 0, "w": 100, "h": 20},
        "attrs": {
            "action_names": ["click"],
            "states": {"focusable": True, "enabled": True, "sensitive": True},
        },
    }
    base.update(kw)
    return base


def test_actionable_button_is_marked() -> None:
    elements = [_elem()]

    summary = annotate_interaction_state(elements)

    assert elements[0]["interaction"]["actionable"] is True
    assert elements[0]["interaction"]["actions"] == ["click"]
    assert summary["num_actionable"] == 1


def test_disabled_button_is_not_actionable() -> None:
    """A greyed-out control still advertises its actions."""
    elements = [
        _elem(attrs={"action_names": ["click"], "states": {"selectable": True}})
    ]

    summary = annotate_interaction_state(elements)

    assert elements[0]["interaction"]["actionable"] is False
    assert elements[0]["interaction"]["enabled"] is False
    assert summary["num_disabled_with_actions"] == 1


def test_static_text_is_not_actionable() -> None:
    elements = [_elem(role="label", attrs={"states": {}})]

    annotate_interaction_state(elements)

    assert elements[0]["interaction"]["actionable"] is False


def test_focusable_entry_without_actions_is_actionable() -> None:
    """Some toolkits expose no Action interface for entries."""
    elements = [
        _elem(
            role="entry",
            attrs={"states": {"focusable": True, "editable": True,
                              "enabled": True, "sensitive": True}},
        )
    ]

    annotate_interaction_state(elements)

    assert elements[0]["interaction"]["actionable"] is True
    assert elements[0]["interaction"]["editable"] is True


def test_click_point_avoids_the_occluder() -> None:
    """The bbox centre can sit under the window covering the element."""
    elem = _elem(
        rect={"x": 0, "y": 0, "w": 100, "h": 20},
        is_occluded=True,
        visible_fragments=[{"x": 0, "y": 0, "w": 20, "h": 20}],
    )

    point = visible_click_point(elem)

    assert point == [10, 10]          # centre of the visible strip
    assert point != [50, 10]          # the bbox centre, which is covered


def test_click_point_falls_back_to_bbox_centre_when_unoccluded() -> None:
    point = visible_click_point(_elem(rect={"x": 10, "y": 10, "w": 40, "h": 20}))

    assert point == [30, 20]


def test_summary_counts_moved_click_points() -> None:
    elements = [
        _elem(is_occluded=True, visible_fragments=[{"x": 0, "y": 0, "w": 20, "h": 20}]),
        _elem(),
    ]

    summary = annotate_interaction_state(elements)

    assert summary["num_click_point_off_center"] == 1


def test_greyed_out_item_reports_not_enabled() -> None:
    """Measured GTK behaviour: an insensitive menu item omits the flags.

    An enabled item carries {"enabled": True, "sensitive": True}; a greyed-out
    one carries only {"selectable": True}. Defaulting the missing flag to
    enabled would report greyed-out controls as operable.
    """
    enabled_item = _elem(
        role="menu item",
        attrs={"action_names": ["click"],
               "states": {"enabled": True, "sensitive": True, "selectable": True}},
    )
    greyed_item = _elem(role="menu item", attrs={"states": {"selectable": True}})

    annotate_interaction_state([enabled_item, greyed_item])

    assert enabled_item["interaction"]["enabled"] is True
    assert enabled_item["interaction"]["actionable"] is True
    assert greyed_item["interaction"]["enabled"] is False
    assert greyed_item["interaction"]["actionable"] is False


def test_missing_state_info_assumes_operable() -> None:
    """No states at all means the toolkit told us nothing, not "disabled"."""
    elem = _elem(attrs={"action_names": ["click"]})

    annotate_interaction_state([elem])

    assert elem["interaction"]["enabled"] is True
