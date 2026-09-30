"""Batches that pin the window count, for targeted diversity subsets.

Two are needed to top the corpus up past a million usable samples without
another generic run: single-window Chromium with a flat page draw, and a bare
desktop with no windows at all. Both are static, so every capture counts - an
episode loses ~10% of its frames to no-op clicks.
"""

from __future__ import annotations

import re
from collections import Counter

import pytest

from deskshot.generation.scene_composer import compose_scene


def _chrome(seed: int):
    return compose_scene(seed, available_apps=["chromium-browser"],
                         force_app_count=1, uniform_pages=True)


def test_a_one_app_pool_is_allowed_when_the_count_is_pinned() -> None:
    """The two-app floor exists to *mix* apps; pinned to one there is no mix."""
    scene = _chrome(9_200_000)
    assert [a.app_name for a in scene.apps] == ["chromium-browser"]


def test_a_one_app_pool_is_still_rejected_for_a_mixed_scene() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        compose_scene(9_200_000, available_apps=["chromium-browser"])


def test_pages_are_near_unique_under_uniform_sampling() -> None:
    doms = Counter()
    for seed in range(9_300_000, 9_300_400):
        for a in _chrome(seed).apps:
            m = re.match(r"catalog:([^:]+):", a.state_ref or "")
            if m:
                doms[m.group(1)] += 1
    assert len(doms) > 0.9 * sum(doms.values()), (
        f"only {len(doms)} domains over {sum(doms.values())} scenes"
    )
    top = doms.most_common(1)[0][1] / sum(doms.values())
    assert top < 0.05, f"top domain took {100*top:.1f}%"


def test_every_chromium_scene_gets_a_catalog_page() -> None:
    """Without uniform the browser takes a catalog page only 74% of the time."""
    for seed in range(9_400_000, 9_400_120):
        for a in _chrome(seed).apps:
            assert (a.state_ref or "").startswith("catalog:"), a.state_ref


def test_the_envelope_still_varies_fully() -> None:
    themes, res, profiles = Counter(), Counter(), Counter()
    for seed in range(9_500_000, 9_500_300):
        s = _chrome(seed)
        themes[s.theme_preset] += 1
        res[s.display_preset] += 1
        profiles[s.desktop_profile] += 1
    assert len(themes) == 7 and len(res) == 7
    assert len(profiles) >= 4


def test_zero_app_batches_still_work_and_avoid_the_empty_desktop() -> None:
    seen = Counter()
    for seed in range(9_600_000, 9_600_400):
        s = compose_scene(seed, force_app_count=0)
        assert s.apps == []
        seen[s.desktop_profile] += 1
    assert "empty" not in seen, "a bare desktop with no icons fails min_elements"
    assert len(seen) >= 3


def test_uniform_pages_is_off_by_default() -> None:
    """The general corpus keeps its popularity prior."""
    doms = Counter()
    for seed in range(9_700_000, 9_700_600):
        s = compose_scene(seed, available_apps=["chromium-browser", "mousepad"],
                          force_app_count=1)
        for a in s.apps:
            m = re.match(r"catalog:([^:]+):", a.state_ref or "")
            if m:
                doms[m.group(1)] += 1
    if sum(doms.values()) > 40:
        top = doms.most_common(1)[0][1] / sum(doms.values())
        assert top > 0.03, "the default prior looks flat; uniform leaked in"
