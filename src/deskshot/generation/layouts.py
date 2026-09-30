"""Window layouts that scale to any number of apps.

The hand-authored layouts cover two to four windows, one branch per
(count, name) pair. Extending that to eight by hand means about thirty more
branches, each a place for an off-by-one to hide, and it fixes the diversity at
whatever someone thought to write down.

These are families instead: each takes a count and a seed and produces that many
rectangles. Size variation is part of the family rather than a decoration,
because uniform tiles are the easy case for occlusion resolution and the one
least like a real desktop. What they deliberately do produce is **overlap** -
windows partially covering each other is the case the annotation pipeline has to
get right, and a layout generator that avoids it is testing nothing.

Three invariants hold for every family, and are tested:

*On screen*  - every rectangle lies inside the work area, so no window is
               annotated at coordinates the screenshot does not contain.
*Usable*     - no rectangle is smaller than `MIN_SIDE`, below which an app
               refuses to draw its content and the sample is empty.
*Deterministic* - the same (family, count, seed, viewport) gives the same
               rectangles, so a scene stays reproducible.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable, Dict, List, Tuple

#: Below this an app stops laying out its content and the capture is worthless.
MIN_SIDE = 260


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    def as_tuple(self) -> Tuple[int, int, int, int]:
        return (self.x, self.y, self.width, self.height)


def _clamp(rect: Rect, area: Rect) -> Rect:
    """Pull a rectangle back inside the work area, shrinking only if it must."""
    width = max(MIN_SIDE, min(rect.width, area.width))
    height = max(MIN_SIDE, min(rect.height, area.height))
    x = min(max(rect.x, area.x), area.x + area.width - width)
    y = min(max(rect.y, area.y), area.y + area.height - height)
    return Rect(int(x), int(y), int(width), int(height))


def _jitter(rng: random.Random, value: int, fraction: float) -> int:
    """Vary a dimension by up to `fraction`, so tiles are not identical."""
    delta = int(value * fraction)
    return value + rng.randint(-delta, delta) if delta > 0 else value


def _grid_shape(count: int) -> Tuple[int, int]:
    """Columns and rows for `count` windows, wider than tall, as screens are."""
    cols = 1
    while cols * cols < count:
        cols += 1
    rows = (count + cols - 1) // cols
    return cols, rows


def grid(count: int, area: Rect, rng: random.Random) -> List[Rect]:
    """Tiles on a grid, each varied in size so edges do not all line up."""
    cols, rows = _grid_shape(count)
    cell_w = area.width // cols
    cell_h = area.height // rows
    out: List[Rect] = []
    for index in range(count):
        col, row = index % cols, index // cols
        width = _jitter(rng, int(cell_w * 0.96), 0.10)
        height = _jitter(rng, int(cell_h * 0.94), 0.12)
        x = area.x + col * cell_w + rng.randint(0, max(1, cell_w // 12))
        y = area.y + row * cell_h + rng.randint(0, max(1, cell_h // 12))
        out.append(_clamp(Rect(x, y, width, height), area))
    return out


def cascade(count: int, area: Rect, rng: random.Random) -> List[Rect]:
    """Overlapping windows stepped down and right, as a person stacks them."""
    step_x = max(40, area.width // (count * 3))
    step_y = max(34, area.height // (count * 3))
    width = int(area.width * 0.62)
    height = int(area.height * 0.68)
    out: List[Rect] = []
    for index in range(count):
        out.append(_clamp(
            Rect(
                area.x + index * step_x,
                area.y + index * step_y,
                _jitter(rng, width, 0.08),
                _jitter(rng, height, 0.08),
            ),
            area,
        ))
    return out


def primary_with_stack(count: int, area: Rect, rng: random.Random) -> List[Rect]:
    """One large window and the rest stacked beside it, the common real layout."""
    if count == 1:
        return [_clamp(Rect(area.x, area.y, area.width, area.height), area)]
    primary_w = int(area.width * rng.uniform(0.52, 0.62))
    rest = count - 1
    side_w = area.width - primary_w
    slot_h = area.height // rest
    out = [_clamp(Rect(area.x, area.y, primary_w, area.height), area)]
    for index in range(rest):
        out.append(_clamp(
            Rect(
                area.x + primary_w + rng.randint(-24, 8),
                area.y + index * slot_h + rng.randint(0, max(1, slot_h // 10)),
                _jitter(rng, side_w, 0.06),
                _jitter(rng, int(slot_h * 0.94), 0.10),
            ),
            area,
        ))
    return out


def scattered(count: int, area: Rect, rng: random.Random) -> List[Rect]:
    """Freely placed windows of differing sizes, with substantial overlap.

    The hardest case for occlusion resolution, and the reason this family
    exists: windows cover each other partially and at arbitrary offsets rather
    than along a grid line.
    """
    out: List[Rect] = []
    for _ in range(count):
        width = int(area.width * rng.uniform(0.34, 0.66))
        height = int(area.height * rng.uniform(0.38, 0.72))
        out.append(_clamp(
            Rect(
                area.x + rng.randint(0, max(1, area.width - width)),
                area.y + rng.randint(0, max(1, area.height - height)),
                width,
                height,
            ),
            area,
        ))
    return out


def columns(count: int, area: Rect, rng: random.Random) -> List[Rect]:
    """Full-height columns of uneven width, like tiled editors."""
    weights = [rng.uniform(0.8, 1.4) for _ in range(count)]
    total = sum(weights)
    out: List[Rect] = []
    cursor = area.x
    for weight in weights:
        width = int(area.width * weight / total)
        out.append(_clamp(
            Rect(cursor, area.y + rng.randint(0, 18), width, _jitter(rng, area.height, 0.06)),
            area,
        ))
        cursor += width
    return out


def maximized(count: int, area: Rect, rng: random.Random) -> List[Rect]:
    """Every window filling the work area, stacked.

    The single most common way people actually work - one window maximised, the
    others behind it - and it was unreachable before: no family produced it and
    no hand-written table contained it. For the pipeline it is also the extreme
    occlusion case, where all but the front window is entirely covered, which is
    the opposite end from `scattered` and just as worth testing.

    A few pixels of variation per window, so the stack is not one rectangle
    repeated and the occlusion logic has real edges to resolve.
    """
    out: List[Rect] = []
    for index in range(count):
        inset = rng.randint(0, 6) + index * rng.randint(0, 3)
        out.append(_clamp(
            Rect(
                area.x + inset,
                area.y + inset,
                area.width - inset * 2,
                area.height - inset * 2,
            ),
            area,
        ))
    return out


def centered(count: int, area: Rect, rng: random.Random) -> List[Rect]:
    """Windows centred on the screen, each a little smaller than the last.

    What a window manager does when it is not told otherwise, and what a person
    does with a single window they are not trying to tile: centred, comfortably
    smaller than the screen. Extra windows sit centred over it in the shape a
    stack of dialogs makes.
    """
    out: List[Rect] = []
    for index in range(count):
        shrink = 1.0 - index * 0.09
        width = int(area.width * rng.uniform(0.62, 0.86) * shrink)
        height = int(area.height * rng.uniform(0.60, 0.88) * shrink)
        out.append(_clamp(
            Rect(
                area.x + (area.width - width) // 2 + rng.randint(-28, 28),
                area.y + (area.height - height) // 2 + rng.randint(-24, 24),
                width,
                height,
            ),
            area,
        ))
    return out


def snapped(count: int, area: Rect, rng: random.Random) -> List[Rect]:
    """The screen divided evenly, the way edge-snapping divides it.

    One window fills it, two take a half each, three are a half plus two
    quarters, four are quarters. Unlike `grid`, the edges line up exactly and
    the sizes are equal - that is the point, because it is what a person gets
    from dragging a window to the edge, and a jittered grid never produces it.

    The split point moves a little, since a person drags the divider.
    """
    if count <= 1:
        return [_clamp(Rect(area.x, area.y, area.width, area.height), area)]

    gap = rng.choice([0, 0, 0, 2, 6])
    split = int(area.width * rng.uniform(0.44, 0.56))
    left_w = split - gap // 2
    right_x = area.x + split + gap // 2
    right_w = area.width - split - gap // 2

    if count == 2:
        return [
            _clamp(Rect(area.x, area.y, left_w, area.height), area),
            _clamp(Rect(right_x, area.y, right_w, area.height), area),
        ]

    if count == 3:
        mid = int(area.height * rng.uniform(0.44, 0.56))
        return [
            _clamp(Rect(area.x, area.y, left_w, area.height), area),
            _clamp(Rect(right_x, area.y, right_w, mid - gap // 2), area),
            _clamp(Rect(right_x, area.y + mid + gap // 2, right_w,
                        area.height - mid - gap // 2), area),
        ]

    # Five and up do not snap to anything a desktop offers, so they fall back to
    # an even split of the same shape rather than pretending otherwise. The gap
    # and the outer margin vary here rather than the cell sizes: equal cells are
    # the whole point of the family, but two scenes at different seeds should
    # still not be byte-identical.
    cols, rows = (2, 2) if count == 4 else _grid_shape(count)
    if count > 4:
        gap = rng.choice([0, 2, 4, 6, 8, 12])
    margin = rng.randint(0, 12)
    inner = Rect(
        area.x + margin, area.y + margin,
        max(MIN_SIDE, area.width - margin * 2),
        max(MIN_SIDE, area.height - margin * 2),
    )
    cell_w = (inner.width - gap * (cols - 1)) // cols
    cell_h = (inner.height - gap * (rows - 1)) // rows
    out: List[Rect] = []
    for index in range(count):
        col, row = index % cols, index // cols
        out.append(_clamp(
            Rect(
                inner.x + col * (cell_w + gap),
                inner.y + row * (cell_h + gap),
                cell_w,
                cell_h,
            ),
            area,
        ))
    return out


#: Every family, by name. Weights are applied by the caller.
LAYOUT_FAMILIES: Dict[str, Callable[[int, Rect, random.Random], List[Rect]]] = {
    "maximized": maximized,
    "centered": centered,
    "snapped": snapped,
    "grid": grid,
    "cascade": cascade,
    "primary_with_stack": primary_with_stack,
    "scattered": scattered,
    "columns": columns,
}

#: How often each family is chosen, **by window count**, because what is common
#: depends on how many windows there are: one window is usually maximised or
#: centred, two are usually snapped to halves, and eight are never any of those.
#: A single flat table put `scattered` at 24% for every count, which is how a
#: corpus ends up looking like nothing anybody's desktop looks like.
#:
#: Two things are being balanced. Realism says the tidy arrangements dominate -
#: people mostly do not leave windows at arbitrary offsets. Coverage says the
#: overlapping ones have to stay well represented, because partial occlusion is
#: the case the annotation pipeline most needs to be exercised on and a tiled
#: layout never produces it. So the common cases lead and the hard cases keep a
#: quarter to a third of the mass, rather than either winning outright.
FAMILY_WEIGHTS_BY_COUNT: Dict[int, Dict[str, int]] = {
    # One window: maximised or centred is nearly all of real life. `cascade` and
    # `scattered` degenerate to "somewhere else on the screen", which does
    # happen, so they keep a modest share.
    1: {"maximized": 30, "centered": 28, "scattered": 16, "cascade": 14, "grid": 12},
    # Two: halves is what edge-snapping gives, and what people reach for when
    # comparing two things. Then one-big-one-small, then overlap.
    2: {"snapped": 30, "primary_with_stack": 20, "cascade": 14, "maximized": 12,
        "scattered": 12, "grid": 8, "centered": 4},
    3: {"snapped": 22, "primary_with_stack": 22, "grid": 18, "cascade": 16,
        "scattered": 16, "columns": 6},
    4: {"snapped": 22, "grid": 22, "primary_with_stack": 20, "cascade": 16,
        "scattered": 16, "columns": 4},
}

#: Five windows and up. Nobody snaps eight windows, and nobody centres them
#: either; at this density a desktop is a cascade or a mess, so those lead.
FAMILY_WEIGHTS_DENSE: Dict[str, int] = {
    "cascade": 26,
    "scattered": 24,
    "grid": 22,
    "primary_with_stack": 16,
    "maximized": 8,
    "columns": 4,
}

#: Kept for callers that do not know the count.
FAMILY_WEIGHTS: Dict[str, int] = FAMILY_WEIGHTS_DENSE


def work_area(width: int, height: int) -> Rect:
    """The screen minus the panel and dock reserves the chrome occupies."""
    top = max(64, height // 20)
    bottom = max(80, height // 13)
    side = max(24, width // 60)
    return Rect(side, top, width - side * 2, height - top - bottom)


def build_layout(
    family: str,
    count: int,
    *,
    width: int,
    height: int,
    seed: int,
) -> List[Rect]:
    """`count` window rectangles from a named family, deterministic in `seed`."""
    if family not in LAYOUT_FAMILIES:
        raise ValueError(f"unknown layout family: {family!r}")
    if count < 1:
        return []
    area = work_area(width, height)
    return LAYOUT_FAMILIES[family](count, area, random.Random(seed))


def family_weights_for(count: int) -> Dict[str, int]:
    """The weighting that applies to a scene with `count` windows."""
    return FAMILY_WEIGHTS_BY_COUNT.get(max(1, count), FAMILY_WEIGHTS_DENSE)


def choose_family(rng: random.Random, count: int = 5) -> str:
    weights = family_weights_for(count)
    names = list(weights)
    return rng.choices(names, weights=[weights[n] for n in names], k=1)[0]


def overlap_ratio(rects: List[Rect]) -> float:
    """Fraction of total window area that is covered by a later window.

    Reported so a batch can be checked for actually containing the hard case,
    rather than assumed to.
    """
    if len(rects) < 2:
        return 0.0
    total = sum(r.width * r.height for r in rects)
    covered = 0
    for i, a in enumerate(rects):
        for b in rects[i + 1:]:
            x0 = max(a.x, b.x)
            y0 = max(a.y, b.y)
            x1 = min(a.x + a.width, b.x + b.width)
            y1 = min(a.y + a.height, b.y + b.height)
            if x1 > x0 and y1 > y0:
                covered += (x1 - x0) * (y1 - y0)
    return round(min(1.0, covered / max(1, total)), 4)
