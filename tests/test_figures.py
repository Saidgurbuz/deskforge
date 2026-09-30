"""What a figure must not get wrong.

The renderer this replaces coloured boxes with `hash(kind)`, which Python salts
per process - two figures in one paper disagreed about what blue meant. Most of
these tests exist because something here was wrong once and is cheap to pin.
"""

from __future__ import annotations

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from deskshot.figures import palette
from deskshot.figures.render import (
    PAPER, _auto_zoom_region, _tokens, _wrap, draw_boxes, figure,
    label_panel, load_capture,
)
from deskshot.figures.svg import write_svg


def _element(x, y, w, h, type_name="Button", **extra):
    element = {"type": type_name, "rect": {"x": x, "y": y, "w": w, "h": h}}
    element.update(extra)
    return element


@pytest.fixture()
def capture_dir(tmp_path: Path) -> Path:
    stem = "scene-deadbeef-step00"
    Image.new("RGB", (400, 300), (200, 205, 215)).save(tmp_path / (stem + ".png"))
    elements = [
        _element(0, 0, 400, 300, "Window"),
        _element(10, 10, 80, 24, "Button", visible_text="Save"),
        _element(10, 50, 120, 20, "Text", visible_text="a label"),
        _element(200, 50, 100, 40, "File Icon", is_occluded=True,
                 visible_fragments=[{"x": 200, "y": 50, "w": 40, "h": 40}]),
    ]
    (tmp_path / (stem + ".elements.leaf.json")).write_text(json.dumps(elements))
    (tmp_path / (stem + ".meta.json")).write_text(json.dumps({
        "launched_apps": ["homebank"], "num_elements_leaf": len(elements),
        "scene": {"seed": 42, "theme_preset": "ubuntu_like"},
    }))
    (tmp_path / (stem + ".screentag.txt")).write_text(
        "<screentag><Window><loc_0><loc_0><loc_500><loc_500></Window></screentag>")
    return tmp_path / stem


# ------------------------------------------------------------------ palette

def test_a_colour_is_the_same_in_another_process() -> None:
    """The bug this package was written to end: salted `hash()` per process."""
    code = (
        "import sys; sys.path.insert(0, %r);"
        "from deskshot.figures.palette import colour_of;"
        "print(colour_of('Button'), colour_of('File Icon'), colour_of('Whatsit'))"
        % str(Path(__file__).resolve().parent.parent / "src")
    )
    env_runs = [
        subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"})
        for seed in ("0", "1", "random")
    ]
    outputs = {run.stdout.strip() for run in env_runs}
    assert len(outputs) == 1, "colour depends on the hash seed: %s" % outputs
    assert outputs.pop() == "%s %s %s" % (
        palette.colour_of("Button"), palette.colour_of("File Icon"),
        palette.colour_of("Whatsit"))


def test_every_family_has_a_distinct_colour() -> None:
    colours = [colour for colour, _ in palette.FAMILIES.values()]
    assert len(set(colours)) == len(colours)
    assert palette.NEUTRAL not in colours


def test_an_unknown_type_still_lands_in_a_family() -> None:
    assert palette.family_of("Icon Button") == "action"
    assert palette.family_of("Folder Icon") == "media"
    assert palette.family_of("Weird New Table") == "structure"
    assert palette.family_of("") == "other"
    assert palette.family_of(None) == "other"


def test_file_icon_is_media_not_input() -> None:
    """"file" would reach the `input` keyword rule; "icon" has to win first."""
    assert palette.family_of("File Icon") == "media"


def test_label_ink_is_readable_on_every_family() -> None:
    for colour, _ in list(palette.FAMILIES.values()) + [(palette.NEUTRAL, "")]:
        ink = palette.readable_ink(colour)
        assert ink in ((17, 17, 20), (255, 255, 255))


# -------------------------------------------------------------------- boxes

def test_boxes_are_drawn_and_degenerate_rects_are_skipped() -> None:
    base = Image.new("RGB", (100, 100), (255, 255, 255))
    elements = [_element(5, 5, 20, 20), _element(0, 0, 0, 10), {"type": "Button"}]
    out, drawable, stats = draw_boxes(base, elements, style=PAPER)
    assert stats["boxes"] == 1 and len(drawable) == 1
    assert out.size == base.size
    assert out.getpixel((15, 15)) != (255, 255, 255)


