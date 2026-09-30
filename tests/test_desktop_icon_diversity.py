"""Where desktop icons go, and how many there are.

The reported symptom was that icons are "mostly in the same top left location".
Two causes: Caja's own volume icons always sat in the left column (removed
elsewhere), and six of the twelve layout templates anchored their first cluster
within 2% of the left edge.
"""

from __future__ import annotations

from deskshot.config import DesktopFixtureConfig
from deskshot.environment.desktop_fixture import compute_icon_positions
from deskshot.environment.diversity import (
    DESKTOP_ICON_LAYOUTS,
    DESKTOP_LAYOUT_TEMPLATES,
    resolve_desktop_fixture_config,
)


def test_every_named_template_is_implemented() -> None:
    """A name with no implementation used to fall back to a random pick.

    That silently ignored the scene's choice, so a run could not reproduce its
    own icon layout.
    """
    assert set(DESKTOP_LAYOUT_TEMPLATES) == set(DESKTOP_ICON_LAYOUTS)
    assert len(DESKTOP_LAYOUT_TEMPLATES) >= 20


def test_templates_reach_the_whole_screen() -> None:
    """Measured on the positions icons actually land on, not on the anchors.

    An anchor at the left edge does not mean the icons stay there - `grid_fill`
    and `bottom_wide_band` both start left and run the width of the screen. So
    the question is where the icons end up: how many templates confine every
    icon to the left third, and whether the set as a whole covers the surface.
    """
    width, height = 1920, 1080
    confined_left = 0
    all_x: list[float] = []
    all_y: list[float] = []
    for name in DESKTOP_ICON_LAYOUTS:
        positions = compute_icon_positions(
            12, seed=5, display_width=width, display_height=height, layout_template=name
        )["positions"]
        xs = [x / width for x, _y in positions]
        ys = [y / height for _x, y in positions]
        all_x.extend(xs)
        all_y.extend(ys)
        if max(xs) < 1 / 3:
            confined_left += 1

    # The set covers the surface.
    assert min(all_x) < 0.1 and max(all_x) > 0.75
    assert min(all_y) < 0.1 and max(all_y) > 0.6
    # And a minority of templates keep everything in the left third. Six of the
    # original twelve did, which is what "always top left" looked like.
    assert confined_left / len(DESKTOP_ICON_LAYOUTS) < 0.4


def test_positions_stay_on_screen_and_honour_the_chosen_template() -> None:
    for name in DESKTOP_LAYOUT_TEMPLATES:
        result = compute_icon_positions(
            14,
            seed=11,
            display_width=1366,
            display_height=768,
            panel_variant="left_dock",
            layout_template=name,
        )
        assert result["layout"] == name
        assert len(result["positions"]) == 14
        for x, y in result["positions"]:
            assert 0 <= x < 1366 and 0 <= y < 768


def test_positions_are_deterministic() -> None:
    kwargs = dict(seed=7, display_width=1920, display_height=1080, layout_template="scatter_wide")
    assert compute_icon_positions(9, **kwargs) == compute_icon_positions(9, **kwargs)


def test_item_counts_span_empty_to_packed() -> None:
    totals = {}
    for profile in ("empty", "sparse", "balanced", "dense", "packed"):
        resolved = resolve_desktop_fixture_config(
            DesktopFixtureConfig(seed=3, profile=profile)
        )
        totals[profile] = resolved.num_folders + resolved.num_files
    assert totals["empty"] == 0
    assert totals["sparse"] < totals["balanced"] < totals["dense"] < totals["packed"]
    assert totals["packed"] >= 24
