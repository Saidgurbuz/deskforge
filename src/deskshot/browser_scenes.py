"""Reusable browser scene templates for config-driven live URL interactions."""

from __future__ import annotations

from typing import Any, Iterable, List, Mapping

from deskshot.config import Action, InteractionSequence


def expand_browser_scene_templates(
    *,
    app_name: str,
    binary: str,
    templates: Iterable[Mapping[str, Any]],
) -> List[InteractionSequence]:
    """Expand declarative browser scene templates into interactions."""
    interactions: List[InteractionSequence] = []
    for raw in templates:
        scene_type = str(raw.get("type", "")).strip().lower()
        if scene_type == "chromium_live_windows":
            if "chromium" not in app_name.lower() and "chromium" not in binary.lower():
                raise ValueError(
                    "chromium_live_windows scene templates can only be used with Chromium manifests"
                )
            interactions.append(_build_chromium_live_windows_scene(raw))
            continue
        raise ValueError(f"Unsupported browser scene template type: {raw.get('type')!r}")
    return interactions


def _build_chromium_live_windows_scene(template: Mapping[str, Any]) -> InteractionSequence:
    """Build a deterministic multi-window Chromium interaction."""
    name = str(template.get("name", "")).strip()
    if not name:
        raise ValueError("Browser scene template requires a non-empty name")

    description = str(template.get("description", "")).strip()
    settle_seconds = float(template.get("settle_seconds", 8.0))
    new_window_shortcut = str(template.get("new_window_shortcut", "ctrl+n")).strip() or "ctrl+n"
    final_wait_seconds = float(template.get("final_wait_seconds", 0.8))

    raw_windows = list(template.get("windows") or [])
    if not raw_windows:
        raise ValueError(f"Browser scene template {name!r} requires at least one window")

    actions: List[Action] = []
    for idx, window in enumerate(raw_windows):
        url = str(window.get("url", "")).strip()
        if not url:
            raise ValueError(f"Browser scene template {name!r} has a window without a URL")

        x = int(window["x"])
        y = int(window["y"])
        width = int(window["width"])
        height = int(window["height"])
        if width <= 0 or height <= 0:
            raise ValueError(f"Browser scene template {name!r} has non-positive window size")

        if idx > 0:
            actions.append(Action(type="key", value=new_window_shortcut, delay=1.0))

        actions.extend(
            [
                Action(type="key", value="ctrl+l", delay=0.3),
                Action(type="type_text", value=url, delay=0.2),
                Action(type="key", value="Return", delay=0.3),
                Action(type="wait", value=f"{settle_seconds:.1f}", delay=0.0),
                Action(type="window_resize_active", value=f"{width}|{height}", delay=0.3),
                Action(type="window_move_active", value=f"{x}|{y}", delay=0.6),
            ]
        )
        actions.extend(_parse_actions(window.get("post_actions") or []))

    if final_wait_seconds > 0:
        actions.append(Action(type="wait", value=f"{final_wait_seconds:.1f}", delay=0.0))

    return InteractionSequence(
        name=name,
        description=description,
        actions=actions,
    )


def _parse_actions(raw_actions: Iterable[Mapping[str, Any]]) -> List[Action]:
    """Convert inline template action dicts into Action objects."""
    actions: List[Action] = []
    for raw in raw_actions:
        action_type = str(raw.get("type", "")).strip()
        if not action_type:
            raise ValueError("Browser scene template action requires a non-empty type")
        actions.append(
            Action(
                type=action_type,
                value=str(raw.get("value", "")),
                delay=float(raw.get("delay", 0.5)),
            )
        )
    return actions
