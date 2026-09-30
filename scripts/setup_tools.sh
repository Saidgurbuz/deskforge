#!/usr/bin/env bash
# setup_tools.sh — Download and extract RPMs for DeskShot (no sudo required)
#
# Usage: bash scripts/setup_tools.sh [TOOLS_DIR]
#   TOOLS_DIR defaults to ./tools

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TOOLS_DIR="${1:-$PROJECT_DIR/tools}"
RPMS_DIR="$TOOLS_DIR/rpms"
EXTRACTED_DIR="$TOOLS_DIR/extracted"

echo "=== DeskShot Tool Setup ==="
echo "Tools dir: $TOOLS_DIR"

mkdir -p "$RPMS_DIR" "$EXTRACTED_DIR"
EXTERNAL_APPS_DIR="$TOOLS_DIR/external_apps"
EXTERNAL_BIN_DIR="$EXTERNAL_APPS_DIR/bin"
EXTERNAL_BUNDLES_DIR="$EXTERNAL_APPS_DIR/bundles"
EXTERNAL_DOWNLOADS_DIR="$EXTERNAL_APPS_DIR/downloads"
EXTERNAL_APPLICATIONS_DIR="$EXTERNAL_APPS_DIR/applications"

# ── Phase 1: Download RPMs ──────────────────────────────────────────────

download_rpm() {
    local pkg="$1"
    # Check if already downloaded
    if ls "$RPMS_DIR"/${pkg}-*.rpm 1>/dev/null 2>&1; then
        echo "  [skip] $pkg (already downloaded)"
        return 0
    fi
    echo "  [download] $pkg"
    dnf download -y --arch=x86_64,noarch --destdir="$RPMS_DIR" "$pkg" 2>/dev/null || {
        echo "  [WARN] Failed to download $pkg — may not be available"
        return 0
    }
}

echo ""
echo "── Downloading RPMs ──"

# Core infrastructure
download_rpm "xorg-x11-server-Xvfb"
download_rpm "xkbcomp"
download_rpm "dbus-daemon"
download_rpm "dbus-tools"

# Input simulation
download_rpm "xdotool"
download_rpm "libxdo"

# Desktop apps
download_rpm "gnome-calculator"
download_rpm "gnome-terminal"
download_rpm "gnome-system-monitor"
download_rpm "baobab"
download_rpm "meld"
download_rpm "file-roller"
download_rpm "gedit"
download_rpm "eog"
download_rpm "evince"
download_rpm "evince-libs"
download_rpm "nautilus"
download_rpm "atril"
download_rpm "atril-libs"
download_rpm "dia"
download_rpm "gucharmap"
download_rpm "keepassxc"
download_rpm "remmina"
download_rpm "xapps"
download_rpm "xreader"
download_rpm "xreader-libs"
download_rpm "xarchiver"
download_rpm "xournalpp"
download_rpm "boost-filesystem"
download_rpm "boost-chrono"
download_rpm "boost-iostreams"
download_rpm "boost-locale"
download_rpm "boost-system"
download_rpm "boost-thread"
download_rpm "harfbuzz-icu"
download_rpm "libreoffice"
download_rpm "libreoffice-core"
download_rpm "libreoffice-data"
download_rpm "libreoffice-filters"
download_rpm "libreoffice-graphicfilter"
download_rpm "libreoffice-gtk3"
download_rpm "libreoffice-langpack-en"
download_rpm "libreoffice-ure"
download_rpm "libreoffice-writer"
download_rpm "libreoffice-calc"
download_rpm "libreoffice-impress"
download_rpm "gpgmepp"
download_rpm "liborcus"
download_rpm "liblangtag"
download_rpm "evolution"
download_rpm "thunderbird"
download_rpm "geany"
download_rpm "mousepad"
download_rpm "kate"
download_rpm "featherpad"
download_rpm "emacs"
download_rpm "pluma"
download_rpm "bluefish"
download_rpm "thonny"
download_rpm "qt-creator"
download_rpm "vim-X11"
download_rpm "notepadqq"
download_rpm "spyder5"
download_rpm "geany-libgeany"
download_rpm "libmousepad0"
download_rpm "kate-libs"
download_rpm "qt5-qtsvg"
download_rpm "qt6-qt5compat"
download_rpm "qt-creator-data"

