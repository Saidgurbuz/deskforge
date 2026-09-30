"""Install richer desktop style packs without sudo.

Style packs are copied/installed into the extracted tool tree so sessions can
use them through XDG_DATA_DIRS without touching system directories.
"""

from __future__ import annotations

import configparser
import logging
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Dict, List, Optional

from deskshot.config import EXTRACTED_DIR, PROJECT_ROOT, TOOLS_DIR
from deskshot.environment.panel_assets import build_macos_panel_assets

logger = logging.getLogger(__name__)


_LOCAL_THEME_PROBE_ROOT = Path("/tmp/theme_probe_1772639258")
_LOCAL_MACOS_REPO_ROOT = Path("/tmp/deskshot_macos_repos")
_LOCAL_WHITESUR_GTK_REPO = PROJECT_ROOT / "tmp" / "WhiteSur-gtk-theme"
_LOCAL_MACTAHOE_GTK_REPO = PROJECT_ROOT / "tmp" / "MacTahoe-gtk-theme"
_LOCAL_QUARTZ_THEME_DIR = PROJECT_ROOT / "tmp" / "Quartz-Night-main" / "Quartz Night"
_LOCAL_WINDOWS_ELEVEN_WALLPAPER = (
    PROJECT_ROOT / "tmp" / "windows-eleven" / "contents" / "Wallpaper" / "wallpaper.jpg"
)
_FIREFOX_THEME_DEST_ROOT = Path("usr/share/deskshot/firefox-themes")
_PLANK_THEME_DEST_ROOT = Path("usr/share/plank/themes")

_REPO_SPECS = {
    "macos_tahoe": {
        "url": "https://github.com/kayozxo/GNOME-macOS-Tahoe.git",
        "local_hint": _LOCAL_THEME_PROBE_ROOT / "macos_tahoe",
    },
    "whitesur_gtk": {
        "url": "https://github.com/vinceliuice/WhiteSur-gtk-theme.git",
        "local_hint": _LOCAL_WHITESUR_GTK_REPO,
    },
    "mactahoe_gtk": {
        "url": "https://github.com/vinceliuice/MacTahoe-gtk-theme.git",
        "local_hint": _LOCAL_MACTAHOE_GTK_REPO,
    },
    "whitesur_icons": {
        "url": "https://github.com/vinceliuice/WhiteSur-icon-theme.git",
        "local_hint": _LOCAL_MACOS_REPO_ROOT / "WhiteSur-icon-theme",
    },
    "whitesur_cursors": {
        "url": "https://github.com/vinceliuice/WhiteSur-cursors.git",
        "local_hint": _LOCAL_MACOS_REPO_ROOT / "WhiteSur-cursors",
    },
    "win11_gtk": {
        "url": "https://github.com/yeyushengfan258/Win11-gtk-theme.git",
        "local_hint": _LOCAL_THEME_PROBE_ROOT / "win11_gtk",
    },
    "win11_icons": {
        "url": "https://github.com/yeyushengfan258/Win11-icon-theme.git",
        "local_hint": _LOCAL_THEME_PROBE_ROOT / "win11_icons",
    },
}


def _resolve_repo(
    name: str,
    cache_root: Path,
    *,
    refresh: bool = False,
) -> Optional[Path]:
    """Resolve repo path from local hint, cache, or git clone fallback."""
    spec = _REPO_SPECS[name]
    local_hint = spec["local_hint"]
    cache_repo = cache_root / "src" / name

    if cache_repo.is_dir() and not refresh:
        return cache_repo

    if local_hint.is_dir() and PROJECT_ROOT in {local_hint, *local_hint.parents}:
        return local_hint

    if local_hint.is_dir() and not refresh:
        return local_hint

    if refresh and cache_repo.exists():
        shutil.rmtree(cache_repo)

    cache_repo.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "clone", "--depth=1", spec["url"], str(cache_repo)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.warning("Failed to clone %s: %s", spec["url"], result.stderr.strip())
        return None

    return cache_repo


def _copy_dir(src: Path, dst: Path) -> bool:
    if not src.is_dir():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(src, dst, dirs_exist_ok=True, copy_function=shutil.copy)
    except (PermissionError, shutil.Error):
        for child in src.rglob("*"):
            rel = child.relative_to(src)
            target = dst / rel
            if child.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if child.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(child, target)
    return True


def _copy_file(src: Path, dst: Path) -> bool:
    if not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, dst)
    except PermissionError:
        shutil.copyfile(src, dst)
    return True


