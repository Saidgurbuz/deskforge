#!/usr/bin/env python
"""What can each app in the pool actually be asked to do?

Settings were the first task type because a check box is the easiest thing to
state a goal about: its state is readable, and the route to it is unique. But
that is a property of *state-bearing widgets*, not of settings, and most apps
carry many more kinds - text entries, combo boxes, spin buttons, page tabs, list
rows. Each is a goal the pipeline can express and verify with no per-app code.

So this counts the whole inventory rather than only check boxes, per app:

*surface*    - menus and items, the navigation the app exposes at all
*dialogs*    - items ending in an ellipsis, each a reachable sub-screen
*stateful*   - check boxes, radio buttons, page tabs: goals of the form "put this
               in that state", reachable in one click
*valued*     - entries, combo boxes, spin buttons: goals of the form "make this
               read X", reachable by click-and-type or click-and-pick
*content*    - editable documents, where the goal is the text itself

An app scoring zero on every count cannot be given a verifiable task at all, and
is better excluded from episode collection than silently producing failures.

Apps are probed two per session because scene composition needs at least two, and
each is focused before being read - a background window advertises its menu bar
but does not open it.

Usage:
    PYTHONPATH=src python scripts/sweep_app_affordances.py \
        --output incremental_checks/vXXX/sweep --display-number 900
"""

from __future__ import annotations

import argparse
import json
import sys
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from deskshot.config import CONFIGS_DIR, PipelineConfig  # noqa: E402
from deskshot.generation.predicates import element_text  # noqa: E402
from deskshot.generation.scene_composer import (  # noqa: E402
    DEFAULT_SCENE_APP_POOL,
    DesktopEnv,
    compose_scene,
)
from deskshot.generation.task_builders import (  # noqa: E402
    build_units,
    probe_dialog_settings,
    probe_menus,
)

STATEFUL_ROLES = {"check box", "radio button", "check menu item", "radio menu item", "page tab"}
VALUED_ROLES = {"combo box", "spin button", "slider"}
ENTRY_ROLES = {"entry", "password text", "text"}


def _inventory(state: List[Dict[str, Any]], app: str) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    editable = 0
    for elem in state:
        if elem.get("app_name") != app:
            continue
        role = str(elem.get("role") or "").strip().lower()
        counts[role] = counts.get(role, 0) + 1
        if role in ENTRY_ROLES and (elem.get("interaction") or {}).get("editable"):
            editable += 1
    return {
        "roles": counts,
        "stateful": sum(counts.get(r, 0) for r in STATEFUL_ROLES),
        "valued": sum(counts.get(r, 0) for r in VALUED_ROLES),
        "editable_text": editable,
    }


