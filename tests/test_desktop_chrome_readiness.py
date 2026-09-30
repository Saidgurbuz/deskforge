"""A scene with no applications still has to wait for the desktop to paint.

Every capture in the corpus with zero applications came out black with a single
leaf element - 100% of a 40-sample scan, across all four desktop profiles. The
cause was arithmetic: the readiness budget is per application, so a scene with
none of them allowed 3.0 + 2.5 x 2 = 8 seconds, Caja had not finished mapping
its desktop window, and the screenshot caught the bare root window. Launching
even one application takes tens of seconds and hid the bug completely.
"""

from __future__ import annotations

from typing import Dict, List

from deskshot.generation.readiness import (
    DESKTOP_CHROME_MIN_BUDGET,
    wait_for_apps_to_settle,
)


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def _counter(script: Dict[str, List[int]]):
    """Return counts from a per-target script, holding the last value."""
    calls = {k: 0 for k in script}

    def count(atspi_name: str) -> int:
        values = script.get(atspi_name, [0])
        i = min(calls.get(atspi_name, 0), len(values) - 1)
        calls[atspi_name] = calls.get(atspi_name, 0) + 1
        return values[i]

    return count


def test_a_scene_with_no_apps_waits_for_caja_far_past_eight_seconds() -> None:
    """The exact regression: caja silent for 20s, then it paints."""
    clock = _Clock()
    # caja reports nothing for ~20s of polling, then settles at 40 nodes.
    silent = [0] * 45
    result = wait_for_apps_to_settle(
        [],
        counter=_counter({"caja": silent + [40] * 20, "mate-panel": [12] * 80}),
        sleep=clock.sleep,
        now=clock.now,
    )
    assert result["settled"] is True, (
        "a bare-desktop scene gave up before caja had drawn anything"
    )
    assert result["waited_sec"] > 8.0
    assert result["counts"]["caja"] == 40


def test_the_old_budget_would_have_failed_this() -> None:
    """Guard the fix by showing the arithmetic it replaced does not suffice."""
    old_budget = 3.0 + 2.5 * 2
    assert old_budget == 8.0
    assert DESKTOP_CHROME_MIN_BUDGET > old_budget


def test_a_desktop_that_never_appears_still_returns_unsettled() -> None:
    """The wait is bounded: a broken session must not hang the shard."""
    clock = _Clock()
    result = wait_for_apps_to_settle(
        [],
        counter=_counter({"caja": [0] * 500, "mate-panel": [12] * 500}),
        sleep=clock.sleep,
        now=clock.now,
    )
    assert result["settled"] is False
    assert "caja" in result["unsettled"]
    assert result["waited_sec"] >= DESKTOP_CHROME_MIN_BUDGET


def test_apps_present_do_not_shorten_the_chrome_wait() -> None:
    clock = _Clock()
    result = wait_for_apps_to_settle(
        [("mousepad", "mousepad")],
        counter=_counter({
            "mousepad": [30] * 200,
            "caja": [0] * 40 + [40] * 200,
            "mate-panel": [12] * 200,
        }),
        sleep=clock.sleep,
        now=clock.now,
    )
    assert result["settled"] is True
    assert result["counts"]["caja"] == 40


def test_chrome_already_among_the_targets_is_not_added_twice() -> None:
    clock = _Clock()
    result = wait_for_apps_to_settle(
        [("caja", "caja")],
        counter=_counter({"caja": [40] * 50, "mate-panel": [12] * 50}),
        sleep=clock.sleep,
        now=clock.now,
    )
    assert result["settled"] is True
    assert sorted(result["counts"]) == ["caja", "mate-panel"]


def test_no_targets_and_no_chrome_returns_immediately() -> None:
    result = wait_for_apps_to_settle([], include_desktop_chrome=False)
    assert result == {"settled": True, "rounds": 0, "waited_sec": 0.0,
                      "unsettled": [], "counts": {}}
