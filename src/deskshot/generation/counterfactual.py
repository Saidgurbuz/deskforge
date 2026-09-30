"""Counterfactual scene variants: same scene, one factor changed.

A seeded generator can do something no crawl can: hold a scene fixed and vary
exactly one property. That gives paired data where the *only* difference is the
factor under test, which supports two things observational datasets cannot.

Robustness training gets genuine positive pairs — the same layout under a
different theme is the same screen, and a model should parse it the same way.

Diagnostics get a controlled ablation: measure accuracy per varied factor and
you learn whether a model's grounding is driven by appearance or by structure.
That also reframes the themed-Linux limitation as a designed axis of variation
rather than a gap in coverage.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Dict, Iterable, List, Optional, Tuple

from deskshot.generation.scene_composer import (
    SceneConfig,
    scene_session_signature,
    scene_signature,
)

# Factors that change how the screen *looks* without changing which apps are
# present, which windows exist, or where they sit. Element geometry may shift
# with theme metrics, but the scene's content is the same.
APPEARANCE_FACTORS = ("theme_preset", "panel_variant", "wallpaper_seed")

# Changes the raster and therefore all geometry, while keeping content fixed.
GEOMETRY_FACTORS = ("display_preset",)

# Changes what is on the desktop behind the windows.
CONTENT_FACTORS = ("desktop_content_pack", "desktop_layout_template", "desktop_profile")

FACTOR_GROUPS: Dict[str, Tuple[str, ...]] = {
    "appearance": APPEARANCE_FACTORS,
    "geometry": GEOMETRY_FACTORS,
    "content": CONTENT_FACTORS,
}


def _factor_values(factor: str) -> List[object]:
    """Alternative values for a factor, drawn from the sampling tables."""
    from deskshot.generation import scene_composer as sc

    if factor == "theme_preset":
        return list(sc.SCENE_THEME_WEIGHTS)
    if factor == "display_preset":
        return list(sc.SCENE_DISPLAY_WEIGHTS)
    if factor == "desktop_profile":
        return list(sc.SCENE_DESKTOP_PROFILE_WEIGHTS)
    if factor == "panel_variant":
        from deskshot.environment.diversity import PANEL_VARIANTS

        return list(PANEL_VARIANTS)
    if factor == "desktop_content_pack":
        from deskshot.environment.diversity import DESKTOP_CONTENT_PACKS

        return list(DESKTOP_CONTENT_PACKS)
    if factor == "desktop_layout_template":
        from deskshot.environment.diversity import DESKTOP_LAYOUT_TEMPLATES

        return list(DESKTOP_LAYOUT_TEMPLATES)
    if factor == "wallpaper_seed":
        return []          # handled numerically below
    return []


def counterfactual_variant(
    scene: SceneConfig,
    factor: str,
    value: object,
) -> SceneConfig:
    """Return `scene` with exactly one factor replaced.

    The scene id is left to the caller to recompute; keeping the seed lets the
    pair be regenerated, and every other field is untouched by construction.
    """
    if not hasattr(scene, factor):
        raise ValueError(f"Unknown scene factor: {factor}")
    return replace(scene, **{factor: value})


def build_counterfactual_pairs(
    scene: SceneConfig,
    *,
    factors: Optional[Iterable[str]] = None,
    variants_per_factor: int = 1,
) -> List[Tuple[str, SceneConfig]]:
    """Build `(factor, variant)` pairs differing from `scene` in one factor each."""
    chosen = list(factors) if factors else list(APPEARANCE_FACTORS + GEOMETRY_FACTORS)
    pairs: List[Tuple[str, SceneConfig]] = []

    for factor in chosen:
        if factor == "wallpaper_seed":
            for i in range(variants_per_factor):
                pairs.append(
                    (factor, counterfactual_variant(scene, factor, scene.wallpaper_seed + 1 + i))
                )
            continue

        current = getattr(scene, factor)
        alternatives = [v for v in _factor_values(factor) if v != current]
        if not alternatives:
            continue
        # Deterministic pick: offset into the alternatives by the scene seed, so
        # the pair is reproducible without needing a separate RNG.
        for i in range(min(variants_per_factor, len(alternatives))):
            value = alternatives[(scene.seed + i) % len(alternatives)]
            pairs.append((factor, counterfactual_variant(scene, factor, value)))

    return pairs


def pair_differs_only_in(base: SceneConfig, variant: SceneConfig, factor: str) -> bool:
    """True when `variant` differs from `base` in exactly `factor`.

    Used as a guard: a counterfactual that changed anything else is not a
    controlled comparison and would quietly poison the diagnostic.
    """
    from dataclasses import fields

    for field in fields(base):
        if field.name in ("scene_id",):
            continue
        same = getattr(base, field.name) == getattr(variant, field.name)
        if field.name == factor and same:
            return False
        if field.name != factor and not same:
            return False
    return True


def variant_changes_session(base: SceneConfig, variant: SceneConfig) -> bool:
    """Whether a variant needs its own desktop session.

    Appearance and geometry factors live in the session envelope, so those pairs
    cannot share a session; content factors sometimes can. Callers use this to
    decide whether the pair can be captured back-to-back in one env.
    """
    return scene_session_signature(base) != scene_session_signature(variant)


def describe_pair(base: SceneConfig, factor: str, variant: SceneConfig) -> Dict[str, object]:
    return {
        "factor": factor,
        "base_value": getattr(base, factor),
        "variant_value": getattr(variant, factor),
        "base_signature": scene_signature(base),
        "variant_signature": scene_signature(variant),
        "shares_session": not variant_changes_session(base, variant),
    }
