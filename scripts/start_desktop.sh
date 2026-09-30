#!/usr/bin/env bash
# start_desktop.sh — Start Xvfb + D-Bus + AT-SPI session for interactive use.
#
# Usage: source scripts/start_desktop.sh [DISPLAY_NUM]
#   DISPLAY_NUM defaults to 99.
#
# This script sets environment variables in the current shell.
# For automated use, prefer the Python DesktopSession context manager.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TOOLS_DIR="$PROJECT_DIR/tools"
BIN_DIR="$TOOLS_DIR/extracted/usr/bin"
LIB_DIR="$TOOLS_DIR/extracted/usr/lib64"

DISPLAY_NUM="${1:-99}"

# ── Check prerequisites ─────────────────────────────────────────────────

if [ ! -x "$BIN_DIR/Xvfb" ]; then
    echo "ERROR: Xvfb not found. Run 'bash scripts/setup_tools.sh' first."
    exit 1
fi

# ── Add extracted tools to PATH ──────────────────────────────────────────

export PATH="$BIN_DIR:$PATH"
export LD_LIBRARY_PATH="${LIB_DIR}:${LD_LIBRARY_PATH:-}"

# GSettings schemas
SCHEMAS_DIR="$TOOLS_DIR/extracted/usr/share/glib-2.0/schemas"
if [ -d "$SCHEMAS_DIR" ]; then
    export GSETTINGS_SCHEMA_DIR="${SCHEMAS_DIR}:${GSETTINGS_SCHEMA_DIR:-}"
fi

# XDG data dirs
export XDG_DATA_DIRS="$TOOLS_DIR/extracted/usr/share:${XDG_DATA_DIRS:-/usr/share}"

# ── Start Xvfb ──────────────────────────────────────────────────────────

echo "Starting Xvfb on display :$DISPLAY_NUM..."
"$BIN_DIR/Xvfb" ":$DISPLAY_NUM" -screen 0 1920x1080x24 -ac -nolisten tcp &
XVFB_PID=$!
export DISPLAY=":$DISPLAY_NUM"
sleep 0.5

if ! kill -0 "$XVFB_PID" 2>/dev/null; then
    echo "ERROR: Xvfb failed to start"
    exit 1
fi
echo "  Xvfb PID: $XVFB_PID"

# ── Start D-Bus session ─────────────────────────────────────────────────

echo "Starting D-Bus session daemon..."
DBUS_ADDR=$("$BIN_DIR/dbus-daemon" --session --fork --print-address 2>/dev/null || true)
if [ -z "$DBUS_ADDR" ]; then
    # Fallback: try dbus-run-session
    echo "  (dbus-daemon --fork failed, trying alternative...)"
    DBUS_ADDR=$("$BIN_DIR/dbus-daemon" --session --fork --print-address 2>&1 | head -1)
fi
export DBUS_SESSION_BUS_ADDRESS="$DBUS_ADDR"
echo "  D-Bus: $DBUS_ADDR"

# ── Start AT-SPI ────────────────────────────────────────────────────────

echo "Starting AT-SPI accessibility bus..."
A11Y_CONF="/usr/share/defaults/at-spi2/accessibility.conf"
if [ -f "$A11Y_CONF" ]; then
    "$BIN_DIR/dbus-daemon" --config-file="$A11Y_CONF" --nofork --print-address > /tmp/deskshot_a11y_bus &
    A11Y_PID=$!
    sleep 0.3
    A11Y_ADDR=$(head -1 /tmp/deskshot_a11y_bus 2>/dev/null || true)
    if [ -n "$A11Y_ADDR" ]; then
        export AT_SPI_BUS_ADDRESS="$A11Y_ADDR"
        echo "  AT-SPI bus: $A11Y_ADDR"
    fi
fi

# Start registryd
REGISTRYD="/usr/libexec/at-spi2-registryd"
if [ -x "$REGISTRYD" ]; then
    DBUS_SESSION_BUS_ADDRESS="${AT_SPI_BUS_ADDRESS:-$DBUS_ADDR}" "$REGISTRYD" &
    echo "  AT-SPI registryd started"
fi

export GTK_MODULES="gail:atk-bridge"

echo ""
echo "=== Desktop session ready ==="
echo "  DISPLAY=$DISPLAY"
echo "  DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS"
echo "  GTK_MODULES=$GTK_MODULES"
echo ""
echo "To stop: kill $XVFB_PID"
echo "To launch an app: gnome-calculator &"
