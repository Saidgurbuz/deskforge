"""Depth-aware occlusion clipping for desktop UI elements.

This module infers visible window stacking and clips element boxes to the
visible region. Fully hidden elements are removed.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from deskshot.config import EXTRACTED_BIN, bin_fix_dir
from deskshot.extraction.visibility_fragments import (
    best_fragment,
    get_visible_fragments,
    set_visible_fragments,
)

logger = logging.getLogger(__name__)


WINDOW_LIKE_ROLES = {"frame", "window", "dialog", "desktop frame", "file chooser"}
OVERLAY_BLOCKER_ROLES = {"dialog", "alert", "notification", "tool tip", "popup menu"}
POPUP_OVERLAY_ELEMENT_ROLES = {
    "popup menu",
    "menu",
    "menu item",
    "check menu item",
    "radio menu item",
    "separator",
    "tool tip",
}
INFERRED_OVERLAY_CLUSTER_ROLES = {
    "push button",
    "radio button",
    "check box",
    "check menu item",
    "menu item",
    "separator",
    "toggle button",
}
PRESERVE_FULL_RECT_PARTIAL_ROLES = {
    "frame",
    "window",
    "desktop frame",
    "file chooser",
    "document web",
    "document frame",
    "landmark",
    "list",
    "list box",
    "scroll pane",
    "table",
    "tree",
    "select",
    "directory pane",
    "page tab list",
    "viewport",
    "layered pane",
    "root pane",
    "section",
}
MIN_MEANINGFUL_OVERLAY_THICKNESS = 3


@dataclass
class WindowLayer:
    window_id: str
    name: str
    rect: Dict[str, int]
    stack_index: int  # bottom -> top (higher = closer to front)


def _window_name_is_system(name: str) -> bool:
    normalized = _normalize_name(name)
    return normalized in {"desktop", "top panel", "bottom panel", "xfwm4"}


def _window_name_is_ignored_decorative(name: str) -> bool:
    normalized = _normalize_name(name)
    return normalized in {"deskshot dock backdrop"}


def _element_center_inside(rect: Dict[str, int], container: Dict[str, int]) -> bool:
    cx = rect.get("x", 0) + rect.get("w", 0) / 2
    cy = rect.get("y", 0) + rect.get("h", 0) / 2
    return (
        container.get("x", 0) <= cx <= container.get("x", 0) + container.get("w", 0)
        and container.get("y", 0) <= cy <= container.get("y", 0) + container.get("h", 0)
    )


def _find_xdotool() -> Optional[str]:
    fixed = bin_fix_dir() / "xdotool"
    if fixed.is_file() and os.access(str(fixed), os.X_OK):
        return str(fixed)
    extracted = EXTRACTED_BIN / "xdotool"
    if extracted.is_file() and os.access(extracted, os.X_OK):
        return str(extracted)
    return shutil.which("xdotool")


def _run_xdotool(args: List[str], display: Optional[str] = None) -> subprocess.CompletedProcess:
    xdotool = _find_xdotool()
    if not xdotool:
        raise FileNotFoundError("xdotool not found")
    env = dict(os.environ)
    if display:
        env["DISPLAY"] = display
    return subprocess.run(
        [xdotool] + args,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def _parse_shell_kv(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def get_window_stack(display: Optional[str] = None) -> List[WindowLayer]:
    """Return visible windows in bottom->top stack order using xdotool."""
    try:
        res = _run_xdotool(["search", "--onlyvisible", "--name", "."], display=display)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if res.returncode != 0:
        return []

    ids = [x.strip() for x in res.stdout.splitlines() if x.strip()]
    windows: List[WindowLayer] = []
    for idx, wid in enumerate(ids):
        geom_res = _run_xdotool(["getwindowgeometry", "--shell", wid], display=display)
        if geom_res.returncode != 0:
            continue
        kv = _parse_shell_kv(geom_res.stdout)
        try:
            x = int(kv.get("X", "0"))
            y = int(kv.get("Y", "0"))
            w = int(kv.get("WIDTH", "0"))
            h = int(kv.get("HEIGHT", "0"))
        except ValueError:
            continue
        if w <= 1 or h <= 1:
            continue

        name_res = _run_xdotool(["getwindowname", wid], display=display)
        name = name_res.stdout.strip() if name_res.returncode == 0 else ""
        if _window_name_is_ignored_decorative(name):
            continue
        windows.append(
            WindowLayer(
                window_id=wid,
                name=name,
                rect={"x": x, "y": y, "w": w, "h": h},
                stack_index=idx,
            )
        )
    return windows


def _gtk_frame_extents(window_id: str, display: Optional[str] = None) -> Optional[Tuple[int, int, int, int]]:
    """Read a GTK window's client-side shadow margins as (left, right, top, bottom).

    A GTK3 menu popup is one X window that is larger than the menu drawn in it:
    the extra is an invisible margin holding the drop shadow. `xdotool` reports
    the whole window, so the shadow has to be subtracted before the geometry
    means anything. GTK publishes exactly that margin in `_GTK_FRAME_EXTENTS`,
    which makes the correction exact and independent of the GTK theme.

    Measured on the xarchiver Archive menu: window (619,339,281,302),
    `_GTK_FRAME_EXTENTS` (42,42,34,50), giving the drawn menu at
    (661,373,197,218) - matching the screenshot to the pixel.

    Returns None when the property is absent (non-GTK or shadowless windows).
    """
    try:
        import ctypes

        xlib = ctypes.CDLL("libX11.so.6")
    except Exception:
        return None

    dpy = None
    try:
        xlib.XOpenDisplay.restype = ctypes.c_void_p
        xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        name = (display or os.environ.get("DISPLAY") or "").encode()
        dpy = xlib.XOpenDisplay(name if name else None)
        if not dpy:
            return None
        xlib.XInternAtom.restype = ctypes.c_ulong
        xlib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        atom = xlib.XInternAtom(dpy, b"_GTK_FRAME_EXTENTS", True)
        if not atom:
            return None

        actual_type = ctypes.c_ulong()
        actual_format = ctypes.c_int()
        nitems = ctypes.c_ulong()
        bytes_after = ctypes.c_ulong()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        xlib.XGetWindowProperty.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long,
            ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
        ]
        status = xlib.XGetWindowProperty(
            dpy, ctypes.c_ulong(int(window_id)), atom, 0, 4, False, 0,
            ctypes.byref(actual_type), ctypes.byref(actual_format),
            ctypes.byref(nitems), ctypes.byref(bytes_after), ctypes.byref(data),
        )
        if status != 0 or not data or nitems.value < 4:
            return None
        values = ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))
        extents = tuple(int(values[i]) for i in range(4))
        xlib.XFree(data)
        return extents  # type: ignore[return-value]
    except Exception:
        return None
    finally:
        if dpy:
            try:
                xlib.XCloseDisplay(ctypes.c_void_p(dpy))
            except Exception:
                pass


def window_content_rect(window: WindowLayer, display: Optional[str] = None) -> Dict[str, int]:
    """The rect a window actually draws in, with any CSD shadow removed."""
    extents = _gtk_frame_extents(window.window_id, display=display)
    rect = dict(window.rect)
    if not extents:
        return rect
    left, right, top, bottom = extents
    inner = {
        "x": rect["x"] + left,
        "y": rect["y"] + top,
        "w": rect["w"] - left - right,
        "h": rect["h"] - top - bottom,
    }
    if inner["w"] <= 0 or inner["h"] <= 0:
        return rect
    return inner


#: How closely an accessible window-like element must match an X window before
#: its rect is taken to *be* that window's rect. GTK reports the two identically
#: for a client-side-decorated toplevel - both include the shadow margin - so
#: the match is exact in practice and the threshold only tolerates rounding.
WINDOW_ELEMENT_MATCH_IOU = 0.98


def strip_shadow_margins(
    stack: List[WindowLayer],
    display: Optional[str] = None,
) -> Tuple[List[WindowLayer], List[Tuple[Dict[str, int], Dict[str, int]]]]:
    """Replace each window's rect with the rect it actually draws in.

    A client-side-decorated GTK window is one X window that is larger than what
    it paints: the surplus is an invisible margin holding the drop shadow, and
    `_GTK_FRAME_EXTENTS` states it exactly. Until it is subtracted the window
    rect is wrong in both directions at once - it occludes windows behind a
    margin nothing is drawn in, and it charges its own uncovered ink for pixels
    that are not part of it.

    Measured on this stack: nautilus reports the X window (-45,-3,980,640) with
    extents (45,45,29,61), so it draws in (0,26,890,550) - 39% smaller by area
    than the rect the pipeline used to treat as the window.

    Returns the corrected stack, plus (padded, content) pairs for the windows
    that changed so accessible elements carrying the same padded rect can be
    corrected to match.
    """
    corrected: List[WindowLayer] = []
    corrections: List[Tuple[Dict[str, int], Dict[str, int]]] = []
    for window in stack:
        content = window_content_rect(window, display=display)
        if content != window.rect:
            corrections.append((dict(window.rect), dict(content)))
        corrected.append(
            WindowLayer(
                window_id=window.window_id,
                name=window.name,
                rect=dict(content),
                stack_index=window.stack_index,
            )
        )
    return corrected, corrections


def shrink_window_elements_to_content(
    elements: List[Dict[str, Any]],
    corrections: List[Tuple[Dict[str, int], Dict[str, int]]],
) -> Dict[str, Any]:
    """Shrink accessible window rects that carry a CSD shadow margin.

    GTK gives a toplevel's accessible object the same extents as its X window,
    shadow margin included, so the annotation for the window is larger than the
    window. The children are unaffected - their rects are real widget positions
    - so only the window-like container needs correcting, and only when it
    matches an X window whose margin `_GTK_FRAME_EXTENTS` described.
    """
    meta: Dict[str, Any] = {"num_windows": 0, "by_app": {}}
    if not corrections:
        return meta
    for elem in elements:
        role = str(elem.get("role") or "").strip().lower()
        if role not in WINDOW_LIKE_ROLES:
            continue
        rect = elem.get("rect")
        if not isinstance(rect, dict) or _area(rect) <= 0:
            continue
        for padded, content in corrections:
            if _iou(rect, padded) < WINDOW_ELEMENT_MATCH_IOU:
                continue
            elem["rect"] = dict(content)
            meta["num_windows"] += 1
            app = str(elem.get("app_name") or "unknown")
            meta["by_app"][app] = meta["by_app"].get(app, 0) + 1
            break
    return meta


def find_unmanaged_popup_windows(
    stack: List[WindowLayer],
    elements: List[Dict[str, Any]],
    screen_area: int = 0,
) -> List[WindowLayer]:
    """X windows that no accessible frame accounts for - i.e. toolkit popups."""
    frames = _accessible_frame_rects(elements)
    return [w for w in stack if _is_unmanaged_popup(w, frames, screen_area)]


def _area(rect: Dict[str, int]) -> int:
    return max(0, rect["w"]) * max(0, rect["h"])


def _intersection(a: Dict[str, int], b: Dict[str, int]) -> Optional[Dict[str, int]]:
    x1 = max(a["x"], b["x"])
    y1 = max(a["y"], b["y"])
    x2 = min(a["x"] + a["w"], b["x"] + b["w"])
    y2 = min(a["y"] + a["h"], b["y"] + b["h"])
    if x2 <= x1 or y2 <= y1:
        return None
    return {"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1}


def _iou(a: Dict[str, int], b: Dict[str, int]) -> float:
    inter = _intersection(a, b)
    if inter is None:
        return 0.0
    ia = _area(inter)
    ua = _area(a) + _area(b) - ia
    return (ia / ua) if ua > 0 else 0.0


def _union_rect(rects: List[Dict[str, int]]) -> Optional[Dict[str, int]]:
    if not rects:
        return None
    x1 = min(r["x"] for r in rects)
    y1 = min(r["y"] for r in rects)
    x2 = max(r["x"] + r["w"] for r in rects)
    y2 = max(r["y"] + r["h"] for r in rects)
    if x2 <= x1 or y2 <= y1:
        return None
    return {"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1}


def subtract_rect(rect: Dict[str, int], occ: Dict[str, int]) -> List[Dict[str, int]]:
    """Subtract one occluder rectangle from one rectangle."""
    inter = _intersection(rect, occ)
    if inter is None:
        return [rect]

    out: List[Dict[str, int]] = []
    rx1, ry1 = rect["x"], rect["y"]
    rx2, ry2 = rect["x"] + rect["w"], rect["y"] + rect["h"]
    ix1, iy1 = inter["x"], inter["y"]
    ix2, iy2 = inter["x"] + inter["w"], inter["y"] + inter["h"]

    # top strip
    if iy1 > ry1:
        out.append({"x": rx1, "y": ry1, "w": rect["w"], "h": iy1 - ry1})
    # bottom strip
    if iy2 < ry2:
        out.append({"x": rx1, "y": iy2, "w": rect["w"], "h": ry2 - iy2})
    # left strip
    if ix1 > rx1:
        out.append({"x": rx1, "y": iy1, "w": ix1 - rx1, "h": iy2 - iy1})
    # right strip
    if ix2 < rx2:
        out.append({"x": ix2, "y": iy1, "w": rx2 - ix2, "h": iy2 - iy1})

    return [r for r in out if r["w"] > 0 and r["h"] > 0]


def _normalize_name(name: str) -> str:
    name = name.lower()
    name = re.sub(r"[^a-z0-9 ]+", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def _dedupe_window_stack(stack: List[WindowLayer]) -> List[WindowLayer]:
    """Collapse duplicate xdotool window entries, keeping the topmost copy."""
    deduped: List[WindowLayer] = []
    for window in sorted(stack, key=lambda w: w.stack_index):
        replaced = False
        for i, prev in enumerate(deduped):
            if _normalize_name(prev.name) != _normalize_name(window.name):
                continue
            if _iou(prev.rect, window.rect) < 0.995:
                continue
            deduped[i] = window
            replaced = True
            break
        if not replaced:
            deduped.append(window)

    return [
        WindowLayer(
            window_id=w.window_id,
            name=w.name,
            rect=dict(w.rect),
            stack_index=i,
        )
        for i, w in enumerate(deduped)
    ]


def _plausible_decorated_window_rect(
    base_rect: Dict[str, int],
    owner_rect: Dict[str, int],
) -> bool:
    """Return True when an AT-SPI frame rect looks like WM decorations around a client rect."""
    if _area(base_rect) <= 0 or _area(owner_rect) <= 0:
        return False
    base_x1 = base_rect["x"]
    base_y1 = base_rect["y"]
    base_x2 = base_rect["x"] + base_rect["w"]
    base_y2 = base_rect["y"] + base_rect["h"]
    owner_x1 = owner_rect["x"]
    owner_y1 = owner_rect["y"]
    owner_x2 = owner_rect["x"] + owner_rect["w"]
    owner_y2 = owner_rect["y"] + owner_rect["h"]

    if owner_x1 > base_x1 or owner_y1 > base_y1 or owner_x2 < base_x2 or owner_y2 < base_y2:
        return False

    left = base_x1 - owner_x1
    top = base_y1 - owner_y1
    right = owner_x2 - base_x2
    bottom = owner_y2 - base_y2
    if left < 0 or top < 0 or right < 0 or bottom < 0:
        return False
    if left > 24 or right > 24 or bottom > 24 or top > 96:
        return False

    return (_area(owner_rect) / max(1, _area(base_rect))) <= 1.35


def _plausible_shadow_padded_window_rect(
    stack_rect: Dict[str, int],
    owner_rect: Dict[str, int],
) -> bool:
    """Return True when an xdotool rect looks like a shadow-expanded wrapper."""
    if _area(stack_rect) <= 0 or _area(owner_rect) <= 0:
        return False

    stack_x1 = stack_rect["x"]
    stack_y1 = stack_rect["y"]
    stack_x2 = stack_rect["x"] + stack_rect["w"]
    stack_y2 = stack_rect["y"] + stack_rect["h"]
    owner_x1 = owner_rect["x"]
    owner_y1 = owner_rect["y"]
    owner_x2 = owner_rect["x"] + owner_rect["w"]
    owner_y2 = owner_rect["y"] + owner_rect["h"]

    if owner_x1 < stack_x1 or owner_y1 < stack_y1 or owner_x2 > stack_x2 or owner_y2 > stack_y2:
        return False

    left = owner_x1 - stack_x1
    top = owner_y1 - stack_y1
    right = stack_x2 - owner_x2
    bottom = stack_y2 - owner_y2
    if left < 0 or top < 0 or right < 0 or bottom < 0:
        return False

    if left > 96 or right > 96 or top > 96 or bottom > 128:
        return False

    owner_area = max(1, _area(owner_rect))
    stack_area = max(1, _area(stack_rect))
    if owner_area / stack_area < 0.70:
        return False

    return max(left, top, right, bottom) >= 16


def _plausible_shadow_padded_owner_rect(
    stack_rect: Dict[str, int],
    owner_rect: Dict[str, int],
) -> bool:
    """Return True when an AT-SPI owner rect looks shadow-padded around the stack rect."""
    if _area(stack_rect) <= 0 or _area(owner_rect) <= 0:
        return False

    stack_x1 = stack_rect["x"]
    stack_y1 = stack_rect["y"]
    stack_x2 = stack_rect["x"] + stack_rect["w"]
    stack_y2 = stack_rect["y"] + stack_rect["h"]
    owner_x1 = owner_rect["x"]
    owner_y1 = owner_rect["y"]
    owner_x2 = owner_rect["x"] + owner_rect["w"]
    owner_y2 = owner_rect["y"] + owner_rect["h"]

    if owner_x1 > stack_x1 or owner_y1 > stack_y1 or owner_x2 < stack_x2 or owner_y2 < stack_y2:
        return False

    left = stack_x1 - owner_x1
    top = stack_y1 - owner_y1
    right = owner_x2 - stack_x2
    bottom = owner_y2 - stack_y2
    if left < 0 or top < 0 or right < 0 or bottom < 0:
        return False
    if left > 96 or right > 96 or top > 96 or bottom > 128:
        return False

    stack_area = max(1, _area(stack_rect))
    owner_area = max(1, _area(owner_rect))
    if stack_area / owner_area < 0.70:
        return False
    if max(left, top, right, bottom) < 16:
        return False

    side_max = max(left, right, bottom)
    return side_max >= 12 and top <= side_max + 12


def _expand_window_stack_with_owner_frames(
    stack: List[WindowLayer],
    *,
    owner_to_stack: Dict[int, int],
    by_dom: Dict[int, Dict[str, Any]],
) -> List[WindowLayer]:
    """Use matched AT-SPI top-level frames as effective occluder rects when they include decorations."""
    expanded_rects: Dict[int, Dict[str, int]] = {w.stack_index: dict(w.rect) for w in stack}
    for owner_dom, stack_idx in owner_to_stack.items():
        owner = by_dom.get(owner_dom)
        if owner is None:
            continue
        role = (owner.get("role") or "").strip().lower()
        if role not in WINDOW_LIKE_ROLES:
            continue
        owner_rect = owner.get("rect", {})
        current_rect = expanded_rects.get(stack_idx)
        if current_rect is None:
            continue
        if _plausible_shadow_padded_owner_rect(current_rect, owner_rect):
            continue
        if _plausible_decorated_window_rect(current_rect, owner_rect):
            expanded_rects[stack_idx] = dict(owner_rect)
            continue
        if _plausible_shadow_padded_window_rect(current_rect, owner_rect):
            expanded_rects[stack_idx] = dict(owner_rect)

    return [
        WindowLayer(
            window_id=w.window_id,
            name=w.name,
            rect=dict(expanded_rects.get(w.stack_index, w.rect)),
            stack_index=w.stack_index,
        )
        for w in stack
    ]


def _overlay_union_for_window(
    window: WindowLayer,
    elements: List[Dict[str, Any]],
    *,
    exclude_menu_bar_anchors: bool = False,
) -> Optional[Dict[str, int]]:
    """Union popup-like element rects that visually live inside one small window."""
    by_dom = {
        int(elem["_dom_index"]): elem
        for elem in elements
        if "_dom_index" in elem
    }
    rects: List[Dict[str, int]] = []
    for elem in elements:
        role = (elem.get("role") or "").strip().lower()
        if role not in POPUP_OVERLAY_ELEMENT_ROLES:
            continue
        parent_dom = elem.get("_parent_dom_index")
        parent_role = (
            (by_dom.get(parent_dom, {}).get("role") or "").strip().lower()
            if isinstance(parent_dom, int)
            else ""
        )
        if exclude_menu_bar_anchors and role == "menu" and parent_role == "menu bar":
            continue
        rect = elem.get("rect", {})
        if _area(rect) <= 0:
            continue
        inter = _intersection(rect, window.rect)
        if inter is None:
            continue
        rect_area = max(1, _area(rect))
        inter_area = _area(inter)
        if inter_area / rect_area < 0.80:
            continue
        if not _element_center_inside(rect, window.rect):
            continue
        rects.append(rect)
    return _union_rect(rects)


#: A window covering more than this share of the screen is a root, desktop or
#: maximised app window, never a popup.
MAX_POPUP_SCREEN_SHARE = 0.6

#: Overlap with an accessible frame above which an X window is taken to *be*
#: that frame. Loose enough to absorb the offset between a window and its
#: window-manager decoration, which are listed as two separate X windows.
APP_FRAME_MATCH_IOU = 0.5

#: How closely an X window must match an accessible frame to *be* that frame.
#:
#: Deliberately much tighter than APP_FRAME_MATCH_IOU. That threshold asks the
#: loose question "might this window be an app window" and errs toward yes,
#: which is the safe direction when deciding not to promote. Identity is the
#: opposite question and needs the strict answer: a menu popup covering most of
#: its own owner scores 0.57 against the owner's frame, which is plainly not the
#: same window, while a real managed window matches its frame at 1.00 - the
#: stack rect and the accessible frame rect agree to the pixel once
#: _GTK_FRAME_EXTENTS has been applied.
MANAGED_WINDOW_MATCH_IOU = 0.85


def _window_is_managed(window: "WindowLayer", frames: List[Dict[str, int]]) -> bool:
    """True when this X window is one the accessibility tree exposes as a frame.

    Managed windows are stacked by the window manager and X reports their order
    correctly, so nothing about them needs repairing. Only override-redirect
    popups - which never appear as frames - are listed out of order.
    """
    return any(_iou(frame, window.rect) >= MANAGED_WINDOW_MATCH_IOU for frame in frames)


#: The role a toolkit gives a *managed* toplevel. GTK publishes an ordinary
#: application window as `frame` and an override-redirect popup - a menu, a
#: combo list - as `window`, so the role is the toolkit stating which of the two
#: this is. Counting `window` as a frame told the promotion guard that every
#: menu was a managed window, at IOU 1.00 against its own accessible node, and
#: silently disabled popup promotion for every GTK app that publishes its menus.
#: Measured across 104 captures: 4 accessible `window` toplevels, all of them
#: nameless popups (bluefish menus, a nautilus context menu); every real
#: application window - 20 apps - is a `frame`.
MANAGED_WINDOW_ROLES = {"frame"}


def _accessible_frame_rects(elements: List[Dict[str, Any]]) -> List[Dict[str, int]]:
    out: List[Dict[str, int]] = []
    for elem in elements:
        role = str(elem.get("role") or "").strip().lower()
        if role not in MANAGED_WINDOW_ROLES:
            continue
        rect = elem.get("rect")
        if isinstance(rect, dict) and _area(rect) > 0:
            out.append(rect)
    return out


def _is_unmanaged_popup(
    window: WindowLayer,
    frames: List[Dict[str, int]],
    screen_area: int,
) -> bool:
    """True when an X window exists that the accessibility tree does not know.

    The element-based test above asks "do popup elements cover this window",
    which fails whenever the popup's own contents are not accessible. Measured
    on qalculate-gtk: it exposes *zero* menu items, so its open menu was never
    recognised as a popup, was left low in the stack, and five of its own
    buttons underneath were annotated as fully visible with no ink behind them.

    This test does not depend on the popup's contents at all. Every ordinary
    window appears in the accessibility tree as a frame; an override-redirect
    popup does not. A visible X window matching no frame, and too small to be a
    root or desktop window, is therefore a popup - and popups are always on top.
    """
    if _window_name_is_system(window.name):
        return False
    if screen_area and _area(window.rect) / screen_area > MAX_POPUP_SCREEN_SHARE:
        return False
    return not any(
        _iou(frame, window.rect) >= APP_FRAME_MATCH_IOU for frame in frames
    )


def _promote_floating_overlay_windows(
    stack: List[WindowLayer],
    elements: List[Dict[str, Any]],
) -> Tuple[List[WindowLayer], List[str]]:
    """Promote small popup/dialog windows that xdotool lists out of order.

    In practice, toolkit popup menus often appear as separate transient X
    windows. `xdotool search --onlyvisible --name .` does not reliably return
    strict z-order, so those popup windows can be listed below overlapping app
    windows. Detect popup-like small windows by overlay-element coverage and
    move them above normal app windows while keeping panels topmost.
    """
    if len(stack) < 2:
        return stack, []

    app_windows = [w for w in stack if not _window_name_is_system(w.name)]
    if len(app_windows) < 2:
        return stack, []

    frames = _accessible_frame_rects(elements)
    screen = _union_rect([w.rect for w in stack]) or {"x": 0, "y": 0, "w": 0, "h": 0}
    screen_area = _area(screen)

    promoted_ids: List[str] = []
    for window in app_windows:
        area = max(1, _area(window.rect))
        best_larger_area = 0
        larger_overlap = False
        best_larger_cover_ratio = 0.0
        for other in app_windows:
            if other.window_id == window.window_id:
                continue
            if _area(other.rect) <= area:
                continue
            inter = _intersection(window.rect, other.rect)
            if inter is None:
                continue
            if _area(inter) / area >= 0.20:
                larger_overlap = True
                best_larger_area = max(best_larger_area, _area(other.rect))
                best_larger_cover_ratio = max(best_larger_cover_ratio, _area(inter) / area)
        if not larger_overlap:
            continue

        # Only ever reorder windows the accessibility tree does not know about.
        #
        # Promotion exists for override-redirect popups, which X lists out of
        # order and which never appear as frames. A window that *does* match a
        # frame is an ordinary managed window, and X's stacking order for it is
        # authoritative - there is nothing to repair.
        #
        # Without this guard the overlay-coverage heuristic promotes real
        # windows: a Chromium window whose page content happens to be 35%
        # covered by menu-like roles was lifted above the three windows drawn on
        # top of it, and everything under its 834x397 rect was then dropped as
        # occluded. One scene lost 165 of HomeBank's 167 elements that way,
        # while Chromium's own hidden elements were annotated as fully visible -
        # a false negative and a false positive from the same mistake.
        if _window_is_managed(window, frames):
            continue

        overlay_union = _overlay_union_for_window(window, elements)
        if overlay_union is None:
            # No accessible contents to judge by. Fall back to the structural
            # test: a visible window the accessibility tree has no frame for is
            # a popup regardless of what is inside it.
            if _is_unmanaged_popup(window, frames, screen_area):
                promoted_ids.append(window.window_id)
            continue
        overlay_cover_ratio = _area(overlay_union) / area
        if overlay_cover_ratio < 0.35:
            continue

        # Popups are usually much smaller than the owner/app window, but tall
        # toolkit menu windows can still be valid overlays while taking a large
        # fraction of the owner area. Allow those only when the window is both
        # mostly covered by popup/menu elements and mostly contained in the
        # larger overlapping frame.
        if best_larger_area and (area / best_larger_area) > 0.45:
            if not (overlay_cover_ratio >= 0.55 and best_larger_cover_ratio >= 0.75):
                continue
        promoted_ids.append(window.window_id)

    if not promoted_ids:
        return stack, []

    promoted = [w for w in stack if w.window_id in promoted_ids]
    normal = [w for w in stack if w.window_id not in promoted_ids and not _window_name_is_system(w.name)]
    system = [w for w in stack if w.window_id not in promoted_ids and _window_name_is_system(w.name)]

    desktop = [w for w in system if _normalize_name(w.name) == "desktop"]
    panels = [w for w in system if _normalize_name(w.name) in {"top panel", "bottom panel"}]
    misc_system = [w for w in system if w not in desktop and w not in panels]

    reordered = misc_system + desktop + normal + promoted + panels
    return [
        WindowLayer(
            window_id=w.window_id,
            name=w.name,
            rect=dict(w.rect),
            stack_index=i,
        )
        for i, w in enumerate(reordered)
    ], promoted_ids


def _owner_match_score(owner_rect: Dict[str, int], owner_name: str, window: WindowLayer) -> float:
    score = _iou(owner_rect, window.rect) * 2.0

    cx = owner_rect["x"] + owner_rect["w"] / 2
    cy = owner_rect["y"] + owner_rect["h"] / 2
    if (
        window.rect["x"] <= cx <= window.rect["x"] + window.rect["w"]
        and window.rect["y"] <= cy <= window.rect["y"] + window.rect["h"]
    ):
        score += 0.5

    on = _normalize_name(owner_name)
    wn = _normalize_name(window.name)
    if on and wn and (on in wn or wn in on):
        score += 0.8

    return score


def _resolve_owner_for_element(
    elem: Dict[str, Any],
    by_dom: Dict[int, Dict[str, Any]],
) -> Optional[int]:
    cur = elem.get("_dom_index")
    owner_dom: Optional[int] = None
    while cur is not None and cur in by_dom:
        node = by_dom[cur]
        role = (node.get("role") or "").strip().lower()
        if role in WINDOW_LIKE_ROLES:
            owner_dom = cur
        cur = node.get("_parent_dom_index")
    return owner_dom


def _find_containing_stack_hint(
    elem: Dict[str, Any],
    elements: List[Dict[str, Any]],
) -> tuple[Optional[int], Optional[int]]:
    """Infer a better window stack from a containing popup/container element.

    Some toolkit alerts/popovers expose visible text nodes that are not nested
    under the popup's actual window-like ancestor. Those descendants should
    inherit the frontmost popup/dialog stack instead of falling back to a broad
    background window or the desktop.
    """
    rect = elem.get("rect", {})
    if _area(rect) <= 0:
        return None, None

    best_stack: Optional[int] = None
    best_owner: Optional[int] = None
    best_area: Optional[int] = None
    elem_app = elem.get("app_name")
    elem_source = elem.get("source")
    if not elem_app:
        return None, None

    for candidate in elements:
        if candidate is elem:
            continue
        cand_stack = candidate.get("_window_stack_index")
        if not isinstance(cand_stack, int):
            continue
        if candidate.get("app_name") != elem_app:
            continue
        if elem_source and candidate.get("source") != elem_source:
            continue

        cand_role = (candidate.get("role") or "").strip().lower()
        if cand_role not in WINDOW_LIKE_ROLES and cand_role not in OVERLAY_BLOCKER_ROLES:
            continue

        cand_rect = candidate.get("rect", {})
        cand_area = _area(cand_rect)
        if cand_area <= _area(rect):
            continue
        inter = _intersection(rect, cand_rect)
        if inter is None:
            continue
        if (_area(inter) / max(1, _area(rect))) < 0.98:
            continue
        if not _element_center_inside(rect, cand_rect):
            continue

        owner_hint = candidate.get("_window_owner_dom_index")
        if owner_hint is None and cand_role in WINDOW_LIKE_ROLES:
            owner_hint = int(candidate["_dom_index"])

        if (
            best_area is None
            or cand_area < best_area
            or (cand_area == best_area and best_stack is not None and cand_stack > best_stack)
        ):
            best_area = cand_area
            best_stack = cand_stack
            best_owner = owner_hint

    return best_stack, best_owner


def _build_stack_occluder_rects(
    stack: List[WindowLayer],
    elements: List[Dict[str, Any]],
    by_dom: Dict[int, Dict[str, Any]],
    *,
    promoted_popup_indices: Optional[set[int]] = None,
) -> Dict[int, List[Dict[str, int]]]:
    """Collect effective occluder rects for each real window stack layer.

    xdotool geometry can miss toolkit-drawn popup chrome bands. When we already
    matched a top-level alert/dialog/popup element onto that same stack, its
    rect is a better occluder for underlying windows than the bare X rect.
    """
    occluders: Dict[int, List[Dict[str, int]]] = {}
    for w in stack:
        base_rect = dict(w.rect)
        normalized_name = _normalize_name(w.name)
        if normalized_name == "plank":
            dock_rects = []
            for elem in elements:
                if elem.get("_window_stack_index") != w.stack_index:
                    continue
                app_name = (elem.get("app_name") or "").strip().lower()
                if "plank" not in app_name:
                    continue
                role = (elem.get("role") or "").strip().lower()
                if role in WINDOW_LIKE_ROLES:
                    continue
                rect = elem.get("rect", {})
                if _area(rect) <= 0:
                    continue
                dock_rects.append(dict(rect))
            dock_union = _union_rect(dock_rects)
            if dock_union is not None and _area(dock_union) > 0:
                base_rect = dock_union
        if promoted_popup_indices and w.stack_index in promoted_popup_indices:
            overlay_union = _overlay_union_for_window(
                w,
                elements,
                exclude_menu_bar_anchors=True,
            )
            if overlay_union is not None and _area(overlay_union) > 0:
                base_rect = dict(overlay_union)
        occluders[w.stack_index] = [base_rect]

    for elem in elements:
        stack_idx = elem.get("_window_stack_index")
        if not isinstance(stack_idx, int):
            continue

        role = (elem.get("role") or "").strip().lower()
        if role not in WINDOW_LIKE_ROLES and role not in OVERLAY_BLOCKER_ROLES:
            continue

        rect = elem.get("rect", {})
        if _area(rect) <= 0:
            continue

        parent_dom = elem.get("_parent_dom_index")
        if isinstance(parent_dom, int):
            parent = by_dom.get(parent_dom)
            if parent is not None:
                parent_role = (parent.get("role") or "").strip().lower()
                parent_stack = parent.get("_window_stack_index")
                if (
                    parent_stack == stack_idx
                    and (parent_role in WINDOW_LIKE_ROLES or parent_role in OVERLAY_BLOCKER_ROLES)
                ):
                    # Keep only the outermost real overlay/container rect for
                    # this stack; nested labels/buttons are not occluders.
                    continue

        bucket = occluders.setdefault(stack_idx, [])
        if any(_iou(rect, existing) >= 0.995 for existing in bucket):
            continue
        bucket.append(dict(rect))

    return occluders


def _should_preserve_full_rect_on_partial_occlusion(
    elem: Dict[str, Any],
    owner_dom: Optional[int],
    by_dom: Dict[int, Dict[str, Any]],
) -> bool:
    """Keep full bbox for broad structural containers under partial overlap."""
    role = (elem.get("role") or "").strip().lower()
    attrs = elem.get("attrs") or {}
    states = attrs.get("states") or {}
    interfaces = attrs.get("interfaces") or {}
    if (
        role in {"text", "entry", "editbar", "edit bar", "password text"}
        and (states.get("editable") or interfaces.get("editable_text"))
    ):
        return True
    if role not in PRESERVE_FULL_RECT_PARTIAL_ROLES:
        return False

    children = elem.get("_children_dom_indices") or elem.get("children_indices") or []
    if not children:
        return False

    # Alert/dialog popups should still clip to the visible region.
    if role in {"dialog"}:
        return False

    if role in {
        "document web",
        "document frame",
        "landmark",
        "section",
        "list",
        "list box",
        "scroll pane",
        "table",
        "tree",
        "select",
        "directory pane",
        "page tab list",
        "viewport",
        "layered pane",
        "root pane",
    }:
        return True

    elem_rect = elem.get("rect", {})
    elem_area = max(1, _area(elem_rect))

    if owner_dom is None or owner_dom not in by_dom:
        return role == "desktop frame"

    owner_elem = by_dom[owner_dom]
    owner_rect = owner_elem.get("rect", {})
    owner_area = max(1, _area(owner_rect))

    # Preserve top-level window frames and owner-sized content containers.
    if owner_dom == elem.get("_dom_index"):
        return role in {"frame", "window", "desktop frame"}

    return (elem_area / owner_area) >= 0.75


def _reindex_elements(elements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    old_parent_lookup = {
        int(e["_dom_index"]): e.get("_parent_dom_index")
        for e in elements
        if "_dom_index" in e
    }
    old_to_new = {int(e["_dom_index"]): i for i, e in enumerate(elements)}

    for i, elem in enumerate(elements):
        old_parent = elem.get("_parent_dom_index")
        while old_parent is not None and old_parent not in old_to_new:
            old_parent = old_parent_lookup.get(old_parent)
        new_parent = old_to_new.get(old_parent) if old_parent is not None else None

        elem["_dom_index"] = i
        elem["_parent_dom_index"] = new_parent
        elem["parent_index"] = new_parent
        elem["_children_dom_indices"] = []
        elem["children_indices"] = []

    for child in elements:
        p = child.get("_parent_dom_index")
        if p is not None and 0 <= p < len(elements):
            elements[p]["_children_dom_indices"].append(child["_dom_index"])

    for elem in elements:
        elem["children_indices"] = list(elem["_children_dom_indices"])

    return elements


def _looks_like_overlay_blocker(elem: Dict[str, Any]) -> bool:
    role = (elem.get("role") or "").strip().lower()
    if role in OVERLAY_BLOCKER_ROLES:
        return True
    return False


DOCUMENT_CONTENT_ROLES = {"document web", "document frame"}


def _is_inside_web_document(
    elem: Dict[str, Any],
    by_dom: Dict[int, Dict[str, Any]],
) -> bool:
    """True when an element is page content rather than browser chrome."""
    parent_dom = elem.get("_parent_dom_index")
    seen: set[int] = set()
    while isinstance(parent_dom, int) and parent_dom in by_dom and parent_dom not in seen:
        seen.add(parent_dom)
        role = (by_dom[parent_dom].get("role") or "").strip().lower()
        if role in DOCUMENT_CONTENT_ROLES:
            return True
        parent_dom = by_dom[parent_dom].get("_parent_dom_index")
    return False


def _is_small_floating_list_overlay(
    elem: Dict[str, Any],
    *,
    owner_dom: Optional[int],
    by_dom: Dict[int, Dict[str, Any]],
) -> bool:
    """Return True for popup-like list overlays, not normal page/content lists.

    Chromium page content and many rich apps expose ordinary content regions as
    `list`, so treating every large list as a popup blocker is too aggressive.
    Only keep list blockers when they are small relative to the owner window
    and look like floating popovers rather than main content panes.

    Page content is excluded outright. Intra-window occlusion here is inferred
    from DOM order, which is a fair model of how a toolkit stacks widgets but
    not of how a browser paints a document: a page positions and layers content
    with CSS, and its `list` nodes are `<ul>`s, not popovers. The browser's own
    popups - omnibox suggestions, autofill - live in the chrome, outside the
    document, so nothing this heuristic is for is lost.

    Measured on a LinkedIn capture: an ordinary `<ul>` of topic pills (627x300
    inside a 1076x933 window) passed every size test, and blanking what lay
    under it dropped four visible nav links (LinkedIn, Top Content, People,
    Learning) and clipped the Back button, tab strip and toolbar to slivers at
    its right edge.
    """
    role = (elem.get("role") or "").strip().lower()
    rect = elem.get("rect", {})
    area = _area(rect)
    if role != "list" or area < 20_000:
        return False
    if owner_dom is None or owner_dom not in by_dom:
        return False
    if _is_inside_web_document(elem, by_dom):
        return False

    owner_rect = by_dom[owner_dom].get("rect", {})
    owner_area = max(1, _area(owner_rect))
    width = max(1, rect.get("w", 0))
    height = max(1, rect.get("h", 0))
    owner_width = max(1, owner_rect.get("w", 0))
    owner_height = max(1, owner_rect.get("h", 0))

    if (area / owner_area) > 0.35:
        return False
    if (width / owner_width) > 0.65:
        return False
    if (height / owner_height) > 0.65:
        return False

    return True


def _group_owner_elements(
    elements: List[Dict[str, Any]],
    by_dom: Dict[int, Dict[str, Any]],
) -> Dict[int, List[Dict[str, Any]]]:
    grouped: Dict[int, List[Dict[str, Any]]] = {}
    for elem in elements:
        owner_dom = elem.get("_window_owner_dom_index")
        if owner_dom is None:
            owner_dom = _resolve_owner_for_element(elem, by_dom)
        if owner_dom is None:
            continue
        grouped.setdefault(owner_dom, []).append(elem)
    return grouped


def _collect_subtree_dom_indices(
    root_dom: int,
    by_dom: Dict[int, Dict[str, Any]],
) -> List[int]:
    out: List[int] = []
    stack = [root_dom]
    seen: set[int] = set()
    while stack:
        dom = stack.pop()
        if dom in seen or dom not in by_dom:
            continue
        seen.add(dom)
        out.append(dom)
        elem = by_dom[dom]
        children = elem.get("_children_dom_indices") or elem.get("children_indices") or []
        stack.extend(int(child) for child in children if child is not None)
    return out


def _children_by_parent(elements: List[Dict[str, Any]]) -> Dict[int, List[Dict[str, Any]]]:
    children: Dict[int, List[Dict[str, Any]]] = {}
    for elem in elements:
        parent = elem.get("_parent_dom_index")
        if isinstance(parent, int):
            children.setdefault(parent, []).append(elem)
    return children


def _collect_subtree_elements_by_parent_links(
    root_dom: int,
    children_by_parent: Dict[int, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    stack = list(children_by_parent.get(root_dom, []))
    seen_objects: set[int] = set()
    while stack:
        elem = stack.pop()
        object_id = id(elem)
        if object_id in seen_objects:
            continue
        seen_objects.add(object_id)
        out.append(elem)
        if "_dom_index" in elem:
            stack.extend(children_by_parent.get(int(elem["_dom_index"]), []))
    return out


def _find_popup_menu_tree_blockers(
    elements: List[Dict[str, Any]],
    by_dom: Dict[int, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    blockers: List[Dict[str, Any]] = []
    children_by_parent = _children_by_parent(elements)

    for elem in elements:
        role = (elem.get("role") or "").strip().lower()
        if role not in {"menu", "popup menu"}:
            continue

        root_dom = int(elem["_dom_index"])
        owner_dom = elem.get("_window_owner_dom_index")
        if owner_dom is None:
            owner_dom = _resolve_owner_for_element(elem, by_dom)
        if owner_dom is None or owner_dom not in by_dom:
            continue

        member_elems = [elem] + _collect_subtree_elements_by_parent_links(root_dom, children_by_parent)
        popup_member_doms: List[int] = []
        popup_rects = [
            m.get("rect", {})
            for m in member_elems
            if (m.get("role") or "").strip().lower() in POPUP_OVERLAY_ELEMENT_ROLES
            and _area(m.get("rect", {})) > 0
            and not (int(m.get("_dom_index", -1)) == root_dom and role == "menu")
        ]
        popup_member_doms = [
            int(m["_dom_index"])
            for m in member_elems
            if (m.get("role") or "").strip().lower() in POPUP_OVERLAY_ELEMENT_ROLES
            and _area(m.get("rect", {})) > 0
            and not (int(m.get("_dom_index", -1)) == root_dom and role == "menu")
        ]
        popup_union = _union_rect(popup_rects)
        if popup_union is None:
            continue

        root_rect = elem.get("rect", {})
        if popup_union == root_rect:
            continue

        expands_beyond_root = (
            popup_union["y"] + popup_union["h"] > root_rect.get("y", 0) + root_rect.get("h", 0) + 8
            or popup_union["x"] < root_rect.get("x", 0) - 8
            or popup_union["x"] + popup_union["w"] > root_rect.get("x", 0) + root_rect.get("w", 0) + 8
        )
        vertical_popup = (
            len(popup_rects) >= 2
            and popup_union.get("y", 0) >= root_rect.get("y", 0) + root_rect.get("h", 0) - 2
        )
        if not expands_beyond_root and not vertical_popup:
            continue

        owner_rect = by_dom[owner_dom].get("rect", {})
        owner_area = max(1, _area(owner_rect))
        if (_area(popup_union) / owner_area) > 0.65:
            continue

        blockers.append(
            {
                "dom_index": max(popup_member_doms),
                "owner_dom": owner_dom,
                "rect": popup_union,
                "member_doms": popup_member_doms,
                "dom_order_sensitive": False,
            }
        )

    return blockers


def _can_join_inferred_overlay_cluster(
    cluster: List[Dict[str, Any]],
    elem: Dict[str, Any],
) -> bool:
    if not cluster:
        return True
    last = cluster[-1]
    if int(elem["_dom_index"]) - int(last["_dom_index"]) > 3:
        return False
    if elem.get("_parent_dom_index") != last.get("_parent_dom_index"):
        return False
    last_rect = last.get("rect", {})
    rect = elem.get("rect", {})
    last_w = max(1, last_rect.get("w", 0))
    rect_w = max(1, rect.get("w", 0))
    width_ratio = min(last_w, rect_w) / max(last_w, rect_w)
    if width_ratio < 0.72:
        return False
    if abs(last_rect.get("x", 0) - rect.get("x", 0)) > 24:
        return False
    union = _union_rect([c.get("rect", {}) for c in cluster])
    if union is None:
        return False
    pad = 18
    padded_union = {
        "x": union["x"] - pad,
        "y": union["y"] - pad,
        "w": union["w"] + pad * 2,
        "h": union["h"] + pad * 2,
    }
    return _intersection(padded_union, rect) is not None


def _is_valid_inferred_overlay_cluster(
    cluster: List[Dict[str, Any]],
    owner_rect: Dict[str, int],
    owner_elems: List[Dict[str, Any]],
) -> bool:
    if len(cluster) < 4:
        return False
    union = _union_rect([c.get("rect", {}) for c in cluster])
    if union is None:
        return False

    union_area = _area(union)
    owner_area = max(1, _area(owner_rect))
    if union_area < 10_000 or (union_area / owner_area) > 0.45:
        return False
    if union["w"] >= int(owner_rect["w"] * 0.8):
        return False

    cluster_start_dom = min(int(c["_dom_index"]) for c in cluster)
    overlap_count = 0
    for elem in owner_elems:
        elem_dom = int(elem["_dom_index"])
        if elem_dom >= cluster_start_dom:
            continue
        if elem.get("_parent_dom_index") == cluster[0].get("_parent_dom_index"):
            inter = _intersection(elem.get("rect", {}), union)
            if inter is not None and _area(inter) >= 20:
                overlap_count += 1
                if overlap_count >= 1:
                    return True
    return False


def _find_inferred_overlay_blockers(
    elements: List[Dict[str, Any]],
    by_dom: Dict[int, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    blockers: List[Dict[str, Any]] = []
    by_owner = _group_owner_elements(elements, by_dom)

    for owner_dom, owner_elems in by_owner.items():
        owner_elem = by_dom.get(owner_dom)
        if owner_elem is None:
            continue
        owner_rect = owner_elem.get("rect", {})
        candidates = [
            elem
            for elem in sorted(owner_elems, key=lambda e: int(e["_dom_index"]))
            if (elem.get("role") or "").strip().lower() in INFERRED_OVERLAY_CLUSTER_ROLES
            and _area(elem.get("rect", {})) > 0
        ]
        if not candidates:
            continue

        cluster: List[Dict[str, Any]] = []
        for elem in candidates:
            if _can_join_inferred_overlay_cluster(cluster, elem):
                cluster.append(elem)
                continue

            if _is_valid_inferred_overlay_cluster(cluster, owner_rect, owner_elems):
                blockers.append(
                    {
                        "dom_index": max(int(c["_dom_index"]) for c in cluster),
                        "owner_dom": owner_dom,
                        "rect": _union_rect([c.get("rect", {}) for c in cluster]),
                        "member_doms": [int(c["_dom_index"]) for c in cluster],
                        "dom_order_sensitive": not _inferred_cluster_is_menu_popup_like(cluster, by_dom),
                    }
                )
            cluster = [elem]

        if _is_valid_inferred_overlay_cluster(cluster, owner_rect, owner_elems):
            blockers.append(
                {
                    "dom_index": max(int(c["_dom_index"]) for c in cluster),
                    "owner_dom": owner_dom,
                    "rect": _union_rect([c.get("rect", {}) for c in cluster]),
                    "member_doms": [int(c["_dom_index"]) for c in cluster],
                    "dom_order_sensitive": not _inferred_cluster_is_menu_popup_like(cluster, by_dom),
                }
            )

    return [b for b in blockers if b.get("rect") is not None]


def _inferred_cluster_is_menu_popup_like(
    cluster: List[Dict[str, Any]],
    by_dom: Dict[int, Dict[str, Any]],
) -> bool:
    if not cluster:
        return False

    menu_roles = {"menu item", "check menu item", "radio menu item", "separator"}
    cluster_roles = {(elem.get("role") or "").strip().lower() for elem in cluster}
    if cluster_roles and cluster_roles.issubset(menu_roles):
        return True

    parents = {
        elem.get("_parent_dom_index")
        for elem in cluster
        if elem.get("_parent_dom_index") is not None
    }
    for parent_dom in parents:
        parent = by_dom.get(int(parent_dom)) if parent_dom is not None else None
        parent_role = (parent.get("role") or "").strip().lower() if parent else ""
        if parent_role in {"menu", "popup menu"}:
            return True

    return False


def _is_ancestor_dom(
    maybe_ancestor: int,
    node_dom: int,
    by_dom: Dict[int, Dict[str, Any]],
) -> bool:
    cur = by_dom.get(node_dom, {}).get("_parent_dom_index")
    seen: set[int] = set()
    while cur is not None and cur not in seen:
        if cur == maybe_ancestor:
            return True
        seen.add(cur)
        cur = by_dom.get(cur, {}).get("_parent_dom_index")
    return False


def _apply_same_window_overlay_occlusion(
    elements: List[Dict[str, Any]],
    *,
    by_dom: Dict[int, Dict[str, Any]],
    min_visible_size: int,
    min_visible_ratio: float,
    allow_partial_clip: bool,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Hide/clamp elements covered by later overlay-like subtrees in one window.

    Some apps render notifications, dialogs, and popovers inside the same
    top-level window, so window-stack occlusion alone is insufficient.
    Approximate intra-window z-order by DOM order: later overlay nodes can
    cover earlier siblings from the same owner window.
    """
    blockers = []
    for elem in elements:
        owner_dom = elem.get("_window_owner_dom_index")
        if owner_dom is None:
            owner_dom = _resolve_owner_for_element(elem, by_dom)
        if not _looks_like_overlay_blocker(elem) and not _is_small_floating_list_overlay(
            elem,
            owner_dom=owner_dom,
            by_dom=by_dom,
        ):
            continue
        blockers.append(
            {
                "dom_index": int(elem["_dom_index"]),
                "owner_dom": owner_dom,
                "rect": elem.get("rect", {}),
                "member_doms": [int(elem["_dom_index"])],
                "dom_order_sensitive": False,
            }
        )
    blockers.extend(_find_popup_menu_tree_blockers(elements, by_dom))
    blockers.extend(_find_inferred_overlay_blockers(elements, by_dom))
    if not blockers:
        return elements, {"num_clipped": 0, "num_dropped": 0}

    clipped = 0
    dropped = 0
    out: List[Dict[str, Any]] = []
    by_dom_local = {int(e["_dom_index"]): e for e in elements if "_dom_index" in e}

    for elem in elements:
        rect = dict(elem.get("rect", {}))
        if rect.get("w", 0) < min_visible_size or rect.get("h", 0) < min_visible_size:
            set_visible_fragments(elem, [], min_visible_size=min_visible_size)
            dropped += 1
            continue

        elem_dom = int(elem["_dom_index"])
        owner_dom = elem.get("_window_owner_dom_index")
        if owner_dom is None:
            owner_dom = _resolve_owner_for_element(elem, by_dom_local)

        remaining = get_visible_fragments(elem)
        if not remaining:
            remaining = [rect]
        for blocker in blockers:
            blocker_dom = int(blocker["dom_index"])
            dom_order_sensitive = blocker.get("dom_order_sensitive", True)
            if dom_order_sensitive and blocker_dom <= elem_dom:
                continue
            if elem_dom in blocker.get("member_doms", []):
                continue
            if blocker_dom in by_dom_local and _is_ancestor_dom(blocker_dom, elem_dom, by_dom_local):
                continue
            if blocker_dom in by_dom_local and _is_ancestor_dom(elem_dom, blocker_dom, by_dom_local):
                continue
            blocker_owner = blocker.get("owner_dom")
            if blocker_owner != owner_dom:
                continue

            occ = blocker.get("rect", {})
            next_remaining: List[Dict[str, int]] = []
            for r in remaining:
                inter = _intersection(r, occ)
                if inter is None:
                    next_remaining.append(r)
                    continue
                # Ignore border-grazing contacts between adjacent layout regions.
                # These are common in browser/document trees and should not count
                # as meaningful occlusion.
                if (
                    inter["w"] < MIN_MEANINGFUL_OVERLAY_THICKNESS
                    or inter["h"] < MIN_MEANINGFUL_OVERLAY_THICKNESS
                ):
                    next_remaining.append(r)
                    continue
                next_remaining.extend(subtract_rect(r, occ))
            remaining = [
                r for r in next_remaining
                if r["w"] >= min_visible_size and r["h"] >= min_visible_size
            ]
            if not remaining:
                break

        remaining = set_visible_fragments(elem, remaining, min_visible_size=min_visible_size)
        original_rect = elem.get("_visibility_source_rect") or rect
        original_area = max(1, _area(original_rect))
        elem["_is_occluded_by_overlap"] = not (
            len(remaining) == 1 and remaining[0] == original_rect
        )
        elem["is_occluded"] = bool(elem["_is_occluded_by_overlap"])
        visible_area = sum(_area(r) for r in remaining)
        visible_ratio = visible_area / original_area
        if not remaining or visible_ratio <= min_visible_ratio:
            set_visible_fragments(elem, [], min_visible_size=min_visible_size)
            elem["_is_occluded_by_overlap"] = True
            elem["is_occluded"] = True
            dropped += 1
            continue

        best_visible = best_fragment(remaining)
        assert best_visible is not None
        if best_visible != rect:
            if allow_partial_clip:
                if _should_preserve_full_rect_on_partial_occlusion(elem, owner_dom, by_dom):
                    elem["_occlusion_partially_covered"] = True
                    elem["_occlusion_preserved_full_rect"] = True
                else:
                    elem["_occlusion_clipped"] = True
                    elem["_overlay_occlusion_clipped"] = True
                    elem["_occlusion_original_rect"] = rect
                    elem["rect"] = best_visible
                    clipped += 1
            else:
                elem["_occlusion_partially_covered"] = True

        out.append(elem)

    return out, {"num_clipped": clipped, "num_dropped": dropped}