def _install_macos_tahoe(repo: Path, themes_dir: Path) -> List[str]:
    installed: List[str] = []
    for theme_name in ("Tahoe-Light", "Tahoe-Dark"):
        src = repo / "gtk" / theme_name
        dst = themes_dir / theme_name
        if _copy_dir(src, dst):
            installed.append(theme_name)
    return installed


def _extract_theme_tarball(tarball: Path, themes_dir: Path) -> list[str]:
    if not tarball.is_file():
        return []
    with tarfile.open(tarball) as tf:
        top_level = sorted({Path(member.name).parts[0] for member in tf.getmembers() if member.name and not member.name.startswith(".")})
        tf.extractall(themes_dir)
    return top_level


def _install_whitesur_gtk(repo: Path, themes_dir: Path) -> List[str]:
    installed: list[str] = []
    for tarball in sorted((repo / "release").glob("WhiteSur-*.tar.xz")):
        installed.extend(_extract_theme_tarball(tarball, themes_dir))
    return sorted(set(installed))


def _install_mactahoe_gtk(repo: Path, themes_dir: Path) -> List[str]:
    installed: list[str] = []
    for tarball in sorted((repo / "release").glob("MacTahoe-*.tar.xz")):
        installed.extend(_extract_theme_tarball(tarball, themes_dir))
    return sorted(set(installed))


