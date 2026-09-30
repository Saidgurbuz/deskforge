from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from deskshot.config import ThemeConfig
from deskshot.environment.backgrounds import (
    _build_gradient_canvas,
    apply_desktop_background,
    stylize_screenshot_background,
)
from deskshot.environment.themes import resolve_theme_config


def test_resolve_theme_config_selects_style_wallpaper_deterministically(monkeypatch) -> None:
    wallpapers = [
        "/tmp/deskshot-neutral-blue.png",
        "/tmp/deskshot-macos-wave.png",
        "/tmp/deskshot-windows-bloom.png",
    ]
    monkeypatch.setattr(
        "deskshot.environment.themes.select_wallpaper_candidates",
        lambda desktop_style, extracted_dir=None, assets_dir=None: wallpapers,
    )
    monkeypatch.setattr(
        "deskshot.environment.themes.select_wallpaper_sample_pool",
        lambda desktop_style, extracted_dir=None, assets_dir=None: wallpapers,
    )
    monkeypatch.setattr(
        "deskshot.environment.themes.list_available_gtk_themes",
        lambda extracted_dir=None: ["Adwaita"],
    )
    monkeypatch.setattr(
        "deskshot.environment.themes.list_available_icon_themes",
        lambda extracted_dir=None: ["Papirus"],
    )
    monkeypatch.setattr(
        "deskshot.environment.themes.list_available_wm_themes",
        lambda extracted_dir=None: ["Default"],
    )

    theme = ThemeConfig(
        gtk_theme="Adwaita",
        icon_theme="Papirus",
        wm_theme="Default",
        desktop_style="macos",
        wallpaper_seed=7,
    )
    resolved = resolve_theme_config(theme)
    assert resolved.wallpaper.endswith("deskshot-macos-wave.png")


def test_resolve_theme_config_prefers_windows_wallpaper_image(monkeypatch) -> None:
    wallpapers = [
        "/tmp/windows-eleven-wallpaper.jpg",
        "/tmp/deskshot-windows-bloom.png",
    ]
    monkeypatch.setattr(
        "deskshot.environment.themes.select_wallpaper_candidates",
        lambda desktop_style, extracted_dir=None, assets_dir=None: wallpapers,
    )
    monkeypatch.setattr(
        "deskshot.environment.themes.select_wallpaper_sample_pool",
        lambda desktop_style, extracted_dir=None, assets_dir=None: wallpapers,
    )
    monkeypatch.setattr(
        "deskshot.environment.themes.list_available_gtk_themes",
        lambda extracted_dir=None: ["Adwaita"],
    )
    monkeypatch.setattr(
        "deskshot.environment.themes.list_available_icon_themes",
        lambda extracted_dir=None: ["Papirus"],
    )
    monkeypatch.setattr(
        "deskshot.environment.themes.list_available_wm_themes",
        lambda extracted_dir=None: ["Default"],
    )

    theme = ThemeConfig(
        gtk_theme="Adwaita",
        icon_theme="Papirus",
        wm_theme="Default",
        desktop_style="windows",
    )
    resolved = resolve_theme_config(theme)
    assert resolved.wallpaper.endswith("windows-eleven-wallpaper.jpg")


def test_apply_desktop_background_uses_display_for_wallpaper(tmp_path: Path, monkeypatch) -> None:
    wallpaper = tmp_path / "wallpaper.jpg"
    Image.new("RGB", (40, 20), (12, 34, 56)).save(wallpaper)

    run_calls: list[list[str]] = []
    popen_calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        run_calls.append(cmd)
        return SimpleNamespace(returncode=0)

    class FakeProc:
        returncode = 0

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, **kwargs):
        popen_calls.append(cmd)
        return FakeProc()

    def fake_find(name: str, *, env: dict[str, str]) -> str | None:
        if name == "display":
            return f"/fake/{name}"
        return None

    monkeypatch.setattr("deskshot.environment.backgrounds.subprocess.run", fake_run)
    monkeypatch.setattr("deskshot.environment.backgrounds.subprocess.Popen", fake_popen)
    monkeypatch.setattr("deskshot.environment.backgrounds._find_binary_on_path", fake_find)

    ok = apply_desktop_background(
        ThemeConfig(wallpaper=str(wallpaper), desktop_style="windows"),
        env={"PATH": "/fake", "DISPLAY": ":99"},
        size=(320, 200),
    )

    assert ok is True
    assert any(cmd[:3] == ["gsettings", "set", "org.mate.background"] for cmd in run_calls)
    assert len(popen_calls) == 1
    assert popen_calls[0][:3] == ["/fake/display", "-window", "root"]
    assert Path(popen_calls[0][3]).name == "root-background.png"


def test_apply_desktop_background_falls_back_to_xsetroot(monkeypatch) -> None:
    run_calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        run_calls.append(cmd)
        return SimpleNamespace(returncode=0)

    def fake_find(name: str, *, env: dict[str, str]) -> str | None:
        if name == "xsetroot":
            return f"/fake/{name}"
        return None

    monkeypatch.setattr("deskshot.environment.backgrounds.subprocess.run", fake_run)
    monkeypatch.setattr("deskshot.environment.backgrounds._find_binary_on_path", fake_find)

    ok = apply_desktop_background(
        ThemeConfig(desktop_style="ubuntu"),
        env={"PATH": "/fake", "DISPLAY": ":99"},
        size=(320, 200),
    )

    assert ok is True
    assert run_calls[-1] == ["/fake/xsetroot", "-solid", "#77216F"]


def test_stylize_screenshot_background_uses_global_gradient_mask(tmp_path: Path) -> None:
    screenshot = tmp_path / "shot.png"
    wallpaper = tmp_path / "wallpaper.png"

    base = _build_gradient_canvas((120, 90), "#4E8AA9", "#3B9440").convert("RGBA")
    # Simulate a desktop icon + label inside a larger chrome bbox.
    for x in range(18, 31):
        for y in range(14, 28):
            base.putpixel((x, y), (240, 240, 240, 255))
    for x in range(14, 38):
        for y in range(32, 37):
            base.putpixel((x, y), (250, 250, 250, 255))
    base.save(screenshot)

    Image.new("RGBA", (120, 90), (180, 60, 120, 255)).save(wallpaper)

    ok = stylize_screenshot_background(
        screenshot,
        theme=ThemeConfig(wallpaper=str(wallpaper), desktop_style="linux"),
        elements=[
            {
                "source": "desktop_chrome",
                "role": "file icon",
                "name": "Computer",
                "inner_text": "Computer",
                "rect": {"x": 10, "y": 10, "w": 34, "h": 30},
            }
        ],
        occlusion_meta=None,
    )

    assert ok is True
    with Image.open(screenshot) as result:
        result = result.convert("RGBA")
        # Background area inside the bbox but outside the real icon should be fully replaced.
        assert result.getpixel((12, 12)) == (180, 60, 120, 255)
        # Icon body should still be preserved.
        assert result.getpixel((20, 18)) == (240, 240, 240, 255)
        # Label pixels should still be preserved.
        assert result.getpixel((20, 34)) == (250, 250, 250, 255)
