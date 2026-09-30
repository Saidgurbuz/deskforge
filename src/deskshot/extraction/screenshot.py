"""Screenshot capture via GDK pixbuf from Xvfb display.

Uses GDK3's pixbuf_get_from_window() — verified working on this server,
no ImageMagick needed.
"""

from __future__ import annotations



import os
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import gi
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GdkPixbuf


if TYPE_CHECKING:  # referenced only from quoted annotations
    import PIL.Image


def capture_screenshot(
    output_path: Optional[Path] = None,
    display: Optional[str] = None,
) -> "PIL.Image.Image":
    """Capture a screenshot from the current (or specified) display.

    Args:
        output_path: If provided, save PNG to this path.
        display: X display string (e.g. ":99"). Defaults to $DISPLAY.

    Returns:
        PIL Image object.
    """
    from PIL import Image
    import io

    if display:
        os.environ["DISPLAY"] = display

    # Open the display
    gdk_display = Gdk.Display.open(os.environ.get("DISPLAY", ":99"))
    if gdk_display is None:
        raise RuntimeError(f"Cannot open display {os.environ.get('DISPLAY')}")

    screen = gdk_display.get_default_screen()
    root = screen.get_root_window()

    width = root.get_width()
    height = root.get_height()

    # Capture pixbuf from root window
    pixbuf = Gdk.pixbuf_get_from_window(root, 0, 0, width, height)
    if pixbuf is None:
        raise RuntimeError("Failed to capture pixbuf from root window")

    # Convert to PIL Image
    success, buf = pixbuf.save_to_bufferv("png", [], [])
    if not success:
        raise RuntimeError("Failed to save pixbuf to buffer")

    image = Image.open(io.BytesIO(buf))

    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(str(output_path), "PNG")

    return image


def get_display_size(display: Optional[str] = None) -> tuple[int, int]:
    """Get the display resolution.

    Returns:
        (width, height) tuple.
    """
    if display:
        os.environ["DISPLAY"] = display

    gdk_display = Gdk.Display.open(os.environ.get("DISPLAY", ":99"))
    if gdk_display is None:
        raise RuntimeError(f"Cannot open display {os.environ.get('DISPLAY')}")

    screen = gdk_display.get_default_screen()
    root = screen.get_root_window()

    return root.get_width(), root.get_height()
