from __future__ import annotations

from pathlib import Path

from PIL import Image

from deskshot.environment.wallpaper_catalog import (
    build_wallpaper_catalog,
    select_wallpaper_candidates,
    select_wallpaper_sample_pool,
)


def _mk_image(path: Path, size=(160, 90)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (80, 120, 160)).save(path)


def test_wallpaper_catalog_classifies_and_prioritizes(tmp_path: Path) -> None:
    assets_dir = tmp_path / "assets"
    extracted_dir = tmp_path / "extracted"

    _mk_image(assets_dir / "wallpapers" / "deskshot-windows-bloom.png")
    _mk_image(assets_dir / "wallpapers" / "stone_bird.jpg")
    _mk_image(extracted_dir / "usr" / "share" / "backgrounds" / "tiles" / "circuit.png")
    _mk_image(extracted_dir / "usr" / "share" / "backgrounds" / "xfce" / "xfce-blue.jpg")

    catalog = build_wallpaper_catalog(extracted_dir=extracted_dir, assets_dir=assets_dir)
    by_stem = {entry.stem: entry for entry in catalog}

    assert by_stem["deskshot-windows-bloom"].kind == "abstract"
    assert "windows" in by_stem["deskshot-windows-bloom"].style_tags
    assert by_stem["stone_bird"].kind == "photo"
    assert "neutral" in by_stem["stone_bird"].style_tags
    assert by_stem["circuit"].kind == "pattern"

    windows_choices = select_wallpaper_candidates(
        "windows",
        extracted_dir=extracted_dir,
        assets_dir=assets_dir,
    )
    assert Path(windows_choices[0]).stem == "deskshot-windows-bloom"

    mac_choices = select_wallpaper_candidates(
        "macos",
        extracted_dir=extracted_dir,
        assets_dir=assets_dir,
    )
    assert Path(mac_choices[0]).stem == "stone_bird"


def test_wallpaper_sample_pool_prefers_realistic_assets(tmp_path: Path) -> None:
    assets_dir = tmp_path / "assets"
    extracted_dir = tmp_path / "extracted"

    _mk_image(assets_dir / "wallpapers" / "deskshot-macos-wave.png")
    _mk_image(extracted_dir / "usr" / "share" / "backgrounds" / "mate" / "nature" / "Aqua.jpg")
    _mk_image(extracted_dir / "usr" / "share" / "backgrounds" / "tiles" / "circuit.png")

    pool = select_wallpaper_sample_pool(
        "macos",
        extracted_dir=extracted_dir,
        assets_dir=assets_dir,
        max_size=4,
    )

    stems = [Path(path).stem for path in pool]
    assert "deskshot-macos-wave" in stems
    assert "Aqua" in stems
    assert "circuit" not in stems


def test_wallpaper_catalog_skips_blank_images(tmp_path: Path) -> None:
    assets_dir = tmp_path / "assets"
    extracted_dir = tmp_path / "extracted"

    _mk_image(assets_dir / "wallpapers" / "valid.jpg")
    Image.new("RGB", (160, 90), (255, 255, 255)).save(assets_dir / "wallpapers" / "blank.jpg")

    catalog = build_wallpaper_catalog(extracted_dir=extracted_dir, assets_dir=assets_dir)
    stems = {entry.stem for entry in catalog}
    assert "valid" in stems
    assert "blank" not in stems


def test_wallpaper_catalog_cache_avoids_reclassifying(tmp_path: Path, monkeypatch) -> None:
    """Classification decodes every image; a cache hit must skip that entirely."""
    from deskshot.environment import wallpaper_catalog as wc

    assets_dir = tmp_path / "assets"
    _mk_image(assets_dir / "wallpapers" / "one.jpg")
    _mk_image(assets_dir / "wallpapers" / "two.jpg")
    monkeypatch.setenv("DESKSHOT_WALLPAPER_CACHE", str(tmp_path / "catalog.json"))
    monkeypatch.setattr(wc, "_CATALOG_MEMO", {})

    first = wc.build_wallpaper_catalog(extracted_dir=tmp_path / "none", assets_dir=assets_dir)
    assert len(first) == 2

    calls = []
    real_blank = wc._is_effectively_blank
    monkeypatch.setattr(
        wc, "_is_effectively_blank", lambda p: calls.append(p) or real_blank(p)
    )

    # In-process memo hit.
    assert wc.build_wallpaper_catalog(extracted_dir=tmp_path / "none", assets_dir=assets_dir) == first
    # On-disk cache hit.
    monkeypatch.setattr(wc, "_CATALOG_MEMO", {})
    assert wc.build_wallpaper_catalog(extracted_dir=tmp_path / "none", assets_dir=assets_dir) == first

    assert calls == [], "cache hit must not decode any image"


def test_wallpaper_catalog_cache_invalidates_when_pool_changes(tmp_path: Path, monkeypatch) -> None:
    from deskshot.environment import wallpaper_catalog as wc

    assets_dir = tmp_path / "assets"
    _mk_image(assets_dir / "wallpapers" / "one.jpg")
    monkeypatch.setenv("DESKSHOT_WALLPAPER_CACHE", str(tmp_path / "catalog.json"))
    monkeypatch.setattr(wc, "_CATALOG_MEMO", {})

    first = wc.build_wallpaper_catalog(extracted_dir=tmp_path / "none", assets_dir=assets_dir)
    assert len(first) == 1

    _mk_image(assets_dir / "wallpapers" / "two.jpg")
    monkeypatch.setattr(wc, "_CATALOG_MEMO", {})
    second = wc.build_wallpaper_catalog(extracted_dir=tmp_path / "none", assets_dir=assets_dir)

    assert {e.stem for e in second} == {"one", "two"}


def test_wallpaper_catalog_recovers_from_corrupt_cache(tmp_path: Path, monkeypatch) -> None:
    from deskshot.environment import wallpaper_catalog as wc

    assets_dir = tmp_path / "assets"
    _mk_image(assets_dir / "wallpapers" / "one.jpg")
    cache = tmp_path / "catalog.json"
    cache.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("DESKSHOT_WALLPAPER_CACHE", str(cache))
    monkeypatch.setattr(wc, "_CATALOG_MEMO", {})

    catalog = wc.build_wallpaper_catalog(extracted_dir=tmp_path / "none", assets_dir=assets_dir)

    assert {e.stem for e in catalog} == {"one"}


def test_wallpaper_catalog_cache_failure_does_not_break_build(tmp_path: Path, monkeypatch) -> None:
    """An unwritable cache location must degrade to a normal rebuild."""
    from deskshot.environment import wallpaper_catalog as wc

    assets_dir = tmp_path / "assets"
    _mk_image(assets_dir / "wallpapers" / "one.jpg")
    monkeypatch.setenv("DESKSHOT_WALLPAPER_CACHE", "/proc/cannot/write/catalog.json")
    monkeypatch.setattr(wc, "_CATALOG_MEMO", {})

    catalog = wc.build_wallpaper_catalog(extracted_dir=tmp_path / "none", assets_dir=assets_dir)

    assert {e.stem for e in catalog} == {"one"}
