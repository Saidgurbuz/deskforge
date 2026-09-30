"""Window controls exist only in pixels, so the detector has to be sure.

xfwm4 draws close/minimise/maximise and publishes nothing to AT-SPI. The first
attempt at reading them back was abandoned rather than shipped, because a
detector that mislabels buttons puts false positives into ground truth - worse
than the gap it closes. These tests pin what made the second attempt work, each
taken from a real title bar.
"""

import numpy as np

from deskshot.extraction.window_controls import detect_controls, name_controls


def _band(width=600, height=28, bg=245):
    return np.full((height + 40, width + 40), bg, dtype=np.int32), {
        "x": 20, "y": 20, "w": width, "h": height,
    }


def _glyph(gray, x, y, w, h, value=40):
    gray[y:y + h, x:x + w] = value


def test_four_evenly_spaced_glyphs_at_the_right_edge_are_the_controls():
    """ubuntu_like draws `^ _ [] X` 33px apart at the right edge."""
    gray, band = _band()
    for i in range(4):
        _glyph(gray, 20 + 600 - 150 + i * 33, 28, 10, 9)
    rects, side = detect_controls(gray, band)
    assert side == "right"
    assert len(rects) == 4
    assert name_controls(4, "right") == ["Shade", "Minimize", "Maximize", "Close"]


def test_glyphs_of_different_heights_still_form_one_group():
    """`^` `_` `[]` `X` measure 7, 2, 9 and 9 px tall. Requiring equal heights
    dropped the 2px minimise line and with it the whole run."""
    gray, band = _band()
    for i, h in enumerate((7, 2, 9, 9)):
        _glyph(gray, 20 + 600 - 150 + i * 33, 30, 10, h)
    rects, _side = detect_controls(gray, band)
    assert len(rects) == 4


def test_the_window_border_is_not_mistaken_for_a_button_group():
    """The border and its shadow hug the edge as evenly spaced strokes - which
    is what a button group looks like - and were being picked instead of the
    three discs sitting further in."""
    gray, band = _band()
    for i in range(3):
        _glyph(gray, 21 + i * 4, 24, 2, 30)
    rects, _side = detect_controls(gray, band)
    assert rects == []


def test_title_text_is_not_a_button_group():
    """Letters vary in width and spacing; buttons do not."""
    gray, band = _band()
    rng = np.random.default_rng(0)
    x = 20 + 200
    for _ in range(12):
        w = int(rng.integers(4, 14))
        _glyph(gray, x, 28, w, 10)
        x += w + int(rng.integers(3, 12))
    rects, _side = detect_controls(gray, band)
    assert rects == []


def test_an_empty_title_bar_yields_nothing():
    gray, band = _band()
    assert detect_controls(gray, band) == ([], "")


def test_names_follow_the_layout_this_project_configures():
    """xfwm4 `O|SHMC` on the right, `CHM|` on the left, read from close
    backwards so a theme that omits shade still names the rest correctly."""
    assert name_controls(4, "right") == ["Shade", "Minimize", "Maximize", "Close"]
    assert name_controls(3, "right") == ["Minimize", "Maximize", "Close"]
    assert name_controls(3, "left") == ["Close", "Minimize", "Maximize"]
