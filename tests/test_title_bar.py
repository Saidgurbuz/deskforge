"""A window's title is visible text that nothing covered.

The frame element carries the title as *text*, but its rect is the whole window,
so no box sits where the title is drawn. Measured across a 12-scene batch, 42% of
all flagged uncovered ink was in the title-bar band.
"""

from deskshot.extraction.run_extraction import (
    MAX_TITLE_BAR_HEIGHT,
    synthesize_title_bars,
)


def _frame(title, x=10, y=10, w=400, h=300, app="ed"):
    return {"role": "frame", "tag": "frame", "name": title,
            "rect": {"x": x, "y": y, "w": w, "h": h}, "app_name": app, "_dom_index": 0}


def _child(y, x=12, w=40, h=20, app="ed", role="push button"):
    return {"role": role, "rect": {"x": x, "y": y, "w": w, "h": h}, "app_name": app}


def _bars(elements):
    return [e for e in elements if e.get("role") == "title bar"]


def test_a_title_bar_is_added_with_the_window_title() -> None:
    els = [_frame("My Doc - Editor"), _child(44)]

    meta = synthesize_title_bars(els)

    assert meta["num_title_bars"] == 1
    bar = _bars(els)[0]
    assert bar["visible_text"] == "My Doc - Editor"
    assert bar["rect"] == {"x": 10, "y": 10, "w": 400, "h": 34}
    assert bar["type"] == "Heading"


def test_the_band_comes_from_the_window_contents_not_a_fixed_guess() -> None:
    """Title bar height varies with theme and with whether the app draws its own
    decoration, so it is measured from where the first widget starts."""
    thin = [_frame("A"), _child(28)]        # first widget 18px down
    thick = [_frame("A"), _child(50)]       # 40px down, still under the cap
    capped = [_frame("A"), _child(200)]     # a window whose content starts far down

    synthesize_title_bars(thin)
    synthesize_title_bars(thick)
    synthesize_title_bars(capped)

    assert _bars(thin)[0]["rect"]["h"] == 18
    assert _bars(thick)[0]["rect"]["h"] == 40
    # Clamped: past this the top band is a header bar, not a title strip.
    assert _bars(capped)[0]["rect"]["h"] == MAX_TITLE_BAR_HEIGHT


def test_a_header_bar_app_gets_no_synthetic_box() -> None:
    """GTK apps that draw their own header bar have widgets at the very top;
    adding a box there would cover content that is already annotated."""
    els = [_frame("Files"), _child(12)]

    assert synthesize_title_bars(els)["num_title_bars"] == 0


def test_a_band_already_covered_by_widgets_is_left_alone() -> None:
    els = [_frame("Browser"), _child(y=14, x=10, w=390, h=28), _child(60)]

    assert synthesize_title_bars(els)["num_title_bars"] == 0


def test_a_window_with_no_real_title_is_skipped() -> None:
    """GTK reports an object-replacement character for windows whose title is
    drawn as an icon; there is no text to annotate."""
    for title in ("", "   ", "￼", "None"):
        els = [_frame(title), _child(44)]
        assert synthesize_title_bars(els)["num_title_bars"] == 0, title


def test_only_the_windows_own_children_set_the_band() -> None:
    """Another app's window overlapping this one must not shrink its title bar."""
    els = [_frame("Mine"), _child(44), _child(y=12, app="other")]

    synthesize_title_bars(els)

    assert _bars(els)[0]["rect"]["h"] == 34


def test_a_synthetic_bar_is_marked_as_such() -> None:
    els = [_frame("A"), _child(44)]

    synthesize_title_bars(els)

    assert _bars(els)[0]["attrs"]["synthesized"] == "title_bar"
    assert _bars(els)[0]["occlusion_state"] == "none"


def test_tiny_windows_are_ignored() -> None:
    els = [_frame("A", w=20, h=20), _child(y=15, x=11, w=5, h=3)]

    assert synthesize_title_bars(els)["num_title_bars"] == 0
