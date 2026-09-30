"""Tests for counterfactual scene variants."""

from __future__ import annotations

import pytest

from deskshot.generation.counterfactual import (
    build_counterfactual_pairs,
    counterfactual_variant,
    describe_pair,
    pair_differs_only_in,
    variant_changes_session,
)
from deskshot.generation.scene_composer import compose_scene

POOL = ["chromium-browser", "xarchiver", "gnome-calculator", "eog"]


def test_variant_changes_exactly_one_factor() -> None:
    """A counterfactual that moved anything else is not a controlled pair."""
    base = compose_scene(4242, available_apps=POOL)

    variant = counterfactual_variant(base, "theme_preset", "windows_redmond")

    assert variant.theme_preset == "windows_redmond"
    assert pair_differs_only_in(base, variant, "theme_preset")


def test_pair_differs_only_in_rejects_multi_factor_change() -> None:
    base = compose_scene(4242, available_apps=POOL)
    variant = counterfactual_variant(base, "theme_preset", "windows_redmond")
    variant = counterfactual_variant(variant, "desktop_profile", "dense")

    assert pair_differs_only_in(base, variant, "theme_preset") is False


def test_pair_differs_only_in_rejects_unchanged_factor() -> None:
    base = compose_scene(4242, available_apps=POOL)

    assert pair_differs_only_in(base, base, "theme_preset") is False


def test_apps_and_layout_are_preserved() -> None:
    """The whole point is that content is held fixed."""
    base = compose_scene(99, available_apps=POOL)

    for _factor, variant in build_counterfactual_pairs(base):
        assert [a.app_name for a in variant.apps] == [a.app_name for a in base.apps]
        assert [a.state_ref for a in variant.apps] == [a.state_ref for a in base.apps]
        assert variant.layout == base.layout
        assert variant.seed == base.seed


def test_build_pairs_is_deterministic() -> None:
    base = compose_scene(77, available_apps=POOL)

    a = build_counterfactual_pairs(base)
    b = build_counterfactual_pairs(base)

    assert [(f, v.theme_preset, v.display_preset) for f, v in a] == \
           [(f, v.theme_preset, v.display_preset) for f, v in b]


def test_each_pair_changes_only_its_own_factor() -> None:
    base = compose_scene(31337, available_apps=POOL)

    for factor, variant in build_counterfactual_pairs(base):
        assert pair_differs_only_in(base, variant, factor), factor


def test_appearance_variant_needs_its_own_session() -> None:
    base = compose_scene(5, available_apps=POOL)
    variant = counterfactual_variant(
        base, "theme_preset", "windows_redmond" if base.theme_preset != "windows_redmond" else "linux_classic"
    )

    assert variant_changes_session(base, variant) is True


def test_describe_pair_reports_both_values() -> None:
    base = compose_scene(11, available_apps=POOL)
    other = "windows_redmond" if base.theme_preset != "windows_redmond" else "linux_classic"
    variant = counterfactual_variant(base, "theme_preset", other)

    info = describe_pair(base, "theme_preset", variant)

    assert info["base_value"] == base.theme_preset
    assert info["variant_value"] == other
    assert info["base_signature"] != info["variant_signature"]
    assert info["shares_session"] is False


def test_wallpaper_variant_is_invisible_to_the_diversity_signature() -> None:
    """scene_signature deliberately ignores wallpaper_seed.

    So a wallpaper-only counterfactual is a near-duplicate to the batch planner
    and cannot be produced through the normal diverse-batch path; such pairs have
    to be generated explicitly.
    """
    base = compose_scene(11, available_apps=POOL)
    variant = counterfactual_variant(base, "wallpaper_seed", base.wallpaper_seed + 1)

    info = describe_pair(base, "wallpaper_seed", variant)

    assert info["base_signature"] == info["variant_signature"]
    assert pair_differs_only_in(base, variant, "wallpaper_seed")


def test_unknown_factor_raises() -> None:
    base = compose_scene(1, available_apps=POOL)

    with pytest.raises(ValueError):
        counterfactual_variant(base, "not_a_factor", 1)
