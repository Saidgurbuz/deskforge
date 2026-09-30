#!/usr/bin/env python
"""What a window's rect really is, and what its toolkit really publishes.

Three questions keep coming back and all three are answerable only against a
live X server, so they live in one probe instead of three throwaway scripts:

1. **Where does a window actually draw?** `xdotool getwindowgeometry` reports the
   whole X window, which for a client-side-decorated GTK app includes an
   invisible margin holding the drop shadow. `_GTK_FRAME_EXTENTS` describes that
   margin exactly. The probe prints both, plus `_NET_FRAME_EXTENTS` and the
   AT-SPI frame rect for the same window, so the four can be compared.

2. **What role does a toolkit give a popup?** GTK toplevels are `frame`; menus
   and other override-redirect popups are `window`. Occlusion depends on telling
   them apart, so the probe reports the role of every top-level accessible.

3. **Does an app publish widget X at all?** The FileZilla answer was "no, and no
   fix in this repo reaches it". `--dump-tree APP` prints the *raw* AT-SPI
   subtree - every node, including roles the extractor skips - so "absent from
   the tree" and "dropped by the pipeline" stop being guesses.

Usage:
    PYTHONPATH=src python scripts/probe_window_geometry.py \
        --display-number 1904 --apps pluma,nautilus \
        --dump-tree pluma --style macos \
        --output incremental_checks/vNNN_topic/probe
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from deskshot.config import (  # noqa: E402
    CONFIGS_DIR,
    AppManifest,
    DisplayConfig,
    SessionConfig,
    ThemeConfig,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("probe_window_geometry")


def _xdotool(args: List[str]) -> str:
    from deskshot.automation.app_launcher import find_binary

    try:
        binary = find_binary("xdotool")
    except FileNotFoundError:
        binary = "xdotool"
    res = subprocess.run([binary] + args, capture_output=True, text=True, timeout=20)
    return res.stdout if res.returncode == 0 else ""


def collect_x_windows() -> List[Dict[str, Any]]:
    """Every visible X window with its geometry and both frame-extent properties."""
    from deskshot.extraction.occlusion import _gtk_frame_extents, get_window_stack

    out: List[Dict[str, Any]] = []
    for layer in get_window_stack():
        gtk = _gtk_frame_extents(layer.window_id)
        rect = dict(layer.rect)
        entry: Dict[str, Any] = {
            "window_id": layer.window_id,
            "name": layer.name,
            "stack_index": layer.stack_index,
            "xdotool_rect": rect,
            "gtk_frame_extents": list(gtk) if gtk else None,
            "net_frame_extents": _net_frame_extents(layer.window_id),
        }
        if gtk:
            left, right, top, bottom = gtk
            entry["content_rect"] = {
                "x": rect["x"] + left,
                "y": rect["y"] + top,
                "w": rect["w"] - left - right,
                "h": rect["h"] - top - bottom,
            }
        out.append(entry)
    return out


def _net_frame_extents(window_id: str) -> Optional[List[int]]:
    """WM decoration thickness, for comparison with the GTK shadow margin."""
    try:
        from deskshot.automation.app_launcher import find_binary

        binary = find_binary("xprop")
    except Exception:
        return None
    res = subprocess.run(
        [binary, "-id", str(window_id), "_NET_FRAME_EXTENTS"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if res.returncode != 0 or "=" not in res.stdout:
        return None
    try:
        return [int(v.strip()) for v in res.stdout.split("=", 1)[1].split(",")]
    except ValueError:
        return None


def collect_toplevel_accessibles() -> List[Dict[str, Any]]:
    """Role and rect of every top-level accessible, per application."""
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    out: List[Dict[str, Any]] = []
    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue
        app_name = app.get_name() or "<unnamed>"
        for j in range(app.get_child_count()):
            top = app.get_child_at_index(j)
            if top is None:
                continue
            try:
                role = top.get_role_name()
            except Exception:
                continue
            try:
                ext = Atspi.Component.get_extents(top, Atspi.CoordType.SCREEN)
            except Exception:
                ext = None
            out.append({
                "app": app_name,
                "role": role,
                "name": top.get_name() or None,
                "rect": (
                    {"x": ext.x, "y": ext.y, "w": ext.width, "h": ext.height}
                    if ext is not None
                    else None
                ),
            })
    return out


def dump_raw_tree(app_name: str, max_nodes: int = 4000) -> List[Dict[str, Any]]:
    """Every node in an app's AT-SPI subtree, with nothing filtered out."""
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    desktop = Atspi.get_desktop(0)
    target = None
    for i in range(desktop.get_child_count()):
        child = desktop.get_child_at_index(i)
        if child is None:
            continue
        if app_name.lower() in (child.get_name() or "").lower():
            target = child
            break
    if target is None:
        return []

    nodes: List[Dict[str, Any]] = []

    def visit(node: Any, depth: int, parent: int) -> None:
        if len(nodes) >= max_nodes:
            return
        try:
            role = node.get_role_name()
        except Exception:
            return
        try:
            ext = Atspi.Component.get_extents(node, Atspi.CoordType.SCREEN)
            rect = {"x": ext.x, "y": ext.y, "w": ext.width, "h": ext.height}
        except Exception:
            rect = None
        try:
            states = sorted(
                str(s.value_nick)
                for s in node.get_state_set().get_states()
            )
        except Exception:
            states = []
        try:
            text = Atspi.Text.get_text(node, 0, -1)
        except Exception:
            text = None
        idx = len(nodes)
        nodes.append({
            "index": idx,
            "parent": parent,
            "depth": depth,
            "role": role,
            "name": node.get_name() or None,
            "text": text,
            "rect": rect,
            "states": states,
        })
        try:
            n = node.get_child_count()
        except Exception:
            return
        for i in range(n):
            try:
                child = node.get_child_at_index(i)
            except Exception:
                continue
            if child is not None:
                visit(child, depth + 1, idx)

    visit(target, 0, -1)
    return nodes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--display-number", type=int, required=True)
    parser.add_argument("--apps", default="pluma")
    parser.add_argument("--dump-tree", default="")
    parser.add_argument("--style", default="macos", choices=["linux", "macos", "windows", "ubuntu"])
    parser.add_argument("--gtk-theme", default="")
    parser.add_argument("--wm-theme", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--settle", type=float, default=3.0)
    parser.add_argument(
        "--context-menu",
        default="",
        help="Window-name substring - right-click that window's centre to open a popup menu",
    )
    args = parser.parse_args()

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    theme_kwargs: Dict[str, Any] = {"desktop_style": args.style}
    if args.gtk_theme:
        theme_kwargs["gtk_theme"] = args.gtk_theme
    if args.wm_theme:
        theme_kwargs["wm_theme"] = args.wm_theme

    session_cfg = SessionConfig(
        display=DisplayConfig(display_number=args.display_number),
        theme=ThemeConfig(**theme_kwargs),
        desktop_env="xfce",
    )

    from deskshot.automation.app_launcher import kill_app, launch_app
    from deskshot.environment.session import DesktopSession

    report: Dict[str, Any] = {"style": args.style, "apps": args.apps}
    procs = []
    session = DesktopSession(session_cfg)
    session.__enter__()
    try:
        report["theme"] = {
            "gtk_theme": session.config.theme.gtk_theme,
            "wm_theme": session.config.theme.wm_theme,
            "desktop_style": session.config.theme.desktop_style,
        }
        report["compositor"] = _compositor_state()

        for app in [a.strip() for a in args.apps.split(",") if a.strip()]:
            manifest = AppManifest.from_yaml(CONFIGS_DIR / "apps" / f"{app}.yaml")
            procs.append(launch_app(manifest, timeout=40.0))
            time.sleep(1.0)

        if args.context_menu:
            report["context_menu"] = _open_context_menu(args.context_menu)

        from deskshot.generation.readiness import wait_for_apps_to_settle

        targets = [(a.strip(), a.strip()) for a in args.apps.split(",") if a.strip()]
        try:
            settle = wait_for_apps_to_settle(targets)
        except Exception as exc:  # pragma: no cover - probe convenience
            settle = {"error": str(exc)}
        report["settle"] = _jsonable(settle)
        time.sleep(args.settle)

        from deskshot.extraction.screenshot import capture_screenshot

        capture_screenshot(output_path=out_dir / "probe.png")
        # A second shot, later, separates decoration from a mid-render capture:
        # a shadow that is painted decoration is identical in both, one that is
        # an unfinished frame is not.
        time.sleep(4.0)
        capture_screenshot(output_path=out_dir / "probe_late.png")
        report["repeat_capture"] = _screenshot_delta(
            out_dir / "probe.png", out_dir / "probe_late.png"
        )

        report["x_windows"] = collect_x_windows()
        report["toplevel_accessibles"] = collect_toplevel_accessibles()
        if args.dump_tree:
            tree = dump_raw_tree(args.dump_tree)
            (out_dir / f"{args.dump_tree}.rawtree.json").write_text(
                json.dumps(tree, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            report["raw_tree"] = {
                "app": args.dump_tree,
                "num_nodes": len(tree),
                "roles": _role_histogram(tree),
            }
    finally:
        for proc in procs:
            try:
                kill_app(proc)
            except Exception:
                pass
        session.__exit__(None, None, None)

    (out_dir / "probe.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in report.items() if k != "x_windows"}, indent=2)[:4000])
    for win in report.get("x_windows", []):
        print(
            f"  {win['name'][:38]:38s} xdotool={win['xdotool_rect']} "
            f"gtk_extents={win['gtk_frame_extents']} net={win['net_frame_extents']}"
        )
    return 0


def _open_context_menu(name_substring: str) -> Dict[str, Any]:
    """Right-click the centre of a named window and report where we clicked."""
    from deskshot.extraction.occlusion import get_window_stack

    match = next(
        (
            w
            for w in get_window_stack()
            if name_substring.lower() in (w.name or "").lower()
        ),
        None,
    )
    if match is None:
        return {"clicked": False, "reason": f"no window matching {name_substring!r}"}
    cx = match.rect["x"] + match.rect["w"] // 2
    cy = match.rect["y"] + match.rect["h"] * 2 // 3
    _xdotool(["mousemove", str(cx), str(cy)])
    time.sleep(0.4)
    _xdotool(["click", "3"])
    time.sleep(1.5)
    return {"clicked": True, "window": match.name, "at": [cx, cy]}


def _screenshot_delta(first: Path, second: Path) -> Dict[str, Any]:
    """How much the screen changed between two captures seconds apart."""
    try:
        import numpy as np
        from PIL import Image
    except Exception as exc:  # pragma: no cover - probe convenience
        return {"error": str(exc)}
    a = np.asarray(Image.open(first).convert("RGB")).astype(int)
    b = np.asarray(Image.open(second).convert("RGB")).astype(int)
    if a.shape != b.shape:
        return {"error": "different sizes"}
    diff = np.abs(a - b).max(axis=2)
    return {
        "max_channel_diff": int(diff.max()),
        "changed_pixel_fraction": float((diff > 2).mean()),
    }


def _role_histogram(nodes: List[Dict[str, Any]]) -> Dict[str, int]:
    hist: Dict[str, int] = {}
    for node in nodes:
        hist[node["role"]] = hist.get(node["role"], 0) + 1
    return dict(sorted(hist.items(), key=lambda kv: -kv[1]))


def _compositor_state() -> Dict[str, Any]:
    """Which compositor, if any, owns the screen."""
    state: Dict[str, Any] = {}
    try:
        procs = subprocess.run(
            ["ps", "-u", os.environ.get("USER", ""), "-o", "pid,comm,args", "--no-headers"],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout
    except Exception:
        procs = ""
    display = os.environ.get("DISPLAY", "")
    state["picom_running"] = any(
        line.split()[1] == "picom" for line in procs.splitlines() if len(line.split()) > 1
    )
    state["display"] = display
    try:
        from deskshot.automation.app_launcher import find_binary

        out = subprocess.run(
            [find_binary("xdpyinfo")], capture_output=True, text=True, timeout=20
        ).stdout
        state["composite_extension"] = "Composite" in out
    except Exception:
        state["composite_extension"] = None
    return state


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
