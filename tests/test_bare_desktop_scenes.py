"""A scene with no applications still has to have something on it.

`empty` desktop profile plus zero applications is the one combination that
produces a screen with nothing in it - wallpaper, a panel, two leaf elements -
and it fails `min_elements` every single time. In the 24-scene verification
batch it was the only rejection. The point of a zero-application scene is to
show the desktop unobstructed, which requires something on the desktop.
"""

from __future__ import annotations

from collections import Counter

from deskshot.generation.scene_composer import (
    SCENE_DESKTOP_PROFILE_WEIGHTS,
    compose_scene,
)


def _scenes(n: int, start: int = 8_500_000):
    return [compose_scene(seed) for seed in range(start, start + n)]


def test_no_zero_app_scene_gets_an_empty_desktop() -> None:
    bare = [s for s in _scenes(4000) if not s.apps]
    assert bare, "the sample produced no zero-application scenes at all"
    offenders = [s.seed for s in bare if s.desktop_profile == "empty"]
    assert not offenders, (
        f"{len(offenders)} bare-desktop scenes have an empty icon field and "
        f"would be rejected for min_elements, e.g. seeds {offenders[:5]}"
    )


def test_bare_desktop_scenes_still_span_the_other_profiles() -> None:
    """The re-draw must not collapse onto one profile."""
    bare = [s for s in _scenes(6000) if not s.apps]
    seen = Counter(s.desktop_profile for s in bare)
    assert len(seen) >= 3, f"bare desktops only used {dict(seen)}"
    assert "empty" not in seen


def test_scenes_with_windows_keep_the_empty_profile() -> None:
    """`empty` is a real desktop; it is only degenerate with nothing on top."""
    withapps = [s for s in _scenes(3000) if s.apps]
    assert any(s.desktop_profile == "empty" for s in withapps), (
        "the empty profile was removed everywhere, not just for bare desktops"
    )


def test_the_profile_table_still_contains_empty() -> None:
    assert "empty" in SCENE_DESKTOP_PROFILE_WEIGHTS


def test_the_redraw_is_deterministic() -> None:
    """Same seed, same profile - the fix must not break regeneration."""
    bare = [s for s in _scenes(2000) if not s.apps][:6]
    assert bare
    for scene in bare:
        assert compose_scene(scene.seed).desktop_profile == scene.desktop_profile
