"""Deterministic diversity primitives for display and desktop sessions."""

from __future__ import annotations

import random
from dataclasses import replace
from typing import Dict, List

from deskshot.config import DesktopFixtureConfig, DisplayConfig


DISPLAY_PRESETS: Dict[str, Dict[str, int]] = {
    "wxga_1366x768": {"width": 1366, "height": 768},
    "hdplus_1600x900": {"width": 1600, "height": 900},
    "fhd_1920x1080": {"width": 1920, "height": 1080},
    "wuxga_1920x1200": {"width": 1920, "height": 1200},
    "qhd_2560x1440": {"width": 2560, "height": 1440},
    "retina_2880x1800": {"width": 2880, "height": 1800},
    "uhd_3840x2160": {"width": 3840, "height": 2160},
}

PANEL_VARIANTS: List[str] = [
    "top",
    "top_slim",
    "top_slim_dock",
    "top_tall",
    "top_dock",
    "bottom",
    "bottom_slim",
    "bottom_tall",
    "left_dock",
    "left_slim_dock",
]

#: Anchors for one icon arrangement, as (x_ratio, y_ratio, columns). Ratios are
#: of the usable area, so a template means the same thing on a 1366x768 and a
#: 4K screen.
#:
#: Twelve of these leaned left: six anchored their first cluster within 2% of
#: the left edge, which is also where Caja parks anything it places itself. The
#: result read as "icons are always top-left". The set below spreads across the
#: whole surface - right edge, bottom, centre, wide bands, full grids - so
#: position carries as much variety as count does.
DESKTOP_ICON_LAYOUTS: Dict[str, List[tuple]] = {
    # Left-anchored, the classic look. Kept, but no longer the bulk.
    "upper_left_two_col": [(0.02, 0.04, 2)],
    "lower_left_two_col": [(0.02, 0.48, 2)],
    "left_column_tall": [(0.02, 0.04, 1)],
    "staggered_left": [(0.02, 0.04, 1), (0.10, 0.28, 1), (0.04, 0.60, 1)],
    "left_and_mid": [(0.02, 0.04, 1), (0.28, 0.56, 2)],
    "left_spine_right_pocket": [(0.02, 0.05, 1), (0.78, 0.44, 2)],
    # Right-anchored. Windows and macOS users both commonly end up here.
    "right_stack": [(0.86, 0.08, 1)],
    "upper_right_two_col": [(0.76, 0.06, 2)],
    "right_column_tall": [(0.90, 0.04, 1)],
    "lower_right_cluster": [(0.72, 0.54, 2)],
    "mid_right_triplet": [(0.52, 0.18, 1), (0.68, 0.36, 1), (0.80, 0.56, 1)],
    # Centre and bands.
    "center_cluster": [(0.34, 0.12, 3)],
    "center_low": [(0.30, 0.46, 4)],
    "top_band": [(0.05, 0.03, 7)],
    "bottom_band": [(0.08, 0.66, 3)],
    "bottom_wide_band": [(0.04, 0.72, 8)],
    # Spread across the whole surface.
    "dual_edge": [(0.02, 0.04, 1), (0.84, 0.10, 1)],
    "tri_corner": [(0.02, 0.05, 1), (0.76, 0.06, 1), (0.12, 0.62, 2)],
    "four_corners": [
        (0.02, 0.04, 1), (0.88, 0.04, 1), (0.02, 0.66, 1), (0.88, 0.66, 1),
    ],
    "grid_fill": [(0.04, 0.05, 6)],
    "right_two_col_low": [(0.72, 0.42, 2)],
    "center_column": [(0.44, 0.06, 1)],
    "mid_band": [(0.20, 0.34, 5)],
    "scatter_wide": [
        (0.06, 0.08, 1), (0.40, 0.20, 1), (0.72, 0.10, 1),
        (0.22, 0.55, 1), (0.62, 0.64, 1),
    ],
}

#: The names, for callers that only sample one. Derived so the two can never
#: drift: a name listed here but absent above used to fall back to a random
#: pick, silently ignoring the scene's choice.
DESKTOP_LAYOUT_TEMPLATES: List[str] = sorted(DESKTOP_ICON_LAYOUTS)

