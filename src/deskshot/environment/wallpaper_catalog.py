"""Wallpaper cataloging and deterministic sampling helpers."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from PIL import Image, ImageStat

from deskshot.config import ASSETS_DIR, EXTRACTED_DIR, TOOLS_DIR

logger = logging.getLogger(__name__)

# Bump when the entry schema or classification rules change.
_CATALOG_CACHE_VERSION = 2

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".svg"}

_STYLE_HINTS = {
    "windows": ("windows", "win11", "bloom", "redmond"),
    "macos": ("macos", "tahoe", "quartz", "sonoma", "bigsur"),
    "ubuntu": ("ubuntu", "yaru"),
    "linux": ("xfce", "mate", "gnome", "fedora", "redhat"),
}

_PHOTO_HINTS = (
    "earth",
    "space",
    "stone",
    "bird",
    "flower",
    "flowers",
    "ladybug",
    "ladybugs",
    "leaf",
    "leaves",
    "nature",
    "ocean",
    "mountain",
    "desert",
    "cloud",
    "forest",
)
_PATTERN_HINTS = (
    "tile",
    "pattern",
    "circuit",
    "numbers",
    "wood",
    "marble",
    "moss",
    "brushed",
    "floral",
    "bark",
    "weave",
    "stonewall",
)
_ABSTRACT_HINTS = (
    "wave",
    "waves",
    "stripe",
    "stripes",
    "shape",
    "shapes",
    "vertical",
    "teal",
    "bloom",
    "neutral",
    "blue",
    "orange",
    "gradient",
    "diffraction",
    "plasma",
)


@dataclass(frozen=True)
class WallpaperEntry:
    path: str
    stem: str
    extension: str
    width: int
    height: int
    aspect_bucket: str
    kind: str
    style_tags: tuple[str, ...]


def _scan_wallpaper_paths(
    extracted_dir: Path | None = None,
    assets_dir: Path | None = None,
) -> List[Path]:
    found: list[Path] = []
    roots = [
        (assets_dir or ASSETS_DIR) / "wallpapers",
        (extracted_dir or EXTRACTED_DIR) / "usr" / "share" / "backgrounds",
        (extracted_dir or EXTRACTED_DIR) / "usr" / "share" / "wallpapers",
    ]
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix.lower() in _IMAGE_EXTS:
                found.append(path.resolve())
    return found


def _safe_image_size(path: Path) -> tuple[int, int]:
    if path.suffix.lower() == ".svg":
        return (0, 0)
    try:
        with Image.open(path) as img:
            return int(img.width), int(img.height)
    except Exception:
        return (0, 0)


def _is_effectively_blank(path: Path) -> bool:
    if path.suffix.lower() == ".svg":
        return False
    try:
        with Image.open(path) as img:
            sample = img.convert("RGB")
            sample.thumbnail((64, 64))
            stat = ImageStat.Stat(sample)
    except Exception:
        return True

    max_std = max(stat.stddev) if stat.stddev else 0.0
    mean = (sum(stat.mean) / len(stat.mean)) if stat.mean else 255.0
    if max_std < 3.0 and mean > 245.0:
        return True
    if max_std < 2.0 and mean < 10.0:
        return True
    return False


def _aspect_bucket(width: int, height: int) -> str:
    if width <= 0 or height <= 0:
        return "unknown"
    ratio = width / max(1, height)
    if ratio >= 2.1:
        return "ultrawide"
    if ratio >= 1.6:
        return "wide"
    if ratio >= 1.2:
        return "standard"
    if ratio >= 0.9:
        return "squareish"
    return "portrait"


def _style_tags(path: Path) -> tuple[str, ...]:
    haystack = f"{path.as_posix().lower()} {path.stem.lower()}"
    tags = [style for style, hints in _STYLE_HINTS.items() if any(h in haystack for h in hints)]
    if not tags:
        tags = ["neutral"]
    elif "neutral" not in tags and any(h in haystack for h in _PHOTO_HINTS + _ABSTRACT_HINTS):
        tags.append("neutral")
    return tuple(sorted(set(tags)))


def _kind(path: Path) -> str:
    haystack = f"{path.as_posix().lower()} {path.stem.lower()}"
    if path.suffix.lower() == ".svg":
        return "vector"
    if "/tiles/" in haystack or any(h in haystack for h in _PATTERN_HINTS):
        return "pattern"
    if any(h in haystack for h in _PHOTO_HINTS):
        return "photo"
    if any(h in haystack for h in _ABSTRACT_HINTS):
        return "abstract"
    if path.suffix.lower() in {".jpg", ".jpeg"}:
        return "photo"
    return "abstract"


def _catalog_fingerprint(paths: List[Path]) -> str:
    """Cheap stat-only signature of the wallpaper set."""
    digest = hashlib.sha256()
    digest.update(f"v{_CATALOG_CACHE_VERSION}:{len(paths)}".encode())
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(f"\n{path}:{stat.st_size}:{int(stat.st_mtime)}".encode())
    return digest.hexdigest()


def _catalog_cache_path(fingerprint: str) -> Path:
    """Cache file for one wallpaper set.

    The name carries the fingerprint so different pools (a test fixture, a
    refreshed wallpaper install) never overwrite each other's entry. A single
    shared file would let concurrent jobs thrash it.
    """
    override = os.environ.get("DESKSHOT_WALLPAPER_CACHE")
    if override:
        return Path(override)
    return TOOLS_DIR / "cache" / f"wallpaper_catalog_{fingerprint[:16]}.json"


def _load_cached_catalog(fingerprint: str) -> Optional[List[WallpaperEntry]]:
    cache_path = _catalog_cache_path(fingerprint)
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if payload.get("fingerprint") != fingerprint:
        return None
    try:
        return [WallpaperEntry(**{**row, "style_tags": tuple(row["style_tags"])})
                for row in payload["entries"]]
    except (KeyError, TypeError):
        return None


def _store_cached_catalog(fingerprint: str, entries: List[WallpaperEntry]) -> None:
    """Write the catalog atomically so concurrent jobs cannot read a torn file."""
    cache_path = _catalog_cache_path(fingerprint)
    payload = {
        "fingerprint": fingerprint,
        "entries": [asdict(entry) for entry in entries],
    }
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(
            dir=str(cache_path.parent), prefix=".wallpaper_catalog_"
        )
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        # mkstemp is 0600; jobs share this project dir under one group.
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, cache_path)
    except OSError:
        logger.debug("Could not persist wallpaper catalog cache", exc_info=True)


# Keyed by fingerprint so one process never rescans the same wallpaper set.
_CATALOG_MEMO: dict[str, List[WallpaperEntry]] = {}


def build_wallpaper_catalog(
    extracted_dir: Path | None = None,
    assets_dir: Path | None = None,
) -> List[WallpaperEntry]:
    """Catalog the wallpaper pool, reusing a cached result when unchanged.

    Classifying an entry decodes the image to reject blank wallpapers. With a
    1400+ file pool that is hundreds of MB of reads, and it previously ran on
    every session start, which dominated per-scene cost. The pool only changes
    during setup, so a stat-based fingerprint is enough to reuse the result.
    """
    paths = _scan_wallpaper_paths(extracted_dir=extracted_dir, assets_dir=assets_dir)
    fingerprint = _catalog_fingerprint(paths)

    memoized = _CATALOG_MEMO.get(fingerprint)
    if memoized is not None:
        return list(memoized)

    cached = _load_cached_catalog(fingerprint)
    if cached is not None:
        _CATALOG_MEMO[fingerprint] = cached
        return list(cached)

    entries = _classify_wallpaper_paths(paths)
    _CATALOG_MEMO[fingerprint] = entries
    _store_cached_catalog(fingerprint, entries)
    return list(entries)


def _probe_wallpaper(path: Path) -> tuple[int, int, bool]:
    """Return (width, height, blank) for one wallpaper."""
    return (*_safe_image_size(path), _is_effectively_blank(path))


def _classify_wallpaper_paths(paths: Iterable[Path]) -> List[WallpaperEntry]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)

    # Classification is dominated by reading ~1.7GB off a shared filesystem, not
    # by decoding, so overlapping the reads is a large win on the one-time build.
    # map() preserves order, keeping the catalog deterministic.
    workers = max(1, min(16, (os.cpu_count() or 4) * 4))
    if len(unique) > 1 and workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            probes = list(pool.map(_probe_wallpaper, unique))
    else:
        probes = [_probe_wallpaper(path) for path in unique]

    entries: list[WallpaperEntry] = []
    for path, (width, height, blank) in zip(unique, probes):
        if width > 0 and height > 0 and blank:
            continue
        entries.append(
            WallpaperEntry(
                path=str(path),
                stem=path.stem,
                extension=path.suffix.lower(),
                width=width,
                height=height,
                aspect_bucket=_aspect_bucket(width, height),
                kind=_kind(path),
                style_tags=_style_tags(path),
            )
        )
    return entries


def list_wallpaper_catalog(
    extracted_dir: Path | None = None,
    assets_dir: Path | None = None,
) -> List[dict]:
    return [asdict(entry) for entry in build_wallpaper_catalog(extracted_dir, assets_dir)]


def select_wallpaper_candidates(
    desktop_style: str,
    *,
    extracted_dir: Path | None = None,
    assets_dir: Path | None = None,
) -> List[str]:
    """Return style-ordered wallpaper paths for a desktop style."""
    style = (desktop_style or "linux").strip().lower()
    catalog = build_wallpaper_catalog(extracted_dir=extracted_dir, assets_dir=assets_dir)

    def _priority(entry: WallpaperEntry) -> tuple[int, int, int, int, str]:
        tags = set(entry.style_tags)
        style_match = 0 if style in tags else (1 if "neutral" in tags else 2)
        kind_priority = {"photo": 0, "abstract": 1, "pattern": 2, "vector": 3}.get(entry.kind, 4)
        aspect_priority = {"wide": 0, "standard": 1, "ultrawide": 2, "squareish": 3, "portrait": 4, "unknown": 5}.get(
            entry.aspect_bucket, 5
        )
        vector_penalty = 1 if entry.extension == ".svg" else 0
        return (style_match, kind_priority, aspect_priority, vector_penalty, entry.path)

    ordered = sorted(catalog, key=_priority)
    return [entry.path for entry in ordered]


def select_wallpaper_sample_pool(
    desktop_style: str,
    *,
    extracted_dir: Path | None = None,
    assets_dir: Path | None = None,
    max_size: int = 1024,
) -> List[str]:
    """Return a realistic high-priority wallpaper pool for seeded sampling.

    The full catalog can include many tiling/pattern assets that are useful as
    tail diversity but should not dominate default seeded generation. This pool
    keeps photo/abstract assets first and only falls back to patterns when the
    filtered set is too small.
    """
    catalog = build_wallpaper_catalog(extracted_dir=extracted_dir, assets_dir=assets_dir)
    if not catalog:
        return []

    style = (desktop_style or "linux").strip().lower()
    ordered_paths = select_wallpaper_candidates(style, extracted_dir=extracted_dir, assets_dir=assets_dir)
    entry_by_path = {entry.path: entry for entry in catalog}

    def _fits(entry: WallpaperEntry) -> bool:
        tags = set(entry.style_tags)
        if style not in tags and "neutral" not in tags:
            return False
        if entry.aspect_bucket == "portrait":
            return False
        return entry.kind in {"photo", "abstract"}

    prioritized = [path for path in ordered_paths if _fits(entry_by_path[path])]
    if len(prioritized) >= min(8, max_size):
        return prioritized[:max_size]

    # Fallback: fill with any remaining non-portrait, non-pattern candidates.
    # If the style really has no realistic pool, fall back to the ordered list.
    seen = set(prioritized)
    for path in ordered_paths:
        if path in seen:
            continue
        entry = entry_by_path[path]
        if entry.aspect_bucket == "portrait" or entry.kind == "pattern":
            continue
        prioritized.append(path)
        seen.add(path)
        if len(prioritized) >= max_size:
            return prioritized
    if prioritized:
        return prioritized[:max_size]
    return ordered_paths[:max_size]
