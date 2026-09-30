"""CLI entry point: dsd setup|install-styles|validate|run|audit|viz|themes

Usage:
    dsd setup              Download + extract RPMs (one-time)
    dsd install-styles     Install richer style packs (best effort)
    dsd validate           Test Xvfb + D-Bus + AT-SPI + health checks
    dsd run                Run extraction pipeline
    dsd audit              Qualify candidate apps for dense AT-SPI extraction
    dsd viz                Visualize annotations
    dsd themes             List available themes and wallpapers
"""

from __future__ import annotations



import argparse
from dataclasses import asdict
import json
import logging
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from deskshot.config import PipelineConfig, CONFIGS_DIR, PROJECT_ROOT


if TYPE_CHECKING:  # referenced only from quoted annotations
    import PIL.Image

PRESET_CHOICES = [
    "linux_classic",
    "windows_redmond",
    "macos_tahoe_like",
    "quartz_night",
    "ubuntu_like",
]

DESKTOP_PROFILE_CHOICES = ["sparse", "balanced", "dense"]


def _scene_batch_result_summary_line(row: dict[str, object] | None) -> str:
    if row is None:
        return "  status=failed"
    stem = str(row.get("stem") or "")
    if not stem:
        return "  status=completed_missing_meta"
    num_elements = row.get("num_elements_filtered")
    timing = row.get("scene_timing")
    suffix = ""
    if isinstance(timing, dict) and timing.get("scene_wall_sec") is not None:
        suffix = f" scene_sec={timing['scene_wall_sec']}"
    if num_elements is None:
        return f"  stem={stem} num_elements=unknown{suffix}"
    return f"  stem={stem} num_elements={num_elements}{suffix}"


def _scene_group_alive(pgid: int) -> bool:
    """True while any process remains in the scene's process group."""
    try:
        os.killpg(pgid, 0)
        return True
    except OSError:
        return False


def _terminate_scene_process(proc: "subprocess.Popen[bytes]") -> None:
    """Kill a scene subprocess and its whole session.

    Scenes spawn Xvfb, D-Bus, AT-SPI and app processes. Killing only the direct
    child would orphan those, so signal the process group. SIGINT first lets
    DesktopSession unwind and clean up its own children.

    The direct child exiting is not proof the group is clear: Xvfb in particular
    can outlive it and keep holding the display number, which breaks any later
    scene assigned that display. So escalate until the group is actually empty.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        return

    for sig, grace in ((signal.SIGINT, 15.0), (signal.SIGTERM, 10.0), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(pgid, sig)
        except OSError:
            break
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            # Reap the child first; a zombie still counts as a group member.
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
            if not _scene_group_alive(pgid):
                return

    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    if _scene_group_alive(pgid):
        logging.warning("[scene-batch] processes survived cleanup in group %s", pgid)


def _persist_scene_wall_clock(row: dict | None, wall_sec: float) -> dict | None:
    """Record end-to-end per-scene wall clock into the row and its meta file.

    Measured around the whole scene run so it includes session startup and
    teardown, which is the number job sizing and cost-per-sample need. Both the
    subprocess and persistent-worker paths go through here so parallel runs are
    not silently missing it.
    """
    if not isinstance(row, dict):
        return row
    timing = row.get("scene_timing")
    row["scene_timing"] = {
        **(timing if isinstance(timing, dict) else {}),
        "scene_wall_sec": round(wall_sec, 3),
    }
    meta_path = row.get("meta")
    if isinstance(meta_path, str) and meta_path:
        try:
            Path(meta_path).write_text(
                json.dumps(row, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            logging.warning("Could not persist scene timing to %s", meta_path)
    return row


def _scene_batch_meta_row(row: dict[str, object] | None) -> dict[str, object] | None:
    if row is None or row.get("num_elements_filtered") is not None:
        return row
    meta_path = row.get("meta")
    if not isinstance(meta_path, str) or not meta_path:
        return row
    try:
        return json.loads(Path(meta_path).read_text(encoding="utf-8"))
    except Exception:
        logging.warning("Could not load scene-batch meta row from %s", meta_path, exc_info=True)
        return row


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


# ── Commands ─────────────────────────────────────────────────────────────

def cmd_setup(args: argparse.Namespace) -> int:
    """Download and extract RPMs."""
    from deskshot.environment.setup import run_setup

    tools_dir = Path(args.tools_dir) if args.tools_dir else None
    run_setup(tools_dir)
    return 0


def cmd_install_styles(args: argparse.Namespace) -> int:
    """Install richer style packs into extracted theme/icon dirs."""
    from deskshot.config import TOOLS_DIR, EXTRACTED_DIR
    from deskshot.environment.stylepacks import install_style_packs

    tools_dir = Path(args.tools_dir) if args.tools_dir else TOOLS_DIR
    extracted_dir = tools_dir / "extracted" if args.tools_dir else EXTRACTED_DIR

    summary = install_style_packs(
        extracted_dir=extracted_dir,
        cache_root=tools_dir / "stylepacks",
        refresh=args.refresh,
    )

    print(f"Installed themes: {', '.join(summary['themes']) or '(none)'}")
    print(f"Installed icons:  {', '.join(summary['icons']) or '(none)'}")
    print(f"Wallpapers:       {', '.join(summary['wallpapers']) or '(none)'}")
    if summary["warnings"]:
        print(f"Warnings: {', '.join(summary['warnings'])}")
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    """Validate compute-node binary portability."""
    from deskshot.preflight import (
        format_preflight_results,
        preflight_failed,
        preflight_results_to_json,
        run_app_binary_preflight,
    )

    app_names = args.apps.split(",") if args.apps else None
    config_dir = Path(args.tools_dir) / "../configs/apps" if args.tools_dir else CONFIGS_DIR / "apps"
    results = run_app_binary_preflight(
        app_names,
        Path(config_dir).resolve(),
        strict_compute=args.strict_compute,
        include_session_binaries=not args.no_session_binaries,
    )
    print(preflight_results_to_json(results) if args.json else format_preflight_results(results))
    return 1 if preflight_failed(results) else 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate desktop session (Xvfb + D-Bus + AT-SPI)."""
    from deskshot.environment.session import DesktopSession
    from deskshot.environment.health import run_all_checks, print_health_report
    from deskshot.config import SessionConfig

    config = SessionConfig()
    if args.tools_dir:
        config.tools_dir = Path(args.tools_dir)
    if args.desktop_env:
        config.desktop_env = args.desktop_env

    desktop_env = config.desktop_env
    print(f"Starting desktop session (desktop_env={desktop_env})...")
    try:
        with DesktopSession(config) as session:
            print(f"Session on display {session.display}")
            print("\nHealth checks:")
            checks = run_all_checks(
                desktop_env=desktop_env,
                tracked_pids=session.tracked_pids,
            )
            all_ok = print_health_report(checks)

            if not all_ok:
                print("\nSome checks failed.")
                return 1

            print("\nAll checks passed!")
            return 0

    except Exception as e:
        print(f"\nSession failed: {e}")
        return 1


