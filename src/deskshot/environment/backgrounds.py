"""Wallpaper selection and live root-window background helpers."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFilter, ImageOps

from deskshot.config import ThemeConfig

_STYLE_COLORS = {
    "linux": ("#4E8AA9", "#3B9440"),
    "windows": ("#4C83C3", "#2B5DA7"),
    "macos": ("#5F8FB5", "#3E7A6D"),
    "ubuntu": ("#77216F", "#E95420"),
}


def apply_desktop_background(
    theme: ThemeConfig,
    *,
    env: dict[str, str] | None = None,
    size: tuple[int, int] | None = None,
) -> bool:
    """Best-effort live background paint for the current X session."""
    runtime_env = dict(os.environ if env is None else env)
    applied = _apply_background_gsettings(theme, env=runtime_env)
    if runtime_env.get("DESKSHOT_DISABLE_ROOT_WALLPAPER_PAINT") == "1":
        return applied
    painted = _paint_root_background(theme, env=runtime_env, size=size)
    return painted or applied


def _apply_background_gsettings(
    theme: ThemeConfig,
    *,
    env: dict[str, str],
) -> bool:
    """Write background metadata into GSettings for MATE/Caja consumers."""
    applied = False
    primary, secondary = _STYLE_COLORS.get(
        (theme.desktop_style or "linux").strip().lower(),
        _STYLE_COLORS["linux"],
    )

    commands = [
        ["gsettings", "set", "org.mate.background", "draw-background", "true"],
        ["gsettings", "set", "org.mate.background", "show-desktop-icons", "true"],
        ["gsettings", "set", "org.mate.SettingsDaemon.plugins.background", "active", "true"],
    ]

    wallpaper = Path(theme.wallpaper) if theme.wallpaper else None
    if wallpaper and wallpaper.is_file():
        commands.extend(
            [
                ["gsettings", "set", "org.mate.background", "picture-filename", str(wallpaper)],
                ["gsettings", "set", "org.mate.background", "picture-options", "zoom"],
            ]
        )
    else:
        commands.extend(
            [
                ["gsettings", "set", "org.mate.background", "picture-filename", ""],
                ["gsettings", "set", "org.mate.background", "picture-options", "none"],
            ]
        )

    commands.extend(
        [
            ["gsettings", "set", "org.mate.background", "color-shading-type", "vertical"],
            ["gsettings", "set", "org.mate.background", "primary-color", primary],
            ["gsettings", "set", "org.mate.background", "secondary-color", secondary],
        ]
    )

    for cmd in commands:
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                timeout=5,
            )
            applied = applied or result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return applied

    return applied


def _paint_root_background(
    theme: ThemeConfig,
    *,
    env: dict[str, str],
    size: tuple[int, int] | None,
) -> bool:
    """Paint the current root window directly instead of post-processing."""
    viewport = size or _resolve_background_size(env)
    if viewport[0] <= 0 or viewport[1] <= 0:
        return False

    image_path = _render_root_background_image(theme, viewport)
    if image_path is not None:
        if _paint_root_background_with_display(image_path, env=env):
            return True

    primary, _ = _STYLE_COLORS.get(
        (theme.desktop_style or "linux").strip().lower(),
        _STYLE_COLORS["linux"],
    )
    xsetroot = _find_binary_on_path("xsetroot", env=env)
    if not xsetroot:
        return False
    try:
        result = subprocess.run(
            [xsetroot, "-solid", primary],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _resolve_background_size(env: dict[str, str]) -> tuple[int, int]:
    width = _parse_positive_int(env.get("DESKSHOT_DISPLAY_WIDTH", ""))
    height = _parse_positive_int(env.get("DESKSHOT_DISPLAY_HEIGHT", ""))
    if width > 0 and height > 0:
        return width, height
    return 1920, 1080


def _parse_positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _render_root_background_image(
    theme: ThemeConfig,
    size: tuple[int, int],
) -> Path | None:
    """Render the target wallpaper to a temporary PNG sized for the root window."""
    try:
        canvas = _build_background_canvas(theme, size)
    except Exception:
        return None
    temp_dir = Path(tempfile.mkdtemp(prefix="deskshot_rootbg_"))
    out = temp_dir / "root-background.png"
    canvas.save(out)
    return out


def _paint_root_background_with_display(path: Path, *, env: dict[str, str]) -> bool:
    """Use ImageMagick's display tool to paint an image onto the X root window."""
    display_bin = _find_binary_on_path("display", env=env)
    magick_bin = _find_binary_on_path("magick", env=env)

    candidates: list[list[str]] = []
    if display_bin:
        candidates.append([display_bin, "-window", "root", str(path)])
    if magick_bin:
        candidates.append([magick_bin, "display", "-window", "root", str(path)])

    try:
        for cmd in candidates:
            proc: subprocess.Popen[str] | None = None
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                )
                try:
                    proc.wait(timeout=0.8)
                    if proc.returncode == 0:
                        return True
                    continue
                except subprocess.TimeoutExpired:
                    # `display -window root` often stays resident after painting.
                    proc.terminate()
                    try:
                        proc.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=0.5)
                    return True
            except FileNotFoundError:
                continue
        return False
    finally:
        shutil.rmtree(path.parent, ignore_errors=True)


