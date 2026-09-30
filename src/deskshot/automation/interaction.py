"""Execute interaction sequences from YAML manifests.

Supports action types:
key, click, type_text, wait, window_focus, window_move, window_move_active,
window_resize_active, randomize_layout, heuristic_click.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Optional

from deskshot.automation import xdotool
from deskshot.config import Action, InteractionSequence
from deskshot.extraction.atspi_walker import walk_application
from deskshot.extraction.occlusion import apply_occlusion_clipping

logger = logging.getLogger(__name__)

HEURISTIC_CLICKABLE_ROLES = {
    "check box",
    "check menu item",
    "link",
    "menu item",
    "page tab",
    "push button",
    "radio button",
    "toggle button",
}
HEURISTIC_WINDOW_SKIP_NAMES = {"desktop", "top panel", "xfwm4"}


def execute_sequence(
    sequence: InteractionSequence,
    display: Optional[str] = None,
    app_name: Optional[str] = None,
) -> None:
    """Execute an interaction sequence.

    Args:
        sequence: The interaction sequence to execute.
        display: X display string. Defaults to $DISPLAY.
    """
    logger.info(f"Executing interaction: {sequence.name}")
    if sequence.description:
        logger.debug(f"  Description: {sequence.description}")

    for i, action in enumerate(sequence.actions):
        logger.debug(f"  Action {i+1}/{len(sequence.actions)}: "
                      f"{action.type} = {action.value!r}")
        execute_action(action, display, app_name=app_name)


def execute_action(
    action: Action,
    display: Optional[str] = None,
    app_name: Optional[str] = None,
) -> None:
    """Execute a single action.

    Action types:
        key: Send keyboard shortcut (e.g. "ctrl+q", "Return", "alt+F4")
        click: Click at x,y coordinates (e.g. "960,540" or "960,540,3" for right-click)
        type_text: Type a text string
        wait: Wait for N seconds
        window_focus: Focus window by title (value: "Window Title")
        window_move: Move window by title (value: "Window Title|x|y")
        window_move_active: Move active window (value: "x|y" or "x,y")
        window_resize_active: Resize active window (value: "w|h" or "w,h")
        randomize_layout: Move visible app windows to randomized positions
        heuristic_click: Click a currently visible AT-SPI target heuristically
    """
    if action.type == "key":
        xdotool.key(action.value, display)

    elif action.type == "click":
        parts = action.value.split(",")
        x, y = int(parts[0]), int(parts[1])
        button = int(parts[2]) if len(parts) > 2 else 1
        xdotool.click(x, y, button, display)

    elif action.type == "type_text":
        xdotool.type_text(action.value, display=display)

    elif action.type == "wait":
        duration = float(action.value) if action.value else action.delay
        time.sleep(duration)

    elif action.type == "window_focus":
        ok = xdotool.focus_window_by_name(action.value, display)
        if not ok:
            logger.warning("window_focus failed for %r", action.value)

    elif action.type == "window_move":
        name, x, y = _parse_window_move_value(action.value)
        ok = xdotool.move_window_by_name(name, x, y, display)
        if not ok:
            logger.warning("window_move failed for %r", action.value)

    elif action.type == "window_move_active":
        x, y = _parse_xy(action.value)
        ok = xdotool.move_active_window(x, y, display)
        if not ok:
            logger.warning("window_move_active failed for %r", action.value)

    elif action.type == "window_resize_active":
        w, h = _parse_wh(action.value)
        ok = xdotool.resize_active_window(w, h, display)
        if not ok:
            logger.warning("window_resize_active failed for %r", action.value)

    elif action.type == "randomize_layout":
        _randomize_layout(action.value or "scatter", display)

    elif action.type == "heuristic_click":
        if not app_name:
            logger.warning("heuristic_click requires app_name/AT-SPI target")
        else:
            target = _choose_heuristic_click_target(app_name, action.value, display)
            if target is None:
                logger.warning("heuristic_click found no eligible target for %s", app_name)
            else:
                rect = target["rect"]
                x = rect["x"] + max(1, rect["w"]) // 2
                y = rect["y"] + max(1, rect["h"]) // 2
                xdotool.click(x, y, 1, display)

    else:
        logger.warning(f"Unknown action type: {action.type}")

    # Post-action delay
    if action.delay > 0:
        time.sleep(action.delay)


def _parse_window_move_value(value: str) -> tuple[str, int, int]:
    """Parse 'Window Title|x|y' (or fallback 'Window Title,x,y')."""
    if "|" in value:
        parts = [p.strip() for p in value.split("|")]
    else:
        parts = [p.strip() for p in value.split(",")]
    if len(parts) < 3:
        raise ValueError(f"Invalid window_move value: {value!r}")
    name = parts[0]
    x = int(parts[-2])
    y = int(parts[-1])
    return name, x, y


def _parse_xy(value: str) -> tuple[int, int]:
    parts = [p.strip() for p in (value.split("|") if "|" in value else value.split(","))]
    if len(parts) < 2:
        raise ValueError(f"Invalid xy value: {value!r}")
    return int(parts[-2]), int(parts[-1])


def _parse_wh(value: str) -> tuple[int, int]:
    parts = [p.strip() for p in (value.split("|") if "|" in value else value.split(","))]
    if len(parts) < 2:
        raise ValueError(f"Invalid wh value: {value!r}")
    return int(parts[-2]), int(parts[-1])


def _parse_heuristic_mode(value: str) -> tuple[str, Optional[int]]:
    if not value:
        return "button", None
    parts = [p.strip() for p in value.split("|") if p.strip()]
    mode = parts[0] if parts else "button"
    seed = int(parts[1]) if len(parts) > 1 else None
    return mode, seed


def _is_visible_in_ancestor_chain(elem: dict, by_dom: dict[int, dict]) -> bool:
    rect = elem.get("rect", {})
    cur = elem.get("_parent_dom_index")
    while cur is not None and cur in by_dom:
        parent = by_dom[cur]
        parent_rect = parent.get("rect", {})
        x = rect.get("x", 0) + rect.get("w", 0) / 2
        y = rect.get("y", 0) + rect.get("h", 0) / 2
        if not (
            parent_rect.get("x", 0) <= x <= parent_rect.get("x", 0) + parent_rect.get("w", 0)
            and parent_rect.get("y", 0) <= y <= parent_rect.get("y", 0) + parent_rect.get("h", 0)
        ):
            return False
        cur = parent.get("_parent_dom_index")
    return True


def _choose_heuristic_click_target(
    app_name: str,
    value: str,
    display: Optional[str] = None,
) -> Optional[dict]:
    mode, seed = _parse_heuristic_mode(value)
    elements = walk_application(app_name)
    if not elements:
        return None
    visible_elements, _meta = apply_occlusion_clipping(
        elements,
        display=display,
        allow_partial_clip=True,
        min_visible_ratio=0.15,
    )
    by_dom = {int(e["_dom_index"]): e for e in visible_elements if "_dom_index" in e}
    candidates = []
    for elem in visible_elements:
        role = (elem.get("role") or "").strip().lower()
        if role not in HEURISTIC_CLICKABLE_ROLES:
            continue
        rect = elem.get("rect", {})
        if rect.get("w", 0) < 12 or rect.get("h", 0) < 12:
            continue
        if not _is_visible_in_ancestor_chain(elem, by_dom):
            continue
        owner_dom = elem.get("_window_owner_dom_index")
        owner = by_dom.get(owner_dom) if owner_dom is not None else None
        if owner is not None:
            owner_rect = owner.get("rect", {})
            if rect.get("y", 0) < owner_rect.get("y", 0) + 42 and rect.get("h", 0) <= 42:
                continue
        text = (elem.get("inner_text") or "").strip()
        if "button" in mode and role not in {"push button", "toggle button", "radio button"}:
            continue
        score = 0.0
        if text:
            score += 2.0
        if role == "push button":
            score += 2.0
        if role in {"toggle button", "radio button"}:
            score += 1.0
        area = rect.get("w", 0) * rect.get("h", 0)
        if 250 <= area <= 40_000:
            score += 1.0
        candidates.append((score, elem))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1]["rect"]["y"], item[1]["rect"]["x"], item[1].get("inner_text", "")))

    if "random" in mode:
        top = [elem for _score, elem in candidates[: min(8, len(candidates))]]
        rng = random.Random(seed)
        return rng.choice(top)
    return candidates[0][1]


def _randomize_layout(style: str, display: Optional[str] = None) -> None:
    width, height = xdotool.get_display_geometry(display)
    rng = random.Random()
    visible = xdotool.list_visible_windows(display)
    windows = [
        win
        for win in visible
        if str(win.get("name", "")).strip().lower() not in HEURISTIC_WINDOW_SKIP_NAMES
        and int(win.get("rect", {}).get("w", 0)) >= 180
        and int(win.get("rect", {}).get("h", 0)) >= 120
        and int(win.get("rect", {}).get("w", 0)) < width
        and int(win.get("rect", {}).get("h", 0)) < height
    ]
    if not windows:
        return

    margin_x = 32
    margin_y = 48
    for idx, win in enumerate(windows):
        rect = win["rect"]
        max_x = max(margin_x, width - int(rect["w"]) - margin_x)
        max_y = max(margin_y, height - int(rect["h"]) - margin_y)
        if style == "cascade":
            x = min(max_x, margin_x + idx * 80)
            y = min(max_y, margin_y + idx * 60)
        else:
            x = rng.randint(margin_x, max_x)
            y = rng.randint(margin_y, max_y)
        xdotool.window_move(str(win["id"]), x, y, display)
