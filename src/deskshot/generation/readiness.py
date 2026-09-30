"""Wait for a scene to stop changing before capturing it.

A fixed sleep is a bet that every app finished drawing in the same time, and the
bet gets worse as the scene gets denser. Measured on a seven-app scene: five of
its six phantom annotations were gnome-calculator buttons that AT-SPI was
already reporting while the window was still rendering, plus one desktop icon
that Caja had not yet drawn. Both are the same failure - the shutter fired
during a redraw - and both produce ground truth describing a screen that was
never shown.

So the wait is a measurement instead. Each app's subtree is counted repeatedly,
and the scene is settled once every count has held still for a few consecutive
polls. An app that never settles is reported rather than silently captured, so a
bad sample is attributable instead of mysterious.

The counting is deliberately cheap: node counts, not built elements. The full
extraction runs once, afterwards, on a screen that has stopped moving.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Consecutive polls a count must hold before it is believed. Two is not enough:
#: an app that loads its content in stages sits still between stages.
DEFAULT_STABLE_ROUNDS = 3

#: Seconds between polls. Short enough that a quick scene is not delayed, long
#: enough that a redraw lands between two of them rather than inside one.
DEFAULT_POLL_INTERVAL = 0.45

#: Budget is per app rather than per scene, because a scene with eight windows
#: has eight things that can still be drawing.
DEFAULT_BASE_TIMEOUT = 3.0
DEFAULT_PER_APP_TIMEOUT = 2.5

#: The desktop is drawn by its own processes, and they settle on their own
#: schedule. Caja in particular populates the desktop icons after the session
#: reports ready, which put icons in the ground truth that the screenshot had
#: not drawn yet - the one phantom left in a dense scene after popups were
#: fixed, and the source of the icon-count nondeterminism measured earlier.
DESKTOP_CHROME_TARGETS: Tuple[Tuple[str, str], ...] = (
    ("caja", "caja"),
    ("mate-panel", "mate-panel"),
)

#: The floor on the wait when the desktop chrome is part of what must be ready.
#:
#: The per-app budget is the wrong shape for the desktop: it scales with the
#: number of applications, and a scene with none of them got 3.0 + 2.5 x 2 = 8
#: seconds. Caja needs longer than that to map its desktop window and paint the
#: wallpaper, so the wait timed out with caja reporting zero nodes and the
#: screenshot was taken of a bare black root window. Every single capture in
#: the corpus with no applications came out that way - 100% of a 40-sample
#: scan, black, one leaf element, across all four desktop profiles - and the
#: bare desktop is a scene type the plan asks for deliberately.
#:
#: Applications hid the bug: launching even one of them takes tens of seconds,
#: which is time Caja spends painting.
DESKTOP_CHROME_MIN_BUDGET = 30.0


def count_app_nodes(atspi_name: str, *, max_nodes: int = 4000) -> int:
    """Number of accessible nodes in an app's subtree.

    Counts rather than builds: the point is to notice change, and a count
    changes whenever the tree does. `max_nodes` bounds the walk so a runaway
    tree cannot make the readiness check cost more than the capture it protects.
    """
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except Exception:  # pragma: no cover - only on a machine without AT-SPI
        return 0

    desktop = Atspi.get_desktop(0)
    if desktop is None:
        return 0

    root = None
    for index in range(desktop.get_child_count()):
        child = desktop.get_child_at_index(index)
        if child is None:
            continue
        name = child.get_name() or ""
        if atspi_name.lower() in name.lower():
            root = child
            break
    if root is None:
        return 0

    total = 0
    stack = [root]
    while stack and total < max_nodes:
        node = stack.pop()
        total += 1
        try:
            for index in range(node.get_child_count()):
                child = node.get_child_at_index(index)
                if child is not None:
                    stack.append(child)
        except Exception:
            continue
    return total


def wait_for_apps_to_settle(
    targets: Sequence[Tuple[str, str]],
    *,
    include_desktop_chrome: bool = True,
    counter: Optional[Callable[[str], int]] = None,
    stable_rounds: int = DEFAULT_STABLE_ROUNDS,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    base_timeout: float = DEFAULT_BASE_TIMEOUT,
    per_app_timeout: float = DEFAULT_PER_APP_TIMEOUT,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> Dict[str, object]:
    """Poll until every app's node count holds still, or the budget runs out.

    `targets` is the `(atspi_name, app_name)` pairs of the launched scene.
    Returns a diagnostic record - it goes into the capture's metadata, so a
    sample taken from an unsettled scene can be found later rather than guessed
    at.
    """
    count = counter or count_app_nodes
    targets = list(targets)
    chrome_added = False
    if include_desktop_chrome:
        known = {name for _atspi, name in targets}
        chrome = [t for t in DESKTOP_CHROME_TARGETS if t[1] not in known]
        targets += chrome
        chrome_added = bool(chrome)
    if not targets:
        return {"settled": True, "rounds": 0, "waited_sec": 0.0, "unsettled": [], "counts": {}}

    budget = base_timeout + per_app_timeout * len(targets)
    if chrome_added:
        # The desktop's own processes do not settle faster because there is
        # less to wait for beside them.
        budget = max(budget, DESKTOP_CHROME_MIN_BUDGET)
    started = now()
    previous: Dict[str, int] = {}
    steady: Dict[str, int] = {name: 0 for _atspi, name in targets}
    rounds = 0

    while True:
        rounds += 1
        current: Dict[str, int] = {}
        for atspi_name, app_name in targets:
            try:
                value = count(atspi_name)
            except Exception:
                logger.warning("readiness: could not count %s", atspi_name, exc_info=True)
                value = -1
            current[app_name] = value
            if previous.get(app_name) == value and value > 0:
                steady[app_name] += 1
            else:
                steady[app_name] = 0
        previous = current

        unsettled = [name for name, hits in steady.items() if hits < stable_rounds]
        waited = now() - started
        if not unsettled:
            logger.info(
                "readiness: scene settled after %.1fs (%d polls), counts=%s",
                waited, rounds, current,
            )
            return {
                "settled": True, "rounds": rounds, "waited_sec": round(waited, 2),
                "unsettled": [], "counts": current,
            }
        if waited >= budget:
            logger.warning(
                "readiness: %s still changing after %.1fs; capturing anyway",
                ", ".join(unsettled), waited,
            )
            return {
                "settled": False, "rounds": rounds, "waited_sec": round(waited, 2),
                "unsettled": unsettled, "counts": current,
            }
        sleep(poll_interval)
