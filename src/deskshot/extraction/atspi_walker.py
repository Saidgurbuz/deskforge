"""Core AT-SPI2 tree walker — traverse accessibility tree → element dicts.

Produces element dicts in the Webshot-compatible schema so downstream
tools (filtering, ScreenTag serialization, visualization) work unchanged.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi

from deskshot.extraction.role_mapping import get_screentag_class, should_skip_role, is_desktop_chrome

logger = logging.getLogger(__name__)

_PLANK_PINNED_ITEMS: list[tuple[str, str]] = [
    ("caja-browser", "Files"),
    ("chromium-browser", "Chromium"),
    ("mousepad", "Mousepad"),
    ("thunderbird", "Thunderbird"),
    ("gnome-calculator", "Calculator"),
    ("xarchiver", "Xarchiver"),
]
_PLANK_DYNAMIC_LABELS: dict[str, str] = {
    "firefox": "Firefox",
    "file-roller": "File Roller",
    "eog": "Image Viewer",
    "vscode": "VS Code",
}

_PLANK_DESKTOP_LABELS: dict[str, str] = {
    "caja-browser": "Files",
    "caja": "Files",
    "chromium-browser": "Chromium",
    "chromium": "Chromium",
    "org.mozilla.firefox": "Firefox",
    "firefox": "Firefox",
    "org.xfce.mousepad": "Mousepad",
    "mousepad": "Mousepad",
    "net.thunderbird.thunderbird": "Thunderbird",
    "org.mozilla.thunderbird": "Thunderbird",
    "mozilla-thunderbird": "Thunderbird",
    "thunderbird": "Thunderbird",
    "org.gnome.calculator": "Calculator",
    "gnome-calculator": "Calculator",
    "galculator": "Calculator",
    "xarchiver": "Xarchiver",
    "org.gnome.fileroller": "File Roller",
    "file-roller": "File Roller",
}


STATE_FLAGS: dict[str, Atspi.StateType] = {
    # Sensitivity/enablement decide whether an advertised action can actually
    # fire; without them a greyed-out button looks clickable to a consumer.
    "enabled": Atspi.StateType.ENABLED,
    "sensitive": Atspi.StateType.SENSITIVE,
    "editable": Atspi.StateType.EDITABLE,
    "focusable": Atspi.StateType.FOCUSABLE,
    "checked": Atspi.StateType.CHECKED,
    "checkable": Atspi.StateType.CHECKABLE,
    "selected": Atspi.StateType.SELECTED,
    "selectable": Atspi.StateType.SELECTABLE,
    "expanded": Atspi.StateType.EXPANDED,
    "expandable": Atspi.StateType.EXPANDABLE,
    "multi_line": Atspi.StateType.MULTI_LINE,
    "single_line": Atspi.StateType.SINGLE_LINE,
    "read_only": Atspi.StateType.READ_ONLY,
    "has_popup": Atspi.StateType.HAS_POPUP,
    "modal": Atspi.StateType.MODAL,
    "pressed": Atspi.StateType.PRESSED,
}


def walk_application(app_name: str, viewport_w: int = 1920,
                     viewport_h: int = 1080) -> List[Dict[str, Any]]:
    """Walk the AT-SPI tree for a named application.

    Args:
        app_name: Application name as it appears in AT-SPI (e.g. "gnome-calculator").
        viewport_w: Viewport width for bounds checking.
        viewport_h: Viewport height for bounds checking.

    Returns:
        List of element dicts in Webshot-compatible schema.
    """
    desktop = Atspi.get_desktop(0)
    if desktop is None:
        logger.error("AT-SPI desktop is None")
        return []

    # Find the application
    app_accessible = None
    for i in range(desktop.get_child_count()):
        child = desktop.get_child_at_index(i)
        if child is None:
            continue
        name = child.get_name() or ""
        if app_name.lower() in name.lower():
            app_accessible = child
            break

    if app_accessible is None:
        available = []
        for i in range(desktop.get_child_count()):
            child = desktop.get_child_at_index(i)
            if child is not None:
                available.append(child.get_name() or "<unnamed>")
        logger.error(f"App '{app_name}' not found. Available: {available}")
        return []

    # Walk the tree
    elements: List[Dict[str, Any]] = []
    _walk_recursive(app_accessible, elements, viewport_w, viewport_h,
                    parent_dom_index=None, depth=0, path=())

    _finalize_local_tree(elements)

    return elements


def _walk_recursive(
    node: Atspi.Accessible,
    elements: List[Dict[str, Any]],
    viewport_w: int,
    viewport_h: int,
    parent_dom_index: Optional[int],
    depth: int,
    path: Tuple[int, ...],
) -> Optional[int]:
    """Recursively walk an AT-SPI node and its children.

    Returns the dom_index of this node in the elements list, or None if skipped.
    """
    try:
        role = node.get_role()
        role_name = node.get_role_name()
    except Exception:
        return None

    # Get bounding box via Component interface
    rect = _get_extents(node)

    # Visibility checks
    if not _is_visible(node):
        # Still walk children — a hidden container may have visible children
        pass
    else:
        skip = should_skip_role(role_name)

        # Get element info
        screentag_class = get_screentag_class(role_name)

        if not skip and rect is not None:
            # Bounds check: skip zero-size or entirely offscreen
            if rect["w"] > 0 and rect["h"] > 0:
                if (rect["x"] + rect["w"] > 0 and rect["y"] + rect["h"] > 0 and
                        rect["x"] < viewport_w and rect["y"] < viewport_h):

                    # Clamp to viewport
                    clamped = _clamp_rect(rect, viewport_w, viewport_h)
                    if clamped["w"] >= 2 and clamped["h"] >= 2:
                        elem = _build_element(
                            node, role_name, screentag_class, clamped,
                            parent_dom_index, depth, path,
                        )
                        my_index = len(elements)
                        elements.append(elem)

                        # Walk children with this node as parent
                        _walk_children(node, elements, viewport_w, viewport_h,
                                       my_index, depth + 1, path)
                        return my_index

    # Walk children with inherited parent (this node was skipped)
    _walk_children(node, elements, viewport_w, viewport_h,
                   parent_dom_index, depth + 1, path)
    return None


def _walk_children(
    node: Atspi.Accessible,
    elements: List[Dict[str, Any]],
    viewport_w: int,
    viewport_h: int,
    parent_dom_index: Optional[int],
    depth: int,
    parent_path: Tuple[int, ...],
) -> None:
    """Walk all children of a node."""
    try:
        n_children = node.get_child_count()
    except Exception:
        return

    for i in range(n_children):
        try:
            child = node.get_child_at_index(i)
            if child is not None:
                _walk_recursive(child, elements, viewport_w, viewport_h,
                                parent_dom_index, depth, parent_path + (i,))
        except Exception:
            continue


def _get_extents(node: Atspi.Accessible) -> Optional[Dict[str, int]]:
    """Get bounding box from AT-SPI Component interface."""
    try:
        component = node.get_component_iface()
        if component is None:
            return None
        # Get extents relative to screen (SCREEN coords)
        extents = component.get_extents(Atspi.CoordType.SCREEN)
        return {
            "x": extents.x,
            "y": extents.y,
            "w": extents.width,
            "h": extents.height,
        }
    except Exception:
        return None


def _clamp_rect(rect: Dict[str, int], viewport_w: int,
                viewport_h: int) -> Dict[str, int]:
    """Clamp rectangle to viewport bounds."""
    x1 = max(0, rect["x"])
    y1 = max(0, rect["y"])
    x2 = min(viewport_w, rect["x"] + rect["w"])
    y2 = min(viewport_h, rect["y"] + rect["h"])
    return {"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1}


def _is_visible(node: Atspi.Accessible) -> bool:
    """Check if node has SHOWING and VISIBLE states."""
    try:
        state_set = node.get_state_set()
        showing = state_set.contains(Atspi.StateType.SHOWING)
        visible = state_set.contains(Atspi.StateType.VISIBLE)
        return showing and visible
    except Exception:
        # If we can't check states, assume visible
        return True


def _get_name(node: Atspi.Accessible) -> str:
    """Get accessible name without conflating it with text-interface content."""
    try:
        name = node.get_name()
        return name.strip() if name else ""
    except Exception:
        return ""


def _get_text(node: Atspi.Accessible) -> Tuple[str, str]:
    """Extract text content from AT-SPI node.

    Returns:
        Tuple of (content, source), where source is one of
        "text_iface", "name", or "none".
    """
    # Try the Text interface first (for text widgets)
    try:
        text_iface = node.get_text_iface()
        if text_iface is not None:
            char_count = text_iface.get_character_count()
            if char_count > 0:
                content = text_iface.get_text(0, char_count)
                if content:
                    return content.strip(), "text_iface"
    except Exception:
        pass

    # Fall back to the accessible name
    name = _get_name(node)
    if name:
        return name, "name"

    return "", "none"


def _get_description(node: Atspi.Accessible) -> str:
    """Get accessible description."""
    try:
        desc = node.get_description()
        return desc.strip() if desc else ""
    except Exception:
        return ""


def _get_action_names(node: Atspi.Accessible) -> List[str]:
    """Collect supported action names from the AT-SPI Action interface."""
    try:
        action_iface = node.get_action_iface()
        if action_iface is None:
            return []
        names: List[str] = []
        for i in range(action_iface.get_n_actions()):
            name = action_iface.get_action_name(i)
            if name:
                names.append(name.strip())
        return names
    except Exception:
        return []


def _get_state_flags(node: Atspi.Accessible) -> Dict[str, bool]:
    """Collect a compact set of true AT-SPI state flags."""
    try:
        state_set = node.get_state_set()
    except Exception:
        return {}

    flags: Dict[str, bool] = {}
    for key, state in STATE_FLAGS.items():
        try:
            if state_set.contains(state):
                flags[key] = True
        except Exception:
            continue
    return flags


def _get_interface_flags(node: Atspi.Accessible) -> Dict[str, bool]:
    """Collect presence flags for high-value AT-SPI interfaces."""
    flags: Dict[str, bool] = {}
    try:
        if node.get_action_iface() is not None:
            flags["action"] = True
    except Exception:
        pass
    try:
        if node.get_editable_text_iface() is not None:
            flags["editable_text"] = True
    except Exception:
        pass
    try:
        if node.get_text_iface() is not None:
            flags["text"] = True
    except Exception:
        pass
    try:
        if node.get_document_iface() is not None:
            flags["document"] = True
    except Exception:
        pass
    try:
        if node.get_value_iface() is not None:
            flags["value"] = True
    except Exception:
        pass
    try:
        if node.get_selection_iface() is not None:
            flags["selection"] = True
    except Exception:
        pass
    try:
        if node.get_image_iface() is not None:
            flags["image"] = True
    except Exception:
        pass
    try:
        if node.get_table_iface() is not None:
            flags["table"] = True
    except Exception:
        pass
    return flags


def _build_element(
    node: Atspi.Accessible,
    role_name: str,
    screentag_class: Optional[str],
    rect: Dict[str, int],
    parent_dom_index: Optional[int],
    depth: int,
    path: Tuple[int, ...],
) -> Dict[str, Any]:
    """Build a Webshot-compatible element dict from an AT-SPI node."""
    text, text_source = _get_text(node)
    name = _get_name(node)
    description = _get_description(node)
    action_names = _get_action_names(node)
    state_flags = _get_state_flags(node)
    interface_flags = _get_interface_flags(node)

    # Use the ScreenTag class as the "type" and "tag" fields
    elem_type = screentag_class or role_name
    tag = role_name  # Use AT-SPI role as the "tag" (analogous to HTML tag)

    attrs: Dict[str, Any] = {}
    if description:
        attrs["description"] = description
    if text_source != "none":
        attrs["text_source"] = text_source
    if action_names:
        attrs["action_names"] = action_names
    if state_flags:
        attrs["states"] = state_flags
    if interface_flags:
        attrs["interfaces"] = interface_flags

    return {
        # HTML-equivalent fields
        "tag": tag,
        "role": role_name,
        "name": name or None,
        "id": None,
        "classes": None,
        "attrs": attrs,

        # Visual positioning
        "rect": rect,
        "z": 0,
        "position": "absolute",

        # Text content
        "inner_text": text,

        # Hierarchy (resolved later)
        "parent_index": None,  # Set after filtering
        "children_indices": [],  # Set after filtering
        "_dom_index": 0,  # Set after walk
        "_parent_dom_index": parent_dom_index,
        "_children_dom_indices": [],  # Resolved in _resolve_hierarchy
        "_depth": depth,
        "_atspi_path": list(path),

        # Classification
        "type": elem_type,
        "vlm_label": None,
        "frame_index": 0,
        "reading_order_index": None,

        # Source tracking
        "source": "app",
    }


def _resolve_hierarchy(elements: List[Dict[str, Any]]) -> None:
    """Resolve _children_dom_indices from _parent_dom_index references."""
    for elem in elements:
        elem["_children_dom_indices"] = []
        elem["children_indices"] = []

    # Build children lists from parent references
    for i, elem in enumerate(elements):
        parent_idx = elem["_parent_dom_index"]
        if parent_idx is not None and 0 <= parent_idx < len(elements):
            elements[parent_idx]["_children_dom_indices"].append(i)

    # Also set initial parent_index / children_indices (pre-filtering)
    for i, elem in enumerate(elements):
        elem["parent_index"] = elem["_parent_dom_index"]
        elem["children_indices"] = list(elem["_children_dom_indices"])


def _finalize_local_tree(elements: List[Dict[str, Any]]) -> None:
    """Assign sequential local dom indices and resolve hierarchy."""
    for i, elem in enumerate(elements):
        elem["_dom_index"] = i
    _resolve_hierarchy(elements)


def _offset_element_indices(elements: List[Dict[str, Any]], offset: int) -> None:
    """Offset local dom indices when merging multiple independent trees."""
    if offset <= 0:
        return
    for elem in elements:
        elem["_dom_index"] += offset
        if elem["_parent_dom_index"] is not None:
            elem["_parent_dom_index"] += offset
        elem["parent_index"] = elem["_parent_dom_index"]
        elem["_children_dom_indices"] = [idx + offset for idx in elem["_children_dom_indices"]]
        elem["children_indices"] = list(elem["_children_dom_indices"])


def _desktop_entry_name(desktop_path: str) -> Optional[str]:
    """Read `Name=` from the `[Desktop Entry]` group of a .desktop file.

    Plank labels a dock item with its launcher's own name, so the launcher file
    is the authoritative label source. Hard-coded maps go stale the moment a new
    app is pinned; this does not. Only the first group is read, because
    `[Desktop Action ...]` groups carry their own unrelated `Name=` keys.
    """
    try:
        with open(desktop_path, "r", encoding="utf-8", errors="replace") as handle:
            in_entry = False
            for line in handle:
                line = line.strip()
                if line.startswith("["):
                    if in_entry:
                        return None
                    in_entry = line == "[Desktop Entry]"
                    continue
                if in_entry and line.startswith("Name="):
                    name = line[5:].strip()
                    return name or None
    except OSError:
        return None
    return None


def _plank_item_label(uri: str) -> str:
    """Best available human label for one Plank launcher URI."""
    path = uri.replace("file://", "")
    stem = os.path.splitext(os.path.basename(path))[0]
    name = _desktop_entry_name(path)
    if name:
        return name
    return _PLANK_DESKTOP_LABELS.get(
        stem.lower(),
        stem.replace("_", " ").replace("-", " ").title(),
    )


def _query_plank_dbus_items() -> Optional[list[dict[str, int | str]]]:
    """Query Plank's session D-Bus API for the current visible dock items.

    Returns a list of dicts with `label`, `x`, and `y` when available, else
    None. The query is intentionally best-effort and should never fail the
    extraction path.
    """
    bus_name = "net.launchpad.plank"
    obj_path = "/net/launchpad/plank/dock1"
    iface = "net.launchpad.plank.Items"

    def _call(method: str, *args: str) -> Optional[str]:
        cmd = [
            "dbus-send",
            "--session",
            "--print-reply",
            f"--dest={bus_name}",
            obj_path,
            f"{iface}.{method}",
            *args,
        ]
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5,
                env=dict(os.environ),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if res.returncode != 0:
            return None
        return res.stdout

    uris: list[str] = []
    for method in ("GetPersistentApplications", "GetTransientApplications"):
        stdout = _call(method)
        if not stdout:
            continue
        uris.extend(re.findall(r'string "([^"]+)"', stdout))

    if not uris:
        return None

    items: list[dict[str, int | str]] = []
    for uri in uris:
        stdout = _call("GetHoverPosition", f"string:{uri}")
        if not stdout:
            continue
        ints = re.findall(r"int32 (-?\d+)", stdout)
        bools = re.findall(r"boolean (true|false)", stdout)
        if len(ints) < 2 or not bools or bools[0] != "true":
            continue
        try:
            items.append(
                {
                    "label": _plank_item_label(uri),
                    "x": int(ints[0]),
                    "y": int(ints[1]),
                }
            )
        except ValueError:
            continue
    return items or None


_PLANK_DOCK_SCHEMA = "net.launchpad.plank.dock.settings:/net/launchpad/plank/docks/dock1/"


def _plank_dock_setting(key: str) -> Optional[str]:
    """Read one live Plank dock setting via gsettings."""
    try:
        res = subprocess.run(
            ["gsettings", "get", _PLANK_DOCK_SCHEMA, key],
            capture_output=True,
            text=True,
            timeout=5,
            env=dict(os.environ),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout.strip().strip("'\"")


def _plank_dock_is_centered() -> bool:
    """True when Plank centres its item row on the screen with no offset.

    Extrapolating unreported dock slots from screen symmetry is only valid for a
    centred dock, so ask Plank rather than assume it. A dock we cannot ask about
    is treated as not centred, which keeps the extrapolation off.
    """
    if _plank_dock_setting("alignment") != "center":
        return False
    offset = _plank_dock_setting("offset")
    return offset is not None and offset in {"0", "0.0"}


def _complete_plank_dock_slots(
    items: list[dict[str, int | str]],
    *,
    icon_size: int,
    viewport_w: int,
) -> list[dict[str, int | str]]:
    """Add the dock slots Plank draws but does not report over D-Bus.

    `GetTransientApplications` only lists running apps Plank could resolve to a
    `.desktop` launcher. An app whose window Plank cannot match - measured on
    this stack for VS Code, Bluefish and Chromium's own running instance - is
    still drawn as a dock icon but is reported nowhere on the API, so querying
    Plank alone can never see it.

    What Plank does guarantee is the layout: one uniform pitch, contiguous
    slots, centred on the screen. Measured live at icon-size 54, the reported
    hover centres were 498/565/632/699/766/833 and then 1101 - a run of pitch 67
    with a 4-pitch jump at the end, i.e. three drawn-but-unreported icons at
    900/967/1034. Both facts are recoverable from geometry:

    - a gap that is an exact multiple of the pitch means unreported items sit
      in between;
    - the slot run is symmetric about the screen centre, so unreported items
      trailing the last reported one show up as a broken symmetry.

    Reconstructing from the layout rather than from a hard-coded app list keeps
    working for apps nobody listed. Bail out unchanged whenever the observed
    positions do not actually look like one uniform row, so that a dock we do
    not understand is under-reported rather than invented.
    """
    ordered = sorted(items, key=lambda item: int(item["x"]))
    if len(ordered) < 2:
        return ordered

    centers = [int(item["x"]) for item in ordered]
    diffs = [b - a for a, b in zip(centers, centers[1:])]
    pitch = min(diffs)
    if pitch < icon_size:
        # Overlapping or stacked items: this is not the uniform row we know how
        # to extrapolate from.
        return ordered
    for diff in diffs:
        steps = round(diff / pitch)
        if steps < 1 or abs(diff - steps * pitch) > 2:
            return ordered

    base = centers[0]
    row_y = int(ordered[0]["y"])
    known: Dict[int, dict[str, int | str]] = {}
    for item, center in zip(ordered, centers):
        known[round((center - base) / pitch)] = item

    low, high = 0, max(known)

    # A centred dock's first and last slot centres straddle the screen centre,
    # so solve for the run that satisfies that. Only for a dock Plank confirms
    # is centred, only when the solution is a clean integer one, and only for a
    # handful of slots - an unreported item is a rounding-error-sized
    # correction, not a doubling of the dock.
    if _plank_dock_is_centered():
        span = (viewport_w - 2 * base) / pitch
        span_rounded = round(span)
        if abs(span - span_rounded) <= 0.08 and 0 <= span_rounded <= 64:
            if span_rounded >= high:
                high = span_rounded
            else:
                low = span_rounded - high

    if (high - low + 1) - len(known) > 12:
        return ordered

    completed: list[dict[str, int | str]] = []
    for index in range(low, high + 1):
        item = known.get(index)
        if item is not None:
            completed.append(item)
            continue
        center = base + index * pitch
        if center - icon_size // 2 < 0 or center + icon_size // 2 > viewport_w:
            return ordered
        completed.append({"label": "", "x": center, "y": row_y})
    return completed


def _augment_plank_elements(
    app_name: str,
    app_elements: List[Dict[str, Any]],
    *,
    viewport_w: int,
    viewport_h: int,
    launched_apps: Optional[List[str]],
) -> List[Dict[str, Any]]:
    """Synthesize dock-item buttons when Plank exposes only a bare frame.

    In this environment, Plank often appears in AT-SPI only as a top-level
    window frame with no per-icon children, even though the dock is visible.
    We control the pinned launchers deterministically, so we can reconstruct a
    centered dock rect and item buttons from that known config plus the current
    launched app set.
    """
    if "plank" not in (app_name or "").lower():
        return app_elements

    root = next(
        (
            elem for elem in app_elements
            if (elem.get("role") or "").strip().lower() in {"frame", "window", "panel"}
        ),
        app_elements[0] if app_elements else None,
    )
    if root is None:
        return app_elements

    real_children = [
        elem for elem in app_elements
        if elem is not root and (elem.get("role") or "").strip().lower() not in {"filler", "separator"}
    ]
    if real_children:
        return app_elements

    icon_size = 54
    dbus_items = _query_plank_dbus_items()

    items_with_pos: list[tuple[str, int, int]] = []
    if dbus_items:
        dbus_items = _complete_plank_dock_slots(
            dbus_items,
            icon_size=icon_size,
            viewport_w=viewport_w,
        )
        half = icon_size // 2
        items_with_pos = [
            (str(item["label"]), int(item["x"]) - half, int(item["y"]))
            for item in dbus_items
        ]
    else:
        items: list[str] = [label for _app, label in _PLANK_PINNED_ITEMS]
        pinned_apps = {app for app, _label in _PLANK_PINNED_ITEMS}
        for app in launched_apps or []:
            if app in pinned_apps:
                continue
            label = _PLANK_DYNAMIC_LABELS.get(app)
            if label and label not in items:
                items.append(label)

        if not items:
            return app_elements

        item_gap = 10
        pad_x = 12
        pad_y = 10
        bottom_margin = 14
        dock_w = len(items) * icon_size + max(0, len(items) - 1) * item_gap + pad_x * 2
        dock_h = icon_size + pad_y * 2
        dock_x = max(0, int((viewport_w - dock_w) / 2))
        dock_y = max(0, viewport_h - dock_h - bottom_margin)
        root["rect"] = {"x": dock_x, "y": dock_y, "w": dock_w, "h": dock_h}
        item_x = dock_x + pad_x
        item_y = dock_y + pad_y
        items_with_pos = [
            (label, item_x + offset * (icon_size + item_gap), item_y)
            for offset, label in enumerate(items)
        ]

    if not items_with_pos:
        return app_elements

    all_x = [x for _label, x, _y in items_with_pos]
    all_y = [y for _label, _x, y in items_with_pos]
    pill_x = min(all_x) - 6
    pill_y = min(all_y) - 5
    pill_w = max(all_x) - min(all_x) + icon_size + 12
    pill_h = icon_size + 11
    root["rect"] = {"x": int(pill_x), "y": int(pill_y), "w": int(pill_w), "h": int(pill_h)}
    root["name"] = root.get("name") or "plank"
    root["inner_text"] = root.get("inner_text") or ""

    root_depth = int(root.get("_depth", 0))
    root_path = tuple(root.get("_atspi_path") or ())
    root_index = int(root.get("_dom_index", 0))
    next_dom = max((int(e.get("_dom_index", 0)) for e in app_elements), default=root_index) + 1

    synthetic_children: list[Dict[str, Any]] = []
    for offset, (label, item_x, item_y) in enumerate(items_with_pos):
        rect = {
            "x": item_x,
            "y": item_y,
            "w": icon_size,
            "h": icon_size,
        }
        synthetic_children.append(
            {
                "tag": "push button",
                "role": "push button",
                "name": label,
                "id": None,
                "classes": None,
                "attrs": {"text_source": "name", "synthetic": "plank-launcher"},
                "rect": rect,
                "z": 0,
                "position": "absolute",
                "inner_text": label,
                "parent_index": root_index,
                "children_indices": [],
                "_dom_index": next_dom + offset,
                "_parent_dom_index": root_index,
                "_children_dom_indices": [],
                "_depth": root_depth + 1,
                "_atspi_path": list(root_path + (1000 + offset,)),
                "type": "Button",
                "vlm_label": None,
                "frame_index": 0,
                "reading_order_index": None,
                "source": "desktop_chrome",
            }
        )

    app_elements.extend(synthetic_children)
    return app_elements


def walk_desktop_chrome(viewport_w: int = 1920,
                        viewport_h: int = 1080,
                        launched_apps: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Walk all desktop chrome AT-SPI applications (panels, desktop, etc.).

    Args:
        viewport_w: Viewport width for bounds checking.
        viewport_h: Viewport height for bounds checking.

    Returns:
        List of element dicts tagged with source="desktop_chrome".
    """
    desktop = Atspi.get_desktop(0)
    if desktop is None:
        logger.error("AT-SPI desktop is None")
        return []

    chrome_elements: List[Dict[str, Any]] = []

    for i in range(desktop.get_child_count()):
        child = desktop.get_child_at_index(i)
        if child is None:
            continue
        app_name = child.get_name() or ""
        if not is_desktop_chrome(app_name):
            continue

        logger.debug(f"Walking desktop chrome app: {app_name}")
        app_elements: List[Dict[str, Any]] = []
        _walk_recursive(child, app_elements, viewport_w, viewport_h,
                        parent_dom_index=None, depth=0, path=())
        app_elements = _augment_plank_elements(
            app_name,
            app_elements,
            viewport_w=viewport_w,
            viewport_h=viewport_h,
            launched_apps=launched_apps,
        )
        _finalize_local_tree(app_elements)

        # Tag all elements as desktop chrome. `_atspi_app_name` is what lets a
        # saved element be resolved back to its live accessible - it is how
        # `populate_visible_text` fetches character geometry - and omitting it
        # here made every desktop-chrome element unresolvable, so each one was
        # dropped as `unsupported_partial` the moment anything overlapped it.
        # Measured on v224: 166 of 412 withheld elements were desktop file
        # icons whose labels are plainly drawn.
        for elem in app_elements:
            elem["source"] = "desktop_chrome"
            elem["app_name"] = app_name
            elem["_atspi_app_name"] = app_name

        _offset_element_indices(app_elements, len(chrome_elements))

        chrome_elements.extend(app_elements)

    _resolve_hierarchy(chrome_elements)

    logger.info(f"Found {len(chrome_elements)} desktop chrome elements")
    return chrome_elements
