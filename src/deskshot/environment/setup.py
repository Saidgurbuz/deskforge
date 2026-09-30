"""Download and extract RPMs for DeskShot (no sudo required).

Wraps the same logic as scripts/setup_tools.sh for programmatic use via `dsd setup`.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import warnings
from pathlib import Path

from deskshot.config import TOOLS_DIR, EXTRACTED_DIR, bin_fix_dir
from deskshot.environment.wallpaper_variants import build_wallpaper_variants


# RPMs to download — grouped by purpose
CORE_RPMS = [
    "xorg-x11-server-Xvfb",
    "xkbcomp",
    "dbus-daemon",
    "dbus-tools",
]

INPUT_RPMS = [
    "xdotool",
    "libxdo",
]

APP_RPMS = [
    "gnome-calculator",
    "gnome-terminal",
    "gnome-system-monitor",
    "baobab",
    "meld",
    "file-roller",
    "gedit",
    "eog",
    "evince",
    "evince-libs",
    "nautilus",
]

NATIVE_PROBE_RPMS = [
    "atril",
    "atril-libs",
    "dia",
    "gucharmap",
    "keepassxc",
    "remmina",
    "xapps",
    "xreader",
    "xreader-libs",
    "xarchiver",
    "xournalpp",
]

BUSINESS_APP_RPMS = [
    "boost-chrono",
    "boost-filesystem",
    "boost-iostreams",
    "boost-locale",
    "boost-system",
    "boost-thread",
    "harfbuzz-icu",
    "libreoffice",
    "libreoffice-core",
    "libreoffice-data",
    "libreoffice-filters",
    "libreoffice-graphicfilter",
    "libreoffice-gtk3",
    "libreoffice-langpack-en",
    "libreoffice-ure",
    "libreoffice-writer",
    "libreoffice-calc",
    "libreoffice-impress",
    "gpgmepp",
    "liborcus",
    "liblangtag",
    "evolution",
    "thunderbird",
]

WORKER_APP_RPMS = [
    "claws-mail",
    "filezilla",
    "gnome-logs",
    "homebank",
    "qalculate-gtk",
    "seahorse",
    "transmission-gtk",
]

EDITOR_IDE_RPMS = [
    "geany",
    "mousepad",
    "kate",
    "featherpad",
    "emacs",
    "pluma",
    "bluefish",
    "Zim",
    "python3-zim",
    "thonny",
    "qt-creator",
    "vim-X11",
    "notepadqq",
    "spyder5",
]
EDITOR_IDE_COMPANION_RPMS = [
    "geany-libgeany",
    "libmousepad0",
    "kate-libs",
    "qt5-qtsvg",
    "qt5-qtx11extras",
    "qt6-qt5compat",
    "qt-creator-data",
]

# Browser for web-window + desktop multi-window scenes.
BROWSER_RPMS = [
    "firefox",
    "chromium",
    "chromium-common",
    "double-conversion",
    "libavcodec-free",
    "libavformat-free",
    "libavutil-free",
    "libcanberra-gtk3",
    "libdav1d",
    "nss-mdns",
    "openh264",
    "mozilla-openh264",
    "noopenh264",
    "pipewire-libs",
    "fdk-aac-free",
]

DEPENDENCY_RPMS = [
    "atkmm",
    "atkmm30",
    "botan2",
    "cairomm",
    "gperftools-libs",
    "glibmm24",
    "gtksourceview4",
    "gtkmm30",
    "gucharmap-libs",
    "libgee",
    "libhandy",
    "libargon2",
    "libzip",
    "libpeas",
    "libpeas-gtk",
    "libsecret",
    "minizip1.2",
    "gnome-desktop3",
    "librsvg2",
    "gspell",
    "enchant",
    "enchant2",
    "libnotify",
    "pcsc-lite-libs",
    "portaudio",
    "qrencode-libs",
    "xcb-util",
    "libXfont2",
    "libXdmcp",
    "libfontenc",
    "libxkbfile",
    "libxklavier",
    "libsigc++20",
    "pangomm",
    "vte291",
]

XFCE_RPMS = [
    "xfce4-session",
    "xfwm4",
    "xfce4-panel",
    "xfdesktop",
    "xfce4-settings",
    "xfconf",
    "libxfce4ui",
    "libxfce4util",
    "garcon",
    "exo",
    "libwnck3",
    "startup-notification",
    "iceauth",
    "Thunar",
    "xfce4-whiskermenu-plugin",
]

MATE_RPMS = [
    "mate-settings-daemon",
    "mate-panel",
    "mate-panel-libs",
    "mate-desktop-libs",
    "mate-menus",
    "mate-menus-libs",
    "caja",
    "caja-schemas",
    "caja-core-extensions",
    "gvfs",
    "gvfs-client",
    "libmateweather",
    "gtk-layer-shell",
]

THEME_RPMS = [
    "papirus-icon-theme",
    "xfwm4-themes",
    "desktop-backgrounds-basic",
    "desktop-backgrounds-compat",
    "desktop-backgrounds-gnome",
    "desktop-backgrounds-waves",
    "gnome-backgrounds",
    "gnome-backgrounds-extras",
    "mate-backgrounds",
    "redhat-backgrounds",
    "xorg-x11-server-utils",
    "ImageMagick",
    "ImageMagick-libs",
    "libraqm",
    "liblqr-1",
]

PICOM_BUILD_RPMS = [
    "meson",
    "ninja-build",
    "libconfig",
    "libconfig-devel",
    "libev",
    "libev-devel",
    "uthash-devel",
    "pcre2",
    "pcre2-devel",
    "pixman",
    "pixman-devel",
    "libX11",
    "libX11-devel",
    "libX11-xcb",
    "libxcb",
    "libxcb-devel",
    "libXau",
    "libXau-devel",
    "libXdmcp",
    "libXdmcp-devel",
    "xcb-util",
    "xcb-util-devel",
    "xcb-util-image",
    "xcb-util-image-devel",
    "xcb-util-renderutil",
    "xcb-util-renderutil-devel",
    "dbus-libs",
    "dbus-devel",
    "xorg-x11-proto-devel",
]

FEDORA_RELEASE_BACKGROUND_RPMS = [
    # Broaden the wallpaper pool materially using official Fedora noarch packs.
    "f35-backgrounds-base",
    "f35-backgrounds-extras-base",
    "f35-backgrounds-gnome",
    "f35-backgrounds-mate",
    "f35-backgrounds-xfce",
    "f35-backgrounds-extras-gnome",
    "f35-backgrounds-extras-mate",
    "f35-backgrounds-extras-xfce",
    "f35-backgrounds-kde",
    "f35-backgrounds-extras-kde",
    "f36-backgrounds-base",
    "f36-backgrounds-extras-base",
    "f36-backgrounds-gnome",
    "f36-backgrounds-mate",
    "f36-backgrounds-xfce",
    "f36-backgrounds-extras-gnome",
    "f36-backgrounds-extras-mate",
    "f36-backgrounds-extras-xfce",
    "f36-backgrounds-kde",
    "f36-backgrounds-extras-kde",
    "f37-backgrounds-base",
    "f37-backgrounds-extras-base",
    "f37-backgrounds-gnome",
    "f37-backgrounds-mate",
    "f37-backgrounds-xfce",
    "f37-backgrounds-extras-gnome",
    "f37-backgrounds-extras-mate",
    "f37-backgrounds-extras-xfce",
    "f37-backgrounds-kde",
    "f37-backgrounds-extras-kde",
    "f38-backgrounds-base",
    "f38-backgrounds-budgie",
    "f38-backgrounds-extras-base",
    "f38-backgrounds-gnome",
    "f38-backgrounds-mate",
    "f38-backgrounds-xfce",
    "f38-backgrounds-extras-gnome",
    "f38-backgrounds-extras-mate",
    "f38-backgrounds-extras-xfce",
    "f38-backgrounds-kde",
    "f38-backgrounds-extras-kde",
    "f39-backgrounds-base",
    "f39-backgrounds-budgie",
    "f39-backgrounds-extras-base",
    "f39-backgrounds-gnome",
    "f39-backgrounds-mate",
    "f39-backgrounds-xfce",
    "f39-backgrounds-extras-gnome",
    "f39-backgrounds-extras-mate",
    "f39-backgrounds-extras-xfce",
    "f39-backgrounds-kde",
    "f39-backgrounds-extras-kde",
]

ALL_RPMS = (
    CORE_RPMS
    + INPUT_RPMS
    + APP_RPMS
    + NATIVE_PROBE_RPMS
    + BUSINESS_APP_RPMS
    + WORKER_APP_RPMS
    + EDITOR_IDE_RPMS
    + EDITOR_IDE_COMPANION_RPMS
    + BROWSER_RPMS
    + DEPENDENCY_RPMS
    + XFCE_RPMS
    + MATE_RPMS
    + THEME_RPMS
    + PICOM_BUILD_RPMS
    + FEDORA_RELEASE_BACKGROUND_RPMS
)
VSCODE_BUNDLE_URL = "https://update.code.visualstudio.com/latest/linux-x64/stable"
VSCODE_ARCHIVE_NAME = "vscode-linux-x64.tar.gz"
VSCODE_BUNDLE_DIRNAME = "VSCode-linux-x64"
PICOM_VERSION = "v13"
PICOM_ARCHIVE_NAME = f"picom-{PICOM_VERSION}.tar.gz"
PICOM_SOURCE_URL = f"https://github.com/yshui/picom/archive/refs/tags/{PICOM_VERSION}.tar.gz"
PICOM_BUNDLE_DIRNAME = f"picom-{PICOM_VERSION}"
PLANK_RPM_BASE_URL = "https://linuxsoft.cern.ch/cern/alma/9.4/synergy/x86_64/os/Packages"
PLANK_RPM_FILENAMES = [
    "bamf-0.5.5-5.el9.x86_64.rpm",
    "bamf-daemon-0.5.5-5.el9.x86_64.rpm",
    "dee-1.2.7-45.el9.x86_64.rpm",
    "granite-6.2.0-2.el9.x86_64.rpm",
    "plank-0.11.89-12.20210202.git013d051.el9.x86_64.rpm",
    "plank-libs-0.11.89-12.20210202.git013d051.el9.x86_64.rpm",
]


def download_rpm(pkg: str, rpms_dir: Path) -> bool:
    """Download a single RPM. Returns True if successful."""
    # Check if already downloaded
    existing = list(rpms_dir.glob(f"{pkg}-*.rpm"))
    if existing:
        print(f"  [skip] {pkg}")
        return True

    print(f"  [download] {pkg}")
    result = subprocess.run(
        ["dnf", "download", "-y", "--arch=x86_64,noarch", "--destdir", str(rpms_dir), pkg],
        capture_output=True,
    )
    if result.returncode != 0:
        print(f"  [WARN] Failed to download {pkg}")
        return False
    return True


def _resolved_marker_path(pkg: str, rpms_dir: Path) -> Path:
    markers_dir = rpms_dir / ".resolved"
    markers_dir.mkdir(parents=True, exist_ok=True)
    return markers_dir / f"{pkg}.done"


def download_rpm_with_deps(pkg: str, rpms_dir: Path) -> bool:
    """Download a package plus all deps, even if the host already has them.

    This is only used for a small set of editor/IDE apps where the extracted
    tree needs app-private libraries and Qt/KF stacks that are otherwise easy
    to miss with manual package curation.
    """
    marker = _resolved_marker_path(pkg, rpms_dir)
    if marker.is_file():
        print(f"  [skip] {pkg} deps")
        return True

    print(f"  [resolve] {pkg} deps")
    result = subprocess.run(
        [
            "dnf",
            "download",
            "-y",
            "--resolve",
            "--alldeps",
            "--arch=x86_64,noarch",
            "--destdir",
            str(rpms_dir),
            pkg,
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        print(f"  [WARN] Failed to resolve deps for {pkg}")
        return False

    marker.write_text("ok\n", encoding="utf-8")
    return True


def extract_rpms(rpms_dir: Path, extracted_dir: Path) -> None:
    """Extract all RPMs in rpms_dir to extracted_dir."""
    extracted_dir.mkdir(parents=True, exist_ok=True)
    for rpm_file in sorted(rpms_dir.glob("*.rpm")):
        print(f"  [extract] {rpm_file.name}")
        subprocess.run(
            f"rpm2cpio {rpm_file} | cpio -idm --quiet",
            shell=True,
            cwd=str(extracted_dir),
            capture_output=True,
        )


def compile_schemas(extracted_dir: Path) -> None:
    """Compile GSettings schemas if present."""
    schemas_dir = extracted_dir / "usr" / "share" / "glib-2.0" / "schemas"
    if schemas_dir.is_dir():
        print("  [schemas] Compiling GSettings schemas...")
        subprocess.run(
            ["glib-compile-schemas", str(schemas_dir)],
            capture_output=True,
        )


def install_vscode_bundle(tools_dir: Path) -> None:
    """Download and extract the VS Code tarball into tools/external_apps."""
    external_root = tools_dir / "external_apps"
    downloads_dir = external_root / "downloads"
    bundles_dir = external_root / "bundles"
    bin_dir = external_root / "bin"
    applications_dir = external_root / "applications"
    archive_path = downloads_dir / VSCODE_ARCHIVE_NAME
    bundle_dir = bundles_dir / VSCODE_BUNDLE_DIRNAME
    code_bin = bundle_dir / "bin" / "code"
    link_path = bin_dir / "code"

    downloads_dir.mkdir(parents=True, exist_ok=True)
    bundles_dir.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)
    applications_dir.mkdir(parents=True, exist_ok=True)

    if not code_bin.is_file():
        if not archive_path.is_file():
            print("  [download] vscode bundle")
            urllib.request.urlretrieve(VSCODE_BUNDLE_URL, archive_path)
        else:
            print("  [skip] vscode bundle archive")

        print("  [extract] vscode bundle")
        with tarfile.open(archive_path) as tar:
            tar.extractall(path=bundles_dir)
    else:
        print("  [skip] vscode bundle")

    if link_path.is_symlink() or link_path.exists():
        link_path.unlink()
    link_path.symlink_to(code_bin.resolve())
    print(f"  [link] {link_path} -> {code_bin.resolve()}")

    desktop_file = applications_dir / "code.desktop"
    desktop_file.write_text(
        "\n".join(
            [
                "[Desktop Entry]",
                "Type=Application",
                "Name=Visual Studio Code",
                "Exec=code --new-window",
                "Terminal=false",
                "Categories=Development;IDE;TextEditor;",
                "StartupWMClass=Code",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(f"  [write] {desktop_file}")


def _external_apps_root(tools_dir: Path) -> Path:
    return tools_dir / "external_apps"


def _external_bin_dir(tools_dir: Path) -> Path:
    return _external_apps_root(tools_dir) / "bin"


def _ensure_external_dirs(tools_dir: Path) -> dict[str, Path]:
    root = _external_apps_root(tools_dir)
    paths = {
        "root": root,
        "downloads": root / "downloads",
        "bundles": root / "bundles",
        "bin": root / "bin",
        "applications": root / "applications",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def _download_url(url: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.is_file():
        print(f"  [download] {destination.name}")
        urllib.request.urlretrieve(url, destination)
    else:
        print(f"  [skip] {destination.name}")
    return destination


def _symlink_or_refresh(link_path: Path, target_path: Path) -> None:
    link_path.parent.mkdir(parents=True, exist_ok=True)
    if link_path.is_symlink() or link_path.exists():
        link_path.unlink()
    link_path.symlink_to(target_path.resolve())
    print(f"  [link] {link_path} -> {target_path.resolve()}")


def install_picom_bundle(
    tools_dir: Path,
    extracted_dir: Path | None = None,
) -> Path | None:
    """Build and stage a rootless picom bundle under tools/external_apps."""
    extracted = extracted_dir or EXTRACTED_DIR
    paths = _ensure_external_dirs(tools_dir)
    downloads_dir = paths["downloads"] / "picom"
    bundles_dir = paths["bundles"]
    bundle_dir = bundles_dir / PICOM_BUNDLE_DIRNAME
    source_root = bundle_dir / "source"
    build_dir = bundle_dir / "build"
    prefix_root = bundle_dir / "prefix"
    installed_bin = prefix_root / "usr" / "local" / "bin" / "picom"
    link_path = paths["bin"] / "picom"

    if installed_bin.is_file():
        _symlink_or_refresh(link_path, installed_bin)
        return installed_bin

    archive_path = _download_url(PICOM_SOURCE_URL, downloads_dir / PICOM_ARCHIVE_NAME)

    if source_root.exists():
        shutil.rmtree(source_root)
    source_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(path=source_root)

    source_candidates = [p for p in source_root.iterdir() if p.is_dir()]
    if not source_candidates:
        raise RuntimeError("picom source extraction produced no source directory")
    source_dir = source_candidates[0]

    if build_dir.exists():
        shutil.rmtree(build_dir)
    if prefix_root.exists():
        shutil.rmtree(prefix_root)
    build_dir.mkdir(parents=True, exist_ok=True)
    prefix_root.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["PATH"] = (
        f"{extracted / 'usr' / 'bin'}:{env.get('PATH', '')}"
    ).rstrip(":")

    python_parts = [str(p) for p in sorted((extracted / "usr" / "lib").glob("python*/site-packages")) if p.is_dir()]
    if python_parts:
        current = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = ":".join(python_parts + ([current] if current else []))

    env["PKG_CONFIG_LIBDIR"] = ":".join(
        [
            str(extracted / "usr" / "lib64" / "pkgconfig"),
            str(extracted / "usr" / "share" / "pkgconfig"),
        ]
    )
    env["PKG_CONFIG_SYSROOT_DIR"] = str(extracted)
    env["LD_LIBRARY_PATH"] = ":".join(
        [
            str(extracted / "usr" / "lib64"),
            str(extracted / "usr" / "lib"),
            env.get("LD_LIBRARY_PATH", ""),
        ]
    ).rstrip(":")
    env["LIBRARY_PATH"] = ":".join(
        [
            str(extracted / "usr" / "lib64"),
            str(extracted / "usr" / "lib"),
            env.get("LIBRARY_PATH", ""),
        ]
    ).rstrip(":")
    env["C_INCLUDE_PATH"] = ":".join(
        [
            str(extracted / "usr" / "include"),
            env.get("C_INCLUDE_PATH", ""),
        ]
    ).rstrip(":")
    env["CFLAGS"] = f"-I{extracted / 'usr' / 'include'} {env.get('CFLAGS', '')}".strip()
    env["LDFLAGS"] = (
        f"-L{extracted / 'usr' / 'lib64'} -L{extracted / 'usr' / 'lib'} {env.get('LDFLAGS', '')}"
    ).strip()

    subprocess.run(
        [
            "python3",
            "-m",
            "mesonbuild.mesonmain",
            "setup",
            "--buildtype=release",
            "-Dopengl=false",
            "-Ddbus=true",
            "-Dregex=true",
            str(build_dir),
            str(source_dir),
        ],
        check=True,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["ninja", "-C", str(build_dir)],
        check=True,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    install_env = dict(env)
    install_env["DESTDIR"] = str(prefix_root)
    subprocess.run(
        ["ninja", "-C", str(build_dir), "install"],
        check=True,
        env=install_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if not installed_bin.is_file():
        raise RuntimeError(f"picom build completed but binary missing: {installed_bin}")

    _symlink_or_refresh(link_path, installed_bin)
    return installed_bin


def install_plank_bundle(
    tools_dir: Path,
    extracted_dir: Path | None = None,
) -> dict[str, Path]:
    """Download and extract a rootless EL9 plank runtime into extracted tools."""
    extracted = extracted_dir or EXTRACTED_DIR
    paths = _ensure_external_dirs(tools_dir)
    downloads_dir = paths["downloads"] / "plank"
    installed: dict[str, Path] = {}

    for filename in PLANK_RPM_FILENAMES:
        url = f"{PLANK_RPM_BASE_URL}/{filename}"
        archive = _download_url(url, downloads_dir / filename)
        print(f"  [extract] {filename}")
        subprocess.run(
            f"rpm2cpio {archive} | cpio -idm --quiet",
            shell=True,
            cwd=str(extracted),
            capture_output=True,
        )

    compile_schemas(extracted)

    plank_bin = extracted / "usr" / "bin" / "plank"
    bamfdaemon = extracted / "usr" / "libexec" / "bamf" / "bamfdaemon"
    if plank_bin.is_file():
        _symlink_or_refresh(paths["bin"] / "plank", plank_bin)
        installed["plank"] = plank_bin
    if bamfdaemon.is_file():
        _symlink_or_refresh(paths["bin"] / "bamfdaemon", bamfdaemon)
        installed["bamfdaemon"] = bamfdaemon
    return installed


XKBCOMP_DIR = Path("/tmp/xkb")
XFWM4_DATA_DIR = Path("/tmp/xfwm4_data_")
MATE_PANEL_DATA_DIR = Path("/tmp/mate-panel_data_")
MATE_PANEL_LIB_DIR = Path("/tmp/mate-panel_libs")
CAJA_DATA_DIR = Path("/tmp/caja_data_")
GNOME_SYSTEM_MONITOR_DATA_DIR = Path("/tmp/gnome-system-monitor_data_")
GEANY_DATA_DIR = Path("/tmp/geany_data_")
RUNTIME_OVERRIDES_DIR = TOOLS_DIR / "runtime_overrides"
CHROMIUM_OVERRIDE_DIRNAME = "chromium-browser"
MPG123_OVERRIDE_VERSION = "1.33.2"
MPG123_OVERRIDE_URL = (
    f"https://downloads.sourceforge.net/project/mpg123/mpg123/"
    f"{MPG123_OVERRIDE_VERSION}/mpg123-{MPG123_OVERRIDE_VERSION}.tar.bz2"
)


def _patch_binary_bytes(
    binary: Path,
    old: bytes,
    new: bytes,
    *,
    label: str,
    replace_all: bool = False,
) -> bool:
    """Patch raw bytes in an ELF binary using same-length replacement."""
    if len(old) != len(new):
        raise ValueError(f"Patch length mismatch for {label}: {len(old)} != {len(new)}")

    if not binary.is_file():
        return False

    data = binary.read_bytes()
    if new in data:
        print(f"  [skip] {label} already patched")
        return True
    if old not in data:
        print(f"  [WARN] Could not patch {label}: pattern not found")
        return False

    count = data.count(old)
    patched = data.replace(old, new) if replace_all else data.replace(old, new, 1)
    # Written through a temporary file and renamed, never in place. These
    # binaries live in a directory shared by every concurrent session, and
    # rewriting one that another session is executing fails with "Text file
    # busy" - or worse, lets that session exec a half-written binary.
    tmp = binary.with_name(f".{binary.name}.{os.getpid()}.patch")
    try:
        tmp.write_bytes(patched)
        shutil.copymode(binary, tmp)
        os.replace(tmp, binary)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    mode = "all" if replace_all else "first"
    print(f"  [patch] {label}: replaced {count if replace_all else 1} occurrence ({mode})")
    return True


def _refresh_symlink(link: Path, target: Path) -> None:
    """Create/refresh symlink (safe for /tmp scratch paths)."""
    target_abs = target.resolve()
    try:
        if link.is_symlink() and link.resolve() == target_abs:
            return
    except FileNotFoundError:
        pass

    for _attempt in range(2):
        try:
            if link.is_symlink() or link.is_file():
                link.unlink()
            elif link.is_dir() and not link.is_symlink():
                shutil.rmtree(link)
        except FileNotFoundError:
            pass

        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(target_abs)
            print(f"  [patch] Linked {link} -> {target_abs}")
            return
        except FileExistsError:
            try:
                if link.is_symlink() and link.resolve() == target_abs:
                    return
            except FileNotFoundError:
                pass

    raise FileExistsError(f"Could not refresh symlink {link} -> {target_abs}")


def patch_runtime_paths(extracted_dir: Path) -> None:
    """Patch extracted binaries that hardcode /usr paths.

    1. Xvfb: '/usr/bin' → '/tmp/xkb' (XkbBinDirectory for xkbcomp)
    2. xfwm4: '/usr/share/xfwm4' → '/tmp/xfwm4_data_' (defaults file)
    3. mate-panel: '/usr/share/mate-panel/' → '/tmp/mate-panel_data_/'
    4. caja: '/usr/share/caja' → '/tmp/caja_data_'
    5. gnome-system-monitor: '/usr/share/gnome-system-monitor'
       → '/tmp/gnome-system-monitor_data_'
    6. geany: '/usr/share/geany' → '/tmp/geany_data_'

    Creates wrapper scripts and symlinks as needed so extracted binaries run
    correctly without requiring writable access to /usr.
    """
    lib_dir = extracted_dir / "usr" / "lib64"

    # ── Xvfb xkbcomp patch ─────────────────────────────────────────────
    xvfb_bin = extracted_dir / "usr" / "bin" / "Xvfb"
    xkbcomp_bin = extracted_dir / "usr" / "bin" / "xkbcomp"

    if xvfb_bin.is_file() and xkbcomp_bin.is_file():
        if Path("/usr/bin/xkbcomp").is_file():
            print("  [skip] System xkbcomp found at /usr/bin/xkbcomp")
        else:
            _patch_binary_bytes(
                xvfb_bin,
                b"/usr/bin\x00",
                b"/tmp/xkb\x00",
                label="Xvfb XkbBinDirectory",
            )

            # Create xkbcomp wrapper
            XKBCOMP_DIR.mkdir(parents=True, exist_ok=True)
            wrapper = XKBCOMP_DIR / "xkbcomp"
            wrapper.write_text(
                f"#!/bin/bash\n"
                f'export LD_LIBRARY_PATH="{lib_dir}:${{LD_LIBRARY_PATH}}"\n'
                f'exec "{xkbcomp_bin}" "$@"\n',
                encoding="utf-8",
            )
            wrapper.chmod(0o755)
            print(f"  [patch] Created xkbcomp wrapper at {wrapper}")

    # ── xfwm4 defaults path patch ──────────────────────────────────────
    xfwm4_bin = extracted_dir / "usr" / "bin" / "xfwm4"
    xfwm4_defaults = extracted_dir / "usr" / "share" / "xfwm4" / "defaults"

    if xfwm4_bin.is_file() and xfwm4_defaults.is_file():
        _patch_binary_bytes(
            xfwm4_bin,
            b"/usr/share/xfwm4\x00",
            b"/tmp/xfwm4_data_\x00",
            label="xfwm4 datadir",
        )

        # Symlink defaults file
        XFWM4_DATA_DIR.mkdir(parents=True, exist_ok=True)
        _refresh_symlink(XFWM4_DATA_DIR / "defaults", xfwm4_defaults)

    # ── mate-panel data path patch ───────────────────────────────────────
    mate_panel_bin = extracted_dir / "usr" / "bin" / "mate-panel"
    mate_panel_share = extracted_dir / "usr" / "share" / "mate-panel"
    if mate_panel_bin.is_file() and mate_panel_share.is_dir():
        _patch_binary_bytes(
            mate_panel_bin,
            b"/usr/share/mate-panel/",
            b"/tmp/mate-panel_data_/",
            label="mate-panel datadir",
            replace_all=True,
        )
        MATE_PANEL_DATA_DIR.mkdir(parents=True, exist_ok=True)
        _refresh_symlink(MATE_PANEL_DATA_DIR / "layouts", mate_panel_share / "layouts")
        _refresh_symlink(MATE_PANEL_DATA_DIR / "applets", mate_panel_share / "applets")
        _refresh_symlink(MATE_PANEL_LIB_DIR, extracted_dir / "usr" / "lib64" / "mate-panel")

        # Applet descriptors hardcode /usr/lib64/mate-panel/*.so.
        applets_dir = mate_panel_share / "applets"
        replaced_count = 0
        for applet_file in sorted(applets_dir.glob("*.mate-panel-applet")):
            text = applet_file.read_text(encoding="utf-8")
            old = "Location=/usr/lib64/mate-panel/"
            new = "Location=/tmp/mate-panel_libs/"
            if old in text:
                applet_file.write_text(text.replace(old, new), encoding="utf-8")
                replaced_count += 1
        if replaced_count:
            print(f"  [patch] Rewrote applet library paths in {replaced_count} descriptor files")

    # ── caja data path patch ─────────────────────────────────────────────
    caja_bin = extracted_dir / "usr" / "bin" / "caja"
    caja_share = extracted_dir / "usr" / "share" / "caja"
    if caja_bin.is_file() and caja_share.is_dir():
        _patch_binary_bytes(
            caja_bin,
            b"/usr/share/caja",
            b"/tmp/caja_data_",
            label="caja datadir",
            replace_all=True,
        )
        _refresh_symlink(CAJA_DATA_DIR, caja_share)

    # ── gnome-system-monitor data path patch ───────────────────────────
    gsm_bin = extracted_dir / "usr" / "bin" / "gnome-system-monitor"
    gsm_share = extracted_dir / "usr" / "share" / "gnome-system-monitor"
    if gsm_bin.is_file() and gsm_share.is_dir():
        _patch_binary_bytes(
            gsm_bin,
            b"/usr/share/gnome-system-monitor",
            b"/tmp/gnome-system-monitor_data_",
            label="gnome-system-monitor datadir",
            replace_all=True,
        )
        _refresh_symlink(GNOME_SYSTEM_MONITOR_DATA_DIR, gsm_share)

    # ── geany data path patch ───────────────────────────────────────────
    geany_bin = extracted_dir / "usr" / "bin" / "geany"
    geany_share = extracted_dir / "usr" / "share" / "geany"
    if geany_bin.is_file() and geany_share.is_dir():
        _patch_binary_bytes(
            geany_bin,
            b"/usr/share/geany",
            b"/tmp/geany_data_",
            label="geany datadir",
            replace_all=True,
        )
        _refresh_symlink(GEANY_DATA_DIR, geany_share)

    # ── D-Bus service exec patch for extracted app activation ───────────
    terminal_service = extracted_dir / "usr" / "share" / "dbus-1" / "services" / "org.gnome.Terminal.service"
    terminal_server = extracted_dir / "usr" / "libexec" / "gnome-terminal-server"
    if terminal_service.is_file() and terminal_server.is_file():
        text = terminal_service.read_text(encoding="utf-8")
        desired_exec = f"Exec={terminal_server.resolve()}"
        new_lines = []
        changed = False
        for line in text.splitlines():
            if line.startswith("Exec=") and line != desired_exec:
                new_lines.append(desired_exec)
                changed = True
            else:
                new_lines.append(line)
        if changed:
            terminal_service.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            print(f"  [patch] Rewrote {terminal_service.name} Exec to extracted runtime")


def _prepend_fix_dir_to_path(fix_dir: Path) -> None:
    """Put the writable side-copy dir on PATH exactly once."""
    entry = str(fix_dir)
    current = [p for p in os.environ.get("PATH", "").split(":") if p]
    if current and current[0] == entry:
        return
    os.environ["PATH"] = ":".join([entry] + [p for p in current if p != entry])


def _staged_copy_is_current(src: Path, dest: Path) -> bool:
    """True when dest is an executable side-copy already matching src."""
    try:
        if not dest.is_file() or not os.access(str(dest), os.X_OK):
            return False
        src_stat = src.stat()
        dest_stat = dest.stat()
    except OSError:
        return False
    return src_stat.st_size == dest_stat.st_size and dest_stat.st_mtime >= src_stat.st_mtime


_RUNTIME_STAGE_STAMP = ".deskshot_stage_stamp"


def _runtime_stage_stamp(src_dir: Path) -> str:
    """Fingerprint a runtime source dir so staging can be skipped when current."""
    parts = []
    for item in sorted(src_dir.iterdir()):
        try:
            st = item.lstat()
        except OSError:
            continue
        parts.append(f"{item.name}:{st.st_size}:{int(st.st_mtime)}")
    return "\n".join(parts)


def _runtime_stage_is_current(src_dir: Path, runtime_dir: Path, entry: Path) -> bool:
    """True when a browser runtime is already staged for this exact source.

    Re-staging copies hundreds of MB per session start, which dominates session
    setup on a shared filesystem and does not scale to large batch runs.
    """
    stamp_file = runtime_dir / _RUNTIME_STAGE_STAMP
    if not runtime_dir.is_dir() or not stamp_file.is_file():
        return False
    if not (entry.exists() and os.access(str(entry), os.X_OK)):
        return False
    try:
        return stamp_file.read_text(encoding="utf-8") == _runtime_stage_stamp(src_dir)
    except OSError:
        return False


def _write_runtime_stage_stamp(src_dir: Path, runtime_dir: Path) -> None:
    try:
        (runtime_dir / _RUNTIME_STAGE_STAMP).write_text(
            _runtime_stage_stamp(src_dir), encoding="utf-8"
        )
    except OSError:
        pass


def _manifest_binary_targets(extracted_dir: Path) -> dict[str, list[Path]]:
    """Derive binary staging targets from the app manifests.

    The hardcoded critical list below only tracks session/infra binaries plus a
    few apps that were repaired by hand. Apps promoted into the scene pool later
    are not added to it, so a root-owned extracted binary without +x silently
    fails to resolve and every scene sampling that app dies at launch. Deriving
    the app targets from the manifests keeps staging correct without a second
    list to maintain.
    """
    from deskshot.config import CONFIGS_DIR, AppManifest

    targets: dict[str, list[Path]] = {}
    for yaml_path in sorted((CONFIGS_DIR / "apps").glob("*.yaml")):
        try:
            binary = (AppManifest.from_yaml(yaml_path).binary or "").strip()
        except Exception:
            continue
        # Absolute paths point at user-owned bundles that keep their own bits.
        if not binary or "/" in binary:
            continue
        targets.setdefault(
            binary,
            [
                extracted_dir / "usr" / "bin" / binary,
                extracted_dir / "usr" / "libexec" / binary,
                extracted_dir / "usr" / "lib64" / binary,
                extracted_dir / "usr" / "lib" / binary,
                extracted_dir / "usr" / "lib64" / "firefox" / binary,
                extracted_dir / "usr" / "lib64" / "thunderbird" / binary,
                extracted_dir / "usr" / "lib" / "thunderbird" / binary,
            ],
        )
    return targets


def _ensure_binaries_executable(extracted_dir: Path) -> None:
    """Ensure critical extracted binaries have execute permission.

    RPM extraction normally preserves mode bits, but root-owned files on
    shared filesystems can lose +x. For each binary, try ``chmod`` first;
    if that fails (permission denied), copy the file to a writable location
    under ``/tmp`` and let the session launcher prefer that side copy.

    Never rewrite the extracted binary in place. If a previous run already
    replaced an extracted binary with a ``/tmp/deskshot_bin_fix`` wrapper,
    restore the real binary from the cached RPMs first.
    """
    import stat

    fix_dir = bin_fix_dir()
    fix_dir.mkdir(parents=True, exist_ok=True)
    rpms_dir = TOOLS_DIR / "rpms"

    critical_targets = {
        "Xvfb": [extracted_dir / "usr" / "bin" / "Xvfb"],
        "xfwm4": [extracted_dir / "usr" / "bin" / "xfwm4"],
        "xfconfd": [
            extracted_dir / "usr" / "lib64" / "xfce4" / "xfconf" / "xfconfd",
            extracted_dir / "usr" / "lib" / "xfce4" / "xfconf" / "xfconfd",
            extracted_dir / "usr" / "bin" / "xfconfd",
        ],
        "xfsettingsd": [extracted_dir / "usr" / "bin" / "xfsettingsd"],
        "mate-panel": [extracted_dir / "usr" / "bin" / "mate-panel"],
        "caja": [extracted_dir / "usr" / "bin" / "caja"],
        "dbus-daemon": [extracted_dir / "usr" / "bin" / "dbus-daemon"],
        "dbus-run-session": [extracted_dir / "usr" / "bin" / "dbus-run-session"],
        "dbus-send": [extracted_dir / "usr" / "bin" / "dbus-send"],
        "firefox": [extracted_dir / "usr" / "bin" / "firefox"],
        "firefox-bin": [extracted_dir / "usr" / "lib64" / "firefox" / "firefox-bin"],
        "chromium-browser": [
            extracted_dir / "usr" / "bin" / "chromium-browser",
            extracted_dir / "usr" / "lib64" / "chromium-browser" / "chromium-browser.sh",
        ],
        "mate-settings-daemon": [
            extracted_dir / "usr" / "libexec" / "mate-settings-daemon",
            extracted_dir / "usr" / "bin" / "mate-settings-daemon",
        ],
        "gvfsd-metadata": [
            extracted_dir / "usr" / "libexec" / "gvfsd-metadata",
            extracted_dir / "usr" / "bin" / "gvfsd-metadata",
        ],
        "xdotool": [extracted_dir / "usr" / "bin" / "xdotool"],
        "xkbcomp": [extracted_dir / "usr" / "bin" / "xkbcomp"],
        "at-spi2-registryd": [
            extracted_dir / "usr" / "libexec" / "at-spi2-registryd",
            extracted_dir / "usr" / "lib64" / "at-spi2-core" / "at-spi2-registryd",
            extracted_dir / "usr" / "lib" / "at-spi2-core" / "at-spi2-registryd",
        ],
        "gedit": [extracted_dir / "usr" / "bin" / "gedit"],
        "nautilus": [extracted_dir / "usr" / "bin" / "nautilus"],
    }
    # App binaries come from the manifests; setdefault keeps the explicit
    # entries above (and their special runtime staging) authoritative.
    for _name, _candidates in _manifest_binary_targets(extracted_dir).items():
        critical_targets.setdefault(_name, _candidates)

    restore_specs = {
        "Xvfb": [("xorg-x11-server-Xvfb-*.rpm", "./usr/bin/Xvfb")],
        "xfwm4": [("xfwm4-*.rpm", "./usr/bin/xfwm4")],
        "xfconfd": [
            ("xfconf-*.rpm", "./usr/lib64/xfce4/xfconf/xfconfd"),
            ("xfconf-*.rpm", "./usr/bin/xfconfd"),
        ],
        "xfsettingsd": [("xfce4-settings-*.rpm", "./usr/bin/xfsettingsd")],
        "mate-panel": [("mate-panel-*.rpm", "./usr/bin/mate-panel")],
        "caja": [("caja-*.rpm", "./usr/bin/caja")],
        "dbus-daemon": [("dbus-daemon-*.rpm", "./usr/bin/dbus-daemon")],
        "dbus-run-session": [("dbus-daemon-*.rpm", "./usr/bin/dbus-run-session")],
        "dbus-send": [("dbus-tools-*.rpm", "./usr/bin/dbus-send")],
        "firefox": [("firefox-*.rpm", "./usr/bin/firefox")],
        "firefox-bin": [("firefox-*.rpm", "./usr/lib64/firefox/firefox-bin")],
        "mate-settings-daemon": [("mate-settings-daemon-*.rpm", "./usr/libexec/mate-settings-daemon")],
        "gvfsd-metadata": [("gvfs-*.rpm", "./usr/libexec/gvfsd-metadata")],
        "xdotool": [("xdotool-*.rpm", "./usr/bin/xdotool")],
        "xkbcomp": [("xkbcomp-*.rpm", "./usr/bin/xkbcomp")],
        "at-spi2-registryd": [("at-spi2-core-*.rpm", "./usr/libexec/at-spi2-registryd")],
        "gedit": [("gedit-*.rpm", "./usr/bin/gedit")],
        "nautilus": [("nautilus-*.rpm", "./usr/bin/nautilus")],
    }

    def _is_fix_wrapper(path: Path, name: str) -> bool:
        try:
            # Only the head matters; reading whole binaries here cost hundreds of
            # MB of filesystem reads per session start.
            with path.open("rb") as handle:
                head = handle.read(4096).decode("utf-8", errors="ignore")[:256]
        except OSError:
            return False
        return (
            f"exec {bin_fix_dir() / name} " in head
            or f"exec /tmp/deskshot_bin_fix/{name} " in head
        )

    def _copy_from_rpms(name: str, dest: Path) -> bool:
        for rpm_glob, member in restore_specs.get(name, []):
            matches = sorted(rpms_dir.glob(rpm_glob))
            if not matches:
                continue
            for rpm_file in matches:
                with tempfile.TemporaryDirectory(prefix=f"deskshot_restore_{name}_") as tmpdir:
                    stage_dir = Path(tmpdir)
                    subprocess.run(
                        f"rpm2cpio {rpm_file} | cpio -idm --quiet {member}",
                        shell=True,
                        cwd=str(stage_dir),
                        capture_output=True,
                    )
                    staged = stage_dir / member.lstrip("./")
                    if not staged.is_file():
                        continue
                    try:
                        if dest.exists():
                            dest.unlink()
                    except OSError:
                        pass
                    shutil.copyfile(staged, dest)
                    dest.chmod(0o755)
                    print(f"  [restore] {name} from cached RPM")
                    return True
        return False

    def _stage_firefox_runtime(src_binary: Path) -> bool:
        src_dir = src_binary.parent
        runtime_dir = fix_dir / "firefox-runtime"
        if _runtime_stage_is_current(src_dir, runtime_dir, fix_dir / "firefox-bin"):
            return True
        runtime_dir.mkdir(parents=True, exist_ok=True)

        for item in src_dir.iterdir():
            dst = runtime_dir / item.name
            try:
                if dst.is_symlink() or dst.is_file():
                    dst.unlink()
                elif dst.is_dir():
                    shutil.rmtree(dst)
            except OSError:
                pass

            if item.name == "firefox-bin":
                try:
                    shutil.copyfile(item, dst)
                    dst.chmod(0o755)
                except OSError:
                    if not _copy_from_rpms("firefox-bin", dst):
                        return False
                continue

            try:
                dst.symlink_to(item.resolve(), target_is_directory=item.is_dir())
            except OSError:
                try:
                    if item.is_dir():
                        shutil.copytree(item, dst, symlinks=True)
                    else:
                        shutil.copyfile(item, dst)
                except OSError:
                    return False

        entry = fix_dir / "firefox-bin"
        try:
            if entry.exists() or entry.is_symlink():
                entry.unlink()
        except OSError:
            pass
        entry.symlink_to((runtime_dir / "firefox-bin").resolve())
        _write_runtime_stage_stamp(src_dir, runtime_dir)
        print(f"  [perms] staged firefox runtime -> {runtime_dir}")
        return True

    def _stage_chromium_runtime(src_binary: Path) -> bool:
        src_dir = src_binary.resolve().parent
        if src_binary.name == "chromium-browser" and src_dir.name != "chromium-browser":
            src_dir = extracted_dir / "usr" / "lib64" / "chromium-browser"
        if not (src_dir / "chromium-browser.sh").is_file() or not (src_dir / "chromium-browser").is_file():
            return False

        runtime_dir = fix_dir / "chromium-runtime"
        if _runtime_stage_is_current(src_dir, runtime_dir, fix_dir / "chromium-browser"):
            return True
        runtime_dir.mkdir(parents=True, exist_ok=True)
        executable_names = {
            "chromium-browser",
            "chromium-browser.sh",
            "chrome_crashpad_handler",
            "chrome-sandbox",
        }

        for item in src_dir.iterdir():
            dst = runtime_dir / item.name
            try:
                if dst.is_symlink() or dst.is_file():
                    dst.unlink()
                elif dst.is_dir():
                    shutil.rmtree(dst)
            except OSError:
                pass

            try:
                if item.name in executable_names and item.is_file():
                    shutil.copyfile(item, dst)
                    dst.chmod(0o755)
                else:
                    dst.symlink_to(item.resolve(), target_is_directory=item.is_dir())
            except OSError:
                return False

        entry = fix_dir / "chromium-browser"
        try:
            if entry.exists() or entry.is_symlink():
                entry.unlink()
        except OSError:
            pass
        entry.symlink_to((runtime_dir / "chromium-browser.sh").resolve())
        _write_runtime_stage_stamp(src_dir, runtime_dir)
        print(f"  [perms] staged chromium runtime -> {runtime_dir}")
        return True

    for name, candidates in critical_targets.items():
        path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if path is None:
            continue
        dest = fix_dir / name
        if name == "firefox-bin":
            if _stage_firefox_runtime(path):
                _prepend_fix_dir_to_path(fix_dir)
                print(f"  [perms] linked {name} -> {dest} (PATH prepend)")
                continue
        if name == "chromium-browser" and not os.access(str(path), os.X_OK):
            if _stage_chromium_runtime(path):
                _prepend_fix_dir_to_path(fix_dir)
                print(f"  [perms] linked {name} -> {dest} (PATH prepend)")
                continue
        if _is_fix_wrapper(path, name):
            if not _copy_from_rpms(name, dest):
                print(f"  [WARN] Could not restore wrapped binary: {name}")
                continue
            _prepend_fix_dir_to_path(fix_dir)
            print(f"  [perms] copied {name} -> {dest} (PATH prepend)")
            continue
        if os.access(str(path), os.X_OK):
            continue
        if _staged_copy_is_current(path, dest):
            # Already repaired by an earlier run; re-copying the source on every
            # session start is pure GPFS traffic, which is the slow path here.
            _prepend_fix_dir_to_path(fix_dir)
            continue
        try:
            path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            print(f"  [perms] chmod +x {name}")
            continue
        except OSError:
            path = next((candidate for candidate in candidates if candidate.is_file()), path)
        try:
            _atomic_stage(path, dest)
            _prepend_fix_dir_to_path(fix_dir)
            print(f"  [perms] copied {name} -> {dest} (PATH prepend)")
        except OSError:
            if _copy_from_rpms(name, dest):
                _prepend_fix_dir_to_path(fix_dir)
                print(f"  [perms] copied {name} -> {dest} (PATH prepend)")
                continue
            continue



@contextlib.contextmanager
def staging_lock(timeout: float = 300.0):
    """Serialise binary staging across concurrent sessions.

    Every session calls `ensure_runtime_path_bridges`, which copies and patches
    binaries in one shared directory. Three scene workers doing that at once is
    what produced `Text file busy: /tmp/deskshot_bin_fix/Xvfb` and, less
    visibly, sessions that started against a binary another worker was still
    writing. The work is idempotent and fast once done, so holding a lock costs
    nothing after the first session and removes the race entirely.
    """
    lock_dir = bin_fix_dir()
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ".staging.lock"
    handle = open(lock_path, "w")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    # Better to proceed unserialised than to fail the session:
                    # the individual writes are atomic even without the lock.
                    warnings.warn("staging lock timed out; proceeding unserialised")
                    break
                time.sleep(0.2)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def ensure_runtime_path_bridges(extracted_dir: Path | None = None) -> None:
    """Recreate /tmp bridge files required by already-patched binaries.

    The extracted binaries are patched once during setup, but the /tmp wrapper
    and symlink targets can disappear between runs or after server cleanup.
    This helper is intentionally lightweight and safe to call at every session
    startup.
    """
    with staging_lock():
        _ensure_runtime_path_bridges_locked(extracted_dir)


def _ensure_runtime_path_bridges_locked(extracted_dir: Path | None = None) -> None:
    root = extracted_dir or EXTRACTED_DIR
    lib_dir = root / "usr" / "lib64"
    fix_dir = bin_fix_dir()

    _ensure_binaries_executable(root)

    xvfb_fix = fix_dir / "Xvfb"
    if xvfb_fix.is_file():
        _patch_binary_bytes(
            xvfb_fix,
            b"/usr/bin\x00",
            b"/tmp/xkb\x00",
            label="runtime Xvfb XkbBinDirectory",
        )

    xfwm4_fix = fix_dir / "xfwm4"
    if xfwm4_fix.is_file():
        _patch_binary_bytes(
            xfwm4_fix,
            b"/usr/share/xfwm4\x00",
            b"/tmp/xfwm4_data_\x00",
            label="runtime xfwm4 datadir",
        )

    mate_panel_fix = fix_dir / "mate-panel"
    if mate_panel_fix.is_file():
        _patch_binary_bytes(
            mate_panel_fix,
            b"/usr/share/mate-panel/",
            b"/tmp/mate-panel_data_/",
            label="runtime mate-panel datadir",
            replace_all=True,
        )

    caja_fix = fix_dir / "caja"
    if caja_fix.is_file():
        _patch_binary_bytes(
            caja_fix,
            b"/usr/share/caja",
            b"/tmp/caja_data_",
            label="runtime caja datadir",
            replace_all=True,
        )

    xkbcomp_bin = root / "usr" / "bin" / "xkbcomp"
    if xkbcomp_bin.is_file():
        XKBCOMP_DIR.mkdir(parents=True, exist_ok=True)
        wrapper = XKBCOMP_DIR / "xkbcomp"
        wrapper.write_text(
            f"#!/bin/bash\n"
            f'export LD_LIBRARY_PATH="{lib_dir}:${{LD_LIBRARY_PATH}}"\n'
            f'exec "{fix_dir / "xkbcomp" if (fix_dir / "xkbcomp").is_file() else xkbcomp_bin}" "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

    xfwm4_defaults = root / "usr" / "share" / "xfwm4" / "defaults"
    if xfwm4_defaults.is_file():
        XFWM4_DATA_DIR.mkdir(parents=True, exist_ok=True)
        _refresh_symlink(XFWM4_DATA_DIR / "defaults", xfwm4_defaults)

    mate_panel_share = root / "usr" / "share" / "mate-panel"
    mate_panel_libs = root / "usr" / "lib64" / "mate-panel"
    if mate_panel_share.is_dir():
        MATE_PANEL_DATA_DIR.mkdir(parents=True, exist_ok=True)
        if (mate_panel_share / "layouts").is_dir():
            _refresh_symlink(MATE_PANEL_DATA_DIR / "layouts", mate_panel_share / "layouts")
        if (mate_panel_share / "applets").is_dir():
            _refresh_symlink(MATE_PANEL_DATA_DIR / "applets", mate_panel_share / "applets")
    if mate_panel_libs.is_dir():
        _refresh_symlink(MATE_PANEL_LIB_DIR, mate_panel_libs)

    caja_share = root / "usr" / "share" / "caja"
    if caja_share.is_dir():
        _refresh_symlink(CAJA_DATA_DIR, caja_share)

    gsm_share = root / "usr" / "share" / "gnome-system-monitor"
    if gsm_share.is_dir():
        _refresh_symlink(GNOME_SYSTEM_MONITOR_DATA_DIR, gsm_share)

    geany_share = root / "usr" / "share" / "geany"
    if geany_share.is_dir():
        _refresh_symlink(GEANY_DATA_DIR, geany_share)

    terminal_service = root / "usr" / "share" / "dbus-1" / "services" / "org.gnome.Terminal.service"
    terminal_server = root / "usr" / "libexec" / "gnome-terminal-server"
    if terminal_service.is_file() and terminal_server.is_file():
        text = terminal_service.read_text(encoding="utf-8")
        desired_exec = f"Exec={terminal_server.resolve()}"
        if desired_exec not in text:
            new_lines = []
            changed = False
            for line in text.splitlines():
                if line.startswith("Exec=") and line != desired_exec:
                    new_lines.append(desired_exec)
                    changed = True
                else:
                    new_lines.append(line)
            if changed:
                terminal_service.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _atomic_stage(source: Path, dest: Path) -> None:
    """Copy a binary into place without ever writing to a path that is in use.

    Writing straight to `dest` fails with "Text file busy" when another process
    is executing it - measured with three scene workers sharing
    `/tmp/deskshot_bin_fix`, where one worker copied Xvfb while another was
    running it, and the scene was lost. Copying to a unique temporary name and
    renaming is atomic: a running process keeps the old inode, and every later
    exec sees a complete file rather than a half-written one.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    try:
        shutil.copyfile(source, tmp)
        tmp.chmod(0o755)
        os.replace(tmp, dest)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _library_exports_symbol(library: Path, symbol: str) -> bool:
    if not library.is_file():
        return False
    result = subprocess.run(
        ["nm", "-D", str(library)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    return symbol in result.stdout


def build_chromium_runtime_override(tools_dir: Path | None = None) -> Path | None:
    """Build a Chromium-only libmpg123 override with the newer ABI Chromium needs."""
    tools = tools_dir or TOOLS_DIR
    override_root = tools / "runtime_overrides" / CHROMIUM_OVERRIDE_DIRNAME
    installed_lib = override_root / "lib" / "libmpg123.so.0"
    if _library_exports_symbol(installed_lib, "mpg123_param2"):
        print(f"  [override] Chromium mpg123 already ready at {installed_lib}")
        return override_root

    src_root = tools / "runtime_overrides" / "src"
    tarball = src_root / f"mpg123-{MPG123_OVERRIDE_VERSION}.tar.bz2"
    unpacked = src_root / f"mpg123-{MPG123_OVERRIDE_VERSION}"
    src_root.mkdir(parents=True, exist_ok=True)
    override_root.mkdir(parents=True, exist_ok=True)

    if not tarball.is_file():
        print(f"  [override] Downloading mpg123 {MPG123_OVERRIDE_VERSION} source...")
        urllib.request.urlretrieve(MPG123_OVERRIDE_URL, tarball)

    if unpacked.exists():
        shutil.rmtree(unpacked)
    with tarfile.open(tarball, "r:bz2") as tf:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            tf.extractall(path=src_root)

    commands = [
        ["./configure", f"--prefix={override_root}", "--disable-static"],
        ["make", "-j2"],
        ["make", "install"],
    ]
    for cmd in commands:
        subprocess.run(
            cmd,
            cwd=str(unpacked),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    if _library_exports_symbol(installed_lib, "mpg123_param2"):
        print(f"  [override] Built Chromium mpg123 override at {installed_lib}")
        return override_root

    print("  [WARN] Chromium mpg123 override built but missing mpg123_param2")
    return None


def patch_xvfb_xkbcomp(extracted_dir: Path) -> None:
    """Backward-compatible alias for older callers."""
    patch_runtime_paths(extracted_dir)


def verify_binaries(extracted_dir: Path | None = None) -> dict[str, bool]:
    """Check which key binaries are available. Returns name → found mapping."""
    root = extracted_dir or EXTRACTED_DIR
    bin_dir = root / "usr" / "bin"
    expected = [
        "Xvfb", "dbus-daemon", "dbus-run-session", "dbus-send",
        "xdotool", "gnome-calculator", "gnome-terminal", "gnome-system-monitor",
        "baobab", "file-roller", "gedit", "eog", "evince", "nautilus",
        "libreoffice", "thunderbird", "zim",
        "xfce4-session", "xfwm4", "xfce4-panel", "xfdesktop",
        "xfconf-query", "xfsettingsd",
        "mate-panel", "caja",
    ]
    results = {}
    for name in expected:
        found = (bin_dir / name).is_file()
        if not found:
            # Check libexec paths
            found = bool(list(root.rglob(name)))
        results[name] = found
        status = "OK" if found else "MISSING"
        print(f"  [{status}] {name}")
    return results


def run_setup(tools_dir: Path | None = None) -> None:
    """Run the full setup process."""
    tools = tools_dir or TOOLS_DIR
    rpms_dir = tools / "rpms"
    extracted_dir = tools / "extracted"

    rpms_dir.mkdir(parents=True, exist_ok=True)
    extracted_dir.mkdir(parents=True, exist_ok=True)

    print("=== DeskShot Tool Setup ===")
    print(f"Tools dir: {tools}")

    print("\n── Downloading RPMs ──")
    for pkg in ALL_RPMS:
        download_rpm(pkg, rpms_dir)
    print("\n── Extracting RPMs ──")
    extract_rpms(rpms_dir, extracted_dir)

    print("\n── Post-extraction ──")
    compile_schemas(extracted_dir)
    patch_runtime_paths(extracted_dir)
    ensure_runtime_path_bridges(extracted_dir)
    summary = build_wallpaper_variants(extracted_dir=extracted_dir)
    print(
        "  [wallpapers] generated "
        f"{summary['generated_files']} variants from {summary['source_count']} sources"
    )
    print("\n── External app bundles (best effort) ──")
    try:
        install_vscode_bundle(tools)
    except Exception as exc:
        print(f"  [WARN] VS Code bundle install failed: {exc}")
    print("\n── Style packs (best effort) ──")
    try:
        from deskshot.environment.stylepacks import install_style_packs

        style_summary = install_style_packs(
            extracted_dir=extracted_dir,
            cache_root=tools / "stylepacks",
        )
        print(f"  [styles] themes: {', '.join(style_summary['themes']) or '(none)'}")
        print(f"  [styles] icons:  {', '.join(style_summary['icons']) or '(none)'}")
        if style_summary["warnings"]:
            print(f"  [styles] warnings: {', '.join(style_summary['warnings'])}")
    except Exception as exc:
        print(f"  [WARN] style pack install failed: {exc}")

    print("\n── Optional shell components (best effort) ──")
    try:
        install_plank_bundle(tools, extracted_dir)
    except Exception as exc:
        print(f"  [WARN] plank runtime install failed: {exc}")
    try:
        install_picom_bundle(tools, extracted_dir)
    except Exception as exc:
        print(f"  [WARN] picom bundle build failed: {exc}")

    print("\n── Chromium override (best effort) ──")
    try:
        build_chromium_runtime_override(tools)
    except Exception as exc:
        print(f"  [WARN] Chromium runtime override failed: {exc}")

    print("\n── Verification ──")
    results = verify_binaries(extracted_dir)

    missing = [k for k, v in results.items() if not v]
    if missing:
        print(f"\n[WARN] Missing binaries: {', '.join(missing)}")
    else:
        print("\n[OK] All binaries available")

    print(f"\n=== Setup complete ===")
    print(f"Tools extracted to: {extracted_dir}")
