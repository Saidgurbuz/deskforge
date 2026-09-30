"""Thin wrapper around xdotool for input simulation."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import logging
import time
from typing import Optional


def _find_xdotool() -> str:
    """Find xdotool binary."""
    # Check extracted tools first
    from deskshot.config import EXTRACTED_BIN, bin_fix_dir
    fixed = bin_fix_dir() / "xdotool"
    if fixed.is_file() and os.access(str(fixed), os.X_OK):
        return str(fixed)
    extracted = EXTRACTED_BIN / "xdotool"
    if extracted.is_file():
        return str(extracted)

    system = shutil.which("xdotool")
    if system:
        return system

    raise FileNotFoundError(
        "xdotool not found. Run `dsd setup` to extract it from RPMs."
    )


logger = logging.getLogger(__name__)


class XdotoolTimeout(RuntimeError):
    """A command blocked rather than failed.

    Distinguished from an ordinary failure because the causes and the responses
    differ: a blocked command usually means another client holds a pointer or
    keyboard grab, which a caller can clear and retry, while a failure means the
    command itself was wrong and retrying will not help.
    """


def _run(
    args: list[str],
    display: Optional[str] = None,
    *,
    timeout: float = 10.0,
) -> str:
    """Run xdotool with given arguments."""
    xdotool = _find_xdotool()
    env = dict(os.environ)
    if display:
        env["DISPLAY"] = display

    try:
        result = subprocess.run(
            [xdotool] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise XdotoolTimeout(
            f"xdotool {' '.join(args)} timed out after {timeout:.1f}s"
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"xdotool {' '.join(args)} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _parse_shell_kv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def key(keys: str, display: Optional[str] = None) -> None:
    """Send keyboard shortcut (e.g. 'ctrl+q', 'Return', 'alt+F4')."""
    _run(["key", "--clearmodifiers", keys], display)


def type_text(text: str, delay_ms: int = 50,
              display: Optional[str] = None) -> None:
    """Type text string character by character."""
    _run(["type", "--delay", str(delay_ms), "--clearmodifiers", text], display)


def _mousemove_settling(x: int, y: int, display: Optional[str] = None) -> None:
    """Move the pointer, preferring the synchronised form but never dying on it.

    `xdotool mousemove --sync` waits for the X server to confirm the pointer
    arrived. When a GTK menu holds a **pointer grab** that confirmation never
    comes, so the call blocks until its timeout and raises - which aborted the
    whole episode. Measured on a 64-shard run: 1,807 step failures, almost all
    of them this, and about half of all episodes ended early because of it.

    Falling back to the unsynchronised move gives up the arrival guarantee, not
    the move. A short settle covers the difference, and losing precision on one
    click is much cheaper than losing every remaining frame of the episode.
    """
    try:
        _run(["mousemove", "--sync", str(x), str(y)], display, timeout=5.0)
        return
    except XdotoolTimeout:
        logger.debug("mousemove --sync timed out at (%d, %d); moving unsynchronised", x, y)
    _run(["mousemove", str(x), str(y)], display, timeout=5.0)
    time.sleep(0.15)


def click(x: int, y: int, button: int = 1,
          display: Optional[str] = None) -> None:
    """Click at screen coordinates."""
    _mousemove_settling(x, y, display)
    _run(["click", str(button)], display)


def mousemove(x: int, y: int, display: Optional[str] = None) -> None:
    """Move mouse to screen coordinates."""
    _mousemove_settling(x, y, display)


def get_active_window(display: Optional[str] = None) -> str:
    """Get the active window ID."""
    return _run(["getactivewindow"], display)


def focus_window(window_id: str, display: Optional[str] = None) -> None:
    """Focus a window by its ID."""
    _run(["windowfocus", "--sync", window_id], display)


def window_size(window_id: str, width: int, height: int,
                display: Optional[str] = None) -> None:
    """Resize a window."""
    last_error: Optional[RuntimeError] = None
    for args in (
        ["windowsize", "--sync", window_id, str(width), str(height)],
        ["windowsize", window_id, str(width), str(height)],
    ):
        try:
            _run(args, display, timeout=5.0)
        except RuntimeError as exc:
            last_error = exc
            continue
        if _wait_for_window_geometry(window_id, w=width, h=height, display=display):
            return
    if _wait_for_window_geometry(window_id, w=width, h=height, display=display):
        return
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Failed to resize window {window_id} to {width}x{height}")


def search_window(name: str, display: Optional[str] = None) -> Optional[str]:
    """Search for a window by name. Returns first window ID or None."""
    try:
        output = _run(["search", "--name", name], display)
        window_ids = output.strip().split('\n')
        if window_ids and window_ids[0]:
            return window_ids[0]
    except RuntimeError:
        pass
    return None


def search_windows(name: str, display: Optional[str] = None) -> list[str]:
    """Search for windows by name. Returns all matching window IDs."""
    try:
        output = _run(["search", "--name", name], display)
    except RuntimeError:
        return []
    return [wid for wid in output.strip().split("\n") if wid.strip()]


def get_window_name(window_id: str, display: Optional[str] = None) -> str:
    """Get a window title by ID."""
    return _run(["getwindowname", window_id], display)


def get_window_geometry(window_id: str, display: Optional[str] = None) -> dict[str, int]:
    """Get X/Y/W/H for a window."""
    output = _run(["getwindowgeometry", "--shell", window_id], display)
    kv = _parse_shell_kv(output)
    return {
        "x": int(kv.get("X", "0")),
        "y": int(kv.get("Y", "0")),
        "w": int(kv.get("WIDTH", "0")),
        "h": int(kv.get("HEIGHT", "0")),
    }


def _geometry_matches(
    geom: dict[str, int],
    *,
    x: Optional[int] = None,
    y: Optional[int] = None,
    w: Optional[int] = None,
    h: Optional[int] = None,
    tolerance: int = 16,
) -> bool:
    checks = []
    if x is not None:
        checks.append(abs(int(geom.get("x", 0)) - int(x)) <= tolerance)
    if y is not None:
        checks.append(abs(int(geom.get("y", 0)) - int(y)) <= tolerance)
    if w is not None:
        checks.append(abs(int(geom.get("w", 0)) - int(w)) <= tolerance)
    if h is not None:
        checks.append(abs(int(geom.get("h", 0)) - int(h)) <= tolerance)
    return all(checks) if checks else True


def _wait_for_window_geometry(
    window_id: str,
    *,
    x: Optional[int] = None,
    y: Optional[int] = None,
    w: Optional[int] = None,
    h: Optional[int] = None,
    display: Optional[str] = None,
    timeout_s: float = 4.0,
    poll_s: float = 0.1,
    tolerance: int = 16,
) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            geom = get_window_geometry(window_id, display)
        except RuntimeError:
            time.sleep(poll_s)
            continue
        if _geometry_matches(geom, x=x, y=y, w=w, h=h, tolerance=tolerance):
            return True
        time.sleep(poll_s)
    return False


def get_display_geometry(display: Optional[str] = None) -> tuple[int, int]:
    """Get current display width and height."""
    output = _run(["getdisplaygeometry"], display)
    parts = re.split(r"\s+", output.strip())
    if len(parts) < 2:
        raise RuntimeError(f"Unexpected xdotool getdisplaygeometry output: {output!r}")
    return int(parts[0]), int(parts[1])


def list_visible_windows(display: Optional[str] = None) -> list[dict[str, object]]:
    """Return visible window metadata."""
    windows: list[dict[str, object]] = []
    for wid in search_windows(".", display):
        try:
            windows.append(
                {
                    "id": wid,
                    "name": get_window_name(wid, display),
                    "rect": get_window_geometry(wid, display),
                }
            )
        except RuntimeError:
            continue
    return windows


def focus_window_by_name(name: str, display: Optional[str] = None) -> bool:
    """Find and focus a window by name. Returns True if successful."""
    wid = search_window(name, display)
    if wid is None:
        return False
    ok = False
    for args in (
        ["windowactivate", wid],
        ["windowraise", wid],
        ["windowfocus", wid],
    ):
        try:
            _run(args, display)
            ok = True
        except RuntimeError:
            continue
    return ok


def move_window_by_name(name: str, x: int, y: int,
                        display: Optional[str] = None) -> bool:
    """Find and move a window by name. Returns True if successful."""
    wid = search_window(name, display)
    if wid is None:
        return False
    try:
        window_move(wid, x, y, display)
        return True
    except RuntimeError:
        return False


def move_active_window(x: int, y: int, display: Optional[str] = None) -> bool:
    """Move the currently active window."""
    try:
        wid = get_active_window(display)
        window_move(wid, x, y, display)
        return True
    except RuntimeError:
        return False


def resize_active_window(width: int, height: int,
                         display: Optional[str] = None) -> bool:
    """Resize the currently active window."""
    try:
        wid = get_active_window(display)
        window_size(wid, width, height, display)
        return True
    except RuntimeError:
        return False


def window_move(window_id: str, x: int, y: int,
                display: Optional[str] = None) -> None:
    """Move a window."""
    last_error: Optional[RuntimeError] = None
    for args in (
        ["windowmove", "--sync", window_id, str(x), str(y)],
        ["windowmove", window_id, str(x), str(y)],
    ):
        try:
            _run(args, display, timeout=5.0)
        except RuntimeError as exc:
            last_error = exc
            continue
        if _wait_for_window_geometry(window_id, x=x, y=y, display=display):
            return
    if _wait_for_window_geometry(window_id, x=x, y=y, display=display):
        return
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Failed to move window {window_id} to ({x},{y})")
