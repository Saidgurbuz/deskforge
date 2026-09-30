"""What a released observation says, and what it must never say.

`meta.json` is an engineering log: absolute host paths, host
monotonic clocks, staging hashes, per-stage element tallies. Publishing it
verbatim would leak the filesystem layout of a shared research machine and bury
the fields anybody actually wants. `record.json` is built by naming the fields
that are allowed out, never by removing the ones that are not - a deny list
silently publishes whatever the next pipeline change adds.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

#: Resolution presets encode their own pixel size, `hdplus_1600x900`.
_RESOLUTION = re.compile(r"(\d{3,5})x(\d{3,5})")


def viewport_of(resolution: Optional[str]) -> Optional[tuple]:
    match = _RESOLUTION.search(resolution or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _theme(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Theme names, never the wallpaper's absolute path on the host."""
    theme = meta.get("theme") or {}
    wallpaper = theme.get("wallpaper")
    return {
        "gtk_theme": theme.get("gtk_theme"),
        "icon_theme": theme.get("icon_theme"),
        "wm_theme": theme.get("wm_theme"),
        # The basename identifies which wallpaper without saying where it lives.
        "wallpaper": wallpaper.rsplit("/", 1)[-1] if isinstance(wallpaper, str) else None,
    }


def _window_stack(meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    stack = ((meta.get("occlusion") or {}).get("window_stack")) or []
    out = []
    for entry in stack:
        if not isinstance(entry, dict):
            continue
        out.append({
            "name": entry.get("name"),
            "rect": entry.get("rect"),
            "stack_index": entry.get("stack_index"),
        })
    return out


def build_record(
    *,
    observation_key: str,
    scene_id: str,
    episode_id: Optional[str],
    step_index: Optional[int],
    split: str,
    group: str,
    meta: Dict[str, Any],
    flags: Dict[str, Any],
    action: Optional[Dict[str, Any]] = None,
    effect: Optional[Dict[str, Any]] = None,
    dataset_version: str = "1.0.0",
) -> Dict[str, Any]:
    """The sanitized per-observation record that ships inside the tar."""
    meta = meta or {}
    scene = meta.get("scene") or {}
    viewport = meta.get("viewport") or {}
    occlusion = meta.get("occlusion") or {}

    record: Dict[str, Any] = {
        "dataset_version": dataset_version,
        "observation_key": observation_key,
        "scene_id": scene_id,
        "episode_id": episode_id,
        "step_index": step_index,
        "split": split,
        "group": group,

        "width": viewport.get("width"),
        "height": viewport.get("height"),
        "desktop_env": meta.get("desktop_env"),
        "apps": sorted(meta.get("launched_apps") or []),
        "theme": _theme(meta),
        "scene": {
            "theme_preset": scene.get("theme_preset"),
            "display_preset": scene.get("display_preset"),
            "panel_variant": scene.get("panel_variant"),
            "desktop_profile": scene.get("desktop_profile"),
            "desktop_layout_template": scene.get("desktop_layout_template"),
            "desktop_content_pack": scene.get("desktop_content_pack"),
            "layout": scene.get("layout"),
            "seed": scene.get("seed"),
        },

        "n_elements": meta.get("num_elements_leaf"),
        "n_elements_filtered": meta.get("num_elements_filtered"),
        "n_windows": len(_window_stack(meta)) or None,
        "window_stack": _window_stack(meta),
        "occlusion": {
            "num_elements_in": occlusion.get("num_elements_in"),
            "num_clipped": occlusion.get("num_clipped"),
            "num_dropped": occlusion.get("num_dropped"),
        },
        "include_desktop_chrome": meta.get("include_desktop_chrome"),
        "screentag_grid": 500,

        "flags": {
            "publishable": bool(flags.get("publishable")),
            "state_train_eligible": bool(flags.get("state_train_eligible")),
            "near_duplicate": bool(flags.get("near_duplicate")),
            "no_op_frame": bool(flags.get("no_op_frame")),
            "missing_apps": sorted(flags.get("missing_apps") or []),
        },
        "provenance": {
            "generator": "deskshot",
            "generator_commit": ((meta.get("provenance") or {}).get("code") or {}).get("commit"),
            "annotation_source": "AT-SPI2 accessibility tree, occlusion-resolved",
        },
    }
    if action is not None:
        record["action_into_this_state"] = action
    if effect is not None:
        record["effect_of_that_action"] = effect
    return record


#: Every key a record may contain, at the top level. The packer asserts against
#: this so a pipeline change cannot quietly widen what is published.
ALLOWED_TOP_LEVEL = frozenset({
    "dataset_version", "observation_key", "scene_id", "episode_id", "step_index",
    "split", "group", "width", "height", "desktop_env", "apps", "theme", "scene",
    "n_elements", "n_elements_filtered", "n_windows", "window_stack", "occlusion",
    "include_desktop_chrome", "screentag_grid", "flags", "provenance",
    "action_into_this_state", "effect_of_that_action",
})


#: Leaves that quote text a reader can see in the screenshot.
#:
#: A window title and a clicked widget's label are annotation, not metadata:
#: `landing_page.html (/tmp/session-6k9s307k/Users/iris/Documents/...) -
#: Bluefish` is painted into the title bar, and a file chooser's combo box
#: really does read `/tmp/session-sxc_9evg/home/anika/Documents`. Both are in
#: `leaf.json` and in the ScreenTag as well. Rewriting them here would make the
#: record disagree with its own pixels and would hide nothing.
#:
#: These are exact leaves, not whole fields: a host path appearing in
#: `action_into_this_state.target_app` would still be a defect and is still
#: caught. `*` matches every element of a list.
#:
#: Measured over 600 random captures: no `/proj/`, no `/u/` and no real
#: username appears anywhere in the released text. What does appear is the
#: ephemeral session root `/tmp/session-<random>/` (32.8%) and synthetic
#: persona homes such as `/Users/anika/` (32.5%), both generated, plus the host
#: name (9.8%). All three are recorded in `docs/known_issues.md`.
VERBATIM_ONSCREEN_TEXT = (
    ("window_stack", "*", "name"),
    ("action_into_this_state", "target_text"),
)


def _without(value: Any, path: tuple) -> Any:
    """`value` with the leaf at `path` removed, for scanning purposes."""
    if not path:
        return None
    head, rest = path[0], path[1:]
    if head == "*":
        if not isinstance(value, list):
            return value
        return [_without(item, rest) for item in value]
    if not isinstance(value, dict) or head not in value:
        return value
    copied = dict(value)
    copied[head] = _without(copied[head], rest) if rest else None
    return copied


def assert_sanitized(record: Dict[str, Any]) -> None:
    """Refuse a record that grew a field nobody approved, or a host path.

    The path scan covers every constructed value. It skips only the exact
    leaves in `VERBATIM_ONSCREEN_TEXT`, which quote the screen.
    """
    extra = set(record) - ALLOWED_TOP_LEVEL
    if extra:
        raise ValueError("record has unapproved top-level fields: %s" % sorted(extra))
    scanned: Any = record
    for path in VERBATIM_ONSCREEN_TEXT:
        scanned = _without(scanned, path)
    blob = repr(scanned)
    for needle in ("/proj/", "/u/", "/home/", "/tmp/"):
        if needle in blob:
            raise ValueError("record leaks a host path containing %r" % needle)
