"""Settling must be measured, not assumed, and a failure must be reported.

The behaviour here is what separates a capture of a finished screen from one
taken mid-redraw - the cause of five of the six phantom annotations in a
seven-app scene.
"""

from deskshot.generation.readiness import wait_for_apps_to_settle

TARGETS = [("mousepad", "mousepad"), ("thunar", "thunar")]


def _fixed(counts):
    return lambda name: counts[name]


def _sequence(series):
    """A counter that returns the next value per app on each call."""
    state = {k: list(v) for k, v in series.items()}

    def _count(name):
        values = state[name]
        return values.pop(0) if len(values) > 1 else values[0]

    return _count


def test_a_still_scene_settles_immediately() -> None:
    result = wait_for_apps_to_settle(
        TARGETS, counter=_fixed({"mousepad": 40, "thunar": 60}),
        sleep=lambda _s: None, include_desktop_chrome=False,
    )

    assert result["settled"] is True
    # Four polls, not three: the first establishes the baseline and the next
    # three are the ones that confirm it has not moved.
    assert result["rounds"] == 4
    assert result["counts"] == {"mousepad": 40, "thunar": 60}


def test_an_app_still_loading_delays_the_capture() -> None:
    """The real case: gnome-calculator reporting buttons while still drawing."""
    counter = _sequence({"mousepad": [40], "thunar": [10, 30, 55, 60]})

    result = wait_for_apps_to_settle(
        TARGETS, counter=counter, sleep=lambda _s: None, include_desktop_chrome=False
    )

    assert result["settled"] is True
    assert result["rounds"] > 4
    assert result["counts"]["thunar"] == 60


def test_a_scene_that_never_settles_is_reported_not_hidden() -> None:
    """Captured anyway - a late sample beats none - but the record says so, so
    a bad sample is attributable instead of mysterious."""
    ticks = iter(range(0, 2000))
    result = wait_for_apps_to_settle(
        TARGETS,
        counter=lambda name: next(ticks),      # never repeats, never settles
        sleep=lambda _s: None,
        now=lambda: next(ticks) * 0.5,
        include_desktop_chrome=False,
    )

    assert result["settled"] is False
    assert set(result["unsettled"]) == {"mousepad", "thunar"}


def test_the_budget_grows_with_the_number_of_apps() -> None:
    """Eight windows have eight things that can still be drawing."""
    elapsed = {"t": 0.0}

    def _now():
        elapsed["t"] += 1.0
        return elapsed["t"]

    many = [(f"app{i}", f"app{i}") for i in range(8)]
    ticks = iter(range(10_000))
    result = wait_for_apps_to_settle(
        many, counter=lambda name: next(ticks), sleep=lambda _s: None, now=_now,
        include_desktop_chrome=False,
    )

    assert result["settled"] is False
    assert result["waited_sec"] >= 3.0 + 2.5 * 8 - 2


def test_an_app_that_reports_nothing_is_not_treated_as_stable() -> None:
    """A zero count means the app is absent, not that it has finished."""
    ticks = iter(range(0, 400))
    result = wait_for_apps_to_settle(
        [("ghost", "ghost")], counter=lambda name: 0,
        sleep=lambda _s: None, now=lambda: next(ticks) * 0.5,
        include_desktop_chrome=False,
    )

    assert result["settled"] is False
    assert result["unsettled"] == ["ghost"]


def test_a_counting_failure_does_not_abort_the_wait() -> None:
    def _boom(name):
        raise RuntimeError("at-spi hiccup")

    ticks = iter(range(0, 400))
    result = wait_for_apps_to_settle(
        TARGETS, counter=_boom, sleep=lambda _s: None, now=lambda: next(ticks) * 0.5,
        include_desktop_chrome=False,
    )

    assert result["settled"] is False


def test_no_apps_is_settled_by_definition() -> None:
    assert wait_for_apps_to_settle([], include_desktop_chrome=False)["settled"] is True


def test_the_desktop_chrome_is_waited_for_too() -> None:
    """Caja populates the desktop icons on its own schedule, which produced an
    icon annotated where the screenshot had drawn nothing."""
    seen = []

    def _count(name):
        seen.append(name)
        return 10

    wait_for_apps_to_settle([("mousepad", "mousepad")], counter=_count, sleep=lambda _s: None)

    assert "caja" in seen
    assert "mate-panel" in seen


def test_chrome_is_not_polled_twice_when_it_is_already_a_target() -> None:
    seen = []
    wait_for_apps_to_settle(
        [("caja", "caja")], counter=lambda n: seen.append(n) or 5, sleep=lambda _s: None
    )

    assert set(seen) == {"caja", "mate-panel"}      # caja once, not twice


def test_chrome_can_be_left_out() -> None:
    seen = []
    wait_for_apps_to_settle(
        [("mousepad", "mousepad")], include_desktop_chrome=False,
        counter=lambda n: seen.append(n) or 5, sleep=lambda _s: None,
    )

    assert set(seen) == {"mousepad"}