def test_an_occluded_element_is_drawn_from_its_visible_fragments() -> None:
    base = Image.new("RGB", (100, 100), (255, 255, 255))
    element = _element(10, 10, 80, 80, "Button", is_occluded=True,
                       visible_fragments=[{"x": 10, "y": 10, "w": 20, "h": 20}])
    _out, drawable, stats = draw_boxes(base, [element], style=PAPER)
    assert stats["occluded"] == 1
    assert drawable[0][3] == [(10, 10, 30, 30)], "the hidden part must not be filled"


def test_containers_are_drawn_before_their_contents() -> None:
    base = Image.new("RGB", (100, 100), (255, 255, 255))
    _out, drawable, _ = draw_boxes(
        base, [_element(5, 5, 10, 10, "Button"), _element(0, 0, 100, 100, "Window")],
        style=PAPER)
    assert drawable[0][1] == "Window"


def test_a_family_filter_drops_everything_else() -> None:
    base = Image.new("RGB", (100, 100), (255, 255, 255))
    elements = [_element(0, 0, 10, 10, "Button"), _element(20, 20, 10, 10, "Text")]
    _out, drawable, _ = draw_boxes(base, elements, style=PAPER, families=["action"])
    assert [row[1] for row in drawable] == ["Button"]


# ------------------------------------------------------------------- labels

def test_labels_are_capped_and_never_overlap() -> None:
    panel = Image.new("RGB", (600, 400), (255, 255, 255))
    elements = [_element(10, 30 * i + 10, 120, 24, "Button", visible_text="b%d" % i)
                for i in range(12)]
    _out, drawable, _ = draw_boxes(panel, elements, style=PAPER)
    placed = label_panel(panel, drawable, style=PAPER, cap=5)
    assert placed == 5


def test_a_label_is_the_same_size_whatever_the_screenshot_resolution() -> None:
    """The zoom-inset bug: chips were drawn native then magnified 3x."""
    heights = []
    for size in ((800, 600), (3840, 2160)):
        base = Image.new("RGB", size, (255, 255, 255))
        element = _element(10, 100, size[0] // 4, size[1] // 12, "Button",
                           visible_text="Save")
        _out, drawable, _ = draw_boxes(base, [element], style=PAPER)
        panel = _out.resize((900, round(900 * size[1] / size[0])), Image.LANCZOS)
        scale = 900 / float(size[0])
        assert label_panel(panel, drawable, style=PAPER, cap=1, scale=scale) == 1
        heights.append(_chip_height(panel))
    assert abs(heights[0] - heights[1]) <= 2, heights


def _chip_height(panel: Image.Image) -> int:
    """Rows holding a solid run of chip fill.

    A run, not a pixel: the box outline is the same colour and every row of the
    box crosses its two vertical edges, so a per-pixel test measures the box.
    A chip is tens of pixels wide and opaque; an outline is two.
    """
    target = palette.rgb(palette.colour_of("Button"))
    rows = 0
    for y in range(panel.height):
        run = best = 0
        for x in range(panel.width):
            pixel = panel.getpixel((x, y))
            run = run + 1 if all(abs(pixel[i] - target[i]) < 12 for i in range(3)) else 0
            best = max(best, run)
        if best >= 20:
            rows += 1
    return rows


def test_a_label_outside_the_crop_is_not_placed() -> None:
    panel = Image.new("RGB", (100, 100), (255, 255, 255))
    element = _element(500, 500, 60, 20, "Button", visible_text="far away")
    _out, drawable, _ = draw_boxes(Image.new("RGB", (800, 800)), [element], style=PAPER)
    assert label_panel(panel, drawable, style=PAPER, cap=9, origin=(0, 0)) == 0


# --------------------------------------------------------------------- wrap

def test_screentag_wraps_even_without_a_single_space() -> None:
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    from deskshot.figures.render import _font
    font = _font(12, mono=True)
    text = "<Window>" + "<loc_100>" * 200
    lines = _wrap(draw, text, font, 300)
    assert len(lines) > 1
    for line in lines:
        assert draw.textbbox((0, 0), line, font=font)[2] <= 300


def test_tokens_break_after_a_closing_bracket() -> None:
    assert _tokens("<a><b> c") == ["<a>", "<b>", " ", "c"]


