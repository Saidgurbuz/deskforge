"""Launch floors must survive the conditions a batch actually runs in.

Measured in a four-worker batch: 28 of 32 scenes failed, 16 of them because
Chromium hit a 24-second ceiling while every other app was given 90. A browser
is the heaviest thing in the pool, so giving it the least time was backwards.
"""

import os

import pytest

from deskshot.generation.scene_composer import (
    _BROWSER_LAUNCH_TIMEOUT_FLOOR,
    _SCENE_LAUNCH_TIMEOUT_FLOOR,
    _scene_launch_timeout_floor,
    launch_timeout_scale_for_workers,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("DESKSHOT_LAUNCH_TIMEOUT_SCALE", raising=False)
    monkeypatch.delenv("DESKSHOT_SCENE_LAUNCH_TIMEOUT", raising=False)


def test_a_browser_gets_at_least_as_long_as_any_other_app() -> None:
    assert _BROWSER_LAUNCH_TIMEOUT_FLOOR >= _SCENE_LAUNCH_TIMEOUT_FLOOR
    assert _scene_launch_timeout_floor("chromium-browser") >= _scene_launch_timeout_floor("mousepad")


def test_a_single_worker_is_not_penalised() -> None:
    assert launch_timeout_scale_for_workers(1) == 1.0
    assert launch_timeout_scale_for_workers(0) == 1.0


def test_more_workers_means_more_time() -> None:
    assert launch_timeout_scale_for_workers(4) > launch_timeout_scale_for_workers(2) > 1.0


def test_the_scale_is_capped() -> None:
    """A stuck app should still fail rather than hold a worker indefinitely."""
    assert launch_timeout_scale_for_workers(64) <= 3.0


def test_the_scale_is_applied_to_the_floor(monkeypatch) -> None:
    monkeypatch.setenv("DESKSHOT_LAUNCH_TIMEOUT_SCALE", "2.0")

    assert _scene_launch_timeout_floor("mousepad") == _SCENE_LAUNCH_TIMEOUT_FLOOR * 2.0


def test_an_invalid_scale_is_ignored(monkeypatch) -> None:
    monkeypatch.setenv("DESKSHOT_LAUNCH_TIMEOUT_SCALE", "soon")

    assert _scene_launch_timeout_floor("mousepad") == _SCENE_LAUNCH_TIMEOUT_FLOOR


def test_an_explicit_override_still_wins(monkeypatch) -> None:
    monkeypatch.setenv("DESKSHOT_SCENE_LAUNCH_TIMEOUT", "12")

    assert _scene_launch_timeout_floor("chromium-browser") == 12.0