DESKTOP_CONTENT_PACKS: List[str] = [
    "mixed_default",
    "business_ops",
    "engineering_dev",
    "research_lab",
    "creative_media",
    "personal_home",
]

#: How much lives on the desktop. `empty` is a real and common choice - plenty
#: of people keep a bare desktop - and it is the only profile that shows the
#: wallpaper undisturbed, so without it the corpus never sees one.
_PROFILE_RANGES: Dict[str, Dict[str, tuple[int, int]]] = {
    "empty": {"folders": (0, 0), "files": (0, 0)},
    "sparse": {"folders": (1, 4), "files": (2, 6)},
    "balanced": {"folders": (3, 7), "files": (4, 10)},
    "dense": {"folders": (6, 12), "files": (8, 16)},
    "packed": {"folders": (10, 16), "files": (14, 24)},
}


def list_display_presets() -> Dict[str, Dict[str, int]]:
    """Return serializable display-preset metadata."""
    return dict(DISPLAY_PRESETS)


def get_display_preset(name: str, *, display_number: int = 99, depth: int = 24) -> DisplayConfig:
    """Build a DisplayConfig from a named preset."""
    try:
        preset = DISPLAY_PRESETS[name]
    except KeyError as exc:
        raise ValueError(f"Unknown display preset: {name}") from exc
    return DisplayConfig(
        display_number=display_number,
        width=preset["width"],
        height=preset["height"],
        depth=depth,
    )


def sample_display_preset(seed: int, *, display_number: int = 99, depth: int = 24) -> tuple[str, DisplayConfig]:
    """Pick one curated display preset deterministically from a seed."""
    names = sorted(DISPLAY_PRESETS)
    rng = random.Random(seed)
    name = names[rng.randrange(len(names))]
    return name, get_display_preset(name, display_number=display_number, depth=depth)


def sample_panel_variant(seed: int) -> str:
    """Pick one supported panel variant deterministically from a seed."""
    rng = random.Random(seed)
    return PANEL_VARIANTS[rng.randrange(len(PANEL_VARIANTS))]


def sample_desktop_layout_template(seed: int) -> str:
    """Pick one supported desktop icon-layout template deterministically."""
    rng = random.Random(seed)
    return DESKTOP_LAYOUT_TEMPLATES[rng.randrange(len(DESKTOP_LAYOUT_TEMPLATES))]


def sample_desktop_content_pack(seed: int) -> str:
    """Pick one semantic desktop content pack deterministically."""
    rng = random.Random(seed)
    return DESKTOP_CONTENT_PACKS[rng.randrange(len(DESKTOP_CONTENT_PACKS))]


def resolve_panel_variant(panel_variant: str, desktop_style: str) -> str:
    """Resolve an explicit or style-default panel variant."""
    variant = (panel_variant or "").strip().lower()
    if variant:
        return variant

    style = (desktop_style or "linux").strip().lower()
    if style == "windows":
        return "bottom_tall"
    if style == "macos":
        return "top_slim_dock"
    if style == "ubuntu":
        return "left_dock"
    return "top"


def resolve_desktop_fixture_config(fixture: DesktopFixtureConfig) -> DesktopFixtureConfig:
    """Fill profile-derived file/folder counts when they are unset."""
    profile = (fixture.profile or "balanced").strip().lower()
    ranges = _PROFILE_RANGES.get(profile, _PROFILE_RANGES["balanced"])
    rng = random.Random(fixture.seed)
    # `empty` legitimately wants zero, so an explicit 0 from the profile must
    # not be read as "unset" the way a 0 from the caller is.
    folders = fixture.num_folders or rng.randint(*ranges["folders"])
    files = fixture.num_files or rng.randint(*ranges["files"])
    if profile == "empty":
        folders = files = 0
    return replace(
        fixture,
        profile=profile,
        num_folders=folders,
        num_files=files,
        layout_template=(
            fixture.layout_template.strip().lower()
            or sample_desktop_layout_template(fixture.seed + 211)
        ),
        content_pack=(
            fixture.content_pack.strip().lower()
            or sample_desktop_content_pack(fixture.seed + 307)
        ),
    )
