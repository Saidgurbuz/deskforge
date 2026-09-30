"""Layout families must scale to any count without leaving the screen.

The three invariants here are the ones that turn into bad ground truth when
broken: a window off-screen is annotated at coordinates the screenshot does not
contain, a window below the minimum size holds no content to annotate, and a
layout that is not deterministic makes its scene unreproducible.
"""

import pytest

from deskshot.generation.layouts import (
    LAYOUT_FAMILIES,
    MIN_SIDE,
    build_layout,
    choose_family,
    overlap_ratio,
    work_area,
)

VIEWPORTS = [(1600, 900), (1920, 1080), (1366, 768)]
COUNTS = [1, 2, 3, 4, 5, 6, 7, 8]


@pytest.mark.parametrize("family", sorted(LAYOUT_FAMILIES))
@pytest.mark.parametrize("count", COUNTS)
def test_every_window_stays_inside_the_work_area(family: str, count: int) -> None:
    width, height = 1600, 900
    area = work_area(width, height)

    rects = build_layout(family, count, width=width, height=height, seed=7)

    assert len(rects) == count
    for r in rects:
        assert r.x >= area.x
        assert r.y >= area.y
        assert r.x + r.width <= area.x + area.width
        assert r.y + r.height <= area.y + area.height


@pytest.mark.parametrize("family", sorted(LAYOUT_FAMILIES))
@pytest.mark.parametrize("count", COUNTS)
def test_no_window_is_too_small_to_draw_content(family: str, count: int) -> None:
    rects = build_layout(family, count, width=1600, height=900, seed=11)

    for r in rects:
        assert r.width >= MIN_SIDE, f"{family}/{count}: width {r.width}"
        assert r.height >= MIN_SIDE, f"{family}/{count}: height {r.height}"


@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_eight_windows_fit_on_every_display_preset(viewport) -> None:
    """Eight is the new ceiling, and the smallest preset is where it fails."""
    width, height = viewport
    for family in sorted(LAYOUT_FAMILIES):
        rects = build_layout(family, 8, width=width, height=height, seed=3)
        assert len(rects) == 8
        for r in rects:
            assert r.width >= MIN_SIDE and r.height >= MIN_SIDE


@pytest.mark.parametrize("family", sorted(LAYOUT_FAMILIES))
def test_a_layout_is_deterministic_in_its_seed(family: str) -> None:
    a = build_layout(family, 5, width=1600, height=900, seed=42)
    b = build_layout(family, 5, width=1600, height=900, seed=42)
    c = build_layout(family, 5, width=1600, height=900, seed=43)

    assert [r.as_tuple() for r in a] == [r.as_tuple() for r in b]
    assert [r.as_tuple() for r in a] != [r.as_tuple() for r in c]


def test_windows_are_not_all_the_same_size() -> None:
    """Uniform tiles are the easy case for occlusion and the least realistic."""
    rects = build_layout("grid", 6, width=1600, height=900, seed=5)

    sizes = {(r.width, r.height) for r in rects}

    assert len(sizes) > 1


def test_the_scattered_family_actually_overlaps() -> None:
    """A layout generator that avoids overlap is not exercising the pipeline."""
    ratios = [
        overlap_ratio(build_layout("scattered", 6, width=1600, height=900, seed=s))
        for s in range(6)
    ]

    assert max(ratios) > 0.15, ratios


def test_overlap_is_reported_as_zero_for_a_single_window() -> None:
    assert overlap_ratio(build_layout("grid", 1, width=1600, height=900, seed=1)) == 0.0


def test_an_unknown_family_is_rejected_rather_than_silently_empty() -> None:
    with pytest.raises(ValueError):
        build_layout("herringbone", 3, width=1600, height=900, seed=1)


def test_family_choice_is_deterministic_in_its_rng() -> None:
    import random

    assert choose_family(random.Random(1)) == choose_family(random.Random(1))


def test_realistic_arrangements_are_reachable() -> None:
    """Maximised, centred and snapped-to-halves are what desktops mostly look
    like, and none of them existed before: no family produced them and no
    hand-written table contained them."""
    for name in ("maximized", "centered", "snapped"):
        assert name in LAYOUT_FAMILIES


def test_maximized_fills_the_work_area() -> None:
    area = work_area(1920, 1080)
    rects = build_layout("maximized", 3, width=1920, height=1080, seed=4)

    for rect in rects:
        assert rect.width >= area.width - 40
        assert rect.height >= area.height - 40
    # All but the front window is covered: the extreme occlusion case, and the
    # opposite end of the range from `scattered`.
    assert overlap_ratio(rects) > 0.6


def test_centered_is_actually_centered() -> None:
    area = work_area(1920, 1080)
    rect = build_layout("centered", 1, width=1920, height=1080, seed=9)[0]

    centre_x = rect.x + rect.width / 2
    centre_y = rect.y + rect.height / 2

    assert abs(centre_x - (area.x + area.width / 2)) < 40
    assert abs(centre_y - (area.y + area.height / 2)) < 40
    # Comfortably smaller than the screen, or it would just be maximised.
    assert rect.width < area.width * 0.95


def test_snapped_halves_meet_and_fill_the_width() -> None:
    """The point of `snapped` over `grid` is that the edges line up exactly -
    that is what dragging a window to the screen edge gives you, and a jittered
    grid never produces it."""
    area = work_area(1920, 1080)
    for seed in range(8):
        left, right = build_layout("snapped", 2, width=1920, height=1080, seed=seed)
        assert left.height == right.height == area.height
        gap = right.x - (left.x + left.width)
        assert 0 <= gap <= 8, gap
        assert left.x == area.x
        assert abs((right.x + right.width) - (area.x + area.width)) <= 8


def test_snapped_quarters_are_equal_sized() -> None:
    rects = build_layout("snapped", 4, width=1920, height=1080, seed=3)

    assert len({(r.width, r.height) for r in rects}) == 1
    assert overlap_ratio(rects) == 0.0


def test_common_arrangements_lead_the_weighting_for_few_windows() -> None:
    """Realism says the tidy arrangements dominate; coverage says the
    overlapping ones must stay well represented. Both, not either."""
    from deskshot.generation.layouts import family_weights_for

    for count, expected_leader in ((1, "maximized"), (2, "snapped")):
        weights = family_weights_for(count)
        assert max(weights, key=weights.get) == expected_leader
        hard = weights.get("scattered", 0) + weights.get("cascade", 0)
        assert hard / sum(weights.values()) > 0.2, count

    dense = family_weights_for(8)
    assert dense.get("snapped", 0) == 0, "nobody snaps eight windows"
    assert max(dense, key=dense.get) in {"cascade", "scattered"}


def test_family_weighting_depends_on_window_count() -> None:
    from deskshot.generation.layouts import family_weights_for

    assert family_weights_for(1) != family_weights_for(8)