# Browser apps
download_rpm "firefox"
download_rpm "chromium"
download_rpm "chromium-common"
download_rpm "double-conversion"
download_rpm "libavcodec-free"
download_rpm "libavformat-free"
download_rpm "libavutil-free"
download_rpm "libcanberra-gtk3"
download_rpm "libdav1d"
download_rpm "nss-mdns"
download_rpm "openh264"
download_rpm "mozilla-openh264"
download_rpm "noopenh264"
download_rpm "pipewire-libs"
download_rpm "fdk-aac-free"

# Common dependencies that apps may need at runtime
download_rpm "atkmm"
download_rpm "atkmm30"
download_rpm "botan2"
download_rpm "cairomm"
download_rpm "gperftools-libs"
download_rpm "glibmm24"
download_rpm "gtksourceview4"
download_rpm "gtkmm30"
download_rpm "gucharmap-libs"
download_rpm "libgee"
download_rpm "libhandy"
download_rpm "libargon2"
download_rpm "libzip"
download_rpm "libpeas"
download_rpm "libpeas-gtk"
download_rpm "libsecret"
download_rpm "minizip1.2"
download_rpm "gnome-desktop3"
download_rpm "librsvg2"
download_rpm "gspell"
download_rpm "enchant"
download_rpm "enchant2"
download_rpm "libnotify"
download_rpm "pcsc-lite-libs"
download_rpm "portaudio"
download_rpm "qrencode-libs"
download_rpm "xcb-util"
download_rpm "libXfont2"
download_rpm "libXdmcp"
download_rpm "libfontenc"
download_rpm "libxkbfile"
download_rpm "libxklavier"
download_rpm "libsigc++20"
download_rpm "pangomm"
download_rpm "vte291"

# XFCE desktop environment
download_rpm "xfce4-session"
download_rpm "xfwm4"
download_rpm "xfce4-panel"
download_rpm "xfdesktop"
download_rpm "xfce4-settings"
download_rpm "xfconf"
download_rpm "libxfce4ui"
download_rpm "libxfce4util"
download_rpm "garcon"
download_rpm "exo"
download_rpm "libwnck3"
download_rpm "startup-notification"
download_rpm "iceauth"
download_rpm "Thunar"
download_rpm "xfce4-whiskermenu-plugin"

# MATE desktop components (panel + file manager for desktop icons)
download_rpm "mate-settings-daemon"
download_rpm "mate-panel"
download_rpm "mate-panel-libs"
download_rpm "mate-desktop-libs"
download_rpm "mate-menus"
download_rpm "mate-menus-libs"
download_rpm "caja"
download_rpm "caja-schemas"
download_rpm "caja-core-extensions"
download_rpm "gvfs"
download_rpm "gvfs-client"
download_rpm "libmateweather"
download_rpm "gtk-layer-shell"