# --------------------------------------------------------------------- zoom

def test_the_zoom_region_shrinks_as_the_factor_grows(capture_dir: Path) -> None:
    capture = load_capture(capture_dir)
    wide = _auto_zoom_region(capture, (400, 300), 200, 150)
    tight = _auto_zoom_region(capture, (400, 300), 60, 45)
    assert (wide[2] - wide[0]) > (tight[2] - tight[0])


def test_the_zoom_region_stays_inside_the_screenshot(capture_dir: Path) -> None:
    capture = load_capture(capture_dir)
    x0, y0, x1, y1 = _auto_zoom_region(capture, (400, 300), 900, 900)
    assert 0 <= x0 < x1 <= 400 and 0 <= y0 < y1 <= 300


# ------------------------------------------------------------------ capture

@pytest.mark.parametrize("suffix", ["", ".png", ".elements.leaf.json", ".meta.json"])
def test_a_capture_loads_from_any_of_its_files(capture_dir: Path, suffix: str) -> None:
    capture = load_capture(Path(str(capture_dir) + suffix))
    assert capture.stem == "scene-deadbeef-step00"
    assert len(capture.elements) == 4
    assert capture.width == 400 and capture.height == 300
    assert capture.screentag and capture.screentag.startswith("<screentag>")


def test_a_missing_screenshot_is_an_error_not_a_blank_figure(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_capture(tmp_path / "nothing")


def test_facts_report_occlusion(capture_dir: Path) -> None:
    facts = dict(load_capture(capture_dir).facts())
    assert facts["elements"] == "4"
    assert facts["apps"] == "homebank"
    assert "occluded" in facts


# ------------------------------------------------------------------- modes

@pytest.mark.parametrize("mode", ["overlay", "pair", "zoom", "tag"])
def test_every_mode_renders(capture_dir: Path, mode: str) -> None:
    image = figure([load_capture(capture_dir)], mode=mode, width=900)
    assert image.width >= 900 and image.height > 100


def test_grid_renders_several(capture_dir: Path) -> None:
    captures = [load_capture(capture_dir)] * 4
    image = figure(captures, mode="grid", width=900, columns=2)
    assert image.width > 400 and image.height > 200


def test_an_unknown_mode_is_refused(capture_dir: Path) -> None:
    with pytest.raises(ValueError):
        figure([load_capture(capture_dir)], mode="nonsense")


def test_rendering_nothing_is_refused() -> None:
    with pytest.raises(ValueError):
        figure([])


def test_a_capture_with_no_elements_still_renders(tmp_path: Path) -> None:
    stem = tmp_path / "bare-step00"
    Image.new("RGB", (200, 150), (10, 10, 10)).save(str(stem) + ".png")
    (tmp_path / "bare-step00.elements.leaf.json").write_text("[]")
    image = figure([load_capture(stem)], mode="overlay", width=400)
    assert image.width >= 400


# --------------------------------------------------------------------- svg

def test_svg_is_well_formed_and_carries_one_rect_per_box(capture_dir: Path, tmp_path: Path) -> None:
    out = write_svg(load_capture(capture_dir), tmp_path / "f.svg", width=800)
    root = ET.parse(str(out)).getroot()
    assert root.tag.endswith("svg")
    rects = root.iter("{http://www.w3.org/2000/svg}rect")
    fills = [r.get("fill") for r in rects]
    assert palette.colour_of("Button") in fills
    assert palette.colour_of("Window") in fills


def test_svg_escapes_label_text(tmp_path: Path) -> None:
    stem = tmp_path / "esc-step00"
    Image.new("RGB", (300, 200), (255, 255, 255)).save(str(stem) + ".png")
    (tmp_path / "esc-step00.elements.leaf.json").write_text(json.dumps(
        [_element(10, 10, 200, 30, "Button", visible_text='a <b> & "c"')]))
    out = write_svg(load_capture(stem), tmp_path / "e.svg", width=600)
    ET.parse(str(out))  # raises if the & or < leaked through unescaped
    assert "&amp;" in out.read_text()


def test_svg_dashes_the_hidden_part_of_an_occluded_element(capture_dir: Path, tmp_path: Path) -> None:
    out = write_svg(load_capture(capture_dir), tmp_path / "o.svg", width=800)
    assert "stroke-dasharray" in out.read_text()
