from __future__ import annotations

from pathlib import Path

from deskshot.config import DesktopFixtureConfig
from deskshot.environment.desktop_fixture import materialize_desktop_fixture
from deskshot.environment.diversity import resolve_desktop_fixture_config


def test_resolve_desktop_fixture_config_samples_profile_ranges() -> None:
    a = resolve_desktop_fixture_config(DesktopFixtureConfig(seed=7, profile="dense"))
    b = resolve_desktop_fixture_config(DesktopFixtureConfig(seed=17, profile="dense"))

    assert 6 <= a.num_folders <= 12
    assert 8 <= a.num_files <= 16
    assert 6 <= b.num_folders <= 12
    assert 8 <= b.num_files <= 16
    assert (a.num_folders, a.num_files) != (b.num_folders, b.num_files)


def test_materialize_desktop_fixture_uses_plain_names(tmp_path: Path) -> None:
    root = tmp_path / "xdg"
    xdg_config = root / "config"
    fixture = DesktopFixtureConfig(enabled=True, seed=23, profile="balanced")

    created = materialize_desktop_fixture(root=root, xdg_config_home=xdg_config, fixture=fixture)
    desktop_dir = created["desktop_dir"]
    names = sorted(p.name for p in desktop_dir.iterdir())

    # Desktop items are named the way a person names them. The old `09_` slot
    # prefix existed to scramble Caja's alphabetical fallback, but icons are
    # positioned explicitly now and the prefix was drawn on every icon.
    assert names
    assert not any(name[:2].isdigit() and name[2:3] == "_" for name in names)
    assert len(set(names)) == len(names)
    assert any((desktop_dir / name).is_dir() for name in names)
    assert any((desktop_dir / name).is_file() for name in names)


def test_materialize_desktop_fixture_preserves_resolved_layout_and_content_pack(tmp_path: Path) -> None:
    result = materialize_desktop_fixture(
        root=tmp_path,
        xdg_config_home=tmp_path / "config",
        fixture=DesktopFixtureConfig(
            enabled=True,
            seed=31,
            profile="balanced",
            layout_template="center_cluster",
            content_pack="engineering_dev",
            num_folders=3,
            num_files=4,
        ),
    )

    names = sorted(p.name for p in result["desktop_dir"].iterdir())

    assert result["layout"] == "center_cluster"
    assert result["content_pack"] == "engineering_dev"
    assert any("pyproject" in name or "README" in name or "AGENTS" in name for name in names)
