#!/usr/bin/env python
"""Render one episode as a slide: instruction, steps, verification, progress.

Built for explaining the long-horizon work to someone who has not seen it. The
thing worth showing is not that a trajectory is long - it is that a single
high-level instruction decomposes into steps that each carry their own
sub-instruction and their own check, and that the goal is only satisfied at the
end. So every frame on the slide is annotated with all three.

Frames are cropped to the window being acted on rather than shown whole. A
2880x1800 desktop shrunk into a grid cell is unreadable, and the point of the
slide is that a reader can see what changed at each step.

Usage:
    PYTHONPATH=src python scripts/make_episode_slide.py \
        --episode incremental_checks/v190_cross_app/task-* \
        --output incremental_checks/vXXX/episode_slide.png
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

FONT_DIR = "/usr/share/fonts"
INK = (24, 26, 32)
MUTED = (110, 118, 132)
RULE = (222, 226, 234)
PAPER = (250, 250, 252)
OK = (28, 150, 88)
ACCENT = (196, 92, 24)
APP_TINT = {0: (46, 96, 190), 1: (150, 58, 140)}


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    for path in glob.glob(f"{FONT_DIR}/**/{name}", recursive=True):
        return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def load_episode(directory: Path) -> Dict[str, Any]:
    return json.loads((directory / "task.json").read_text(encoding="utf-8"))


def _step_apps(episode: Dict[str, Any], steps: List[Dict[str, Any]]) -> List[str]:
    """Which app each *step* drives, by walking the plan alongside the steps.

    Keying a dict by skill name looked equivalent and was not: a cross-app plan
    contains `open_menu(Edit)` once per app, so the second entry overwrote the
    first and every frame in the first segment was cropped to the wrong window.
    Walking both sequences keeps duplicates distinct, and skips plan entries
    that emitted no step - a satisfied skill contributes none.
    """
    plan = episode.get("plan") or []
    apps: List[str] = []
    current = ""
    cursor = 0
    for step in steps:
        while cursor < len(plan):
            name = str(plan[cursor].get("skill") or "")
            match = re.match(r"focus\((.+)\)$", name)
            if match:
                current = match.group(1)
            if name == step["skill"]:
                cursor += 1
                break
            cursor += 1
        apps.append(current)
    return apps


def _window_box(
    directory: Path, stem: str, app: str, size: Tuple[int, int]
) -> Tuple[int, int, int, int]:
    """The acted-on app's window, padded.

    The window rather than the dialog inside it: cropping to the dialog alone
    makes the change legible but removes the context that says *where* it is
    happening - which app, which menu it came from, what else is on screen. A
    reader needs to see the window to follow the story.
    """
    width, height = size
    path = directory / f"{stem}.elements.leaf.json"
    if not path.is_file():
        return (0, 0, width, height)
    data = json.loads(path.read_text(encoding="utf-8"))
    elements = data if isinstance(data, list) else data.get("elements", [])

    def _rects(roles: Tuple[str, ...]) -> List[Dict[str, Any]]:
        return [
            e["rect"] for e in elements
            if str(e.get("role")) in roles
            and (not app or e.get("app_name") == app)
            and isinstance(e.get("rect"), dict)
            and int(e["rect"].get("w", 0)) > 80 and int(e["rect"].get("h", 0)) > 80
        ]

    boxes = _rects(("frame", "window")) or _rects(("dialog", "alert", "file chooser"))
    if not boxes:
        return (0, 0, width, height)
    pad = 46
    x0 = max(0, min(b["x"] for b in boxes) - pad)
    y0 = max(0, min(b["y"] for b in boxes) - pad)
    x1 = min(width, max(b["x"] + b["w"] for b in boxes) + pad)
    y1 = min(height, max(b["y"] + b["h"] for b in boxes) + pad)
    return (x0, y0, x1, y1)


def _wrap(draw, text: str, font, max_width: int) -> List[str]:
    words, lines, line = text.split(), [], ""
    for word in words:
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width:
            line = trial
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def _fit_aspect(img: Image.Image, aspect: float) -> Image.Image:
    """Centre `img` on a canvas of the given aspect, padding with its own paper."""
    width, height = img.size
    if width / height >= aspect:
        canvas_w, canvas_h = width, int(round(width / aspect))
    else:
        canvas_w, canvas_h = int(round(height * aspect)), height
    canvas = Image.new("RGB", (canvas_w, canvas_h), PAPER)
    canvas.paste(img, ((canvas_w - width) // 2, (canvas_h - height) // 2))
    return canvas


def _write_pptx(
    pptx_path: Path,
    episode: Dict[str, Any],
    steps: List[Dict[str, Any]],
    crops: List[Tuple[Image.Image, Dict[str, Any], str]],
    png_path: Path,
    title: str,
) -> None:
    """A real slide, not a picture of one.

    The rendered PNG goes on a second slide as a fallback, but the first slide
    carries the title, instruction, screenshots and captions as separate shapes,
    so every piece of text can be edited in PowerPoint rather than retyped.
    """
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Emu, Inches, Pt

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    blank = prs.slide_layouts[6]
    slide = prs.slides.add_slide(blank)

    def _text(x, y, w, h, text, size, *, bold=False, color=(24, 26, 32), mono=False):
        box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        frame = box.text_frame
        frame.word_wrap = True
        para = frame.paragraphs[0]
        run = para.add_run()
        run.text = text
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = RGBColor(*color)
        if mono:
            run.font.name = "Consolas"
        return box

    _text(0.45, 0.25, 12.4, 0.6, title, 28, bold=True)
    _text(0.45, 0.86, 12.4, 0.3, "Instruction given to the pipeline", 11,
          color=MUTED)
    _text(0.45, 1.12, 12.4, 0.8, episode["instruction"], 13)

    cols = 5 if len(steps) > 6 else 3
    rows = (len(steps) + cols - 1) // cols
    left0, top0 = 0.45, 2.05
    cell_w = 12.4 / cols
    cell_h = (7.5 - top0 - 0.7) / rows

    tmp_dir = pptx_path.parent / "_slide_frames"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    for index, (shot, step, _app) in enumerate(crops):
        col, row = index % cols, index // cols
        x = left0 + col * cell_w
        y = top0 + row * cell_h
        frame_png = tmp_dir / f"step_{index:02d}.png"
        shot.save(frame_png)
        pic_h = cell_h - 0.62
        pic_w = pic_h * shot.width / shot.height
        if pic_w > cell_w - 0.14:
            pic_w = cell_w - 0.14
            pic_h = pic_w * shot.height / shot.width
        slide.shapes.add_picture(
            str(frame_png), Inches(x + (cell_w - pic_w) / 2), Inches(y),
            width=Inches(pic_w), height=Inches(pic_h),
        )
        mark = "verified" if step["subgoal_met"] else "FAILED"
        _text(x, y + pic_h + 0.02, cell_w, 0.24,
              f"{index + 1}. {mark}  -  goal {step['goal_progress'] * 100:.0f}%", 10,
              color=OK if step["subgoal_met"] else ACCENT)
        _text(x, y + pic_h + 0.24, cell_w, 0.34, step["intent"], 10, mono=True)

    verified = sum(1 for s in steps if s["subgoal_met"])
    changed = sum(1 for s in steps if s["changed"])
    _text(0.45, 7.0, 12.4, 0.4,
          f"{verified}/{len(steps)} steps verified against their own postcondition   |   "
          f"{changed}/{len(steps)} changed the screen   |   "
          f"goal satisfied only at the final step   |   "
          f"plan derived from the apps themselves, not hand-written",
          10, color=MUTED)

    picture_slide = prs.slides.add_slide(blank)
    picture_slide.shapes.add_picture(
        str(png_path), Inches(0), Inches(0.6), width=Inches(13.333)
    )

    pptx_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(pptx_path))


def build_slide(
    directory: Path,
    out_path: Path,
    *,
    columns: int = 3,
    max_steps: int = 0,
    title: str = "Long-horizon tasks with per-step verification",
    pptx_path: Optional[Path] = None,
) -> None:
    episode = load_episode(directory)
    steps = episode["steps"][: max_steps or None]
    apps = list(episode.get("metadata", {}).get("apps") or [episode.get("app_name") or ""])

    h1 = _font("DejaVuSans-Bold.ttf", 40)
    h2 = _font("DejaVuSans.ttf", 25)
    mono = _font("DejaVuSansMono.ttf", 19)
    cap = _font("DejaVuSans-Bold.ttf", 20)
    small = _font("DejaVuSans.ttf", 18)

    tile_w = 560
    gap = 26
    pad = 52
    rows = (len(steps) + columns - 1) // columns

    # First pass: crop every frame so the tile height can be derived from them.
    step_apps = _step_apps(episode, steps)
    crops: List[Tuple[Image.Image, Dict[str, Any], str]] = []
    for step, step_app in zip(steps, step_apps):
        stem = step["observation_stem"]
        png = directory / f"{stem}.png"
        img = Image.open(png).convert("RGB")
        app = step_app or (apps[0] if apps else "")
        img = img.crop(_window_box(directory, stem, app, img.size))
        img.thumbnail((tile_w, 10_000), Image.LANCZOS)
        crops.append((img, step, app))

    shot_h = max(c[0].height for c in crops)
    caption_h = 96
    tile_h = shot_h + caption_h

    header_h = 244
    footer_h = 96
    width = pad * 2 + columns * tile_w + (columns - 1) * gap
    height = header_h + rows * (tile_h + gap) - gap + footer_h + pad

    slide = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(slide)

    # ---- header -------------------------------------------------------------
    draw.text((pad, 44), title, font=h1, fill=INK)
    label = "Instruction given to the pipeline"
    draw.text((pad, 104), label, font=small, fill=MUTED)
    instruction = episode["instruction"]
    for i, line in enumerate(_wrap(draw, instruction, h2, width - pad * 2 - 20)[:3]):
        draw.text((pad, 130 + i * 32), line, font=h2, fill=INK)
    draw.line((pad, header_h - 18, width - pad, header_h - 18), fill=RULE, width=2)

    # ---- tiles --------------------------------------------------------------
    for index, (shot, step, app) in enumerate(crops):
        col, row = index % columns, index // columns
        x = pad + col * (tile_w + gap)
        y = header_h + row * (tile_h + gap)
        tint = APP_TINT.get(apps.index(app) if app in apps else 0, APP_TINT[0])

        draw.rectangle([x - 2, y - 2, x + tile_w + 2, y + tile_h + 2], fill=(255, 255, 255))
        sx = x + (tile_w - shot.width) // 2
        slide.paste(shot, (sx, y))
        draw.rectangle([sx, y, sx + shot.width, y + shot.height], outline=RULE, width=1)

        cy = y + shot_h + 12
        # Step number chip, coloured by which app the step drives.
        chip = f"{index + 1}"
        cw = int(draw.textlength(chip, font=cap)) + 20
        draw.rounded_rectangle([x, cy, x + cw, cy + 30], 8, fill=tint)
        draw.text((x + 10, cy + 4), chip, font=cap, fill=(255, 255, 255))

        mark = "verified" if step["subgoal_met"] else "failed"
        draw.text((x + cw + 12, cy + 5), mark, font=small, fill=OK if step["subgoal_met"] else ACCENT)

        pct = f"goal {step['goal_progress'] * 100:.0f}%"
        draw.text((x + tile_w - draw.textlength(pct, font=small), cy + 5),
                  pct, font=small, fill=MUTED)

        for i, line in enumerate(_wrap(draw, step["intent"], mono, tile_w)[:2]):
            draw.text((x, cy + 38 + i * 24), line, font=mono, fill=INK)

        # Progress bar: the goal is only satisfied at the end, and the slide
        # should make that visible at a glance.
        by = y + tile_h - 8
        draw.rectangle([x, by, x + tile_w, by + 6], fill=(232, 234, 240))
        filled = int(tile_w * float(step["goal_progress"]))
        if filled:
            draw.rectangle([x, by, x + filled, by + 6], fill=OK)

    # ---- footer -------------------------------------------------------------
    fy = header_h + rows * (tile_h + gap) - gap + 26
    draw.line((pad, fy, width - pad, fy), fill=RULE, width=2)
    verified = sum(1 for s in steps if s["subgoal_met"])
    changed = sum(1 for s in steps if s["changed"])
    facts = (
        f"{verified}/{len(steps)} steps verified against their own postcondition     "
        f"{changed}/{len(steps)} changed the screen     "
        f"goal satisfied only at the final step     "
        f"plan derived from the apps themselves, not hand-written"
    )
    draw.text((pad, fy + 22), facts, font=small, fill=MUTED)

    # Letterbox onto a true 16:9 canvas. Composing straight to the grid gave
    # 0.71:1 - portrait - which is not a slide shape, and a deck will either
    # letterbox it itself or crop it.
    slide = _fit_aspect(slide, 16 / 9)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    slide.save(out_path)
    print(f"{out_path}  {slide.size[0]}x{slide.size[1]}"
          f"  ratio={slide.size[0] / slide.size[1]:.2f}  steps={len(steps)}")

    if pptx_path is not None:
        _write_pptx(pptx_path, episode, steps, crops, out_path, title)
        print(f"{pptx_path}  editable slide")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--episode", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--columns", type=int, default=3)
    ap.add_argument("--max-steps", type=int, default=0)
    ap.add_argument("--title", default="Long-horizon tasks with per-step verification")
    ap.add_argument("--pptx", default=None, help="Also write an editable .pptx here")
    args = ap.parse_args()

    matches = sorted(glob.glob(args.episode))
    if not matches:
        print(f"no episode at {args.episode}")
        return 1
    build_slide(
        Path(matches[0]), Path(args.output),
        columns=args.columns, max_steps=args.max_steps, title=args.title,
        pptx_path=Path(args.pptx) if args.pptx else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