def _install_whitesur_icons(repo: Path, icons_dir: Path) -> List[str]:
    install_script = repo / "install.sh"
    if not install_script.is_file():
        return []
    result = subprocess.run(
        [
            "bash",
            str(install_script),
            "-d",
            str(icons_dir),
            "-n",
            "WhiteSur",
            "-a",
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        logger.warning("WhiteSur icon install failed: %s", detail)
        return []

    installed: list[str] = []
    for icon_name in ("WhiteSur", "WhiteSur-light", "WhiteSur-dark"):
        if (icons_dir / icon_name / "index.theme").is_file():
            installed.append(icon_name)
    return installed


def _install_whitesur_cursors(repo: Path, icons_dir: Path) -> List[str]:
    src = repo / "dist"
    dst = icons_dir / "WhiteSur-cursors"
    if not _copy_dir(src, dst):
        return []
    if not (dst / "index.theme").is_file():
        return []
    return ["WhiteSur-cursors"]


def _install_macos_tahoe_wallpapers(repo: Path, backgrounds_dir: Path) -> List[str]:
    installed: list[str] = []
    source_dir = repo / ".config" / "walls" / "Tahoe"
    if not source_dir.is_dir():
        return installed
    target_dir = backgrounds_dir / "deskshot-stylepacks"
    for src in sorted(source_dir.glob("Tahoe-5k-*.jpg")):
        dst = target_dir / src.name
        if _copy_file(src, dst):
            installed.append(str(dst))
    return installed


def _install_mactahoe_wallpapers(repo: Path, backgrounds_dir: Path) -> List[str]:
    installed: list[str] = []
    source_dir = repo / "wallpaper"
    if not source_dir.is_dir():
        return installed
    target_dir = backgrounds_dir / "deskshot-stylepacks"
    for src in sorted(source_dir.glob("MacTahoe-*.jp*g")):
        dst = target_dir / src.name
        if _copy_file(src, dst):
            installed.append(str(dst))
    return installed


def _install_local_quartz_night(themes_dir: Path) -> List[str]:
    theme_name = _LOCAL_QUARTZ_THEME_DIR.name
    if not _copy_dir(_LOCAL_QUARTZ_THEME_DIR, themes_dir / theme_name):
        return []
    if not (themes_dir / theme_name / "gtk-3.0" / "gtk.css").is_file():
        return []
    return [theme_name]


def _run_win11_gtk_install(repo: Path, themes_dir: Path, color: str) -> bool:
    result = subprocess.run(
        [
            "bash",
            "install.sh",
            "-d",
            str(themes_dir),
            "-n",
            "Win11",
            "-t",
            "default",
            "-c",
            color,
            "-s",
            "standard",
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.warning("Win11 GTK install (%s) failed: %s", color, result.stderr.strip())
        return False
    return True


def _install_win11_gtk(repo: Path, themes_dir: Path) -> List[str]:
    installed: List[str] = []
    if _run_win11_gtk_install(repo, themes_dir, "light"):
        if (themes_dir / "Win11-Light" / "gtk-3.0").is_dir():
            installed.append("Win11-Light")
    if _run_win11_gtk_install(repo, themes_dir, "dark"):
        if (themes_dir / "Win11-Dark" / "gtk-3.0").is_dir():
            installed.append("Win11-Dark")
    return installed


def _install_win11_icons(repo: Path, icons_dir: Path) -> List[str]:
    result = subprocess.run(
        [
            "bash",
            "install.sh",
            "-d",
            str(icons_dir),
            "-n",
            "Win11",
            "-t",
            "default",
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        logger.warning("Win11 icon install failed: %s", detail)
        return []

    installed: List[str] = []
    for icon_name in ("Win11", "Win11-dark"):
        if (icons_dir / icon_name / "index.theme").is_file():
            installed.append(icon_name)
    return installed


def _stage_repo_firefox_theme(repo_firefox_dir: Path, destination_root: Path) -> bool:
    if not repo_firefox_dir.is_dir():
        return False
    shutil.copytree(repo_firefox_dir, destination_root, dirs_exist_ok=True)
    return True


def _install_firefox_themes(extracted: Path) -> List[str]:
    installed: List[str] = []
    firefox_root = extracted / _FIREFOX_THEME_DEST_ROOT
    if _stage_repo_firefox_theme(_LOCAL_MACTAHOE_GTK_REPO / "other" / "firefox", firefox_root / "mactahoe"):
        installed.append("mactahoe")
    if _stage_repo_firefox_theme(_LOCAL_WHITESUR_GTK_REPO / "other" / "firefox", firefox_root / "whitesur"):
        installed.append("whitesur")
    return installed


def _patch_plank_theme(theme_dir: Path) -> bool:
    dock_theme = theme_dir / "dock.theme"
    if not dock_theme.is_file():
        return False

    parser = configparser.ConfigParser()
    parser.optionxform = str
    parser.read(dock_theme, encoding="utf-8")
    if not parser.has_section("PlankTheme"):
        parser.add_section("PlankTheme")
    if not parser.has_section("PlankDockTheme"):
        parser.add_section("PlankDockTheme")

    dark_variant = "dark" in theme_dir.name.lower()
    plank = parser["PlankTheme"]
    dock = parser["PlankDockTheme"]

    if dark_variant:
        plank["TopRoundness"] = "26"
        plank["BottomRoundness"] = "26"
        plank["LineWidth"] = "1"
        plank["OuterStrokeColor"] = "0;;0;;0;;70"
        plank["FillStartColor"] = "40;;40;;44;;210"
        plank["FillEndColor"] = "34;;34;;38;;220"
        plank["InnerStrokeColor"] = "255;;255;;255;;24"
    else:
        plank["TopRoundness"] = "26"
        plank["BottomRoundness"] = "26"
        plank["LineWidth"] = "1"
        plank["OuterStrokeColor"] = "108;;108;;116;;55"
        plank["FillStartColor"] = "244;;244;;248;;210"
        plank["FillEndColor"] = "234;;234;;240;;220"
        plank["InnerStrokeColor"] = "255;;255;;255;;120"

    dock["HorizPadding"] = "35"
    dock["TopPadding"] = "110"
    dock["BottomPadding"] = "14"
    dock["ItemPadding"] = "10"
    dock["IndicatorSize"] = dock.get("IndicatorSize", "5")
    dock["IconShadowSize"] = "0"

    with dock_theme.open("w", encoding="utf-8") as f:
        parser.write(f)
    return True


def _install_plank_themes(extracted: Path) -> List[str]:
    installed: List[str] = []
    target_root = extracted / _PLANK_THEME_DEST_ROOT
    target_root.mkdir(parents=True, exist_ok=True)

    theme_sources = [
        (_LOCAL_MACTAHOE_GTK_REPO / "other" / "plank" / "theme-Light", "MacTahoe-Light"),
        (_LOCAL_MACTAHOE_GTK_REPO / "other" / "plank" / "theme-Dark", "MacTahoe-Dark"),
        (_LOCAL_WHITESUR_GTK_REPO / "other" / "plank" / "theme-Light", "WhiteSur-Light"),
        (_LOCAL_WHITESUR_GTK_REPO / "other" / "plank" / "theme-Dark", "WhiteSur-Dark"),
    ]
    for src, name in theme_sources:
        dst = target_root / name
        if _copy_dir(src, dst):
            _patch_plank_theme(dst)
            installed.append(name)
    return installed


def _install_local_windows_wallpaper(backgrounds_dir: Path) -> List[str]:
    dst = backgrounds_dir / "deskshot-stylepacks" / "windows-eleven-wallpaper.jpg"
    if not _copy_file(_LOCAL_WINDOWS_ELEVEN_WALLPAPER, dst):
        return []
    return [str(dst)]


def install_style_packs(
    extracted_dir: Path | None = None,
    *,
    cache_root: Path | None = None,
    refresh: bool = False,
) -> Dict[str, List[str]]:
    """Install richer themes/icons into extracted tree (best effort)."""
    extracted = extracted_dir or EXTRACTED_DIR
    cache = cache_root or (TOOLS_DIR / "stylepacks")

    themes_dir = extracted / "usr" / "share" / "themes"
    icons_dir = extracted / "usr" / "share" / "icons"
    backgrounds_dir = extracted / "usr" / "share" / "backgrounds"
    themes_dir.mkdir(parents=True, exist_ok=True)
    icons_dir.mkdir(parents=True, exist_ok=True)
    backgrounds_dir.mkdir(parents=True, exist_ok=True)

    summary: Dict[str, List[str]] = {
        "themes": [],
        "icons": [],
        "wallpapers": [],
        "panel_assets": [],
        "plank_themes": [],
        "firefox_themes": [],
        "warnings": [],
    }

    summary["themes"].extend(_install_local_quartz_night(themes_dir))
    summary["wallpapers"].extend(_install_local_windows_wallpaper(backgrounds_dir))

    mac_repo = _resolve_repo("macos_tahoe", cache, refresh=refresh)
    if mac_repo is None:
        summary["warnings"].append("macos_tahoe_repo_unavailable")
    else:
        summary["themes"].extend(_install_macos_tahoe(mac_repo, themes_dir))
        summary["wallpapers"].extend(_install_macos_tahoe_wallpapers(mac_repo, backgrounds_dir))

    whitesur_gtk_repo = _resolve_repo("whitesur_gtk", cache, refresh=refresh)
    if whitesur_gtk_repo is None:
        summary["warnings"].append("whitesur_gtk_repo_unavailable")
    else:
        summary["themes"].extend(_install_whitesur_gtk(whitesur_gtk_repo, themes_dir))

    mactahoe_gtk_repo = _resolve_repo("mactahoe_gtk", cache, refresh=refresh)
    if mactahoe_gtk_repo is None:
        summary["warnings"].append("mactahoe_gtk_repo_unavailable")
    else:
        summary["themes"].extend(_install_mactahoe_gtk(mactahoe_gtk_repo, themes_dir))
        summary["wallpapers"].extend(_install_mactahoe_wallpapers(mactahoe_gtk_repo, backgrounds_dir))

    whitesur_icons_repo = _resolve_repo("whitesur_icons", cache, refresh=refresh)
    if whitesur_icons_repo is None:
        summary["warnings"].append("whitesur_icons_repo_unavailable")
    else:
        summary["icons"].extend(_install_whitesur_icons(whitesur_icons_repo, icons_dir))

    whitesur_cursors_repo = _resolve_repo("whitesur_cursors", cache, refresh=refresh)
    if whitesur_cursors_repo is None:
        summary["warnings"].append("whitesur_cursors_repo_unavailable")
    else:
        summary["icons"].extend(_install_whitesur_cursors(whitesur_cursors_repo, icons_dir))

    win11_gtk_repo = _resolve_repo("win11_gtk", cache, refresh=refresh)
    if win11_gtk_repo is None:
        summary["warnings"].append("win11_gtk_repo_unavailable")
    else:
        summary["themes"].extend(_install_win11_gtk(win11_gtk_repo, themes_dir))

    win11_icons_repo = _resolve_repo("win11_icons", cache, refresh=refresh)
    if win11_icons_repo is None:
        summary["warnings"].append("win11_icons_repo_unavailable")
    else:
        summary["icons"].extend(_install_win11_icons(win11_icons_repo, icons_dir))

    # De-duplicate while preserving deterministic sorted output.
    summary["themes"] = sorted(set(summary["themes"]))
    summary["icons"] = sorted(set(summary["icons"]))
    summary["wallpapers"] = sorted(set(summary["wallpapers"]))
    summary["plank_themes"] = sorted(set(_install_plank_themes(extracted)))
    summary["firefox_themes"] = sorted(set(_install_firefox_themes(extracted)))
    panel_assets_dir = backgrounds_dir / "deskshot-stylepacks" / "panel-assets"
    summary["panel_assets"] = sorted(
        set(build_macos_panel_assets(themes_dir, panel_assets_dir))
    )

    return summary
