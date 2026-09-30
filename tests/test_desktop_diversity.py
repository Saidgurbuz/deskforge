from pathlib import Path

from deskshot.config import DesktopFixtureConfig
from deskshot.environment.desktop_fixture import (
    compute_icon_positions,
    materialize_desktop_fixture,
)
from deskshot.environment.diversity import (
    DESKTOP_CONTENT_PACKS,
    DESKTOP_LAYOUT_TEMPLATES,
    get_display_preset,
    list_display_presets,
    resolve_panel_variant,
    resolve_desktop_fixture_config,
    sample_desktop_content_pack,
    sample_desktop_layout_template,
    sample_display_preset,
    sample_panel_variant,
)
from deskshot.environment.mate_config import _build_panel_layout_file


def test_display_presets_are_exposed() -> None:
    presets = list_display_presets()
    assert "wxga_1366x768" in presets
    assert presets["fhd_1920x1080"]["width"] == 1920


def test_get_and_sample_display_preset_are_deterministic() -> None:
    preset = get_display_preset("hdplus_1600x900", display_number=3)
    assert preset.display_number == 3
    assert (preset.width, preset.height) == (1600, 900)

    name_a, sampled_a = sample_display_preset(17)
    name_b, sampled_b = sample_display_preset(17)
    assert name_a == name_b
    assert sampled_a.screen_str == sampled_b.screen_str


def test_fixture_profile_resolution_fills_default_counts() -> None:
    resolved = resolve_desktop_fixture_config(
        DesktopFixtureConfig(profile="dense", num_folders=0, num_files=0)
    )
    assert resolved.num_folders >= 7
    assert resolved.num_files >= 10
    assert resolved.layout_template in DESKTOP_LAYOUT_TEMPLATES
    assert resolved.content_pack in DESKTOP_CONTENT_PACKS


def test_materialize_desktop_fixture_creates_desktop_files(tmp_path: Path) -> None:
    xdg_config = tmp_path / "config"
    fixture = DesktopFixtureConfig(seed=11, profile="sparse")
    resolved = resolve_desktop_fixture_config(fixture)
    result = materialize_desktop_fixture(
        root=tmp_path,
        xdg_config_home=xdg_config,
        fixture=fixture,
    )

    desktop = result["desktop_dir"]
    names = sorted(p.name for p in desktop.iterdir())
    # The home directory is named for the session persona, not the account
    # running the capture: its absolute path is drawn in app title bars.
    assert result["home_dir"].parent == tmp_path / "home"
    assert result["home_dir"].name == result["persona_username"]
    assert result["persona_username"] not in {"", "said"}
    # Names are the bare content-pack names now - no `09_` ordering prefix,
    # which used to be drawn on every desktop icon.
    assert not any(name[:2].isdigit() and name[2:3] == "_" for name in names)
    assert len(names) == resolved.num_folders + resolved.num_files
    assert (xdg_config / "user-dirs.dirs").is_file()
    assert any(name.endswith(".txt") or name.endswith(".md") or name.endswith(".csv") for name in names)


def test_materialize_desktop_fixture_can_be_disabled(tmp_path: Path) -> None:
    result = materialize_desktop_fixture(
        root=tmp_path,
        xdg_config_home=tmp_path / "config",
        fixture=DesktopFixtureConfig(enabled=False),
    )
    assert list(result["desktop_dir"].iterdir()) == []


def test_panel_variant_overrides_style_default() -> None:
    top = _build_panel_layout_file("windows", "top_slim")
    bottom = _build_panel_layout_file("linux", "bottom_tall")
    default_windows = _build_panel_layout_file("windows", "")
    default_macos = _build_panel_layout_file("macos", "")
    default_ubuntu = _build_panel_layout_file("ubuntu", "")

    assert "orientation=top" in top
    assert "size=24" in top
    assert "orientation=bottom" in bottom
    assert "size=34" in bottom
    assert "orientation=bottom" in default_windows
    assert "[Toplevel top]" in default_macos
    assert "[Toplevel dock]" in default_macos
    assert "object-type=launcher" in default_macos
    assert "expand=false" in default_macos
    assert "x-centered=true" in default_macos
    assert "orientation=left" in default_ubuntu
    assert "object-type=launcher" in default_ubuntu


def test_sample_panel_variant_is_valid() -> None:
    variant = sample_panel_variant(9)
    assert variant in {
        "top",
        "top_slim",
        "top_slim_dock",
        "top_tall",
        "top_dock",
        "bottom",
        "bottom_slim",
        "bottom_tall",
        "left_dock",
        "left_slim_dock",
    }


def test_sampled_desktop_layout_and_content_pack_are_valid() -> None:
    assert sample_desktop_layout_template(9) in DESKTOP_LAYOUT_TEMPLATES
    assert sample_desktop_content_pack(9) in DESKTOP_CONTENT_PACKS


def test_left_dock_panel_variant_reserves_desktop_icon_space() -> None:
    positions = compute_icon_positions(
        6,
        seed=3,
        display_width=1366,
        display_height=768,
        panel_variant="left_dock",
        layout_template="upper_left_two_col",
    )["positions"]

    assert positions
    assert min(x for x, _y in positions) >= 64


def test_resolve_panel_variant_uses_style_default() -> None:
    assert resolve_panel_variant("", "windows") == "bottom_tall"
    assert resolve_panel_variant("", "macos") == "top_slim_dock"
    assert resolve_panel_variant("", "ubuntu") == "left_dock"
    assert resolve_panel_variant("top_tall", "windows") == "top_tall"