# Themes and wallpapers
download_rpm "papirus-icon-theme"
download_rpm "xfwm4-themes"
download_rpm "desktop-backgrounds-basic"
download_rpm "desktop-backgrounds-compat"
download_rpm "desktop-backgrounds-gnome"
download_rpm "desktop-backgrounds-waves"
download_rpm "gnome-backgrounds"
download_rpm "gnome-backgrounds-extras"
download_rpm "mate-backgrounds"
download_rpm "redhat-backgrounds"
download_rpm "f35-backgrounds-base"
download_rpm "f35-backgrounds-extras-base"
download_rpm "f35-backgrounds-gnome"
download_rpm "f35-backgrounds-mate"
download_rpm "f35-backgrounds-xfce"
download_rpm "f35-backgrounds-extras-gnome"
download_rpm "f35-backgrounds-extras-mate"
download_rpm "f35-backgrounds-extras-xfce"
download_rpm "f35-backgrounds-kde"
download_rpm "f35-backgrounds-extras-kde"
download_rpm "f36-backgrounds-base"
download_rpm "f36-backgrounds-extras-base"
download_rpm "f36-backgrounds-gnome"
download_rpm "f36-backgrounds-mate"
download_rpm "f36-backgrounds-xfce"
download_rpm "f36-backgrounds-extras-gnome"
download_rpm "f36-backgrounds-extras-mate"
download_rpm "f36-backgrounds-extras-xfce"
download_rpm "f36-backgrounds-kde"
download_rpm "f36-backgrounds-extras-kde"
download_rpm "f37-backgrounds-base"
download_rpm "f37-backgrounds-extras-base"
download_rpm "f37-backgrounds-gnome"
download_rpm "f37-backgrounds-mate"
download_rpm "f37-backgrounds-xfce"
download_rpm "f37-backgrounds-extras-gnome"
download_rpm "f37-backgrounds-extras-mate"
download_rpm "f37-backgrounds-extras-xfce"
download_rpm "f37-backgrounds-kde"
download_rpm "f37-backgrounds-extras-kde"
download_rpm "f38-backgrounds-base"
download_rpm "f38-backgrounds-budgie"
download_rpm "f38-backgrounds-extras-base"
download_rpm "f38-backgrounds-gnome"
download_rpm "f38-backgrounds-mate"
download_rpm "f38-backgrounds-xfce"
download_rpm "f38-backgrounds-extras-gnome"
download_rpm "f38-backgrounds-extras-mate"
download_rpm "f38-backgrounds-extras-xfce"
download_rpm "f38-backgrounds-kde"
download_rpm "f38-backgrounds-extras-kde"
download_rpm "f39-backgrounds-base"
download_rpm "f39-backgrounds-budgie"
download_rpm "f39-backgrounds-extras-base"
download_rpm "f39-backgrounds-gnome"
download_rpm "f39-backgrounds-mate"
download_rpm "f39-backgrounds-xfce"
download_rpm "f39-backgrounds-extras-gnome"
download_rpm "f39-backgrounds-extras-mate"
download_rpm "f39-backgrounds-extras-xfce"
download_rpm "f39-backgrounds-kde"
download_rpm "f39-backgrounds-extras-kde"
download_rpm "xorg-x11-server-utils"
download_rpm "ImageMagick"
download_rpm "ImageMagick-libs"
download_rpm "libraqm"
download_rpm "liblqr-1"

# Build/runtime deps for rootless picom bundle
download_rpm "meson"
download_rpm "ninja-build"
download_rpm "libconfig"
download_rpm "libconfig-devel"
download_rpm "libev"
download_rpm "libev-devel"
download_rpm "uthash-devel"
download_rpm "pcre2"
download_rpm "pcre2-devel"
download_rpm "pixman"
download_rpm "pixman-devel"
download_rpm "libX11"
download_rpm "libX11-devel"
download_rpm "libX11-xcb"
download_rpm "libxcb"
download_rpm "libxcb-devel"
download_rpm "libXau"
download_rpm "libXau-devel"
download_rpm "libXdmcp-devel"
download_rpm "xcb-util-devel"
download_rpm "xcb-util-image"
download_rpm "xcb-util-image-devel"
download_rpm "xcb-util-renderutil"
download_rpm "xcb-util-renderutil-devel"
download_rpm "dbus-libs"
download_rpm "dbus-devel"
download_rpm "xorg-x11-proto-devel"

echo ""
echo "── Extracting RPMs ──"

# ── Phase 2: Extract all RPMs ───────────────────────────────────────────

