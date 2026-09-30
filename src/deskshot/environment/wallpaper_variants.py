"""Deterministic wallpaper variant generation for large-scale diversity."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageStat

from deskshot.config import ASSETS_DIR, EXTRACTED_DIR
from deskshot.environment.wallpaper_catalog import build_wallpaper_catalog


_OUTPUT_SUBDIR = ("usr", "share", "backgrounds", "deskshot-generated")
_TARGET_SIZE = (1600, 900)

_VARIANTS = (
    "center",
    "left_focus",
    "right_focus",
    "top_focus",
    "bottom_focus",
    "warm",
    "cool",
    "vivid",
    "muted",
    "high_contrast",
    "soft_contrast",
    "mirror",
    "soft_blur",
    "warm_mirror",
    "cool_vivid",
)


def _eligible_source_paths(
    *,
    extracted_dir: Path,
    assets_dir: Path,
    max_sources: int,
) -> list[Path]:
    rows = build_wallpaper_catalog(extracted_dir=extracted_dir, assets_dir=assets_dir)
    chosen: list[Path] = []
    for row in rows:
        path = Path(row.path)
        if "deskshot-generated" in path.parts:
            continue
        if row.extension == ".svg":
            continue
        if row.aspect_bucket == "portrait":
            continue
        if row.kind not in {"photo", "abstract"}:
            continue
        if not path.is_file():
            continue
        chosen.append(path)
        if len(chosen) >= max_sources:
            break
    return chosen


def _cover_resize(image: Image.Image, size: tuple[int, int], *, anchor: str) -> Image.Image:
    target_w, target_h = size
    src_w, src_h = image.size
    scale = max(target_w / max(1, src_w), target_h / max(1, src_h))
    resized = image.resize((max(1, int(round(src_w * scale))), max(1, int(round(src_h * scale)))), Image.LANCZOS)
    crop_w = max(0, resized.width - target_w)
    crop_h = max(0, resized.height - target_h)
    if anchor == "left_focus":
        left = 0
        top = crop_h // 2
    elif anchor == "right_focus":
        left = crop_w
        top = crop_h // 2
    elif anchor == "top_focus":
        left = crop_w // 2
        top = 0
    elif anchor == "bottom_focus":
        left = crop_w // 2
        top = crop_h
    else:
        left = crop_w // 2
        top = crop_h // 2
    return resized.crop((left, top, left + target_w, top + target_h))


def _tint(img: Image.Image, rgb_scale: tuple[float, float, float]) -> Image.Image:
    r, g, b = img.split()
    r = r.point(lambda v: max(0, min(255, int(v * rgb_scale[0]))))
    g = g.point(lambda v: max(0, min(255, int(v * rgb_scale[1]))))
    b = b.point(lambda v: max(0, min(255, int(v * rgb_scale[2]))))
    return Image.merge("RGB", (r, g, b))


def _render_variant(source: Path, variant: str, *, size: tuple[int, int]) -> Image.Image:
    with Image.open(source) as raw:
        img = raw.convert("RGB")

    anchor = variant if variant in {"left_focus", "right_focus", "top_focus", "bottom_focus"} else "center"
    out = _cover_resize(img, size, anchor=anchor)

    if variant == "warm":
        out = _tint(out, (1.05, 1.0, 0.93))
    elif variant == "cool":
        out = _tint(out, (0.94, 1.0, 1.06))
    elif variant == "vivid":
        out = ImageEnhance.Color(out).enhance(1.28)
    elif variant == "muted":
        out = ImageEnhance.Color(out).enhance(0.82)
    elif variant == "high_contrast":
        out = ImageEnhance.Contrast(out).enhance(1.18)
    elif variant == "soft_contrast":
        out = ImageEnhance.Contrast(out).enhance(0.9)
    elif variant == "mirror":
        out = ImageOps.mirror(out)
    elif variant == "soft_blur":
        out = out.filter(ImageFilter.GaussianBlur(radius=1.1))
    elif variant == "warm_mirror":
        out = _tint(ImageOps.mirror(out), (1.04, 1.0, 0.94))
    elif variant == "cool_vivid":
        out = ImageEnhance.Color(_tint(out, (0.95, 1.0, 1.05))).enhance(1.18)

    return out


def _is_usable_render(image: Image.Image) -> bool:
    sample = image.convert("RGB").copy()
    sample.thumbnail((64, 64))
    stat = ImageStat.Stat(sample)
    max_std = max(stat.stddev) if stat.stddev else 0.0
    mean = (sum(stat.mean) / len(stat.mean)) if stat.mean else 255.0
    if max_std < 3.0 and mean > 245.0:
        return False
    if max_std < 2.0 and mean < 10.0:
        return False
    return True


def build_wallpaper_variants(
    *,
    extracted_dir: Path | None = None,
    assets_dir: Path | None = None,
    max_sources: int = 100,
    variants: Iterable[str] = _VARIANTS,
    refresh: bool = False,
) -> dict[str, int | str]:
    """Generate a large deterministic wallpaper variant pool.

    Returns a small summary with the source count and generated file count.
    """
    extracted = extracted_dir or EXTRACTED_DIR
    assets = assets_dir or ASSETS_DIR
    out_dir = extracted.joinpath(*_OUTPUT_SUBDIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    marker = out_dir / "manifest.json"

    variant_list = list(variants)
    if marker.is_file() and not refresh:
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            expected = int(data.get("generated_files", 0))
            actual = len(list(out_dir.glob("*.jpg")))
            if actual >= expected > 0:
                return data
        except Exception:
            pass

    sources = _eligible_source_paths(
        extracted_dir=extracted,
        assets_dir=assets,
        max_sources=max_sources,
    )
    usable_sources: list[Path] = []
    for source in sources:
        preview = _render_variant(source, "center", size=_TARGET_SIZE)
        if _is_usable_render(preview):
            usable_sources.append(source)

    if refresh:
        for path in out_dir.glob("*.jpg"):
            path.unlink()

    for source in usable_sources:
        digest = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:8]
        stem = f"{source.stem.replace(' ', '_')}-{digest}"
        for variant in variant_list:
            out_path = out_dir / f"{stem}__{variant}.jpg"
            if out_path.is_file() and not refresh:
                continue
            image = _render_variant(source, variant, size=_TARGET_SIZE)
            if not _is_usable_render(image):
                continue
            image.save(out_path, format="JPEG", quality=86)

    generated = len(list(out_dir.glob("*.jpg")))

    summary: dict[str, int | str] = {
        "source_count": len(usable_sources),
        "variants_per_source": len(variant_list),
        "generated_files": generated,
        "output_dir": str(out_dir),
    }
    marker.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