def probe_app(env: Any, app: str, *, max_dialogs: int) -> Dict[str, Any]:
    roles: Dict[str, str] = {}
    menus = probe_menus(env, app_name=app, roles_out=roles)
    units = build_units(menus, app_name=app)

    settings: List[Dict[str, Any]] = []
    dialogs_opened = 0
    for unit in units[:max_dialogs]:
        found = probe_dialog_settings(
            env, menu=unit["menu"], item=unit["item"], app_name=app
        )
        if found:
            dialogs_opened += 1
        settings.extend(found)

    base = _inventory(env.peek(), app)
    return {
        "app": app,
        "num_menus": len(menus),
        "num_items": sum(len(v) for v in menus.values()),
        "num_dialog_units": len(units),
        "dialogs_with_settings": dialogs_opened,
        "num_settings": len(settings),
        "settings_tabs": sorted({str(s["tab"]) for s in settings}),
        "base_inventory": base,
        "menus": menus,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", required=True)
    ap.add_argument("--display-number", type=int, default=900)
    ap.add_argument("--apps", default=None, help="Comma-separated; default is the pool")
    ap.add_argument("--max-dialogs", type=int, default=4)
    ap.add_argument("--seed", type=int, default=991003)
    ap.add_argument("--single", action="store_true",
                    help="Probe exactly --apps in this process; used by the parent sweep")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    pool = args.apps.split(",") if args.apps else sorted(DEFAULT_SCENE_APP_POOL)

    config_path = CONFIGS_DIR / "pipeline.yaml"
    config = PipelineConfig.from_yaml(config_path) if config_path.exists() else PipelineConfig()

    if args.single:
        return _probe_pair_here(pool, config, args)

    # One process per pair. Sharing a process across pairs left the previous
    # session's D-Bus and AT-SPI registry half-alive, and every pair after the
    # first lost its apps to "did not appear in AT-SPI tree within 90s" - 16
    # launch timeouts in one run, while the same apps probe fine on their own.
    # `scene-batch` isolates scenes for exactly this reason; this follows it.
    results: List[Dict[str, Any]] = []
    pairs = [pool[i:i + 2] for i in range(0, len(pool), 2)]
    display = args.display_number

    for index, pair in enumerate(pairs):
        if len(pair) < 2:
            pair = pair + [p for p in pool if p not in pair][:1]
        display += 2
        part = out / f"part_{index}.json"
        cmd = [
            sys.executable, str(Path(__file__).resolve()),
            "--single", "--apps", ",".join(pair),
            "--output", str(out), "--display-number", str(display),
            "--max-dialogs", str(args.max_dialogs), "--seed", str(args.seed),
        ]
        print(f"\n=== {pair} (display {display})", flush=True)
        env_vars = {**os.environ, "DESKSHOT_SWEEP_PART": str(index)}
        try:
            subprocess.run(cmd, cwd=str(PROJECT_ROOT), timeout=args.timeout,
                           env=env_vars, check=False)
        except subprocess.TimeoutExpired:
            print(f"  pair {pair} timed out after {args.timeout}s", flush=True)
        if part.is_file():
            results.extend(json.loads(part.read_text(encoding="utf-8")))
        else:
            for app in pair:
                results.append({"app": app, "error": "pair produced no result"})
        for r in results[-2:]:
            if "error" in r:
                print(f"  {r['app']:22} ERROR {r['error'][:56]}", flush=True)
            else:
                inv = r["base_inventory"]
                print(f"  {r['app']:22} menus={r['num_menus']:2} items={r['num_items']:3}"
                      f" dialogs={r['num_dialog_units']:2} settings={r['num_settings']:3}"
                      f" stateful={inv['stateful']:3} valued={inv['valued']:2}"
                      f" text={inv['editable_text']:2}", flush=True)
        (out / "affordances.json").write_text(
            json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    return _report(results)


def _probe_pair_here(pool, config, args) -> int:
    """Probe one pair in this process and write its part file."""
    out = Path(args.output)
    results: List[Dict[str, Any]] = []
    config.session.display.display_number = args.display_number
    try:
        scene = compose_scene(args.seed, available_apps=list(pool))
        with DesktopEnv(config, output_dir=out / "scratch",
                        include_desktop_chrome=False) as env:
            if env.reset(scene) is None:
                raise RuntimeError("no initial observation")
            for app in [a.app_name for a in scene.apps]:
                try:
                    results.append(probe_app(env, app, max_dialogs=args.max_dialogs))
                except Exception as exc:
                    results.append({"app": app, "error": f"{type(exc).__name__}: {exc}"})
    except Exception as exc:
        for app in pool:
            results.append({"app": app, "error": f"{type(exc).__name__}: {exc}"})
    part = out / f"part_{os.environ.get('DESKSHOT_SWEEP_PART', '00')}.json"
    part.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


def _report(results: List[Dict[str, Any]]) -> int:
    usable = [r for r in results if r.get("num_settings")]
    print(f"\n{len(usable)}/{len(results)} app(s) expose settable widgets")
    for r in sorted(results, key=lambda r: -(r.get("num_settings") or 0)):
        if "error" in r:
            continue
        inv = r["base_inventory"]
        print(f"  {r['app']:22} settings={r['num_settings']:3} dialogs={r['num_dialog_units']:2}"
              f" items={r['num_items']:3} stateful={inv['stateful']:3}"
              f" valued={inv['valued']:2} text={inv['editable_text']:2}")
    failed = [r["app"] for r in results if "error" in r]
    if failed:
        print(f"\nfailed: {', '.join(failed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
