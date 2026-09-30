#!/usr/bin/env python
"""Can dialog widgets actually be set, and do settings interfere?

Two questions block the dialog-descent design, and both are empirical:

*Are the widgets drivable?* A dialog's check boxes carry real `checked` state -
unlike menu items on this stack - which is what makes them usable as verifiable
goals. But the page tabs and the spin button came back `actionable=False` in a
captured Preferences dialog. If tabs cannot be clicked, the settings on the other
panels are unreachable and the reachable goal set is a third of what it looks.

*Do settings interfere?* A conjunctive goal is only a sound task if setting the
fourth thing does not undo the first. Nothing guarantees that in general - a
"reset to defaults" or a mutually exclusive pair would break it - so it has to be
measured rather than assumed.

Everything here runs against a live session and reports what happened, including
when the answer is no.

Usage:
    PYTHONPATH=src python scripts/verify_dialog_widgets.py \
        --output incremental_checks/vXXX/verify --display-number 850
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from deskshot.config import CONFIGS_DIR, PipelineConfig  # noqa: E402
from deskshot.generation.actions import SceneAction  # noqa: E402
from deskshot.generation.predicates import ElementMatch, element_text  # noqa: E402
from deskshot.generation.scene_composer import DesktopEnv, compose_scene  # noqa: E402
from deskshot.generation.skills import OpenMenu, _pick  # noqa: E402

WIDGET_ROLES = ("check box", "radio button", "page tab", "spin button", "combo box", "slider")


def _widgets(state: List[Dict[str, Any]], app: str) -> List[Dict[str, Any]]:
    out = []
    for elem in state:
        if elem.get("app_name") != app:
            continue
        role = str(elem.get("role") or "").strip().lower()
        if role not in WIDGET_ROLES:
            continue
        it = elem.get("interaction") or {}
        out.append({
            "role": role,
            "label": element_text(elem),
            "uid": elem.get("uid"),
            "actionable": bool(it.get("actionable")),
            "has_click_point": it.get("click_point") is not None,
            "checked": it.get("checked"),
            "rect": elem.get("rect"),
        })
    return out


def _find(state: List[Dict[str, Any]], app: str, role: str, label: str) -> Optional[Dict[str, Any]]:
    for w in _widgets(state, app):
        if w["role"] == role and w["label"] == label:
            return w
    return None


def _click(env: Any, state: List[Dict[str, Any]], uid: str) -> None:
    env.act(SceneAction(type="click", target_uid=uid), state)


def open_preferences(env: Any, app: str) -> List[Dict[str, Any]]:
    """Edit > Preferences..., verified by the dialog appearing."""
    state = env.peek()
    skill = OpenMenu.build("Edit", app_name=app, expect_item="Preferences...")
    action = skill.next_action(state)
    if action is None:
        raise RuntimeError("no Edit menu")
    env.act(action, state)
    state = env.peek()
    item = _pick(state, ElementMatch(role_any=("menu item",), text="Preferences...", app_name=app))
    if item is None:
        raise RuntimeError("no Preferences item")
    env.act(SceneAction(type="click", target_uid=item.get("uid")), state)
    return env.peek()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", required=True)
    ap.add_argument("--display-number", type=int, default=850)
    ap.add_argument("--app", default="mousepad")
    ap.add_argument("--seed", type=int, default=991003)
    args = ap.parse_args()

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    config_path = CONFIGS_DIR / "pipeline.yaml"
    config = PipelineConfig.from_yaml(config_path) if config_path.exists() else PipelineConfig()
    config.session.display.display_number = args.display_number
    scene = compose_scene(args.seed, available_apps=[args.app, "gnome-calculator"])

    report: Dict[str, Any] = {"app": args.app, "checks": {}}

    with DesktopEnv(config, output_dir=out, include_desktop_chrome=False) as env:
        env.reset(scene)
        state = open_preferences(env, args.app)
        widgets = _widgets(state, args.app)
        report["widgets"] = widgets
        print(f"\nPreferences exposes {len(widgets)} widget(s):")
        for w in widgets:
            print(f"  {w['role']:12} {w['label'][:34]:36} actionable={w['actionable']!s:5}"
                  f" click_point={w['has_click_point']!s:5} checked={w['checked']}")

        # --- 1. is a check box drivable, and does its state follow? -----------
        box = next((w for w in widgets if w["role"] == "check box"), None)
        if box:
            before = box["checked"]
            _click(env, state, box["uid"])
            state = env.peek()
            after = (_find(state, args.app, "check box", box["label"]) or {}).get("checked")
            report["checks"]["checkbox_toggles"] = {
                "label": box["label"], "before": before, "after": after,
                "ok": before is not None and after is not None and before != after,
            }
            print(f"\ncheck box '{box['label']}': {before} -> {after}")

        # --- 2. can a page tab be clicked, and does the panel change? ---------
        tabs = [w for w in widgets if w["role"] == "page tab"]
        if len(tabs) > 1:
            before_labels = {w["label"] for w in _widgets(env.peek(), args.app)}
            target = tabs[1]
            _click(env, state, target["uid"])
            state = env.peek()
            after_labels = {w["label"] for w in _widgets(state, args.app)}
            report["checks"]["page_tab_switches"] = {
                "tab": target["label"],
                "actionable": target["actionable"],
                "widgets_before": len(before_labels),
                "widgets_after": len(after_labels),
                "new_widgets": sorted(after_labels - before_labels)[:12],
                "ok": bool(after_labels - before_labels),
            }
            print(f"\npage tab '{target['label']}' (actionable={target['actionable']}): "
                  f"{len(before_labels)} -> {len(after_labels)} widgets, "
                  f"new={sorted(after_labels - before_labels)[:6]}")

        # --- 3. does a conjunction of settings hold together? -----------------
        # Back to the first tab, then set several boxes and check them all at
        # the end rather than one at a time - interference is precisely what a
        # per-setting check would miss.
        if tabs:
            _click(env, state, tabs[0]["uid"])
            state = env.peek()
        boxes = [w for w in _widgets(state, args.app) if w["role"] == "check box"][:4]
        wanted = {}
        for b in boxes:
            fresh = _find(env.peek(), args.app, "check box", b["label"])
            if fresh is None or fresh["checked"] is None:
                continue
            _click(env, env.peek(), fresh["uid"])
            wanted[b["label"]] = not fresh["checked"]

        final = env.peek()
        got = {lbl: (_find(final, args.app, "check box", lbl) or {}).get("checked")
               for lbl in wanted}
        held = {lbl: wanted[lbl] == got[lbl] for lbl in wanted}
        report["checks"]["conjunction_holds"] = {
            "wanted": wanted, "got": got, "per_setting": held,
            "ok": bool(wanted) and all(held.values()),
        }
        print("\nconjunction of settings:")
        for lbl in wanted:
            mark = "ok " if held[lbl] else "BROKEN"
            print(f"  {mark} {lbl[:40]:42} wanted={wanted[lbl]} got={got[lbl]}")

    (out / "dialog_widgets.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    ok = all(c.get("ok") for c in report["checks"].values())
    print(f"\nall checks passed: {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
