"""Draw a capture's annotations well enough to put in a paper.

The audit renderer this replaces drew every box in a hash-derived hue at one
pixel, wrote the element index in red on top, and pasted the result next to the
raw screenshot. It answered "is this box on that widget", which is what it was
for, and it is unusable in a figure: the colours changed between runs, 366
labels overlapped into a solid block, and a 3840x2160 screenshot shrank to
illegibility in a two-column layout.

What a figure has to do instead:

* **Say what the annotation knows.** A box is drawn from `visible_fragments` -
  the pixels the element actually occupies after occlusion - and the hidden
  remainder of its `rect` is drawn dashed. Modal and amodal geometry in one
  picture is the thing this corpus has that a screenshot-plus-DOM does not.
* **Stay legible when it is 3.5 inches wide.** Boxes are drawn at native
  resolution and the panel is resampled once, so lines anti-alias instead of
  aliasing; type is composed afterwards at final size so it stays crisp.
* **Not lie by omission.** Labels are placed only where they fit without
  covering another label, and the caption says how many were placed against how
  many exist.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from deskshot.figures.palette import (
    colour_of, family_of, legend_entries, readable_ink, rgb,
)

Rect = Tuple[int, int, int, int]  # x0, y0, x1, y1 inclusive-exclusive
#: (element, type name, family, visible fragments) - what actually got drawn.
Drawable = Tuple[Dict[str, Any], str, str, List[Rect]]


# --------------------------------------------------------------------- style

@dataclass
class Style:
    """Everything a caller might want to restyle, in one place."""

    name: str = "paper"
    background: Tuple[int, int, int] = (255, 255, 255)
    panel: Tuple[int, int, int] = (247, 248, 250)
    ink: Tuple[int, int, int] = (24, 26, 32)
    muted: Tuple[int, int, int] = (110, 118, 132)
    rule: Tuple[int, int, int] = (218, 222, 230)
    #: Outline width in *screenshot* pixels, before the panel is resampled.
    stroke: int = 2
    fill_alpha: int = 34
    stroke_alpha: int = 235
    #: The hidden part of an occluded element.
    occluded_alpha: int = 120
    occluded_dash: int = 10
    label_size: int = 13
    body_size: int = 14
    title_size: int = 17
    pad: int = 22
    radius: int = 5
    shadow: bool = True


PAPER = Style()
DARK = Style(
    name="dark",
    background=(18, 20, 26),
    panel=(26, 29, 37),
    ink=(232, 236, 243),
    muted=(146, 156, 174),
    rule=(52, 58, 70),
    fill_alpha=44,
)
STYLES = {"paper": PAPER, "dark": DARK}


def _font(size: int, bold: bool = False, mono: bool = False):
    names = (
        ["DejaVuSansMono-Bold.ttf", "DejaVuSansMono.ttf"] if mono
        else ["DejaVuSans-Bold.ttf", "DejaVuSans.ttf"]
    )
    if not bold:
        names = names[1:] + names[:1]
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> Tuple[int, int]:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left, bottom - top


# ------------------------------------------------------------------ capture

@dataclass
class Capture:
    """One annotated screenshot, loaded from the files that share a stem."""

    stem: str
    png: Path
    elements: List[Dict[str, Any]] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)
    screentag: Optional[str] = None
    width: int = 0
    height: int = 0

    @property
    def type_counts(self) -> Dict[str, int]:
        return dict(Counter(str(e.get("type") or "unknown") for e in self.elements))

    @property
    def occluded(self) -> int:
        return sum(1 for e in self.elements if e.get("is_occluded"))

    def facts(self) -> List[Tuple[str, str]]:
        """The caption line, as label/value pairs."""
        meta = self.meta or {}
        scene = meta.get("scene") or {}
        rows: List[Tuple[str, str]] = [("elements", f"{len(self.elements):,}")]
        types = len(self.type_counts)
        rows.append(("types", str(types)))
        if self.width and self.height:
            rows.append(("viewport", f"{self.width}x{self.height}"))
        apps = meta.get("launched_apps") or []
        if apps:
            rows.append(("apps", ", ".join(str(a) for a in apps)))
        theme = scene.get("theme_preset") or meta.get("theme")
        if theme:
            rows.append(("theme", str(theme)))
        if self.occluded:
            rows.append(("occluded", f"{self.occluded:,} ({100.0 * self.occluded / max(1, len(self.elements)):.0f}%)"))
        seed = scene.get("seed")
        if seed is not None:
            rows.append(("seed", str(seed)))
        return rows


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _as_elements(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("elements")
    if not isinstance(payload, list):
        return []
    return [e for e in payload if isinstance(e, dict)]


def load_capture(target: Path, view: str = "leaf") -> Capture:
    """Load by capture stem, by its PNG, or by any file sharing the stem."""
    target = Path(target)
    name = target.name
    for suffix in (".png", ".elements.leaf.json", ".elements.json", ".meta.json",
                   ".screentag.txt", ".verdict.json"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    here = target.parent
    stem = name
    suffix = {"leaf": ".elements.leaf.json", "filtered": ".elements.json",
              "unfiltered": ".elements.unfiltered.json",
              "amodal": ".elements.amodal.json"}.get(view, ".elements.leaf.json")

    png = here / (stem + ".png")
    if not png.is_file():
        raise FileNotFoundError("no screenshot at %s" % png)
    with Image.open(png) as probe:
        width, height = probe.size

    tag_path = here / (stem + ".screentag.txt")
    try:
        screentag = tag_path.read_text(encoding="utf-8") if tag_path.is_file() else None
    except OSError:
        screentag = None

    return Capture(
        stem=stem,
        png=png,
        elements=_as_elements(_read_json(here / (stem + suffix))),
        meta=_read_json(here / (stem + ".meta.json")) or {},
        screentag=screentag,
        width=width,
        height=height,
    )


# ------------------------------------------------------------------- boxes

def _rect_of(source: Any) -> Optional[Rect]:
    if not isinstance(source, dict):
        return None
    try:
        x, y = int(source["x"]), int(source["y"])
        w, h = int(source["w"]), int(source["h"])
    except (KeyError, TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return x, y, x + w, y + h


def _fragments(element: Dict[str, Any]) -> List[Rect]:
    """The pixels this element actually occupies, occlusion applied."""
    out = []
    for fragment in element.get("visible_fragments") or []:
        rect = _rect_of(fragment)
        if rect:
            out.append(rect)
    if out:
        return out
    rect = _rect_of(element.get("rect"))
    return [rect] if rect else []


def _dashed_rectangle(draw: ImageDraw.ImageDraw, box: Rect, colour, width: int, dash: int) -> None:
    x0, y0, x1, y1 = box
    gap = max(3, dash // 2)
    step = dash + gap
    for x in range(x0, x1, step):
        end = min(x + dash, x1)
        draw.line([(x, y0), (end, y0)], fill=colour, width=width)
        draw.line([(x, y1 - 1), (end, y1 - 1)], fill=colour, width=width)
    for y in range(y0, y1, step):
        end = min(y + dash, y1)
        draw.line([(x0, y), (x0, end)], fill=colour, width=width)
        draw.line([(x1 - 1, y), (x1 - 1, end)], fill=colour, width=width)


def _area(box: Rect) -> int:
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def _overlaps(a: Rect, b: Rect) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _label_for(element: Dict[str, Any], mode: str) -> str:
    kind = str(element.get("type") or element.get("role") or "?")
    if mode == "type":
        return kind
    text = (element.get("visible_text") or element.get("name") or "").strip()
    text = " ".join(text.split())
    if mode == "text":
        return text or kind
    if text and text.lower() != kind.lower():
        return "%s  %s" % (kind, text[:28] + ("…" if len(text) > 28 else ""))
    return kind


def draw_boxes(
    base: Image.Image,
    elements: Sequence[Dict[str, Any]],
    *,
    style: Style = PAPER,
    show_occluded: bool = True,
    families: Optional[Iterable[str]] = None,
    types: Optional[Iterable[str]] = None,
    dim: float = 0.0,
) -> Tuple[Image.Image, List[Drawable], Dict[str, int]]:
    """Boxes over `base`, at `base`'s own resolution.

    Returns the image, the elements that were drawn, and counts. **No labels** -
    those are added by `label_panel` after the panel has been resampled, so a
    chip is the same size in the finished figure whether the screenshot was
    1366x768 or 3840x2160, and a zoom inset does not magnify its own labels
    into billboards.

    Containers are drawn first so a button on a panel is not buried under the
    panel's fill; among equals, larger first.
    """
    canvas = base.convert("RGB")
    if dim > 0:
        canvas = Image.blend(canvas, Image.new("RGB", canvas.size, (255, 255, 255)), dim)

    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer, "RGBA")

    wanted_families = set(families) if families else None
    wanted_types = {t.lower() for t in types} if types else None

    drawable = []
    for element in elements:
        kind = str(element.get("type") or "unknown")
        if wanted_types is not None and kind.lower() not in wanted_types:
            continue
        family = family_of(kind)
        if wanted_families is not None and family not in wanted_families:
            continue
        fragments = _fragments(element)
        if not fragments:
            continue
        drawable.append((element, kind, family, fragments))

    # Containers behind everything, then largest first.
    drawable.sort(key=lambda row: (row[2] != "container", -_area(row[3][0])))

    stats = Counter()
    for element, kind, _family, fragments in drawable:
        colour = rgb(colour_of(kind))
        stats["boxes"] += 1

        if show_occluded and element.get("is_occluded"):
            whole = _rect_of(element.get("rect"))
            if whole:
                _dashed_rectangle(
                    draw, whole, colour + (style.occluded_alpha,),
                    max(1, style.stroke - 1), style.occluded_dash,
                )
                stats["occluded"] += 1

        for fragment in fragments:
            draw.rectangle(fragment, fill=colour + (style.fill_alpha,))
            draw.rectangle(
                [fragment[0], fragment[1], fragment[2] - 1, fragment[3] - 1],
                outline=colour + (style.stroke_alpha,), width=style.stroke,
            )

    canvas = Image.alpha_composite(canvas.convert("RGBA"), layer).convert("RGB")
    return canvas, drawable, dict(stats)


def label_panel(
    panel: Image.Image,
    drawable: Sequence[Drawable],
    *,
    style: Style = PAPER,
    label_text: str = "both",
    cap: int = 40,
    scale: float = 1.0,
    origin: Tuple[int, int] = (0, 0),
) -> int:
    """Chips on an already-sized panel. Returns how many were placed.

    `scale` and `origin` map screenshot pixels onto this panel, so the same
    call works for a whole capture resampled to figure width and for a 3x crop
    of one corner of it.

    Containers go last: a window's chip is the least informative thing on the
    screen and it sits exactly where a menu bar's chip wants to be. A chip that
    would land on top of one already placed is dropped rather than nudged -
    a nudged chip points at the wrong widget, which is worse than no chip.
    """
    width, height = panel.size
    draw = ImageDraw.Draw(panel, "RGBA")
    font = _font(style.label_size, bold=True)
    pad_x, pad_y = 7, 4

    ordered = sorted(drawable, key=lambda row: (row[2] == "container", -_area(row[3][0])))

    placed: List[Rect] = []
    count = 0
    for element, kind, _family, fragments in ordered:
        if count >= cap:
            break
        text = _label_for(element, label_text)
        if not text:
            continue
        box = _project(fragments[0], scale, origin)
        if box[2] <= 0 or box[3] <= 0 or box[0] >= width or box[1] >= height:
            continue  # outside this crop

        tw, th = _text_size(draw, text, font)
        cw, ch = tw + 2 * pad_x, th + 2 * pad_y
        if cw > (box[2] - box[0]) * 2.2 and cw > width * 0.17:
            continue  # a chip far wider than its element reads as noise
        if cw > width:
            continue

        cx = min(max(0, box[0]), width - cw)
        cy = box[1] - ch - 3
        if cy < 0:
            cy = min(box[1], height - ch)
        if cy < 0:
            continue
        chip = (cx, cy, cx + cw, cy + ch)
        if any(_overlaps(chip, other) for other in placed):
            continue

        colour = rgb(colour_of(kind))
        draw.rounded_rectangle(chip, radius=style.radius, fill=colour + (245,))
        draw.text((cx + pad_x, cy + pad_y), text,
                  font=font, fill=readable_ink(colour_of(kind)) + (255,))
        placed.append(chip)
        count += 1
    return count


def _project(box: Rect, scale: float, origin: Tuple[int, int]) -> Rect:
    ox, oy = origin
    return (round((box[0] - ox) * scale), round((box[1] - oy) * scale),
            round((box[2] - ox) * scale), round((box[3] - oy) * scale))


# ------------------------------------------------------------------ layout

def _resample(image: Image.Image, target_width: Optional[int]) -> Image.Image:
    """One resample, at the end, so box edges anti-alias instead of aliasing."""
    if not target_width or target_width >= image.width:
        return image
    height = max(1, round(image.height * target_width / image.width))
    return image.resize((target_width, height), Image.LANCZOS)


def _fit(image: Image.Image, target_width: Optional[int]) -> Tuple[Image.Image, float]:
    """Resample and report the factor, so boxes can be re-projected onto it."""
    if not target_width or target_width == image.width:
        return image.copy(), 1.0
    scale = target_width / float(image.width)
    height = max(1, round(image.height * scale))
    return image.resize((target_width, height), Image.LANCZOS), scale


def _shadowed(canvas: Image.Image, box: Rect, style: Style) -> None:
    if not style.shadow:
        return
    x0, y0, x1, y1 = box
    pad = 12
    shadow = Image.new("RGBA", (x1 - x0 + 2 * pad, y1 - y0 + 2 * pad), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rectangle(
        [pad, pad, shadow.width - pad, shadow.height - pad], fill=(0, 0, 0, 46)
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(6))
    canvas.alpha_composite(shadow, (x0 - pad, y0 - pad + 3))


def _panel_frame(draw: ImageDraw.ImageDraw, box: Rect, style: Style) -> None:
    draw.rectangle([box[0] - 1, box[1] - 1, box[2], box[3]],
                   outline=style.rule + (255,), width=1)


def _caption_line(facts: Sequence[Tuple[str, str]]) -> str:
    return "   ·   ".join("%s %s" % (label, value) for label, value in facts)


def _draw_legend(
    canvas: Image.Image,
    origin: Tuple[int, int],
    width: int,
    counts: Dict[str, int],
    style: Style,
    *,
    detail: bool = True,
) -> int:
    """Family swatches down the side. Returns the height it used."""
    draw = ImageDraw.Draw(canvas, "RGBA")
    head = _font(style.body_size, bold=True)
    body = _font(style.body_size - 1)
    small = _font(style.body_size - 3)

    x, y = origin
    draw.text((x, y), "annotation classes", font=head, fill=style.ink + (255,))
    y += _text_size(draw, "Ag", head)[1] + 12

    swatch = style.body_size + 2
    for row in legend_entries(counts):
        draw.rounded_rectangle([x, y, x + swatch, y + swatch], radius=3,
                               fill=rgb(row["colour"]) + (255,))
        label = "%s  %s" % (row["family"], "{:,}".format(row["count"]))
        draw.text((x + swatch + 9, y + 1), label, font=body, fill=style.ink + (255,))
        y += swatch + 5
        if detail:
            for type_name, count in row["types"][:4]:
                draw.text((x + swatch + 9, y), "%s  %s" % (type_name, "{:,}".format(count)),
                          font=small, fill=style.muted + (255,))
                y += _text_size(draw, "Ag", small)[1] + 3
            if len(row["types"]) > 4:
                draw.text((x + swatch + 9, y), "+%d more" % (len(row["types"]) - 4),
                          font=small, fill=style.muted + (255,))
                y += _text_size(draw, "Ag", small)[1] + 3
        y += 8
    _ = width
    return y - origin[1]


def _legend_height(counts: Dict[str, int], style: Style, detail: bool) -> int:
    rows = legend_entries(counts)
    line = style.body_size + 7
    total = style.body_size + 12 + line
    for row in rows:
        total += line + 8
        if detail:
            shown = min(4, len(row["types"])) + (1 if len(row["types"]) > 4 else 0)
            total += shown * (style.body_size - 3 + 3)
    return total


def _compose(
    panels: Sequence[Tuple[Image.Image, str]],
    *,
    title: str,
    caption: str,
    counts: Optional[Dict[str, int]],
    style: Style,
    legend: bool,
    legend_detail: bool,
    gap: int = 18,
) -> Image.Image:
    """Panels side by side, a legend column, a title and a caption."""
    pad = style.pad
    draw_probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    title_font = _font(style.title_size, bold=True)
    body_font = _font(style.body_size)
    caption_font = _font(style.body_size - 1)

    legend_width = 0
    if legend and counts:
        widest = max(
            [_text_size(draw_probe, "%s  %s" % (r["family"], "{:,}".format(r["count"])), body_font)[0]
             for r in legend_entries(counts)]
            + [_text_size(draw_probe, "%s  99,999" % t, _font(style.body_size - 3))[0]
               for r in legend_entries(counts) for t, _ in r["types"][:4]]
            + [_text_size(draw_probe, "annotation classes", title_font)[0]]
        )
        legend_width = widest + style.body_size + 22

    panel_w = sum(p.width for p, _ in panels) + gap * (len(panels) - 1)
    panel_h = max(p.height for p, _ in panels)
    caption_h = _text_size(draw_probe, "Ag", caption_font)[1] + 10 if caption else 0
    header_h = _text_size(draw_probe, "Ag", title_font)[1] + 12 if title else 0
    sub_h = _text_size(draw_probe, "Ag", body_font)[1] + 8 if any(t for _, t in panels) else 0

    body_h = max(panel_h + sub_h, _legend_height(counts or {}, style, legend_detail) if legend_width else 0)
    width = pad * 2 + panel_w + (legend_width + gap if legend_width else 0)
    height = pad * 2 + header_h + body_h + caption_h

    canvas = Image.new("RGBA", (width, height), style.background + (255,))
    draw = ImageDraw.Draw(canvas, "RGBA")

    y = pad
    if title:
        draw.text((pad, y), title, font=title_font, fill=style.ink + (255,))
        y += header_h

    x = pad
    for panel, sub in panels:
        if sub:
            draw.text((x, y), sub, font=body_font, fill=style.muted + (255,))
        top = y + sub_h
        _shadowed(canvas, (x, top, x + panel.width, top + panel.height), style)
        canvas.paste(panel, (x, top))
        _panel_frame(draw, (x, top, x + panel.width, top + panel.height), style)
        x += panel.width + gap

    if legend_width and counts:
        _draw_legend(canvas, (x, y + sub_h), legend_width, counts, style, detail=legend_detail)

    if caption:
        draw.line([(pad, height - pad - caption_h + 2), (width - pad, height - pad - caption_h + 2)],
                  fill=style.rule + (255,), width=1)
        draw.text((pad, height - pad - caption_h + 9), caption,
                  font=caption_font, fill=style.muted + (255,))
    return canvas.convert("RGB")


def _mark_region(panel: Image.Image, region: Rect, style: Style, colour=(214, 40, 40)) -> Image.Image:
    out = panel.convert("RGBA")
    draw = ImageDraw.Draw(out, "RGBA")
    draw.rectangle(list(region), outline=colour + (255,), width=max(2, panel.width // 400))
    return out.convert("RGB")


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> List[str]:
    """Wrap on spaces, and on `>` when there are no spaces to wrap on.

    ScreenTag runs like `<Window><loc_15><loc_27><loc_197>` contain no space
    for hundreds of characters, so a space-only wrapper let every markup line
    run off the right edge of the panel. Breaking after `>` keeps a tag and its
    four coordinates together where it can and never overflows.
    """
    lines: List[str] = []
    for paragraph in text.split("\n"):
        current = ""
        for word in _tokens(paragraph):
            probe = current + word
            if current and _text_size(draw, probe.strip(), font)[0] > width:
                lines.append(current.rstrip())
                current = word.lstrip()
            else:
                current = probe
        lines.append(current.rstrip())
    return lines


def _tokens(paragraph: str) -> List[str]:
    """Split into pieces that may end a line: after a space, after a `>`."""
    out: List[str] = []
    piece = ""
    for char in paragraph:
        piece += char
        if char in (" ", ">"):
            out.append(piece)
            piece = ""
    if piece:
        out.append(piece)
    return out


def _text_panel(
    text: str,
    *,
    width: int,
    height: int,
    style: Style,
    size: int = 13,
    highlight: bool = True,
) -> Image.Image:
    """ScreenTag beside its screenshot, with the tags picked out in colour."""
    panel = Image.new("RGB", (width, height), style.panel)
    draw = ImageDraw.Draw(panel)
    font = _font(size, mono=True)
    pad = 14
    lines = _wrap(draw, text, font, width - 2 * pad)
    line_h = _text_size(draw, "Ag", font)[1] + 4

    y = pad
    for line in lines:
        if y + line_h > height - pad:
            draw.text((pad, y), "…", font=font, fill=style.muted)
            break
        if not highlight:
            draw.text((pad, y), line, font=font, fill=style.ink)
        else:
            x = pad
            for piece in _split_tags(line):
                colour = style.ink if piece[1] is None else rgb(colour_of(piece[1]))
                draw.text((x, y), piece[0], font=font, fill=colour)
                x += _text_size(draw, piece[0], font)[0]
        y += line_h
    return panel


def _split_tags(line: str) -> List[Tuple[str, Optional[str]]]:
    """Split a ScreenTag line into (text, type-or-None) runs for colouring."""
    out: List[Tuple[str, Optional[str]]] = []
    i = 0
    while i < len(line):
        start = line.find("<", i)
        if start < 0:
            out.append((line[i:], None))
            break
        if start > i:
            out.append((line[i:start], None))
        end = line.find(">", start)
        if end < 0:
            out.append((line[start:], None))
            break
        tag = line[start:end + 1]
        inner = tag.strip("</>").split()[0] if tag.strip("</>") else ""
        name = inner.replace("_", " ")
        out.append((tag, name if family_of(name) != "other" else None))
        i = end + 1
    return out


def figure(
    captures: Sequence[Capture],
    *,
    mode: str = "overlay",
    style: Style = PAPER,
    width: Optional[int] = 1600,
    labels: str = "auto",
    label_text: str = "both",
    max_labels: int = 40,
    show_occluded: bool = True,
    families: Optional[Iterable[str]] = None,
    types: Optional[Iterable[str]] = None,
    legend: bool = True,
    legend_detail: bool = True,
    title: Optional[str] = None,
    caption: Optional[str] = None,
    zoom: Optional[Rect] = None,
    zoom_factor: float = 3.0,
    columns: int = 3,
) -> Image.Image:
    """Render one figure. `mode` picks what the figure is *for*.

    overlay - the annotation on the pixels, the working view
    pair    - raw beside annotated, the "what does this add" figure
    zoom    - annotated with a region called out at `zoom_factor`, for showing
              that a 12-pixel table cell really does have its own box
    tag     - annotated beside the ScreenTag a model is trained to emit
    grid    - many captures at once, for a diversity plate
    """
    if not captures:
        raise ValueError("nothing to render")

    if mode == "grid":
        return _grid(captures, style=style, width=width, columns=columns,
                     labels=labels, label_text=label_text, max_labels=max_labels,
                     show_occluded=show_occluded, families=families, types=types,
                     title=title, caption=caption)

    capture = captures[0]
    base = Image.open(capture.png).convert("RGB")
    annotated, drawable, _stats = draw_boxes(
        base, capture.elements, style=style, show_occluded=show_occluded,
        families=families, types=types,
    )

    counts = capture.type_counts
    cap = 0 if labels == "none" else (10 ** 9 if labels == "all" else max_labels)

    panel_width = width
    if mode in ("pair", "tag"):
        panel_width = max(320, (width or annotated.width) // 2 - 12)
    elif mode == "zoom":
        panel_width = max(320, int((width or annotated.width) * 0.62))

    placed = 0
    if mode == "overlay":
        panel, scale = _fit(annotated, panel_width)
        placed = label_panel(panel, drawable, style=style, label_text=label_text,
                             cap=cap, scale=scale)
        panels = [(panel, "")]
    elif mode == "pair":
        panel, scale = _fit(annotated, panel_width)
        placed = label_panel(panel, drawable, style=style, label_text=label_text,
                             cap=cap, scale=scale)
        panels = [
            (_resample(base, panel_width), "screenshot"),
            (panel, "%d annotated elements" % len(capture.elements)),
        ]
    elif mode == "zoom":
        inset_width = max(280, (width or annotated.width) - panel_width - 18)
        main_scale = panel_width / float(annotated.width) if panel_width else 1.0
        main_height = round(annotated.height * main_scale)

        # The region is derived from the magnification, not guessed. An inset
        # that is meant to be 3x the main panel and is `inset_width` wide can
        # only show inset_width / (3 * main_scale) screenshot pixels; sizing
        # the region first and resampling to fit produced a "3x" inset that was
        # actually showing the crop at 0.7x.
        if zoom is None:
            per_pixel = max(zoom_factor, 0.1) * main_scale
            region = _auto_zoom_region(
                capture, annotated.size,
                max(40, round(inset_width / per_pixel)),
                max(30, round(main_height / per_pixel)),
            )
        else:
            region = _clamp_region(zoom, annotated.size)

        marked, _ = _fit(_mark_region(annotated, region, style), panel_width)
        crop = annotated.crop(region)
        inset, inset_scale = _fit(crop, inset_width)
        # The crop starts at `region`, so chips have to be offset by it or they
        # would all be placed relative to the top-left of the whole screenshot.
        placed = label_panel(inset, drawable, style=style, label_text=label_text,
                             cap=cap, scale=inset_scale, origin=(region[0], region[1]))
        panels = [
            (marked, "full capture"),
            (inset, "%dx%d region, %.1fx the panel beside it"
             % (region[2] - region[0], region[3] - region[1],
                inset_scale / main_scale if main_scale else 1.0)),
        ]
    elif mode == "tag":
        panel, scale = _fit(annotated, panel_width)
        placed = label_panel(panel, drawable, style=style, label_text=label_text,
                             cap=cap, scale=scale)
        tag_text = capture.screentag or "(no .screentag.txt beside this capture)"
        panels = [
            (panel, "%d annotated elements" % len(capture.elements)),
            (_text_panel(tag_text, width=panel_width, height=panel.height, style=style),
             "ScreenTag target"),
        ]
    else:
        raise ValueError("unknown mode %r" % mode)

    facts = capture.facts()
    if cap:
        facts.append(("labelled", "%d of %d" % (placed, len(capture.elements))))
    auto_caption = _caption_line(facts)

    return _compose(
        panels,
        title=title if title is not None else capture.stem,
        caption=caption if caption is not None else auto_caption,
        counts=counts,
        style=style,
        legend=legend,
        legend_detail=legend_detail,
    )


def _clamp_region(region: Rect, size: Tuple[int, int]) -> Rect:
    width, height = size
    x0 = min(max(0, region[0]), max(0, width - 1))
    y0 = min(max(0, region[1]), max(0, height - 1))
    return x0, y0, min(region[2], width), min(region[3], height)


def _auto_zoom_region(capture: Capture, size: Tuple[int, int],
                      win_w: int, win_h: int) -> Rect:
    """The window of this size holding the most small elements.

    Where the fine-grained claim lives: a hand-picked crop is better, but a
    figure script that needs one every time is a figure script nobody runs over
    a thousand samples.
    """
    width, height = size
    win_w = max(40, min(win_w, width))
    win_h = max(30, min(win_h, height))
    best, best_score = (0, 0, win_w, win_h), -1
    centres = []
    for element in capture.elements:
        rect = _rect_of(element.get("rect"))
        if not rect or family_of(str(element.get("type"))) == "container":
            continue
        centres.append(((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2, _area(rect)))
    if not centres:
        return best
    for cx, cy, _ in centres:
        x0 = min(max(0, cx - win_w // 2), max(0, width - win_w))
        y0 = min(max(0, cy - win_h // 2), max(0, height - win_h))
        box = (x0, y0, x0 + win_w, y0 + win_h)
        # Small elements score highest: the point is dense fine-grained boxes.
        score = sum(1.0 / (1.0 + area / 4000.0)
                    for ex, ey, area in centres
                    if box[0] <= ex < box[2] and box[1] <= ey < box[3])
        if score > best_score:
            best, best_score = box, score
    return best


def _grid(
    captures: Sequence[Capture],
    *,
    style: Style,
    width: Optional[int],
    columns: int,
    labels: str,
    label_text: str,
    max_labels: int,
    show_occluded: bool,
    families: Optional[Iterable[str]],
    types: Optional[Iterable[str]],
    title: Optional[str],
    caption: Optional[str],
) -> Image.Image:
    columns = max(1, columns)
    gap = 14
    pad = style.pad
    cell_w = max(200, ((width or 1600) - pad * 2 - gap * (columns - 1)) // columns)

    tiles: List[Image.Image] = []
    counts: Counter = Counter()
    for capture in captures:
        base = Image.open(capture.png).convert("RGB")
        annotated, drawable, _ = draw_boxes(
            base, capture.elements, style=style, show_occluded=show_occluded,
            families=families, types=types,
        )
        counts.update(capture.type_counts)
        tile, scale = _fit(annotated, cell_w)
        # A grid tile is a few hundred pixels wide. Chips are opt-in here
        # because at that size they cover the layout they are describing.
        if labels == "all":
            label_panel(tile, drawable, style=style, label_text=label_text,
                        cap=max_labels, scale=scale)
        tiles.append(tile)

    rows = (len(tiles) + columns - 1) // columns
    row_heights = [max(t.height for t in tiles[r * columns:(r + 1) * columns])
                   for r in range(rows)]
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    title_font = _font(style.title_size, bold=True)
    small = _font(style.body_size - 2)
    cap_font = _font(style.body_size - 1)
    label_h = _text_size(probe, "Ag", small)[1] + 6
    header_h = _text_size(probe, "Ag", title_font)[1] + 12 if title else 0
    caption_h = _text_size(probe, "Ag", cap_font)[1] + 12

    height = (pad * 2 + header_h + caption_h
              + sum(h + label_h + gap for h in row_heights) - gap)
    canvas = Image.new("RGBA", (pad * 2 + columns * cell_w + (columns - 1) * gap, height),
                       style.background + (255,))
    draw = ImageDraw.Draw(canvas, "RGBA")

    y = pad
    if title:
        draw.text((pad, y), title, font=title_font, fill=style.ink + (255,))
        y += header_h
    for r in range(rows):
        x = pad
        for tile, capture in zip(tiles[r * columns:(r + 1) * columns],
                                 captures[r * columns:(r + 1) * columns]):
            _shadowed(canvas, (x, y, x + tile.width, y + tile.height), style)
            canvas.paste(tile, (x, y))
            _panel_frame(draw, (x, y, x + tile.width, y + tile.height), style)
            note = "%s · %d elements" % (capture.stem[:34], len(capture.elements))
            draw.text((x, y + tile.height + 4), note, font=small, fill=style.muted + (255,))
            x += cell_w + gap
        y += row_heights[r] + label_h + gap

    text = caption if caption is not None else "%d captures · %s elements · %d classes" % (
        len(captures), "{:,}".format(sum(counts.values())), len(counts))
    draw.line([(pad, height - pad - caption_h + 2), (canvas.width - pad, height - pad - caption_h + 2)],
              fill=style.rule + (255,), width=1)
    draw.text((pad, height - pad - caption_h + 9), text, font=cap_font, fill=style.muted + (255,))
    return canvas.convert("RGB")