cd "$EXTRACTED_DIR"
for rpm_file in "$RPMS_DIR"/*.rpm; do
    [ -f "$rpm_file" ] || continue
    basename_rpm="$(basename "$rpm_file")"
    echo "  [extract] $basename_rpm"
    rpm2cpio "$rpm_file" | cpio -idm --quiet 2>/dev/null || true
done

echo ""
echo "── Post-extraction setup ──"

# ── Phase 3: Compile GSettings schemas ──────────────────────────────────

SCHEMAS_DIR="$EXTRACTED_DIR/usr/share/glib-2.0/schemas"
if [ -d "$SCHEMAS_DIR" ]; then
    echo "  [schemas] Compiling GSettings schemas..."
    glib-compile-schemas "$SCHEMAS_DIR" 2>/dev/null || true
fi

echo "  [wallpapers] Building deterministic variant pool..."
PYTHONPATH=src python scripts/build_wallpaper_variants.py --extracted-dir "$EXTRACTED_DIR" --assets-dir "$PROJECT_ROOT/assets" 2>/dev/null || true

echo "  [external] Installing VS Code bundle..."
mkdir -p "$EXTERNAL_BIN_DIR" "$EXTERNAL_BUNDLES_DIR" "$EXTERNAL_DOWNLOADS_DIR" "$EXTERNAL_APPLICATIONS_DIR"
VSCODE_ARCHIVE="$EXTERNAL_DOWNLOADS_DIR/vscode-linux-x64.tar.gz"
VSCODE_BUNDLE_DIR="$EXTERNAL_BUNDLES_DIR/VSCode-linux-x64"
if [ ! -x "$VSCODE_BUNDLE_DIR/bin/code" ]; then
    if [ ! -f "$VSCODE_ARCHIVE" ]; then
        if command -v curl >/dev/null 2>&1; then
            curl -L "https://update.code.visualstudio.com/latest/linux-x64/stable" -o "$VSCODE_ARCHIVE"
        elif command -v wget >/dev/null 2>&1; then
            wget -O "$VSCODE_ARCHIVE" "https://update.code.visualstudio.com/latest/linux-x64/stable"
        else
            echo "  [WARN] Missing curl/wget, cannot download VS Code bundle"
        fi
    else
        echo "  [skip] VS Code archive"
    fi
    if [ -f "$VSCODE_ARCHIVE" ]; then
        tar -xzf "$VSCODE_ARCHIVE" -C "$EXTERNAL_BUNDLES_DIR"
    fi
else
    echo "  [skip] VS Code bundle"
fi
if [ -x "$VSCODE_BUNDLE_DIR/bin/code" ]; then
    ln -sfn "$VSCODE_BUNDLE_DIR/bin/code" "$EXTERNAL_BIN_DIR/code"
    cat > "$EXTERNAL_APPLICATIONS_DIR/code.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Visual Studio Code
Exec=code --new-window
Terminal=false
Categories=Development;IDE;TextEditor;
StartupWMClass=Code
EOF
fi

# ── Phase 4: Patch hardcoded runtime paths in extracted binaries ────────

echo "  [patch] Patching runtime paths..."
export EXTRACTED_DIR
python3 <<'PY'
from pathlib import Path
import os
import shutil
import stat

extracted = Path(os.environ["EXTRACTED_DIR"])
bin_dir = extracted / "usr" / "bin"
lib_dir = extracted / "usr" / "lib64"


def patch_binary(binary: Path, old: bytes, new: bytes, label: str, replace_all: bool = False) -> None:
    if len(old) != len(new):
        print(f"  [WARN] {label}: length mismatch ({len(old)} != {len(new)})")
        return
    if not binary.is_file():
        return

    data = binary.read_bytes()
    if new in data:
        print(f"  [skip] {label} already patched")
        return
    if old not in data:
        print(f"  [WARN] {label}: pattern not found")
        return

    count = data.count(old)
    patched = data.replace(old, new) if replace_all else data.replace(old, new, 1)
    binary.write_bytes(patched)
    print(f"  [patch] {label}: replaced {count if replace_all else 1} occurrence(s)")


def refresh_symlink(link: Path, target: Path) -> None:
    target_abs = target.resolve()
    if link.is_symlink() or link.is_file():
        link.unlink()
    elif link.is_dir():
        shutil.rmtree(link)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target_abs)
    print(f"  [patch] Linked {link} -> {target_abs}")


xvfb = bin_dir / "Xvfb"
xkbcomp = bin_dir / "xkbcomp"
if xvfb.is_file() and xkbcomp.is_file():
    if Path("/usr/bin/xkbcomp").is_file():
        print("  [skip] System xkbcomp found at /usr/bin/xkbcomp")
    else:
        patch_binary(
            xvfb,
            b"/usr/bin\x00",
            b"/tmp/xkb\x00",
            "Xvfb XkbBinDirectory",
        )
        xkb_dir = Path("/tmp/xkb")
        xkb_dir.mkdir(parents=True, exist_ok=True)
        wrapper = xkb_dir / "xkbcomp"
        wrapper.write_text(
            f"#!/bin/bash\n"
            f'export LD_LIBRARY_PATH="{lib_dir}:${{LD_LIBRARY_PATH}}"\n'
            f'exec "{xkbcomp}" "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        print(f"  [patch] Created xkbcomp wrapper at {wrapper}")

xfwm4 = bin_dir / "xfwm4"
xfwm4_defaults = extracted / "usr" / "share" / "xfwm4" / "defaults"
if xfwm4.is_file() and xfwm4_defaults.is_file():
    patch_binary(
        xfwm4,
        b"/usr/share/xfwm4\x00",
        b"/tmp/xfwm4_data_\x00",
        "xfwm4 datadir",
    )
    (Path("/tmp/xfwm4_data_")).mkdir(parents=True, exist_ok=True)
    refresh_symlink(Path("/tmp/xfwm4_data_/defaults"), xfwm4_defaults)

mate_panel = bin_dir / "mate-panel"
mate_panel_share = extracted / "usr" / "share" / "mate-panel"
if mate_panel.is_file() and mate_panel_share.is_dir():
    patch_binary(
        mate_panel,
        b"/usr/share/mate-panel/",
        b"/tmp/mate-panel_data_/",
        "mate-panel datadir",
        replace_all=True,
    )
    mate_root = Path("/tmp/mate-panel_data_")
    mate_root.mkdir(parents=True, exist_ok=True)
    refresh_symlink(mate_root / "layouts", mate_panel_share / "layouts")
    refresh_symlink(mate_root / "applets", mate_panel_share / "applets")
    refresh_symlink(Path("/tmp/mate-panel_libs"), extracted / "usr" / "lib64" / "mate-panel")
    applets_dir = mate_panel_share / "applets"
    patched_files = 0
    for applet_file in sorted(applets_dir.glob("*.mate-panel-applet")):
        text = applet_file.read_text(encoding="utf-8")
        old = "Location=/usr/lib64/mate-panel/"
        new = "Location=/tmp/mate-panel_libs/"
        if old in text:
            applet_file.write_text(text.replace(old, new), encoding="utf-8")
            patched_files += 1
    if patched_files:
        print(f"  [patch] Rewrote applet library paths in {patched_files} descriptor files")

caja = bin_dir / "caja"
caja_share = extracted / "usr" / "share" / "caja"
if caja.is_file() and caja_share.is_dir():
    patch_binary(
        caja,
        b"/usr/share/caja",
        b"/tmp/caja_data_",
        "caja datadir",
        replace_all=True,
    )
    refresh_symlink(Path("/tmp/caja_data_"), caja_share)
PY

# ── Phase 5: Install richer style packs (best effort) ──────────────────

echo ""
echo "── Style packs (best effort) ──"
if TOOLS_DIR_ENV="$TOOLS_DIR" EXTRACTED_DIR_ENV="$EXTRACTED_DIR" \
   PYTHONPATH="$PROJECT_DIR/src:${PYTHONPATH:-}" \
   python3 <<'PY'
import os
from pathlib import Path

from deskshot.environment.stylepacks import install_style_packs

tools_dir = Path(os.environ["TOOLS_DIR_ENV"])
extracted_dir = Path(os.environ["EXTRACTED_DIR_ENV"])

summary = install_style_packs(
    extracted_dir=extracted_dir,
    cache_root=tools_dir / "stylepacks",
)
print(f"  [styles] themes: {', '.join(summary['themes']) if summary['themes'] else '(none)'}")
print(f"  [styles] icons:  {', '.join(summary['icons']) if summary['icons'] else '(none)'}")
if summary["warnings"]:
    print(f"  [styles] warnings: {', '.join(summary['warnings'])}")
PY
then
    :
else
    echo "  [WARN] style pack install step failed"
fi

# ── Phase 6: Install optional macOS shell components (best effort) ──────

echo ""
echo "── Optional shell components (best effort) ──"
if TOOLS_DIR_ENV="$TOOLS_DIR" EXTRACTED_DIR_ENV="$EXTRACTED_DIR" \
   PYTHONPATH="$PROJECT_DIR/src:${PYTHONPATH:-}" \
   python3 <<'PY'
import os
from pathlib import Path

from deskshot.environment.setup import install_picom_bundle, install_plank_bundle

tools_dir = Path(os.environ["TOOLS_DIR_ENV"])
extracted_dir = Path(os.environ["EXTRACTED_DIR_ENV"])
install_plank_bundle(tools_dir, extracted_dir)
install_picom_bundle(tools_dir, extracted_dir)
PY
then
    :
else
    echo "  [WARN] optional shell component install failed"
fi

# ── Phase 7: Build Chromium runtime override (best effort) ──────────────

echo ""
echo "── Chromium override (best effort) ──"
if TOOLS_DIR_ENV="$TOOLS_DIR" PYTHONPATH="$PROJECT_DIR/src:${PYTHONPATH:-}" \
   python3 <<'PY'
import os
from pathlib import Path

from deskshot.environment.setup import build_chromium_runtime_override

tools_dir = Path(os.environ["TOOLS_DIR_ENV"])
build_chromium_runtime_override(tools_dir)
PY
then
    :
else
    echo "  [WARN] Chromium override build failed"
fi

# ── Phase 8: Verify key binaries ────────────────────────────────────────

echo ""
echo "── Verification ──"

BIN_DIR="$EXTRACTED_DIR/usr/bin"

check_binary() {
    local name="$1"
    if [ -x "$BIN_DIR/$name" ]; then
        echo "  [OK] $name"
    else
        # Also check libexec
        if find "$EXTRACTED_DIR" -name "$name" -executable 2>/dev/null | head -1 | grep -q .; then
            echo "  [OK] $name (in libexec)"
        else
            echo "  [MISSING] $name"
        fi
    fi
}

check_binary "Xvfb"
check_binary "dbus-daemon"
check_binary "dbus-run-session"
check_binary "dbus-send"
check_binary "xdotool"
check_binary "gnome-calculator"
check_binary "gedit"
check_binary "eog"
check_binary "evince"
check_binary "nautilus"

# XFCE binaries
check_binary "xfce4-session"
check_binary "xfwm4"
check_binary "xfce4-panel"
check_binary "xfdesktop"
check_binary "xfconf-query"
check_binary "xfsettingsd"

# MATE binaries
check_binary "mate-panel"
check_binary "caja"

# ── Phase 7: Check library resolution ──────────────────────────────────

echo ""
echo "── Library check (Xvfb) ──"
if [ -x "$BIN_DIR/Xvfb" ]; then
    missing=$(ldd "$BIN_DIR/Xvfb" 2>/dev/null | grep "not found" || true)
    if [ -z "$missing" ]; then
        echo "  [OK] All Xvfb libraries resolved"
    else
        echo "  [WARN] Missing libraries:"
        echo "$missing" | sed 's/^/    /'
    fi
fi

echo ""
echo "=== Setup complete ==="
echo "Tools extracted to: $EXTRACTED_DIR"
echo ""
echo "To use: export PATH=\"$BIN_DIR:\$PATH\""