def apply_occlusion_clipping(
    elements: List[Dict[str, Any]],
    *,
    display: Optional[str] = None,
    window_stack: Optional[List[WindowLayer]] = None,
    min_visible_size: int = 2,
    min_visible_ratio: float = 0.0,
    allow_partial_clip: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Clip element boxes to visible regions using window depth ordering."""
    stack = window_stack if window_stack is not None else get_window_stack(display=display)
    stack = _dedupe_window_stack(stack)
    if not stack:
        return elements, {
            "enabled": False,
            "reason": "no_window_stack",
            "num_elements_in": len(elements),
            "num_elements_out": len(elements),
            "num_clipped": 0,
            "num_dropped": 0,
            "window_stack": [],
        }

    by_dom = {int(e["_dom_index"]): e for e in elements if "_dom_index" in e}
    stack, promoted_popup_window_ids = _promote_floating_overlay_windows(stack, elements)
    owner_to_stack: Dict[int, int] = {}

    owners = sorted(
        {
            od
            for e in elements
            for od in [_resolve_owner_for_element(e, by_dom)]
            if od is not None and od in by_dom
        }
    )
    for owner_dom in owners:
        owner_elem = by_dom[owner_dom]
        owner_rect = owner_elem.get("rect", {})
        owner_name = owner_elem.get("inner_text", "") or owner_elem.get("tag", "")
        best_idx: Optional[int] = None
        best_score = -1.0
        for w in stack:
            score = _owner_match_score(owner_rect, owner_name, w)
            if score > best_score or (
                best_idx is not None and abs(score - best_score) < 1e-9 and w.stack_index > best_idx
            ):
                best_score = score
                best_idx = w.stack_index
        if best_idx is not None and best_score >= 0.25:
            owner_to_stack[owner_dom] = best_idx

    stack = _expand_window_stack_with_owner_frames(
        stack,
        owner_to_stack=owner_to_stack,
        by_dom=by_dom,
    )
    num_clipped = 0
    num_dropped = 0
    out: List[Dict[str, Any]] = []

    sorted_stack = sorted(stack, key=lambda w: w.stack_index)
    by_idx = {w.stack_index: w for w in sorted_stack}
    stack_indices = sorted(by_idx.keys())
    promoted_popup_indices = {
        w.stack_index
        for w in sorted_stack
        if w.window_id in promoted_popup_window_ids
    }
    desktop_stack_idx = next(
        (w.stack_index for w in sorted_stack if _normalize_name(w.name) == "desktop"),
        stack_indices[0] if stack_indices else None,
    )

    for elem in elements:
        owner_dom = _resolve_owner_for_element(elem, by_dom)
        stack_idx = owner_to_stack.get(owner_dom) if owner_dom is not None else None
        elem["_window_owner_dom_index"] = owner_dom
        elem["_window_stack_index"] = stack_idx
        if stack_idx is not None:
            elem["z"] = stack_idx

        if stack_idx is None:
            best_idx: Optional[int] = None
            best_score = -1.0
            for w in sorted_stack:
                score = _owner_match_score(
                    elem.get("rect", {}),
                    elem.get("inner_text", "") or elem.get("tag", ""),
                    w,
                )
                if score > best_score or (
                    best_idx is not None and abs(score - best_score) < 1e-9 and w.stack_index > best_idx
                ):
                    best_score = score
                    best_idx = w.stack_index
            if best_idx is not None and best_score >= 0.45:
                stack_idx = best_idx
                elem["_window_stack_index"] = stack_idx
                elem["z"] = stack_idx
            elif elem.get("source") == "desktop_chrome" and desktop_stack_idx is not None:
                stack_idx = desktop_stack_idx
                elem["_window_stack_index"] = stack_idx
                elem["z"] = stack_idx

    for elem in elements:
        stack_idx = elem.get("_window_stack_index")
        owner_dom = elem.get("_window_owner_dom_index")

        role = (elem.get("role") or "").strip().lower()
        parent_dom = elem.get("_parent_dom_index")
        parent_role = (
            (by_dom.get(parent_dom, {}).get("role") or "").strip().lower()
            if isinstance(parent_dom, int)
            else ""
        )
        popup_promotable_role = role in POPUP_OVERLAY_ELEMENT_ROLES and not (
            role == "menu" and parent_role == "menu bar"
        )
        if popup_promotable_role and promoted_popup_indices:
            rect = elem.get("rect", {})
            popup_match_idx: Optional[int] = None
            popup_match_score = 0.0
            for idx in sorted(promoted_popup_indices):
                win = by_idx[idx]
                inter = _intersection(rect, win.rect)
                if inter is None:
                    continue
                rect_area = max(1, _area(rect))
                inter_ratio = _area(inter) / rect_area
                if inter_ratio < 0.80:
                    continue
                if not _element_center_inside(rect, win.rect):
                    continue
                if inter_ratio > popup_match_score:
                    popup_match_score = inter_ratio
                    popup_match_idx = idx
            if popup_match_idx is not None and (stack_idx is None or popup_match_idx > stack_idx):
                stack_idx = popup_match_idx
                elem["_window_stack_index"] = stack_idx
                elem["z"] = stack_idx

        # Only for elements the tree left without a window of their own.
        #
        # The hint exists because some toolkits expose a popup's text nodes
        # outside the popup's window-like ancestor, so those orphans have to
        # inherit its stack geometrically. Applied to elements that *do* have an
        # owner, it does the opposite of its purpose: qalculate's calculator
        # buttons sit inside an open menu's rect, inherited the menu's stack
        # index, and were then above everything that could have covered them -
        # five widgets fully behind a popup, annotated as visible.
        hinted_stack, hinted_owner = (
            _find_containing_stack_hint(elem, elements)
            if owner_dom is None
            else (None, None)
        )
        if hinted_stack is not None and (stack_idx is None or hinted_stack > stack_idx):
            stack_idx = hinted_stack
            elem["_window_stack_index"] = stack_idx
            elem["z"] = stack_idx
        if owner_dom is None and hinted_owner is not None:
            owner_dom = hinted_owner
            elem["_window_owner_dom_index"] = owner_dom

    stack_occluders = _build_stack_occluder_rects(
        sorted_stack,
        elements,
        by_dom,
        promoted_popup_indices=promoted_popup_indices,
    )

    for elem in elements:
        stack_idx = elem.get("_window_stack_index")
        owner_dom = elem.get("_window_owner_dom_index")
        if stack_idx is None:
            out.append(elem)
            continue

        rect = dict(elem.get("rect", {}))
        if rect.get("w", 0) < min_visible_size or rect.get("h", 0) < min_visible_size:
            set_visible_fragments(elem, [], min_visible_size=min_visible_size)
            num_dropped += 1
            continue

        remaining = get_visible_fragments(elem)
        if not remaining:
            remaining = [rect]
        for idx in stack_indices:
            if idx <= stack_idx:
                continue
            occluder_rects = stack_occluders.get(idx) or [by_idx[idx].rect]
            for occ in occluder_rects:
                next_remaining: List[Dict[str, int]] = []
                for r in remaining:
                    next_remaining.extend(subtract_rect(r, occ))
                remaining = [
                    r for r in next_remaining
                    if r["w"] >= min_visible_size and r["h"] >= min_visible_size
                ]
                if not remaining:
                    break
            if not remaining:
                break

        remaining = set_visible_fragments(elem, remaining, min_visible_size=min_visible_size)
        original_rect = elem.get("_visibility_source_rect") or rect
        original_area = max(1, _area(original_rect))
        elem["_is_occluded_by_overlap"] = not (
            len(remaining) == 1 and remaining[0] == original_rect
        )
        elem["is_occluded"] = bool(elem["_is_occluded_by_overlap"])
        visible_area = sum(_area(r) for r in remaining)
        visible_ratio = visible_area / original_area

        if not remaining or visible_ratio <= min_visible_ratio:
            set_visible_fragments(elem, [], min_visible_size=min_visible_size)
            elem["_is_occluded_by_overlap"] = True
            elem["is_occluded"] = True
            num_dropped += 1
            continue

        best_visible = best_fragment(remaining)
        assert best_visible is not None
        if best_visible != rect:
            if allow_partial_clip:
                if _should_preserve_full_rect_on_partial_occlusion(elem, owner_dom, by_dom):
                    elem["_occlusion_partially_covered"] = True
                    elem["_occlusion_preserved_full_rect"] = True
                else:
                    elem["_occlusion_clipped"] = True
                    elem["_occlusion_original_rect"] = rect
                    elem["rect"] = best_visible
                    num_clipped += 1
            else:
                # Keep original box when partially occluded (drop-only mode).
                elem["_occlusion_partially_covered"] = True

        out.append(elem)

    out, overlay_meta = _apply_same_window_overlay_occlusion(
        out,
        by_dom=by_dom,
        min_visible_size=min_visible_size,
        min_visible_ratio=min_visible_ratio,
        allow_partial_clip=allow_partial_clip,
    )

    num_clipped += overlay_meta["num_clipped"]
    num_dropped += overlay_meta["num_dropped"]
    out = _reindex_elements(out)
    meta = {
        "enabled": True,
        "num_elements_in": len(elements),
        "num_elements_out": len(out),
        "num_clipped": num_clipped,
        "num_dropped": num_dropped,
        "num_overlay_clipped": overlay_meta["num_clipped"],
        "num_overlay_dropped": overlay_meta["num_dropped"],
        "num_owner_windows_matched": len(owner_to_stack),
        "num_promoted_popup_windows": len(promoted_popup_window_ids),
        "promoted_popup_window_ids": list(promoted_popup_window_ids),
        "window_stack": [
            {
                "window_id": w.window_id,
                "name": w.name,
                "rect": w.rect,
                "stack_index": w.stack_index,
            }
            for w in sorted_stack
        ],
    }
    return out, meta
