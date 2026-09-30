"""Which roles are worth clicking first, pinned to what was measured.

The original tiers were nominated from one episode. Over 2,644 real transitions
- each step's ScreenTag compared against the step before it - three of the
nominated high-yield roles turned out to change nothing 30-53% of the time and
produced 31% of every no-op in the corpus. This file keeps them out of the top
tier, and keeps the genuinely high-yield roles in it, so the measurement is not
quietly undone later.
"""

from __future__ import annotations

from deskshot.generation.actions import (
    _HIGH_YIELD_ROLES,
    _LOW_YIELD_ROLES,
    _MEASURED_LOW_YIELD_ROLES,
    _target_priority,
    sample_actionable_targets,
)


def _elem(role: str, text: str = "Save", w: int = 80, h: int = 24, uid: str = "u"):
    return {
        "uid": uid, "role": role, "inner_text": text,
        "rect": {"x": 0, "y": 0, "w": w, "h": h},
        "interaction": {"actionable": True, "click_point": {"x": 10, "y": 10}},
    }


def test_measured_low_yield_roles_are_not_high_yield() -> None:
    for role in _MEASURED_LOW_YIELD_ROLES:
        assert role not in _HIGH_YIELD_ROLES, (
            f"{role!r} changed nothing in 30%+ of measured clicks; it must not "
            "outrank roles that actually move the screen"
        )


def test_roles_that_measured_well_stayed_high_yield() -> None:
    # 0.0%-6.2% no-op in the same measurement.
    for role in ("check box", "list item", "menu item", "page tab", "menu",
                 "toggle button"):
        assert role in _HIGH_YIELD_ROLES


def test_a_menu_item_outranks_a_spin_button() -> None:
    assert _target_priority(_elem("menu item")) < _target_priority(_elem("spin button"))


def test_a_spin_button_still_outranks_an_inert_container() -> None:
    """Demoted, not banished - the dataset still needs these interactions."""
    assert _target_priority(_elem("spin button")) < _target_priority(
        _elem("drawing area", text="")
    )
    assert "spin button" not in _LOW_YIELD_ROLES


def test_demoted_roles_are_still_sampled_when_they_are_all_there_is() -> None:
    targets = sample_actionable_targets(
        [_elem("radio button", uid="a"), _elem("table column header", uid="b")],
        limit=8,
    )
    assert {t["uid"] for t in targets} == {"a", "b"}


def test_high_yield_target_is_ordered_before_a_demoted_one() -> None:
    targets = sample_actionable_targets(
        [_elem("spin button", uid="spin"), _elem("menu item", uid="menu")], limit=8
    )
    assert [t["uid"] for t in targets] == ["menu", "spin"]
