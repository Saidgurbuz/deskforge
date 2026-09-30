"""Helpers for rendering macOS-style panel assets from upstream theme data."""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from PIL import Image, ImageDraw, ImageFilter


@dataclass(frozen=True)
class PlankThemeSpec:
    theme_name: str
    top_roundness: int
    bottom_roundness: int
    line_width: int
    outer_stroke: tuple[int, int, int, int]
    fill_start: tuple[int, int, int, int]
    fill_end: tuple[int, int, int, int]
    inner_stroke: tuple[int, int, int, int]


def _parse_rgba(raw: str) -> tuple[int, int, int, int]:
    parts = [part.strip() for part in raw.split(";;")]
    ints = [int(part or "0") for part in parts[:4]]
    while len(ints) < 4:
        ints.append(0)
    return tuple(max(0, min(255, value)) for value in ints[:4])


def load_plank_theme_spec(theme_dir: Path) -> Optional[PlankThemeSpec]:
    dock_theme = theme_dir / "plank" / "dock.theme"
    if not dock_theme.is_file():
        return None

    parser = configparser.ConfigParser()
    parser.read(dock_theme, encoding="utf-8")
    if not parser.has_section("PlankTheme"):
        return None

    plank = parser["PlankTheme"]
    return PlankThemeSpec(
        theme_name=theme_dir.name,
        top_roundness=plank.getint("TopRoundness", fallback=23),
        bottom_roundness=plank.getint("BottomRoundness", fallback=23),
        line_width=plank.getint("LineWidth", fallback=0),
        outer_stroke=_parse_rgba(plank.get("OuterStrokeColor", fallback="0;;0;;0;;0")),
        fill_start=_parse_rgba(plank.get("FillStartColor", fallback="209;;209;;209;;150")),
        fill_end=_parse_rgba(plank.get("FillEndColor", fallback="209;;209;;209;;150")),
        inner_stroke=_parse_rgba(plank.get("InnerStrokeColor", fallback="210;;210;;210;;50")),
    )


def _lerp(a: int, b: int, t: float) -> int:
    return int(round(a + (b - a) * t))


def _rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return mask


def render_plank_dock_background(
    spec: PlankThemeSpec,
    output_path: Path,
    *,
    width: int = 620,
    height: int = 82,
) -> Path:
    """Render a rounded translucent dock backdrop derived from plank metadata.

    The output is wider (620px default) and taller (82px) so the
    stretched MATE panel image covers the launchers with generous padding
    and the pill shape stays clearly visible.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Render at 2x then downsample for smoother edges.
    scale = 2
    sw, sh = width * scale, height * scale
    inset_x = 14 * scale
    inset_y = 12 * scale
    rect = (inset_x, inset_y, sw - inset_x, sh - inset_y)
    rect_w = rect[2] - rect[0]
    rect_h = rect[3] - rect[1]
    radius = max(12 * scale, min(spec.top_roundness * scale, rect_h // 2))

    canvas = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))

    # ── Drop shadow (two layers for soft realistic depth) ───────────
    for blur_r, alpha_mult in [(10 * scale, 0.5), (4 * scale, 0.7)]:
        shadow = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
        base_a = 50 if spec.fill_start[3] < 170 else 70
        a = int(base_a * alpha_mult)
        ImageDraw.Draw(shadow).rounded_rectangle(rect, radius=radius, fill=(0, 0, 0, a))
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius=blur_r))
        canvas.alpha_composite(shadow)

    # ── Fill gradient ───────────────────────────────────────────────
    gradient = Image.new("RGBA", (rect_w, rect_h), (0, 0, 0, 0))
    for y in range(rect_h):
        t = 0.0 if rect_h <= 1 else y / (rect_h - 1)
        color = tuple(_lerp(spec.fill_start[i], spec.fill_end[i], t) for i in range(4))
        ImageDraw.Draw(gradient).line((0, y, rect_w, y), fill=color)

    mask = _rounded_mask((rect_w, rect_h), radius)
    gradient.putalpha(mask)
    canvas.alpha_composite(gradient, dest=(rect[0], rect[1]))

    # ── Highlight strip at the top for glass depth ──────────────────
    highlight = Image.new("RGBA", (rect_w, rect_h), (0, 0, 0, 0))
    hl_height = max(1, rect_h // 5)
    for y in range(hl_height):
        t = 1.0 - (y / max(1, hl_height - 1))
        a = int(28 * t)  # subtle top highlight
        ImageDraw.Draw(highlight).line((0, y, rect_w, y), fill=(255, 255, 255, a))
    hl_mask = _rounded_mask((rect_w, rect_h), radius)
    highlight.putalpha(hl_mask)
    canvas.alpha_composite(highlight, dest=(rect[0], rect[1]))

    # ── Strokes ─────────────────────────────────────────────────────
    draw = ImageDraw.Draw(canvas)
    if spec.inner_stroke[3] > 0:
        draw.rounded_rectangle(rect, radius=radius, outline=spec.inner_stroke, width=scale)
    if spec.outer_stroke[3] > 0:
        draw.rounded_rectangle(rect, radius=radius, outline=spec.outer_stroke,
                               width=max(scale, (spec.line_width or 1) * scale))

    # Downsample to target size.
    canvas = canvas.resize((width, height), Image.LANCZOS)
    canvas.save(output_path)
    return output_path


def build_macos_panel_assets(themes_dir: Path, assets_dir: Path) -> list[str]:
    """Build dock background images for every installed theme that ships plank data."""
    created: list[str] = []
    for theme_dir in sorted(p for p in themes_dir.iterdir() if p.is_dir()):
        spec = load_plank_theme_spec(theme_dir)
        if spec is None:
            continue
        output = assets_dir / f"{theme_dir.name}__dock.png"
        render_plank_dock_background(spec, output)
        created.append(str(output))
    return created


def dock_background_path(theme_name: str, assets_dir: Path) -> Optional[Path]:
    candidate = assets_dir / f"{theme_name}__dock.png"
    if candidate.is_file():
        return candidate
    return None

