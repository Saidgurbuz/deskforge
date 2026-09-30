"""Thin rules are decoration; the coverage audit must not score them.

Measured over the 12-scene batch in `incremental_checks/v213_combined`, ink in
runs at most 3px thick and at least 20px long was 50-99% of every app's flagged
uncovered ink - baobab 94%, gnome-logs 99%, nautilus 90%, file-roller 98%. It is
window borders, GTK pane dividers, toolbar edges and table grid lines: how a
container draws its own edge, not a widget. A window's own border is flagged
only because this audit deliberately excludes the frame rect from coverage so it
can ask about the window's *contents*.

The discount has to be narrow enough that it never hides a real gap. The two
gaps this audit did catch - HomeBank's 11x11 expander triangle and xarchiver's
16x16 folder icon - are the reference cases below.
"""

import importlib.util
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "audit_element_coverage", PROJECT_ROOT / "scripts" / "audit_element_coverage.py"
)
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)


def _naive_run_lengths(mask, axis):
    m = mask if axis == 1 else mask.T
    out = np.zeros_like(m, dtype=np.int32)
    for i, row in enumerate(m):
        idx = np.flatnonzero(row)
        if idx.size == 0:
            continue
        for run in np.split(idx, np.where(np.diff(idx) != 1)[0] + 1):
            out[i, run] = len(run)
    return out if axis == 1 else out.T


def _window_png(tmp_path, extra=None):
    """A 200x200 window with a 1px border and one annotated black widget."""
    img = Image.new("L", (200, 200), 255)
    draw = ImageDraw.Draw(img)
    draw.rectangle([10, 10, 189, 189], outline=0, width=1)
    draw.rectangle([40, 40, 69, 59], fill=0)
    if extra is not None:
        extra(draw)
    path = tmp_path / "capture.png"
    img.save(path)
    return path


def _elements():
    return [
        {"role": "frame", "app_name": "probe", "name": "Probe",
         "rect": {"x": 10, "y": 10, "w": 180, "h": 180}},
        {"role": "push button", "app_name": "probe",
         "rect": {"x": 38, "y": 38, "w": 34, "h": 24}},
    ]


def _audit(tmp_path, name, extra=None):
    directory = tmp_path / name
    directory.mkdir()
    return audit.audit_capture(
        _window_png(directory, extra=extra), _elements()
    )["windows"][0]


def test_run_lengths_matches_a_naive_scan() -> None:
    """The vectorised run-length fill is the whole discount; a per-run Python
    loop over a 2560x1440 mask takes minutes, so it had to be replaced, and a
    wrong replacement would silently mis-classify every pixel."""
    rng = np.random.default_rng(7)
    mask = rng.random((40, 60)) < 0.35

    for axis in (0, 1):
        assert np.array_equal(
            audit._run_lengths(mask, axis), _naive_run_lengths(mask, axis)
        )


def test_a_window_border_counts_as_decoration_not_a_missing_widget(tmp_path) -> None:
    """The frame rect is excluded from coverage on purpose, so the window's own
    1px border always lands in the flagged mask. It is drawn by the frame, which
    is annotated.

    The residual is the border's four corners: a corner pixel sits in a long run
    in *both* directions, so it is thin in neither. Four pixels per intersection
    is noise - it must not be enough to report a gap."""
    row = _audit(tmp_path, "plain")

    assert row["gaps"] == []
    assert row["decoration_ink"] > 0
    assert row["uncovered_ink"] < 0.02 * row["decoration_ink"]


def test_decoration_is_reported_rather_than_silently_dropped(tmp_path) -> None:
    """A metric that hides what it decided not to measure cannot be checked."""
    row = _audit(tmp_path, "plain")

    assert row["decoration_ratio"] > 0
    assert row["ink"] >= row["uncovered_ink"] + row["decoration_ink"]


def test_an_expander_sized_gap_survives_the_discount(tmp_path) -> None:
    """HomeBank's disclosure triangle is 11px across (measured: drawn at
    x=188..198 on the row at y=240). Discounting it would have hidden the
    very gap this audit was built to find."""
    def triangle(draw):
        draw.polygon([(120, 120), (131, 120), (125, 130)], fill=0)

    baseline = _audit(tmp_path, "plain")
    row = _audit(tmp_path, "expander", extra=triangle)

    assert row["uncovered_ink"] - baseline["uncovered_ink"] >= 20
    assert row["decoration_ink"] == baseline["decoration_ink"]


def test_a_row_icon_sized_gap_survives_the_discount(tmp_path) -> None:
    """xarchiver's folder icons are 16x16 outlines (measured: drawn at
    x=109..125 on the row at y=869) and were the top uncovered cluster in that
    window. An outline is thin, so only the length rule keeps it: at 16px its
    strokes are shorter than a rule has to be."""
    def folder(draw):
        draw.rectangle([120, 120, 135, 135], outline=0, width=1)
        draw.line([120, 124, 135, 124], fill=0)

    baseline = _audit(tmp_path, "plain")
    row = _audit(tmp_path, "icon", extra=folder)

    assert row["uncovered_ink"] - baseline["uncovered_ink"] >= 40
    assert row["decoration_ink"] == baseline["decoration_ink"]


def test_a_long_thin_rule_is_discounted_wherever_it_is_drawn(tmp_path) -> None:
    """Pane dividers and table grid lines sit in the middle of a window, not
    only at its edge, and are the same thing as the border."""
    def divider(draw):
        draw.line([20, 150, 180, 150], fill=0, width=1)

    baseline = _audit(tmp_path, "plain")
    row = _audit(tmp_path, "ruled", extra=divider)

    assert row["uncovered_ink"] == baseline["uncovered_ink"]
    assert row["decoration_ink"] - baseline["decoration_ink"] >= 160
