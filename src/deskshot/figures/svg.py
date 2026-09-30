"""The same figure as vector, for a paper that a reviewer will zoom into.

A 2560x1440 screenshot in a two-column layout is about 3.4 inches wide. At that
size a raster figure has thrown away the very thing the figure is arguing: that
there is a box on a twelve-pixel table cell. In SVG the screenshot stays raster
- it is a photograph of pixels, there is nothing to vectorise - but every box,
chip and piece of type is a real vector object, so the boxes stay hairline
sharp and the labels stay selectable and searchable in the PDF.

Written by hand rather than through a drawing library because the output is a
few hundred rectangles and a dependency that has to be installed on a compute
node is worse than sixty lines of string formatting.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from deskshot.figures.palette import colour_of, family_of, legend_entries, readable_ink
from deskshot.figures.render import (
    Capture, Rect, Style, PAPER, _area, _fragments, _label_for, _overlaps, _rect_of,
)


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _rgb_css(colour: str) -> str:
    return colour


def _ink_css(colour: str) -> str:
    r, g, b = readable_ink(colour)
    return "rgb(%d,%d,%d)" % (r, g, b)


#: Rough advance width per character for DejaVu Sans Bold at 1px, used only to
#: reserve space for a label chip. SVG measures text itself at render time; we
#: only need to know whether two chips would collide.
_CHAR_ADVANCE = 0.60


def write_svg(
    capture: Capture,
    out: Path,
    *,
    style: Style = PAPER,
    width: Optional[int] = 1600,
    labels: str = "auto",
    label_text: str = "both",
    max_labels: int = 40,
    show_occluded: bool = True,
    families: Optional[Iterable[str]] = None,
    types: Optional[Iterable[str]] = None,
    legend: bool = True,
    embed_image: bool = True,
) -> Path:
    """Write `out`. Coordinates stay in screenshot pixels via `viewBox`."""
    sw, sh = capture.width, capture.height
    legend_rows = legend_entries(capture.type_counts) if legend else []
    legend_w = int(sw * 0.22) if legend_rows else 0
    pad = max(12, sw // 120)
    caption_h = max(26, sh // 46)
    total_w = sw + legend_w + (pad * 3 if legend_w else pad * 2)
    total_h = sh + pad * 2 + caption_h

    scale = 1.0 if not width else width / float(total_w)
    parts: List[str] = []
    parts.append(
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        'width="%d" height="%d" viewBox="0 0 %d %d" '
        'font-family="DejaVu Sans, Helvetica, Arial, sans-serif">'
        % (round(total_w * scale), round(total_h * scale), total_w, total_h)
    )
    parts.append('<rect width="%d" height="%d" fill="rgb(%d,%d,%d)"/>'
                 % (total_w, total_h, *style.background))

    parts.append('<g transform="translate(%d,%d)">' % (pad, pad))
    if embed_image:
        payload = base64.b64encode(capture.png.read_bytes()).decode("ascii")
        href = "data:image/png;base64," + payload
    else:
        href = capture.png.name
    parts.append('<image x="0" y="0" width="%d" height="%d" xlink:href="%s"/>'
                 % (sw, sh, href))

    drawable = _select(capture.elements, families, types)
    stroke = max(1.0, sw / 1400.0 * style.stroke)

    for element, kind, _family, fragments in drawable:
        colour = _rgb_css(colour_of(kind))
        if show_occluded and element.get("is_occluded"):
            whole = _rect_of(element.get("rect"))
            if whole:
                parts.append(
                    '<rect x="%d" y="%d" width="%d" height="%d" fill="none" '
                    'stroke="%s" stroke-width="%.2f" stroke-opacity="%.2f" '
                    'stroke-dasharray="%d %d"/>'
                    % (whole[0], whole[1], whole[2] - whole[0], whole[3] - whole[1],
                       colour, stroke * 0.75, style.occluded_alpha / 255.0,
                       style.occluded_dash, max(3, style.occluded_dash // 2))
                )
        for fragment in fragments:
            parts.append(
                '<rect x="%d" y="%d" width="%d" height="%d" fill="%s" '
                'fill-opacity="%.3f" stroke="%s" stroke-width="%.2f" '
                'stroke-opacity="%.2f"/>'
                % (fragment[0], fragment[1], fragment[2] - fragment[0],
                   fragment[3] - fragment[1], colour, style.fill_alpha / 255.0,
                   colour, stroke, style.stroke_alpha / 255.0)
            )

    if labels != "none":
        cap = 10 ** 9 if labels == "all" else max_labels
        parts.extend(_label_chips(drawable, (sw, sh), label_text, cap, style))
    parts.append("</g>")

    if legend_rows:
        parts.extend(_legend(legend_rows, sw + pad * 2, pad, legend_w, sh, style))

    parts.append(
        '<text x="%d" y="%d" font-size="%d" fill="rgb(%d,%d,%d)">%s</text>'
        % (pad, total_h - pad + caption_h // 3, max(11, sh // 90),
           *style.muted, _escape(_caption(capture)))
    )
    parts.append("</svg>")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(parts), encoding="utf-8")
    return out


def _caption(capture: Capture) -> str:
    return "%s   ·   %s" % (
        capture.stem,
        "   ·   ".join("%s %s" % pair for pair in capture.facts()),
    )


def _select(
    elements: Sequence[Dict[str, Any]],
    families: Optional[Iterable[str]],
    types: Optional[Iterable[str]],
) -> List[Tuple[Dict[str, Any], str, str, List[Rect]]]:
    wanted_families = set(families) if families else None
    wanted_types = {t.lower() for t in types} if types else None
    rows = []
    for element in elements:
        kind = str(element.get("type") or "unknown")
        if wanted_types is not None and kind.lower() not in wanted_types:
            continue
        family = family_of(kind)
        if wanted_families is not None and family not in wanted_families:
            continue
        fragments = _fragments(element)
        if fragments:
            rows.append((element, kind, family, fragments))
    rows.sort(key=lambda row: (row[2] != "container", -_area(row[3][0])))
    return rows


def _label_chips(
    drawable: Sequence[Tuple[Dict[str, Any], str, str, List[Rect]]],
    size: Tuple[int, int],
    label_text: str,
    cap: int,
    style: Style,
) -> List[str]:
    width, height = size
    font_size = max(10, int(width / 1400.0 * style.label_size))
    pad_x, pad_y = int(font_size * 0.55), int(font_size * 0.34)
    ordered = sorted(drawable, key=lambda row: (row[2] == "container", -_area(row[3][0])))

    out: List[str] = []
    placed: List[Rect] = []
    count = 0
    for element, kind, _family, fragments in ordered:
        if count >= cap:
            break
        text = _label_for(element, label_text)
        if not text:
            continue
        box = fragments[0]
        tw = int(len(text) * font_size * _CHAR_ADVANCE)
        cw, ch = tw + 2 * pad_x, font_size + 2 * pad_y
        if cw > (box[2] - box[0]) * 2.2 and cw > width * 0.19:
            continue
        cx = min(max(0, box[0]), width - cw)
        cy = box[1] - ch - 3
        if cy < 0:
            cy = min(box[1], height - ch)
        chip = (cx, cy, cx + cw, cy + ch)
        if any(_overlaps(chip, other) for other in placed):
            continue
        colour = colour_of(kind)
        out.append(
            '<g><rect x="%d" y="%d" width="%d" height="%d" rx="%d" fill="%s" '
            'fill-opacity="0.96"/><text x="%d" y="%d" font-size="%d" '
            'font-weight="600" fill="%s">%s</text></g>'
            % (cx, cy, cw, ch, max(2, font_size // 3), colour,
               cx + pad_x, cy + ch - pad_y - 1, font_size,
               _ink_css(colour), _escape(text))
        )
        placed.append(chip)
        count += 1
    return out


def _legend(rows, x: int, y: int, width: int, height: int, style: Style) -> List[str]:
    size = max(11, height // 62)
    swatch = size + 2
    out = [
        '<text x="%d" y="%d" font-size="%d" font-weight="700" fill="rgb(%d,%d,%d)">'
        'annotation classes</text>' % (x, y + size, int(size * 1.15), *style.ink)
    ]
    cursor = y + size + int(size * 1.9)
    for row in rows:
        out.append('<rect x="%d" y="%d" width="%d" height="%d" rx="3" fill="%s"/>'
                   % (x, cursor - swatch + 3, swatch, swatch, row["colour"]))
        out.append('<text x="%d" y="%d" font-size="%d" fill="rgb(%d,%d,%d)">%s %s</text>'
                   % (x + swatch + 8, cursor, size, *style.ink,
                      _escape(row["family"]), "{:,}".format(row["count"])))
        cursor += int(size * 1.55)
        for type_name, count in row["types"][:4]:
            out.append('<text x="%d" y="%d" font-size="%d" fill="rgb(%d,%d,%d)">%s %s</text>'
                       % (x + swatch + 8, cursor, int(size * 0.85), *style.muted,
                          _escape(type_name), "{:,}".format(count)))
            cursor += int(size * 1.25)
        if len(row["types"]) > 4:
            out.append('<text x="%d" y="%d" font-size="%d" fill="rgb(%d,%d,%d)">+%d more</text>'
                       % (x + swatch + 8, cursor, int(size * 0.85), *style.muted,
                          len(row["types"]) - 4))
            cursor += int(size * 1.25)
        cursor += int(size * 0.6)
        if cursor > y + height:
            break
    _ = width
    return out