def stylize_screenshot_background(
    screenshot_path: Path,
    *,
    theme: ThemeConfig,
    elements: List[Dict[str, Any]],
    occlusion_meta: Optional[Dict[str, Any]] = None,
) -> bool:
    """Fallback background stylization when live desktop repaint is unavailable."""
    if not screenshot_path.is_file():
        return False

    with Image.open(screenshot_path) as image:
        base = image.convert("RGBA")
        wallpaper = _build_background_canvas(theme, base.size)
        mask = _build_background_mask(
            base,
            elements=elements,
            occlusion_meta=occlusion_meta,
            desktop_style=theme.desktop_style,
        )
        composed = Image.composite(wallpaper, base, mask)
        composed.save(screenshot_path)
    return True


def _build_background_canvas(theme: ThemeConfig, size: tuple[int, int]) -> Image.Image:
    width, height = size
    wallpaper = Path(theme.wallpaper) if theme.wallpaper else None
    if wallpaper and wallpaper.is_file():
        with Image.open(wallpaper) as source:
            return ImageOps.fit(
                source.convert("RGBA"),
                (width, height),
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
    primary, secondary = _STYLE_COLORS.get(
        (theme.desktop_style or "linux").strip().lower(),
        _STYLE_COLORS["linux"],
    )
    return _build_gradient_canvas(size, primary, secondary)


def _build_gradient_canvas(
    size: tuple[int, int],
    primary: str,
    secondary: str,
) -> Image.Image:
    width, height = size
    image = Image.new("RGBA", size)
    draw = ImageDraw.Draw(image)
    p0 = ImageColor.getrgb(primary)
    p1 = ImageColor.getrgb(secondary)
    denom = max(1, height - 1)
    for y in range(height):
        t = y / denom
        color = tuple(int(p0[i] * (1.0 - t) + p1[i] * t) for i in range(3))
        draw.line([(0, y), (width, y)], fill=color + (255,))
    return image


def _build_background_mask(
    base: Image.Image,
    *,
    elements: List[Dict[str, Any]],
    occlusion_meta: Optional[Dict[str, Any]],
    desktop_style: str = "linux",
) -> Image.Image:
    width, height = base.size
    mask = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(mask)

    for rect in _window_stack_rects(occlusion_meta):
        draw.rectangle(_rect_box(rect), fill=0)

    for elem in elements:
        rect = elem.get("rect", {})
        if not _valid_rect(rect):
            continue
        if elem.get("source") != "desktop_chrome":
            continue
        name = (elem.get("inner_text") or elem.get("name") or "").strip().lower()
        role = (elem.get("role") or "").strip().lower()
        if name == "desktop" and role in {"desktop frame", "frame", "window"}:
            continue
        if name in {"top panel", "bottom panel"} and role in {"frame", "window"}:
            draw.rectangle(_rect_box(rect), fill=0)

    primary, secondary = _STYLE_COLORS.get(
        desktop_style.strip().lower(),
        _STYLE_COLORS["linux"],
    )
    reference = _build_gradient_canvas((width, height), primary, secondary).convert("RGB")
    diff = ImageChops.difference(base.convert("RGB"), reference)
    dr, dg, db = diff.split()
    max_diff = ImageChops.lighter(ImageChops.lighter(dr, dg), db)

    low_threshold = 6
    high_threshold = 22
    foreground = max_diff.point(
        lambda v: 0 if v <= low_threshold else (
            255 if v >= high_threshold else int((v - low_threshold) * 255 / (high_threshold - low_threshold))
        ),
        mode="L",
    )
    foreground = foreground.filter(ImageFilter.MaxFilter(size=5))
    foreground = foreground.filter(ImageFilter.GaussianBlur(radius=1.0))
    desktop_replace = ImageChops.invert(foreground)
    mask = ImageChops.multiply(mask, desktop_replace)

    return mask


def _window_stack_rects(occlusion_meta: Optional[Dict[str, Any]]) -> Iterable[Dict[str, int]]:
    if not occlusion_meta:
        return []
    rects: List[Dict[str, int]] = []
    for window in occlusion_meta.get("window_stack", []):
        name = (window.get("name") or "").strip().lower()
        if name in {"desktop", "xfwm4"}:
            continue
        rect = window.get("rect", {})
        if _valid_rect(rect):
            rects.append(rect)
    return rects


def _valid_rect(rect: Dict[str, Any]) -> bool:
    return rect.get("w", 0) > 1 and rect.get("h", 0) > 1


def _rect_box(rect: Dict[str, int]) -> tuple[int, int, int, int]:
    return (
        rect["x"],
        rect["y"],
        rect["x"] + rect["w"] - 1,
        rect["y"] + rect["h"] - 1,
    )


def _find_binary_on_path(name: str, *, env: dict[str, str]) -> str | None:
    return shutil.which(name, path=env.get("PATH"))