def cmd_themes(args: argparse.Namespace) -> int:
    """List available themes and wallpapers."""
    from deskshot.environment.themes import (
        list_theme_presets,
        list_available_gtk_themes,
        list_available_icon_themes,
        list_available_wm_themes,
        list_available_wallpapers,
    )
    from deskshot.environment.diversity import (
        DESKTOP_CONTENT_PACKS,
        DESKTOP_LAYOUT_TEMPLATES,
        PANEL_VARIANTS,
        list_display_presets,
    )
    from deskshot.environment.wallpaper_catalog import list_wallpaper_catalog

    print("── Theme Presets ──")
    presets = list_theme_presets()
    for name, cfg in presets.items():
        print(f"  {name}: style={cfg['desktop_style']}, "
              f"gtk={cfg['gtk_theme']}, icon={cfg['icon_theme']}, wm={cfg['wm_theme']}")

    print("── GTK Themes ──")
    for t in list_available_gtk_themes() or ["(none found)"]:
        print(f"  {t}")

    print("\n── Icon Themes ──")
    for t in list_available_icon_themes() or ["(none found)"]:
        print(f"  {t}")

    print("\n── WM Themes (xfwm4) ──")
    for t in list_available_wm_themes() or ["(none found)"]:
        print(f"  {t}")

    print("\n── Wallpapers ──")
    wallpapers = list_available_wallpapers()
    print(f"  total: {len(wallpapers)}")
    if wallpapers:
        for w in wallpapers:
            print(f"  {w}")
    else:
        print("  (none found)")

    catalog = list_wallpaper_catalog()
    if catalog:
        by_kind: dict[str, int] = {}
        for entry in catalog:
            by_kind[entry["kind"]] = by_kind.get(entry["kind"], 0) + 1
        print(f"  by kind: {by_kind}")

    print("\n── Display Presets ──")
    for name, preset in list_display_presets().items():
        print(f"  {name}: {preset['width']}x{preset['height']}")

    print("\n── Panel Variants ──")
    for name in PANEL_VARIANTS:
        print(f"  {name}")

    print("\n── Desktop Layout Templates ──")
    for name in DESKTOP_LAYOUT_TEMPLATES:
        print(f"  {name}")

    print("\n── Desktop Content Packs ──")
    for name in DESKTOP_CONTENT_PACKS:
        print(f"  {name}")

    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Run extraction pipeline."""
    from deskshot.pipeline.orchestrator import run_pipeline
    from deskshot.environment.themes import get_theme_preset
    from deskshot.environment.diversity import get_display_preset

    # Load config
    config_path = CONFIGS_DIR / "pipeline.yaml"
    if config_path.exists():
        config = PipelineConfig.from_yaml(config_path)
    else:
        config = PipelineConfig()

    if args.tools_dir:
        config.session.tools_dir = Path(args.tools_dir)
    if args.output:
        config.output_dir = Path(args.output)

    # CLI overrides for desktop env and theme
    if args.desktop_env:
        config.session.desktop_env = args.desktop_env
    if args.preset:
        config.session.theme = get_theme_preset(args.preset, resolve=False)
    if args.theme:
        config.session.theme.gtk_theme = args.theme
    if args.icon_theme:
        config.session.theme.icon_theme = args.icon_theme
    if args.wm_theme:
        config.session.theme.wm_theme = args.wm_theme
    if args.wallpaper:
        config.session.theme.wallpaper = args.wallpaper
    if args.wallpaper_seed is not None:
        config.session.theme.wallpaper_seed = args.wallpaper_seed
    if args.panel_variant:
        config.session.theme.panel_variant = args.panel_variant
    if args.display_preset:
        preset = get_display_preset(
            args.display_preset,
            display_number=config.session.display.display_number,
            depth=config.session.display.depth,
        )
        config.session.display.width = preset.width
        config.session.display.height = preset.height
    if args.width:
        config.session.display.width = args.width
    if args.height:
        config.session.display.height = args.height
    if args.desktop_seed is not None:
        config.session.desktop_fixture.seed = args.desktop_seed
    if args.desktop_profile:
        config.session.desktop_fixture.profile = args.desktop_profile

    # Parse app names
    app_names = args.apps.split(",") if args.apps else None
    interaction_names = args.interactions.split(",") if args.interactions else None

    results = run_pipeline(
        config,
        app_names=app_names,
        interaction_names=interaction_names,
        output_dir=config.output_dir,
        include_desktop_chrome=args.include_chrome,
    )

    print(f"\nExtracted {len(results)} samples")
    for r in results:
        print(f"  {r['stem']} ({r['num_elements']} elements)")

    return 0 if results else 1


def cmd_scene(args: argparse.Namespace) -> int:
    """Compose and optionally run one mixed-app scene."""
    from deskshot.generation.scene_composer import compose_scene, run_scene_extraction

    config_path = CONFIGS_DIR / "pipeline.yaml"
    if config_path.exists():
        config = PipelineConfig.from_yaml(config_path)
    else:
        config = PipelineConfig()

    if args.tools_dir:
        config.session.tools_dir = Path(args.tools_dir)
    if args.display_number is not None:
        config.session.display.display_number = int(args.display_number)

    app_pool = args.apps.split(",") if args.apps else None
    scene = compose_scene(args.seed, available_apps=app_pool)
    print(json.dumps(asdict(scene), indent=2))

    if args.describe_only:
        return 0

    output_dir = Path(args.output) if args.output else config.output_dir
    result = run_scene_extraction(
        scene,
        config,
        output_dir=output_dir,
        include_desktop_chrome=args.include_chrome,
    )
    if not result:
        return 1

    print(f"\nSaved scene capture: {result['stem']} ({result['num_elements']} elements)")
    return 0



def cmd_scene_task(args: argparse.Namespace) -> int:
    """Run one goal-directed task and save its verified trajectory.

    The app is probed for its menus, a plan of the requested horizon is composed
    from what it offers, and every step is checked against its own postcondition
    as it runs. A failure is reported with the skill that failed rather than
    written out as if it had worked.
    """
    from deskshot.generation.scene_composer import (
        DesktopEnv,
        compose_scene,
        DEFAULT_SCENE_APP_POOL,
    )
    from deskshot.generation.task_builders import (
        build_units,
        discover_effects,
        opens_dialog,
        plan_long_task,
        affordance_task,
        discover_affordances,
        plan_cross_app_task,
        plan_settings_task,
        probe_dialog_settings,
        probe_menus,
    )
    from deskshot.generation.tasks import run_task

    config_path = CONFIGS_DIR / "pipeline.yaml"
    config = PipelineConfig.from_yaml(config_path) if config_path.exists() else PipelineConfig()
    if args.tools_dir:
        config.session.tools_dir = Path(args.tools_dir)
    if args.display_number is not None:
        config.session.display.display_number = args.display_number

    app_pool = args.apps.split(",") if args.apps else list(DEFAULT_SCENE_APP_POOL)
    scene = compose_scene(args.seed, available_apps=app_pool)
    target_app = args.target_app or scene.apps[0].app_name

    output_dir = Path(args.output) if args.output else config.output_dir
    output_dir = output_dir / f"task-{scene.scene_id}"
    output_dir.mkdir(parents=True, exist_ok=True)

    with DesktopEnv(
        config, output_dir=output_dir, include_desktop_chrome=args.include_chrome
    ) as env:
        if env.reset(scene) is None:
            print("Task failed: initial observation returned nothing")
            return 1

        # Menu probing is skipped for modes that never use a menu. It is not
        # only wasted minutes: effect probing activates menu items, and a GTK
        # menu left open holds a *pointer grab*, so the next xdotool mousemove
        # blocks until it times out. That is what made every affordance run die
        # at step 0 with "mousemove --sync 882 214 timed out after 10.0s" - the
        # coordinate was fine, the pointer was simply not free to move.
        needs_menus = args.mode in ("tour", "settings") or args.probe_only
        item_roles: dict[str, str] = {}
        menu_map = probe_menus(
            env, app_name=target_app, max_menus=args.max_menus, roles_out=item_roles
        ) if needs_menus else {}
        # Probing moved through the app's menus without observing, so the last
        # observation predates it. The task is planned and verified against the
        # state as it is now, not as it was before the probe.
        env.observe()
        units = build_units(menu_map, app_name=target_app)
        effects = (
            discover_effects(
                env, menu_map, app_name=target_app, max_probes=args.max_effect_probes
            )
            if args.max_effect_probes and needs_menus
            else []
        )
        probe = {
            "app_name": target_app,
            "menus": menu_map,
            "num_units": len(units),
            "units": units,
            "item_roles": item_roles,
            "effects": effects,
        }
        (output_dir / "probe.json").write_text(
            json.dumps(probe, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        if needs_menus:
            print(
                f"Probed {target_app}: {len(menu_map)} menu(s), {len(units)} usable "
                f"unit(s), {len(effects)} measured effect(s)"
            )
        for menu, items in menu_map.items():
            print(f"  {menu}: {len(items)} item(s)")
        for eff in effects:
            print(f"  effect: {eff['menu']}/{eff['item']} -> {eff['predicate']['kind']} "
                  f"{eff['predicate']['match']}")
        if args.probe_only:
            return 0

        if args.mode == "affordance":
            # No menu navigation at all: whatever state-bearing widgets are on
            # screen become the goal. This is the path for apps with a header bar
            # instead of a menu bar, where menu-based discovery finds nothing.
            # From the observation, not a peek: a peeked tree has no occlusion,
            # so its click points can sit under whatever window is covering the
            # widget, and the pointer move lands on the wrong app. The
            # observation's click points are the occlusion-aware ones.
            env.observe()
            found = discover_affordances(env.last_elements, app_name=target_app)
            (output_dir / "affordances.json").write_text(
                json.dumps([a.to_dict() for a in found], indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            kinds: dict[str, int] = {}
            for a in found:
                kinds[a.kind] = kinds.get(a.kind, 0) + 1
            print(f"{target_app}: {len(found)} affordance(s) on screen {kinds}")
            task = affordance_task(
                found[: args.num_settings], task_id=f"{target_app}-affordance-{args.seed}",
                app_name=target_app,
            )
            if task is None:
                print(f"No affordance task for {target_app}: nothing stateful on screen")
                return 1
        elif args.mode == "cross-app":
            per_app: dict[str, list] = {}
            probes: dict[str, dict] = {}
            for app in [a.app_name for a in scene.apps]:
                menus = probe_menus(env, app_name=app, max_menus=args.max_menus)
                found: list = []
                for unit in build_units(menus, app_name=app):
                    found += probe_dialog_settings(
                        env, menu=unit["menu"], item=unit["item"], app_name=app
                    )
                    if found:
                        break
                per_app[app] = found
                probes[app] = {"menus": menus, "units": build_units(menus, app_name=app)}
                print(f"  {app}: {len(found)} settable check box(es) "
                      f"from {len(probes[app]['units'])} dialog(s)")
            (output_dir / "settings.json").write_text(
                json.dumps({"settings": per_app, "probes": probes}, indent=2,
                           ensure_ascii=False),
                encoding="utf-8",
            )
            task = plan_cross_app_task(
                per_app, seed=args.seed, settings_per_app=args.num_settings
            )
            if task is None:
                print("No cross-app task: fewer than two apps offered settings")
                return 1
            env.observe()
        elif args.mode == "settings":
            settings: list[dict] = []
            for unit in units:
                if not opens_dialog(unit["item"]):
                    continue
                settings += probe_dialog_settings(
                    env, menu=unit["menu"], item=unit["item"], app_name=target_app
                )
                if len(settings) >= args.num_settings * 2:
                    break
            (output_dir / "settings.json").write_text(
                json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(f"Found {len(settings)} settable check box(es) across "
                  f"{len({s['dialog'] for s in settings})} dialog(s)")
            task = plan_settings_task(
                settings, app_name=target_app, seed=args.seed,
                num_settings=args.num_settings,
            )
            if task is None:
                print(f"No settings task could be built for {target_app}")
                return 1
            env.observe()
        else:
            task = plan_long_task(
                menu_map,
                app_name=target_app,
                target_steps=args.target_steps,
                seed=args.seed,
                effects=effects,
            )
        if task is None:
            print(f"No task could be built for {target_app}: nothing safe to act on")
            return 1
        print(f"Task {task.task_id}: {len(task.skills)} skills, up to {task.max_steps} steps")
        print(f"  {task.instruction}")

        def _log(record) -> None:
            mark = "ok " if record.subgoal_met else "..."
            print(
                f"  [{record.step_index:3}] {mark} {record.intent[:58]:60}"
                f" progress={record.goal_progress:.2f} changed={record.changed}"
            )

        result = run_task(task, env, on_step=_log)

    (output_dir / "task.json").write_text(
        json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    verified = sum(1 for s in result.steps if s.subgoal_met)
    effective = sum(1 for s in result.steps if s.changed)
    print(
        f"\n{result.status}: {len(result.steps)} step(s), "
        f"{verified} subgoal(s) reached, {effective} changed the screen -> {output_dir}"
    )
    if result.reason:
        print(f"  reason: {result.reason}")
    return 0 if result.succeeded else 1


def cmd_scene_episode(args: argparse.Namespace) -> int:
    """Capture a scene as an episode of observations with actions between them.

    --steps 0 is a plain static capture, identical to `scene`; the episode path
    is the same code, just observed more than once.
    """
    from deskshot.generation.actions import SceneAction, sample_actionable_targets
    from deskshot.generation.scene_composer import (
        DesktopEnv,
        compose_scene,
        DEFAULT_SCENE_APP_POOL,
    )

    config_path = CONFIGS_DIR / "pipeline.yaml"
    config = PipelineConfig.from_yaml(config_path) if config_path.exists() else PipelineConfig()
    if args.tools_dir:
        config.session.tools_dir = Path(args.tools_dir)
    app_pool = args.apps.split(",") if args.apps else list(DEFAULT_SCENE_APP_POOL)
    scene = compose_scene(args.seed, available_apps=app_pool)

    output_dir = Path(args.output) if args.output else config.output_dir
    if args.steps > 0:
        output_dir = output_dir / f"episode-{scene.scene_id}"
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.display_number is not None:
        config.session.display.display_number = args.display_number

    steps: list[dict[str, object]] = []
    rng = random.Random(args.seed)

    with DesktopEnv(
        config,
        output_dir=output_dir,
        include_desktop_chrome=args.include_chrome,
    ) as env:
        first = env.reset(scene)
        if first is None:
            print("Episode failed: initial observation returned nothing")
            return 1
        steps.append({"step_index": 0, "action": None, "observation_stem": first.get("stem")})

        for _ in range(args.steps):
            targets = sample_actionable_targets(env.last_elements, limit=args.candidate_pool)
            if not targets:
                logging.warning("[episode] no actionable targets left; stopping early")
                break
            target = rng.choice(targets)
            action = SceneAction(
                type="click",
                target_uid=target["uid"],
                metadata={"role": target.get("role"), "text": target.get("inner_text")},
            )
            try:
                result = env.step(action)
            except Exception:
                logging.exception("[episode] step failed")
                break
            observation = result.get("observation") or {}
            steps.append(
                {
                    "step_index": result.get("step_index"),
                    "action": result.get("action"),
                    "diff": result.get("diff"),
                    "observation_stem": observation.get("stem"),
                }
            )

    episode = {
        "scene_id": scene.scene_id,
        "seed": scene.seed,
        "requested_steps": args.steps,
        "captured_steps": len(steps),
        "scene": asdict(scene),
        "steps": steps,
    }
    (output_dir / "episode.json").write_text(
        json.dumps(episode, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Episode {scene.scene_id}: {len(steps)} observation(s) -> {output_dir}")
    for entry in steps:
        diff = entry.get("diff") or {}
        if diff:
            print(
                f"  step {entry['step_index']}: +{len(diff.get('appeared', []))}"
                f" -{len(diff.get('disappeared', []))}"
                f" moved={len(diff.get('moved', []))}"
                f" occluded={len(diff.get('newly_occluded', []))}"
                f" revealed={len(diff.get('revealed', []))}"
            )
    return 0




#: How long an episode runs, as shares of the episode population. A single fixed
#: length would make every trajectory the same shape, and long-horizon behaviour
#: is exactly what varies with length. Longer episodes are also *cheaper per
#: sample* - session setup is ~90s and a further frame is ~10s - so leaning long
#: costs nothing in throughput and buys the horizon.
EPISODE_LENGTH_TIERS = ((0.30, 2, 4), (0.40, 5, 9), (0.30, 10, None))

#: Seconds between one worker's desktop coming up and the next one's, so the
#: node's inotify instances are not all claimed in the same instant.
WORKER_START_STAGGER_SEC = 12


def _episode_step_count(rng, fraction: float, maximum: int) -> int:
    """Actions in one episode: 0 for a static capture, else a sampled length."""
    if rng.random() >= fraction:
        return 0
    roll = rng.random()
    cumulative = 0.0
    for share, low, high in EPISODE_LENGTH_TIERS:
        cumulative += share
        if roll <= cumulative:
            top = maximum if high is None else min(high, maximum)
            return max(1, rng.randint(min(low, top), max(1, top)))
    return max(1, maximum)


def _audit_one_capture(
    scene: object,
    row: Dict[str, Any],
    *,
    output_dir: Path,
    accepted_hashes: Dict[str, str],
    accepted_rows: List[Dict[str, Any]],
) -> None:
    """Judge one finished capture, and never let that judgement lose the capture.

    Auditing happens after a shard has run every one of its scenes - hours of
    compute that is already safely on disk. A bug here therefore destroys the
    most expensive thing in the run while adding nothing, which is exactly what
    happened: a `NameError` in the verdict writer killed all 64 shards of a
    52,000-scene run *after* they had finished capturing.

    So the audit is wrapped. Its results are worth having and it is worth fixing
    when it breaks, but it is derivable from files that already exist, and
    nothing derivable should be able to take down something that is not.
    """
    from deskshot.generation.scene_composer import evaluate_scene_capture

    try:
        audit = evaluate_scene_capture(
            scene, output_dir=output_dir, meta=row, accepted_hashes=accepted_hashes
        )
    except Exception:
        logging.exception(
            "[scene-batch] audit failed for %s; keeping the capture and continuing",
            row.get("stem"),
        )
        # Unjudged, not discarded. `scripts/audit_privacy.py` and `qa.py` can
        # both speak for it later, and a missing verdict is recoverable where a
        # lost shard is not.
        accepted_rows.append(row)
        return

    row["scene_batch_audit"] = audit
    try:
        _write_sample_verdict(output_dir, scene, row, audit)
    except Exception:
        logging.exception("[scene-batch] could not write verdict for %s", row.get("stem"))

    if audit.get("accepted"):
        accepted_hashes[str(row["stem"])] = str(audit["screenshot_hash"])
        accepted_rows.append(row)


def _write_sample_verdict(
    output_dir: Path, scene: object, row: Dict[str, Any], audit: Dict[str, Any]
) -> None:
    """Drop the accept/reject verdict next to the sample it describes.

    The verdict already exists inside `scene_batch_results.NofM.json`, but that
    file is one giant blob written when the shard finishes - so it is gone if the
    shard is killed, and reading it to answer "is this sample usable?" means
    parsing megabytes of per-scene role histograms. A few hundred bytes beside
    each capture is written as the capture is judged, survives a kill, and makes
    filtering a corpus a directory walk.

    Only 61% of scenes are accepted, and the largest single reason is
    `missing_apps` - an app that did not come up - which leaves a perfectly good
    screenshot with correct annotations, just fewer windows than planned. Whether
    those belong in a training set is a judgement for whoever uses the corpus,
    which is precisely why the reason is recorded rather than the files deleted.
    """
    stem = str(row.get("stem") or "")
    if not stem:
        return
    from deskshot.generation.scene_composer import _capture_dir_for

    here = _capture_dir_for(output_dir, row, stem)
    payload = {
        "stem": stem,
        "seed": getattr(scene, "seed", None),
        "scene_id": getattr(scene, "scene_id", ""),
        "accepted": bool(audit.get("accepted")),
        "status": audit.get("status"),
        "reasons": audit.get("reasons") or [],
        "missing_apps": audit.get("missing_apps") or [],
        "quality_ok": audit.get("quality_ok"),
        "failed_checks": [
            c.get("name") for c in (audit.get("quality_checks") or []) if not c.get("ok")
        ],
        "near_duplicate_of": audit.get("near_duplicate_of") or "",
        "screenshot_hash": audit.get("screenshot_hash") or "",
    }
    target = here / f"{stem}.verdict.json"
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        tmp.replace(target)
    except OSError:
        logging.warning("could not write verdict for %s", stem, exc_info=True)


def _index_captures(output_dir: Path) -> Dict[str, List[Path]]:
    """Every capture already on disk, grouped by scene id, from **one** walk.

    `_capture_looks_complete` used to rglob per planned scene. That is a full
    tree walk each time: a shard with 800 planned scenes and 650 captured ones
    meant millions of GPFS stat calls before a resumed shard did any work.
    Walking once and indexing turns resume from minutes into seconds, which
    matters because resume is the normal way a preempted shard restarts.
    """
    index: Dict[str, List[Path]] = {}
    for meta in output_dir.rglob("scene-*-step*.meta.json"):
        name = meta.name
        try:
            scene_id = name.split("scene-", 1)[1].split("-step", 1)[0]
        except IndexError:
            continue
        index.setdefault(scene_id, []).append(meta)
    return index


def _capture_looks_complete(
    output_dir: Path, scene_id: str, index: Optional[Dict[str, List[Path]]] = None
) -> bool:
    """Is this scene really finished, or did something die halfway through it?

    Presence of a meta file is not enough. A SIGKILL mid-write left a 0-byte
    `meta.json` in a real run, and trusting that would skip the scene forever
    while leaving the broken sample in the corpus. So every meta the scene wrote
    has to parse and name its own stem, and the screenshot it refers to has to
    exist.
    """
    metas = (
        index.get(scene_id, [])
        if index is not None
        else list(output_dir.rglob(f"scene-{scene_id}-*.meta.json"))
    )
    if not metas:
        return False
    for meta in metas:
        try:
            payload = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        stem = str(payload.get("stem") or "")
        if not stem or not (meta.parent / f"{stem}.png").is_file():
            return False
    return True


def _read_plan_shard(
    plan_path: Optional[str], shard: str
) -> Optional[List[tuple]]:
    """Seeds this worker owns, from a plan written by `scene-plan`.

    `shard` is `k/n`: worker k of n takes every n-th entry. Striding rather than
    slicing into contiguous blocks keeps every shard's mix of themes, sizes and
    apps representative, so a shard that dies does not remove a whole region of
    the corpus.
    """
    if not plan_path:
        return None
    entries = []
    with open(plan_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    index, total = 0, 1
    if shard:
        index_text, _, total_text = shard.partition("/")
        index, total = int(index_text), int(total_text or 1)
        if not 0 <= index < total:
            raise SystemExit(f"--shard {shard!r} is out of range")
    return [
        (
            int(row["seed"]),
            int(row.get("steps") or 0),
            # Absent in plans written before these were recorded; None and False
            # are exactly the defaults such a plan was composed with.
            (None if row.get("force_app_count") is None
             else int(row["force_app_count"])),
            bool(row.get("uniform_pages", False)),
        )
        for row in entries[index::total]
    ]


def cmd_scene_plan(args: argparse.Namespace) -> int:
    """Plan a corpus once, so shards cannot duplicate each other.

    `scene-batch` deduplicates within its own batch. Run twenty shards and each
    is internally diverse while knowing nothing about the other nineteen, so the
    corpus as a whole can repeat itself. Planning up front and handing each
    worker a slice of the same plan makes that impossible by construction, and
    the plan is also the record of what the corpus was *meant* to contain -
    which is what makes a run resumable and auditable.

    A scene is a pure function of its seed **and the parameters it was composed
    with**. The plan therefore carries those parameters per row: a batch planned
    with `--force-app-count 1 --uniform-pages` re-composed as an ordinary
    multi-app scene when `scene-batch` rebuilt it from the seed alone, so 80,000
    single-window Chromium scenes came back as generic ones with different
    scene ids.
    """
    from deskshot.generation.scene_composer import compose_corpus_plan

    app_pool = args.apps.split(",") if args.apps else None
    episode_rng = random.Random(args.start_seed ^ 0x5EED5)
    started = time.monotonic()
    accepted, rejected, next_seed = compose_corpus_plan(
        start_seed=args.start_seed,
        count=args.count,
        available_apps=app_pool,
        window=args.similarity_window,
        max_attempts=args.max_attempts or None,
        force_app_count=(
            args.force_app_count if args.force_app_count >= 0 else None
        ),
        uniform_pages=bool(getattr(args, "uniform_pages", False)),
    )
    elapsed = time.monotonic() - started

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        for index, planned in enumerate(accepted):
            # Whether a scene is captured once or stepped through is decided
            # here, in the plan, rather than at run time. A corpus cannot gain
            # episodes afterwards without re-running every scene, so the split
            # has to be settled before generation - and recording it per scene
            # keeps a resumed or re-sharded run identical to the first one.
            steps = _episode_step_count(episode_rng, args.episode_fraction, args.episode_steps)
            handle.write(
                json.dumps(
                    {
                        "index": index,
                        "steps": steps,
                        "seed": planned.scene.seed,
                        "scene_id": planned.scene.scene_id,
                        "signature": planned.signature,
                        "num_apps": len(planned.scene.apps),
                        # What this row was composed with, so a batch rebuilds
                        # the same scene rather than a generic one.
                        "force_app_count": (
                            args.force_app_count if args.force_app_count >= 0 else None
                        ),
                        "uniform_pages": bool(getattr(args, "uniform_pages", False)),
                        "apps": [app.app_name for app in planned.scene.apps],
                        "theme_preset": planned.scene.theme_preset,
                        "display_preset": planned.scene.display_preset,
                    },
                    sort_keys=True,
                )
                + "\n"
            )

    counts: Dict[int, int] = {}
    for planned in accepted:
        counts[len(planned.scene.apps)] = counts.get(len(planned.scene.apps), 0) + 1
    print(f"planned {len(accepted)} scenes in {elapsed:.1f}s -> {out}")
    if args.episode_fraction > 0:
        episodes = sum(1 for line in out.read_text(encoding="utf-8").splitlines()
                       if json.loads(line)["steps"] > 0)
        lengths = [json.loads(line)["steps"] for line in out.read_text(encoding="utf-8").splitlines()]
        ep = [n for n in lengths if n]
        print(f"  {len(ep)} of them episodes, {sum(ep) + len(ep)} frames, "
              f"lengths {min(ep)}-{max(ep)} (mean {sum(ep)/len(ep):.1f} actions)")
        print(f"  expected samples: {len(lengths) - len(ep) + sum(ep) + len(ep)}")
    print(f"  rejected {len(rejected)}, next free seed {next_seed}")
    print(f"  windows per scene: {sorted(counts.items())}")
    if len(accepted) < args.count:
        print(f"  WARNING: asked for {args.count}; raise --max-attempts")
        return 1
    return 0


def cmd_scene_batch(args: argparse.Namespace) -> int:
    """Compose and run a deterministic batch of mixed-app scenes."""
    from concurrent.futures import ThreadPoolExecutor

    from deskshot.generation.scene_composer import (
        build_batch_element_coverage,
        build_batch_distribution,
        compose_diverse_scene_batch,
        evaluate_scene_capture,
        group_scenes_by_session_envelope,
        run_scene_group,
    )
    from deskshot.generation.scene_worker import (
        PersistentSceneWorker,
        WorkerSceneResponse,
        stripe_scene_jobs,
    )

    config_path = CONFIGS_DIR / "pipeline.yaml"
    if config_path.exists():
        config = PipelineConfig.from_yaml(config_path)
    else:
        config = PipelineConfig()

    if args.tools_dir:
        config.session.tools_dir = Path(args.tools_dir)
    if args.base_display_number is not None:
        config.session.display.display_number = int(args.base_display_number)

    app_pool = args.apps.split(",") if args.apps else None
    output_dir = Path(args.output) if args.output else config.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    resume_left_nothing_to_do = False
    plan_seeds = _read_plan_shard(getattr(args, "plan", None), getattr(args, "shard", ""))
    if plan_seeds is None:
        if args.start_seed is None or args.count is None:
            print("scene-batch needs --start-seed and --count, or a --plan")
            return 2
    else:
        args.start_seed = plan_seeds[0][0] if plan_seeds else 0
        args.count = len(plan_seeds)
    plan_steps_by_seed: Dict[int, int] = {}
    planning_budget = args.max_attempts or max(args.count * 12, args.count + 12)
    session_group_size = max(1, int(args.session_group_size or 1))
    parallel_workers = max(1, int(args.parallel_workers or 1))
    if parallel_workers > 1 and session_group_size > 1:
        print("Parallel scene-batch currently supports only --session-group-size 1")
        return 1
    planned_scenes = []
    rejected_prelaunch = []
    scene_cursor = args.start_seed
    planning_attempts_used = 0

    if plan_seeds is not None:
        # The plan already settled which scenes exist and that none of them
        # repeat another shard's. This shard just runs its slice.
        from deskshot.generation.scene_composer import (
            PlannedScene,
            compose_scene,
            scene_signature,
        )

        for seed, _steps, force_count, uniform in plan_seeds:
            scene = compose_scene(
                seed,
                available_apps=app_pool,
                force_app_count=force_count,
                uniform_pages=uniform,
            )
            planned_scenes.append(PlannedScene(scene=scene, signature=scene_signature(scene)))
        plan_steps_by_seed = {seed: steps for seed, steps, _f, _u in plan_seeds}
        if getattr(args, "resume", False):
            # Capture stems are deterministic in (seed, step) now, so a scene
            # that is already on disk is the scene this shard would produce -
            # not merely a similar one. Without this a shard killed at 900 of
            # 1000 starts over, which on a multi-hour run is the difference
            # between a retry and a restart.
            capture_index = _index_captures(output_dir)
            done = [
                planned for planned in planned_scenes
                if _capture_looks_complete(
                    output_dir, planned.scene.scene_id, capture_index
                )
            ]
            if done:
                finished = {planned.scene.scene_id for planned in done}
                planned_scenes = [
                    planned for planned in planned_scenes
                    if planned.scene.scene_id not in finished
                ]
                logging.info(
                    "[scene-batch] resume: %d of %d scenes already captured, %d to go",
                    len(done), len(done) + len(planned_scenes), len(planned_scenes),
                )
                # A shard whose slice is already entirely on disk has nothing to
                # do and has therefore succeeded. Without this it accepts zero
                # new rows, exits 1, never writes its done-marker, and the
                # supervisor puts it back on the cluster again and again until
                # the attempt cap - so a run could not report itself finished.
                resume_left_nothing_to_do = not planned_scenes
        # The shard's size is the plan's, not the caller's: passing --count as
        # well would either truncate the shard or ask for scenes it does not
        # contain.
        args.count = len(planned_scenes)
        logging.info("[scene-batch] running %d scenes from plan shard", len(planned_scenes))

    while plan_seeds is None and len(planned_scenes) < args.count and planning_attempts_used < planning_budget:
        remaining = args.count - len(planned_scenes)
        new_plans, new_rejections, next_seed = compose_diverse_scene_batch(
            start_seed=scene_cursor,
            count=remaining,
            available_apps=app_pool,
            max_attempts=planning_budget - planning_attempts_used,
            existing_scenes=[row.scene for row in planned_scenes],
            session_group_size=session_group_size,
        )
        planning_attempts_used += next_seed - scene_cursor
        scene_cursor = next_seed
        planned_scenes.extend(new_plans)
        rejected_prelaunch.extend(new_rejections)
        if not new_plans:
            break

    scenes = [planned.scene for planned in planned_scenes]
    print(json.dumps([asdict(scene) for scene in scenes], indent=2))

    if args.describe_only:
        return 0

    # Set before the snapshot below, not after: scenes run in subprocesses that
    # inherit a *copy* of the environment taken here, so anything exported later
    # never reaches them. Setting it afterwards left Chromium on the unscaled
    # 90s floor and five more scenes died on it.
    if parallel_workers > 1:
        from deskshot.generation.scene_composer import launch_timeout_scale_for_workers

        scale = launch_timeout_scale_for_workers(parallel_workers)
        os.environ["DESKSHOT_LAUNCH_TIMEOUT_SCALE"] = f"{scale:.2f}"
        logging.info("[scene-batch] launch timeout scale x%.2f for %d workers",
                     scale, parallel_workers)

    env = dict(os.environ)
    current_py = env.get("PYTHONPATH", "")
    src_path = str(PROJECT_ROOT / "src")
    env["PYTHONPATH"] = src_path if not current_py else f"{src_path}:{current_py}"
    scene_timeout = args.scene_timeout if args.scene_timeout > 0 else None

    def _run_scene_subprocess(scene: object, display_number: int) -> tuple[object, object]:
        steps = plan_steps_by_seed.get(scene.seed, 0)
        cmd = [
            sys.executable,
            "-m",
            "deskshot.cli",
            "scene-episode" if steps else "scene",
            "--seed",
            str(scene.seed),
            "--output",
            str(output_dir),
            "--display-number",
            str(display_number),
        ]
        if steps:
            cmd.extend(["--steps", str(steps)])
        if app_pool:
            cmd.extend(["--apps", ",".join(app_pool)])
        if args.tools_dir:
            cmd.extend(["--tools-dir", args.tools_dir])
        cmd.append("--include-chrome" if args.include_chrome else "--no-include-chrome")

        # Stream straight to the log instead of buffering until exit, so a
        # running scene can be inspected, and bound it so one wedged scene
        # cannot consume the whole job's wall clock.
        scene_log = output_dir / f"scene_seed{scene.seed:04d}.log"
        scene_wall_started = time.monotonic()
        with scene_log.open("w", encoding="utf-8") as log_handle:
            proc = subprocess.Popen(
                cmd,
                cwd=str(PROJECT_ROOT),
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                returncode = proc.wait(timeout=scene_timeout)
            except subprocess.TimeoutExpired:
                _terminate_scene_process(proc)
                returncode = -1
                logging.warning(
                    "[scene-batch] scene seed=%s timed out after %ss; killed",
                    scene.seed,
                    scene_timeout,
                )
                log_handle.write(
                    f"\nDESKSHOT: scene timed out after {scene_timeout}s and was killed\n"
                )

        scene_wall_sec = round(time.monotonic() - scene_wall_started, 3)
        # An episode writes into its own subdirectory, a static capture into
        # the batch directory; look in both rather than making the caller care.
        # rglob, not glob: with the corpus layout a capture lives in
        # st/<xx>/ or ep/<scene_id>/ rather than the root.
        meta_files = sorted(output_dir.rglob(f"scene-{scene.scene_id}-*.meta.json"))
        row = None
        if returncode == 0 and meta_files:
            row = json.loads(meta_files[-1].read_text(encoding="utf-8"))
            row.setdefault("meta", str(meta_files[-1]))
            row = _persist_scene_wall_clock(row, scene_wall_sec)
        return scene, row

    def _run_scene_persistent_workers(scene_batch: list[object]) -> list[tuple[object, object]]:
        lanes = stripe_scene_jobs(scene_batch, parallel_workers)

        def _run_lane(worker_index: int, lane: list[tuple[int, object]]) -> list[tuple[int, object, object]]:
            if not lane:
                return []
            display_number = config.session.display.display_number + worker_index
            # Stagger the start. Every worker brings up a whole desktop - Xvfb,
            # window manager, panel, file-manager desktop, then its apps - and
            # all of that wants inotify, whose instance limit is per user per
            # node. Starting them together produces a transient peak that no
            # steady-state budget covers: failures clustered in the first four
            # minutes of a run, 60 in one minute, then subsided. Spreading the
            # starts costs seconds once per job.
            if worker_index:
                time.sleep(WORKER_START_STAGGER_SEC * worker_index)
            logging.info("[scene-batch] worker %02d start display=%s scenes=%s", worker_index, display_number, len(lane))
            worker = PersistentSceneWorker(
                worker_id=f"w{worker_index:02d}",
                display_number=display_number,
                output_dir=output_dir,
                project_root=PROJECT_ROOT,
                env=env,
                tools_dir=args.tools_dir,
                include_desktop_chrome=args.include_chrome,
            )
            lane_results: list[tuple[int, object, object]] = []
            try:
                worker.start()
                for scene_index, scene in lane:
                    logging.info("[scene-batch] worker %s running seed=%s", worker.worker_id, scene.seed)
                    response: WorkerSceneResponse = worker.run_scene(
                        scene, plan_steps_by_seed.get(scene.seed, 0)
                    )
                    scene_log = output_dir / f"scene_seed{scene.seed:04d}.log"
                    log_lines = [
                        f"worker_id={response.worker_id}",
                        f"display_number={response.display_number}",
                        f"elapsed_s={response.elapsed_s:.3f}",
                        f"ok={response.ok}",
                        f"stderr_log={worker.stderr_path.name}",
                    ]
                    if response.row is not None:
                        log_lines.append(f"stem={response.row.get('stem', '')}")
                    if response.error:
                        log_lines.append(f"error={response.error}")
                    if response.traceback:
                        log_lines.extend(["traceback:", response.traceback.rstrip()])
                    scene_log.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
                    worker_row = _scene_batch_meta_row(response.row)
                    worker_row = _persist_scene_wall_clock(worker_row, response.elapsed_s)
                    lane_results.append((scene_index, scene, worker_row))
            finally:
                logging.info("[scene-batch] worker %s closing", worker.worker_id)
                worker.close()
                logging.info("[scene-batch] worker %s closed", worker.worker_id)
            return lane_results

        ordered_results: list[tuple[int, object, object]] = []
        with ThreadPoolExecutor(max_workers=parallel_workers) as pool:
            futures = [
                pool.submit(_run_lane, worker_index, lane)
                for worker_index, lane in enumerate(lanes)
                if lane
            ]
            for future in futures:
                ordered_results.extend(future.result())

        ordered_results.sort(key=lambda item: item[0])
        return [(scene, row) for _index, scene, row in ordered_results]

    rows = []
    accepted_hashes: dict[str, str] = {}
    accepted_rows: list[dict[str, object]] = []
    while len(accepted_rows) < args.count and planning_attempts_used < planning_budget:
        pending_scenes = scenes[len(rows):]
        if not pending_scenes and plan_seeds is not None:
            # Running from a plan means this shard's contents were settled
            # before any capture ran. Topping up after a rejection would plan
            # scenes the plan does not contain - different between runs and
            # between shards - which is exactly what the plan exists to prevent.
            # A shard that loses captures comes up short; that is recorded, not
            # papered over.
            logging.info(
                "[scene-batch] plan shard exhausted with %d of %d captures accepted",
                len(accepted_rows), len(planned_scenes),
            )
            break
        if not pending_scenes:
            remaining = args.count - len(accepted_rows)
            new_plans, new_rejections, next_seed = compose_diverse_scene_batch(
                start_seed=scene_cursor,
                count=remaining,
                available_apps=app_pool,
                max_attempts=planning_budget - planning_attempts_used,
                existing_scenes=[row.scene for row in planned_scenes],
                session_group_size=session_group_size,
            )
            planning_attempts_used += next_seed - scene_cursor
            scene_cursor = next_seed
            planned_scenes.extend(new_plans)
            rejected_prelaunch.extend(new_rejections)
            scenes = [planned.scene for planned in planned_scenes]
            pending_scenes = scenes[len(rows):]
            if not pending_scenes:
                break

        if session_group_size <= 1:
            if parallel_workers <= 1:
                for scene in pending_scenes:
                    display_number = config.session.display.display_number + len(rows)
                    scene, row = _run_scene_subprocess(scene, display_number)
                    if row is not None:
                        _audit_one_capture(
                            scene,
                            row,
                            output_dir=output_dir,
                            accepted_hashes=accepted_hashes,
                            accepted_rows=accepted_rows,
                        )
                    rows.append((scene, row))
                    if len(accepted_rows) >= args.count:
                        break
            else:
                batch_results = _run_scene_persistent_workers(pending_scenes)
                logging.info("[scene-batch] persistent batch returned rows=%s", len(batch_results))
                for scene, row in batch_results:
                    logging.info("[scene-batch] auditing seed=%s row=%s", scene.seed, row is not None)
                    if row is not None:
                        _audit_one_capture(
                            scene,
                            row,
                            output_dir=output_dir,
                            accepted_hashes=accepted_hashes,
                            accepted_rows=accepted_rows,
                        )
                    rows.append((scene, row))
                    logging.info(
                        "[scene-batch] processed seed=%s accepted_rows=%s total_rows=%s",
                        scene.seed,
                        len(accepted_rows),
                        len(rows),
                    )
                    if len(accepted_rows) >= args.count:
                        break
        else:
            for scene_group in group_scenes_by_session_envelope(pending_scenes):
                group_log = output_dir / f"scene_group_seed{scene_group[0].seed:04d}.log"
                try:
                    group_rows = run_scene_group(
                        scene_group,
                        config,
                        output_dir=output_dir,
                        include_desktop_chrome=args.include_chrome,
                    )
                    group_log.write_text(
                        "\n".join(
                            [
                                f"group_size={len(scene_group)}",
                                *[
                                    f"seed={scene.seed} scene_id={scene.scene_id} status={'ok' if row is not None else 'failed'}"
                                    for scene, row in group_rows
                                ],
                            ]
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                except Exception as exc:
                    group_log.write_text(
                        f"group_size={len(scene_group)}\nerror={exc}\n",
                        encoding="utf-8",
                    )
                    group_rows = [(scene, None) for scene in scene_group]

                for scene, row in group_rows:
                    row = _scene_batch_meta_row(row)
                    if row is not None:
                        _audit_one_capture(
                            scene,
                            row,
                            output_dir=output_dir,
                            accepted_hashes=accepted_hashes,
                            accepted_rows=accepted_rows,
                        )
                    rows.append((scene, row))
                    if len(accepted_rows) >= args.count:
                        break
                if len(accepted_rows) >= args.count:
                    break

    # Shards may share one output directory - the captures cannot collide,
    # because a stem is a function of the seed and shards hold disjoint seeds.
    # These run-level files *would* collide, so they carry the shard in the name.
    shard_tag = ""
    if getattr(args, "shard", ""):
        shard_tag = "." + str(args.shard).replace("/", "of")

    (output_dir / f"scene_batch{shard_tag}.json").write_text(
        json.dumps([asdict(scene) for scene in scenes], indent=2),
        encoding="utf-8",
    )
    (output_dir / f"scene_batch_rejected_prelaunch{shard_tag}.json").write_text(
        json.dumps(
            [
                {
                    "scene": asdict(row.scene),
                    "signature": row.signature,
                    "reason": row.reason,
                    "similarity_score": row.similarity_score,
                    "similar_to_seed": row.similar_to_seed,
                    "similar_to_scene_id": row.similar_to_scene_id,
                }
                for row in rejected_prelaunch
            ],
            indent=2,
        ),
        encoding="utf-8",
    )

    # Compare the annotations against the pixels before the batch is called
    # done. Two of the defects found in this project - text geometry offset from
    # its glyphs, and popups the accessibility tree cannot see - were invisible
    # from the tree alone, because the tree was self-consistent in both cases.
    # A pixel check is the only one that sees that class at all, so it runs on
    # every batch rather than when somebody remembers.
    pixel_audit: dict[str, object] = {}
    if args.pixel_audit and any(row is not None for _scene, row in rows):
        try:
            proc = subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "scripts" / "audit_annotation_pixels.py"),
                 "--root", str(output_dir), "--output", str(output_dir / "audit")],
                cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=1800,
                env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
            )
            report_path = output_dir / "audit" / "pixel_audit.json"
            if report_path.is_file():
                data = json.loads(report_path.read_text(encoding="utf-8"))
                pixel_audit = {
                    "captures": data.get("captures"),
                    "text_elements": data.get("text_elements"),
                    "phantom_rate": data.get("phantom_rate"),
                    "drift_rate": data.get("drift_rate"),
                }
                print(f"Pixel audit: phantoms {data.get('phantom_rate', 0):.2%} "
                      f"drift {data.get('drift_rate', 0):.2%} "
                      f"over {data.get('text_elements')} text elements")
            elif proc.returncode != 0:
                logging.warning("[scene-batch] pixel audit failed: %s", proc.stderr[-300:])
        except Exception:
            logging.warning("[scene-batch] pixel audit could not run", exc_info=True)

    summary_lines = [
        f"start_seed: {args.start_seed}",
        f"count: {args.count}",
        f"session_group_size: {session_group_size}",
        f"parallel_workers: {parallel_workers}",
        f"base_display_number: {config.session.display.display_number}",
        f"planning_budget: {planning_budget}",
        f"planning_attempts_used: {planning_attempts_used}",
        f"planned: {len(scenes)}",
        f"rejected_prelaunch: {len(rejected_prelaunch)}",
        f"completed: {sum(1 for _scene, row in rows if row is not None)}",
        f"accepted: {len(accepted_rows)}",
        f"rejected_post: {sum(1 for _scene, row in rows if row is not None and not row.get('scene_batch_audit', {}).get('accepted'))}",
        f"pixel_audit: {json.dumps(pixel_audit) if pixel_audit else 'skipped'}",
        "",
    ]
    for scene, row in rows:
        audit = row.get("scene_batch_audit") if row is not None else None
        summary_lines.extend(
            [
                f"- seed={scene.seed} scene_id={scene.scene_id}",
                f"  layout={scene.layout} theme={scene.theme_preset} display={scene.display_preset}",
                f"  apps={', '.join(f'{app.app_name}:{app.state_ref}' for app in scene.apps)}",
                _scene_batch_result_summary_line(row),
                (
                    f"  audit={audit['status']} reasons={','.join(audit['reasons']) or 'ok'}"
                    if audit is not None
                    else ""
                ),
            ]
        )
    if rejected_prelaunch:
        summary_lines.extend(["", "## Prelaunch Rejections"])
        for row in rejected_prelaunch:
            summary_lines.append(
                f"- seed={row.scene.seed} scene_id={row.scene.scene_id} reason={row.reason} score={row.similarity_score}"
            )
    if accepted_rows:
        distributions = build_batch_distribution(
            [{"scene": row["scene"]} for row in accepted_rows]
        )
        coverage = build_batch_element_coverage(output_dir, accepted_rows)
        summary_lines.extend(["", "## Distributions"])
        for key, bucket in distributions.items():
            summary_lines.append(f"- {key}: {bucket}")
        summary_lines.extend(["", "## Element Coverage"])
        summary_lines.append(f"- filtered_types: {coverage['filtered_type_counts']}")
        summary_lines.append(f"- leaf_types: {coverage['leaf_type_counts']}")
        summary_lines.append(f"- filtered_roles: {coverage['filtered_role_counts']}")
        summary_lines.append(f"- leaf_roles: {coverage['leaf_role_counts']}")
        summary_lines.append(f"- filtered_sources: {coverage['filtered_source_counts']}")
        summary_lines.append(f"- leaf_sources: {coverage['leaf_source_counts']}")
        summary_lines.append(f"- filtered_apps: {coverage['filtered_app_counts']}")
        summary_lines.append(f"- leaf_apps: {coverage['leaf_app_counts']}")
        summary_lines.append(f"- leaf_window_apps: {coverage['leaf_window_app_counts']}")
        summary_lines.append(
            f"- filtered_type_scene_frequency: {coverage['filtered_type_scene_frequency']}"
        )
        summary_lines.append(
            f"- leaf_type_scene_frequency: {coverage['leaf_type_scene_frequency']}"
        )
        summary_lines.append(
            f"- filtered_role_scene_frequency: {coverage['filtered_role_scene_frequency']}"
        )
        summary_lines.append(
            f"- leaf_role_scene_frequency: {coverage['leaf_role_scene_frequency']}"
        )
        (output_dir / f"scene_batch_coverage{shard_tag}.json").write_text(
            json.dumps(coverage, indent=2),
            encoding="utf-8",
        )
    (output_dir / f"summary{shard_tag}.md").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    (output_dir / f"scene_batch_results{shard_tag}.json").write_text(
        json.dumps(
            [
                {
                    "scene": asdict(scene),
                    "result": row,
                }
                for scene, row in rows
            ],
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"\nSaved {len(accepted_rows)} accepted scene captures into {output_dir}")
    if resume_left_nothing_to_do:
        return 0
    return 0 if accepted_rows else 1


def cmd_scene_worker(args: argparse.Namespace) -> int:
    """Run a persistent internal worker for scene-batch execution."""
    from deskshot.generation.scene_worker import run_scene_worker_main
    from deskshot.pipeline.orchestrator import load_manifests

    protocol_out = sys.stdout
    sys.stdout = sys.stderr

    config_path = CONFIGS_DIR / "pipeline.yaml"
    if config_path.exists():
        config = PipelineConfig.from_yaml(config_path)
    else:
        config = PipelineConfig()

    if args.tools_dir:
        config.session.tools_dir = Path(args.tools_dir)
    if args.display_number is not None:
        config.session.display.display_number = int(args.display_number)

    output_dir = Path(args.output) if args.output else config.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    manifests = {m.app_name: m for m in load_manifests(config.apps_config_dir)}

    return run_scene_worker_main(
        worker_id=args.worker_id or f"display{config.session.display.display_number}",
        config=config,
        output_dir=output_dir,
        include_desktop_chrome=args.include_chrome,
        preloaded_manifests=manifests,
        protocol_out=protocol_out,
    )


def cmd_audit(args: argparse.Namespace) -> int:
    """Run dense-annotation qualification probes for candidate apps."""
    from deskshot.audit.app_audit import resolve_probe_manifests, run_app_audit
    from deskshot.environment.themes import get_theme_preset
    from deskshot.environment.diversity import get_display_preset

    config_path = CONFIGS_DIR / "pipeline.yaml"
    if config_path.exists():
        config = PipelineConfig.from_yaml(config_path)
    else:
        config = PipelineConfig()

    if args.tools_dir:
        config.session.tools_dir = Path(args.tools_dir)
    if args.output:
        output_dir = Path(args.output)
    else:
        output_dir = config.output_dir

    if args.desktop_env:
        config.session.desktop_env = args.desktop_env
    if args.preset:
        config.session.theme = get_theme_preset(args.preset, resolve=False)
    if args.wallpaper:
        config.session.theme.wallpaper = args.wallpaper
    if args.wallpaper_seed is not None:
        config.session.theme.wallpaper_seed = args.wallpaper_seed
    if args.panel_variant:
        config.session.theme.panel_variant = args.panel_variant
    if args.display_preset:
        preset = get_display_preset(
            args.display_preset,
            display_number=config.session.display.display_number,
            depth=config.session.display.depth,
        )
        config.session.display.width = preset.width
        config.session.display.height = preset.height
    if args.width:
        config.session.display.width = args.width
    if args.height:
        config.session.display.height = args.height
    if args.desktop_seed is not None:
        config.session.desktop_fixture.seed = args.desktop_seed
    if args.desktop_profile:
        config.session.desktop_fixture.profile = args.desktop_profile

    app_names = args.apps.split(",") if args.apps else None
    manifests = resolve_probe_manifests(
        config.apps_config_dir,
        app_names=app_names,
        discover=args.discover,
        include_hidden=args.include_hidden,
        limit=args.limit,
    )
    if not manifests:
        print("No candidate manifests resolved")
        return 1

    rows = run_app_audit(
        config,
        manifests,
        output_dir=output_dir,
        include_desktop_chrome=args.include_chrome,
        browser_url=args.browser_url,
        browser_wait_seconds=args.browser_wait,
    )

    print(f"\nAudited {len(rows)} app candidates")
    for row in rows:
        if row.get("status") == "launch_failed":
            print(f"  {row['app_name']}: launch_failed")
            continue
        audit = row["audit"]
        metrics = audit["metrics"]
        print(
            f"  {row['app_name']}: {audit['status']} "
            f"(elements={metrics['num_elements']}, "
            f"types={metrics['num_type_diversity']}, "
            f"interactive={metrics['num_interactive_elements']}, "
            f"same_area_clusters={metrics['same_area_cluster_count']})"
        )

    print(f"\nSummary written to {output_dir / 'summary.md'}")
    return 0


def cmd_viz(args: argparse.Namespace) -> int:
    """Visualize annotations with bounding boxes."""
    from deskshot.config import ensure_webshot_importable

    raw_dir = Path(args.raw) if args.raw else PROJECT_ROOT / "data" / "raw"
    viz_dir = Path(args.output) if args.output else PROJECT_ROOT / "data" / "viz"
    viz_dir.mkdir(parents=True, exist_ok=True)

    # Try to use webshot visualizer
    try:
        ensure_webshot_importable()
        from webshot.visualize import visualize_one, VizOptions
        has_webshot_viz = True
    except ImportError:
        has_webshot_viz = False

    from PIL import Image, ImageDraw, ImageFont
    import json

    png_files = sorted(raw_dir.glob("*.png"))
    if not png_files:
        print(f"No PNG files found in {raw_dir}")
        return 1

    count = 0
    for png_path in png_files:
        elements_path = png_path.with_suffix(".elements.json")
        if not elements_path.exists():
            continue

        with open(elements_path) as f:
            elements = json.load(f)

        if has_webshot_viz:
            img = Image.open(png_path)
            record = {
                "image": img,
                "elements": elements,
                "text_spans": [],
            }
            viz_img = visualize_one(record, VizOptions())
        else:
            viz_img = _basic_visualize(png_path, elements)

        viz_path = viz_dir / png_path.name
        viz_img.save(str(viz_path))
        count += 1

    print(f"Visualized {count} screenshots → {viz_dir}")
    return 0


def _basic_visualize(
    png_path: Path,
    elements: list,
) -> "PIL.Image.Image":
    """Basic bounding-box visualization (fallback without webshot)."""
    from PIL import Image
    from deskshot.extraction.visualize import render_elements_visualization

    return render_elements_visualization(Image.open(png_path).copy(), elements)


# ── Main ─────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser (split out from main so tests can inspect it)."""
    parser = argparse.ArgumentParser(
        prog="dsd",
        description="DeskShot: Desktop UI annotation pipeline",
    )
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # setup
    p_setup = subparsers.add_parser("setup", help="Download + extract RPMs")
    p_setup.add_argument("--tools-dir", help="Custom tools directory")

    # validate
    p_validate = subparsers.add_parser("validate",
                                        help="Test Xvfb + D-Bus + AT-SPI")
    p_validate.add_argument("--tools-dir", help="Custom tools directory")
    p_validate.add_argument("--desktop-env", choices=["none", "metacity", "xfce"],
                            help="Desktop environment to validate")

    # install-styles
    p_install_styles = subparsers.add_parser(
        "install-styles",
        help="Install richer style packs into extracted tree",
    )
    p_install_styles.add_argument("--tools-dir", help="Custom tools directory")
    p_install_styles.add_argument("--refresh", action="store_true",
                                  help="Refresh style pack cache by recloning")

    # run
    p_run = subparsers.add_parser("run", help="Run extraction pipeline")
    p_run.add_argument("--apps", help="Comma-separated app names to process")
    p_run.add_argument("--interactions",
                       help="Comma-separated interaction names")
    p_run.add_argument("--output", "-o", help="Output directory")
    p_run.add_argument("--tools-dir", help="Custom tools directory")
    p_run.add_argument("--desktop-env", choices=["none", "metacity", "xfce"],
                       help="Desktop environment")
    p_run.add_argument("--preset",
                       choices=PRESET_CHOICES,
                       help="Theme/layout preset")
    p_run.add_argument("--theme", help="GTK theme name")
    p_run.add_argument("--icon-theme", help="Icon theme name")
    p_run.add_argument("--wm-theme", help="XFWM theme name")
    p_run.add_argument("--wallpaper", help="Wallpaper path")
    p_run.add_argument("--wallpaper-seed", type=int,
                       help="Deterministic wallpaper choice seed when no explicit wallpaper is given")
    p_run.add_argument("--panel-variant",
                       help="Panel layout variant (top, top_slim, top_tall, bottom, bottom_slim, bottom_tall, left_dock, left_slim_dock)")
    p_run.add_argument("--display-preset", help="Named viewport preset")
    p_run.add_argument("--width", type=int, help="Override viewport width")
    p_run.add_argument("--height", type=int, help="Override viewport height")
    p_run.add_argument("--desktop-seed", type=int,
                       help="Seed for managed desktop files/folders")
    p_run.add_argument("--desktop-profile", choices=DESKTOP_PROFILE_CHOICES,
                       help="Desktop file/folder density profile")
    p_run.add_argument("--include-chrome", action="store_true",
                       help="Include desktop chrome (panels, taskbar) in extraction")

    # scene
    p_scene = subparsers.add_parser("scene", help="Compose and run one mixed-app scene")
    p_scene.add_argument("--seed", type=int, required=True, help="Deterministic scene seed")
    p_scene.add_argument("--apps", help="Optional comma-separated app allowlist")
    p_scene.add_argument("--output", "-o", help="Output directory")
    p_scene.add_argument("--tools-dir", help="Custom tools directory")
    p_scene.add_argument("--display-number", type=int,
                         help="Override X display number for this one scene run")
    p_scene.add_argument("--describe-only", action="store_true",
                         help="Print the composed scene without running it")
    p_scene.add_argument("--include-chrome", action=argparse.BooleanOptionalAction,
                         default=True,
                         help="Include desktop chrome in saved artifacts")

    # preflight
    p_preflight = subparsers.add_parser("preflight", help="Validate app binary portability on this node")
    p_preflight.add_argument("--apps", help="Comma-separated app names to check")
    p_preflight.add_argument("--tools-dir", help="Custom tools directory")
    p_preflight.add_argument("--strict-compute", action="store_true", help="Fail non-portable system PATH resolutions")
    p_preflight.add_argument("--json", action="store_true", help="Emit JSON results")
    p_preflight.add_argument("--no-session-binaries", action="store_true", help="Skip desktop/session binary checks")

    # scene-episode
    p_scene_episode = subparsers.add_parser(
        "scene-episode", help="Capture a scene as a sequence of observations with actions")
    p_scene_episode.add_argument("--seed", type=int, required=True, help="Deterministic scene seed")
    p_scene_episode.add_argument("--steps", type=int, default=0,
                                 help="Actions to take after the first observation (0 = static capture)")
    p_scene_episode.add_argument("--apps", help="Optional comma-separated app allowlist")
    p_scene_episode.add_argument("--output", "-o", help="Output directory")
    p_scene_episode.add_argument("--tools-dir", help="Custom tools directory")
    p_scene_episode.add_argument("--display-number", type=int, help="Override X display number")
    p_scene_episode.add_argument("--candidate-pool", type=int, default=8,
                                 help="How many actionable targets to sample an action from")
    p_scene_episode.add_argument("--include-chrome", action=argparse.BooleanOptionalAction,
                                 default=True, help="Include desktop chrome in saved artifacts")

    # scene-task
    p_scene_task = subparsers.add_parser(
        "scene-task",
        help="Run a goal-directed task, verifying every step against a predicate")
    p_scene_task.add_argument("--seed", type=int, required=True, help="Deterministic scene seed")
    p_scene_task.add_argument("--apps", help="Optional comma-separated app allowlist")
    p_scene_task.add_argument("--target-app", help="App to build the task for (default: first in scene)")
    p_scene_task.add_argument("--target-steps", type=int, default=30,
                              help="Requested horizon; units are composed until the plan is this long")
    p_scene_task.add_argument("--max-menus", type=int, default=8, help="Menus to probe")
    p_scene_task.add_argument("--output", "-o", help="Output directory")
    p_scene_task.add_argument("--tools-dir", help="Custom tools directory")
    p_scene_task.add_argument("--display-number", type=int, help="Override X display number")
    p_scene_task.add_argument("--include-chrome", action=argparse.BooleanOptionalAction,
                              default=True, help="Include desktop chrome in saved artifacts")
    p_scene_task.add_argument("--max-effect-probes", type=int, default=6,
                              help="Menu items to test for a measurable effect (0 to skip)")
    p_scene_task.add_argument("--mode",
                              choices=["tour", "settings", "cross-app", "affordance"],
                              default="tour",
                              help="tour: chain dialog round trips; "
                                   "settings: one conjunctive task over dialog check boxes; "
                                   "cross-app: one task spanning every app in the scene; "
                                   "affordance: goals over whatever stateful widgets are "
                                   "on screen, no menus needed")
    p_scene_task.add_argument("--num-settings", type=int, default=6,
                              help="settings mode: how many check boxes to change")
    p_scene_task.add_argument("--probe-only", action="store_true",
                              help="Report the menus and units found, run nothing")

    # scene-batch
    p_scene_plan = subparsers.add_parser(
        "scene-plan",
        help="Plan a whole corpus up front so shards cannot duplicate each other",
    )
    p_scene_plan.add_argument("--start-seed", type=int, required=True)
    p_scene_plan.add_argument("--count", type=int, required=True)
    p_scene_plan.add_argument("--output", required=True, help="plan file (JSONL)")
    p_scene_plan.add_argument("--apps", default="", help="comma-separated pool override")
    p_scene_plan.add_argument(
        "--similarity-window",
        type=int,
        default=256,
        help="how many recent scenes a candidate is compared against (0 disables)",
    )
    p_scene_plan.add_argument("--max-attempts", type=int, default=0)
    p_scene_plan.add_argument(
        "--uniform-pages",
        action="store_true",
        help="draw Chromium pages flat from the wide catalog instead of the "
             "popularity prior. The prior is strong - it turned a 1,000-site "
             "list into 50 observed domains with google.com at 27.5%% - so a "
             "batch whose purpose is page diversity needs this.",
    )
    p_scene_plan.add_argument(
        "--force-app-count",
        type=int,
        default=-1,
        help="plan every scene with exactly this many windows instead of "
             "drawing from SCENE_APP_COUNT_WEIGHTS. Use 0 for a dedicated "
             "bare-desktop batch: it is 3%% of the natural distribution, so "
             "collecting 5,000 by filtering would need 167,000 planned scenes.",
    )
    p_scene_plan.add_argument(
        "--episode-fraction",
        type=float,
        default=0.0,
        help="share of scenes to capture as multi-step episodes (0-1)",
    )
    p_scene_plan.add_argument(
        "--episode-steps",
        type=int,
        default=3,
        help="LONGEST episode, in actions. Lengths are sampled per episode "
             "(see EPISODE_LENGTH_TIERS), not fixed at this value.",
    )
    p_scene_plan.set_defaults(func=cmd_scene_plan)

    p_scene_batch = subparsers.add_parser("scene-batch", help="Compose and run a deterministic batch of mixed-app scenes")
    # Not required with --plan: the plan already says which seeds exist and how
    # many this shard owns, and passing a count as well could only disagree
    # with it.
    p_scene_batch.add_argument("--start-seed", type=int, default=None,
                               help="First scene seed (unused with --plan)")
    p_scene_batch.add_argument("--count", type=int, default=None,
                               help="Number of scenes (unused with --plan)")
    p_scene_batch.add_argument("--apps", help="Optional comma-separated app allowlist")
    p_scene_batch.add_argument("--output", "-o", help="Output directory")
    p_scene_batch.add_argument("--tools-dir", help="Custom tools directory")
    p_scene_batch.add_argument("--parallel-workers", type=int, default=1,
                               help="Run isolated scene subprocesses in parallel across display numbers")
    p_scene_batch.add_argument("--base-display-number", type=int, default=99,
                               help="Base X display number for scene-batch workers")
    p_scene_batch.add_argument("--max-attempts", type=int,
                               help="Maximum seeds to try while rejecting near-duplicate scene plans")
    p_scene_batch.add_argument("--scene-timeout", type=int, default=900,
                               help="Kill and retry a scene subprocess after this many seconds (0 disables)")
    p_scene_batch.add_argument("--session-group-size", type=int, default=1,
                               help="Reuse one desktop session for small consecutive scene groups sharing one session envelope")
    p_scene_batch.add_argument("--describe-only", action="store_true",
                               help="Print the composed scenes without running them")
    p_scene_batch.add_argument("--pixel-audit", action=argparse.BooleanOptionalAction,
                               default=True,
                               help="Audit annotations against the screenshots when the batch ends")
    p_scene_batch.add_argument("--include-chrome", action=argparse.BooleanOptionalAction,
                               default=True,
                               help="Include desktop chrome in saved artifacts")

    # scene-worker (internal)
    p_scene_batch.add_argument(
        "--plan", default="", help="run seeds from a scene-plan file instead of planning"
    )
    p_scene_batch.add_argument(
        "--shard", default="", help="with --plan: this worker's slice, as k/n"
    )
    p_scene_batch.add_argument(
        "--resume",
        action="store_true",
        help="with --plan: skip scenes already captured into --output",
    )

    p_scene_worker = subparsers.add_parser("scene-worker", help=argparse.SUPPRESS)
    p_scene_worker.add_argument("--output", "-o", help="Output directory")
    p_scene_worker.add_argument("--tools-dir", help="Custom tools directory")
    p_scene_worker.add_argument("--display-number", type=int, required=True,
                                help="Pinned X display number for this worker")
    p_scene_worker.add_argument("--worker-id", help="Logical worker identifier")
    p_scene_worker.add_argument("--include-chrome", action=argparse.BooleanOptionalAction,
                                default=True,
                                help="Include desktop chrome in saved artifacts")

    # audit
    p_audit = subparsers.add_parser(
        "audit",
        help="Qualify candidate apps for dense AT-SPI extraction",
    )
    p_audit.add_argument("--apps", help="Comma-separated app names to audit")
    p_audit.add_argument("--discover", action="store_true",
                         help="Discover extracted GUI candidates from .desktop files")
    p_audit.add_argument("--include-hidden", action="store_true",
                         help="Include NoDisplay desktop entries during discovery")
    p_audit.add_argument("--limit", type=int,
                         help="Max number of discovered candidates to audit")
    p_audit.add_argument("--output", "-o", help="Audit output directory")
    p_audit.add_argument("--tools-dir", help="Custom tools directory")
    p_audit.add_argument("--desktop-env", choices=["none", "metacity", "xfce"],
                         help="Desktop environment")
    p_audit.add_argument("--preset",
                         choices=PRESET_CHOICES,
                         help="Theme/layout preset")
    p_audit.add_argument("--wallpaper", help="Wallpaper path")
    p_audit.add_argument("--wallpaper-seed", type=int,
                         help="Deterministic wallpaper choice seed when no explicit wallpaper is given")
    p_audit.add_argument("--panel-variant",
                         help="Panel layout variant (top, top_slim, top_tall, bottom, bottom_slim, bottom_tall)")
    p_audit.add_argument("--display-preset", help="Named viewport preset")
    p_audit.add_argument("--width", type=int, help="Override viewport width")
    p_audit.add_argument("--height", type=int, help="Override viewport height")
    p_audit.add_argument("--desktop-seed", type=int,
                         help="Seed for managed desktop files/folders")
    p_audit.add_argument("--desktop-profile", choices=DESKTOP_PROFILE_CHOICES,
                         help="Desktop file/folder density profile")
    p_audit.add_argument("--browser-url",
                         help="Override browser probes to open a live URL instead of the local fixture")
    p_audit.add_argument("--browser-wait", type=float,
                         help="Extra wait time in seconds for browser probe pages to settle")
    p_audit.add_argument("--include-chrome", action=argparse.BooleanOptionalAction,
                         default=True,
                         help="Include desktop chrome in saved artifacts")

    # viz
    p_viz = subparsers.add_parser("viz", help="Visualize annotations")
    p_viz.add_argument("--raw", help="Raw data directory")
    p_viz.add_argument("--output", "-o", help="Visualization output directory")

    # themes
    subparsers.add_parser("themes", help="List available themes and wallpapers")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(args.verbose)

    commands = {
        "setup": cmd_setup,
        "install-styles": cmd_install_styles,
        "preflight": cmd_preflight,
        "validate": cmd_validate,
        "run": cmd_run,
        "scene": cmd_scene,
        "scene-episode": cmd_scene_episode,
        "scene-task": cmd_scene_task,
        "scene-plan": cmd_scene_plan,
        "scene-batch": cmd_scene_batch,
        "scene-worker": cmd_scene_worker,
        "audit": cmd_audit,
        "viz": cmd_viz,
        "themes": cmd_themes,
    }

    sys.exit(commands[args.command](args))


if __name__ == "__main__":
    main()
