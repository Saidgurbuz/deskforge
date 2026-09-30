from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from deskshot.environment.wallpaper_variants import build_wallpaper_variants


def _mk_image(path: Path, size=(1600, 900), color=(90, 120, 150)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)


def test_build_wallpaper_variants_generates_expected_outputs(tmp_path: Path) -> None:
    assets_dir = tmp_path / "assets"
    extracted_dir = tmp_path / "extracted"

    _mk_image(assets_dir / "wallpapers" / "forest.jpg")
    _mk_image(extracted_dir / "usr" / "share" / "backgrounds" / "mate" / "stone_bird.jpg")

    summary = build_wallpaper_variants(
        extracted_dir=extracted_dir,
        assets_dir=assets_dir,
        max_sources=2,
        variants=("center", "warm", "mirror"),
        refresh=True,
    )

    out_dir = extracted_dir / "usr" / "share" / "backgrounds" / "deskshot-generated"
    generated = sorted(out_dir.glob("*.jpg"))

    assert summary["source_count"] == 2
    assert summary["variants_per_source"] == 3
    assert summary["generated_files"] == 6
    assert len(generated) == 6

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["generated_files"] == 6


def test_build_wallpaper_variants_skips_blank_sources(tmp_path: Path) -> None:
    assets_dir = tmp_path / "assets"
    extracted_dir = tmp_path / "extracted"

    _mk_image(assets_dir / "wallpapers" / "forest.jpg")
    _mk_image(assets_dir / "wallpapers" / "blank.jpg", color=(255, 255, 255))

    summary = build_wallpaper_variants(
        extracted_dir=extracted_dir,
        assets_dir=assets_dir,
        max_sources=2,
        variants=("center",),
        refresh=True,
    )

    out_dir = extracted_dir / "usr" / "share" / "backgrounds" / "deskshot-generated"
    generated = sorted(out_dir.glob("*.jpg"))

    assert summary["source_count"] == 1
    assert len(generated) == 1
