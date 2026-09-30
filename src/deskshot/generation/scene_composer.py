"""Deterministic mixed-app scene composition and execution."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
import json
import logging
import os
import random
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from deskshot.automation.app_launcher import (
    AppHandoffFailed,
    kill_app,
    kill_apps_by_atspi_name,
    launch_app,
    wait_for_app_absent_from_atspi,
)
from deskshot.automation.interaction import execute_sequence
from deskshot.automation.xdotool import (
    XdotoolTimeout,
    click as xdo_click,
    focus_window_by_name,
    key as xdo_key,
    move_active_window,
    resize_active_window,
    type_text as xdo_type_text,
)
from deskshot.generation.readiness import wait_for_apps_to_settle
from deskshot.generation.layouts import build_layout, choose_family
from deskshot.generation.actions import (
    SceneAction,
    diff_states,
    resolve_action_point,
)
from deskshot.browser_catalog import (
    browser_catalog_state_family,
    build_browser_catalog_sequence,
    sample_browser_catalog_state,
)
from deskshot.config import AppManifest, InteractionChain, InteractionSequence, PipelineConfig
from deskshot.environment.diversity import (
    DISPLAY_PRESETS,
    get_display_preset,
    sample_desktop_content_pack,
    sample_desktop_layout_template,
)
from deskshot.environment.session import DesktopSession
from deskshot.environment.themes import get_theme_preset
from deskshot.extraction.run_extraction import _capture_current_state, stable_hash
from deskshot.pipeline.orchestrator import load_manifests
from deskshot.postprocessing.quality import build_stage_survival_summary, run_quality_checks
from PIL import Image

logger = logging.getLogger(__name__)

SCENE_THEME_WEIGHTS: Dict[str, int] = {
    "linux_classic": 26,
    "windows_redmond": 24,
    "macos_tahoe_like": 17,
    "macos_tahoe_glass": 8,
    "quartz_night": 8,
    "quartz_night_nord": 5,
    "ubuntu_like": 25,
}

SCENE_DISPLAY_WEIGHTS: Dict[str, int] = {
    "wxga_1366x768": 18,
    "hdplus_1600x900": 14,
    "fhd_1920x1080": 30,
    "wuxga_1920x1200": 16,
    "qhd_2560x1440": 12,
    "retina_2880x1800": 6,
    "uhd_3840x2160": 4,
}

SCENE_DESKTOP_PROFILE_WEIGHTS: Dict[str, int] = {
    "empty": 8,
    "sparse": 16,
    "balanced": 44,
    "dense": 24,
    "packed": 8,
}

# Counts above four are served by the generative layout families in
# `layouts.py` rather than by hand-written rectangles.
#
# Zero is a real screen, not a degenerate one: wallpaper, panel, dock and
# desktop icons with nothing on top. It is the only state in which the desktop
# chrome is unoccluded, so without it the corpus never shows a model what a full
# icon field or an unobstructed panel looks like - and a person's screen does
# look like that. It stays uncommon because it is also the least dense.
#
# The dense tail was thin: measured over 597 captures, seven- and eight-window
# scenes were 4.4% of the corpus between them. Those are the scenes that carry
# the occlusion, z-order and small-window cases the pipeline exists to get
# right, so they are worth more per sample than another two-window scene. The
# tail is raised rather than made common - real desktops are not usually that
# crowded, and the distribution should still look like a desktop's.
SCENE_APP_COUNT_WEIGHTS: Dict[int, int] = {
    0: 3,
    1: 6,
    2: 22,
    3: 21,
    4: 16,
    5: 12,
    6: 9,
    7: 7,
    8: 4,
}

#: What `layout` is called when there are no windows to lay out.
BARE_DESKTOP_LAYOUT = "bare_desktop"

# A browser is the most common thing on a desktop, but it was on **85% of
# scenes** (measured over 1,000 compositions) - a corpus where a model would
# learn that a browser is nearly always present, and where Chromium's page
# content dominates the element distribution. It is also the app with the most
# open annotation defects (backlog items 4, 5 and 7 are all Chromium), so
# over-representing it maximises exposure to them.
#
# Two levers, and the weight was the larger one: even at zero forced presence,
# a weight of 8.0 against a next-highest 4.5 still put it in 63% of scenes.
# Both are lowered, landing at ~51% - common, not near-universal.
SCENE_BROWSER_PRESENCE_PROB = 0.30

#: Share of two-to-four-app scenes that use a generative family instead of the
#: hand-written table, so the common counts are not limited to a fixed handful.
#: Raised from 0.45 once the families gained the arrangements people actually
#: use - maximised, centred, snapped to halves and quarters. The hand-written
#: tables stay, but they no longer carry most of the mass, because they were
#: twelve fixed rectangle sets and the families vary within each arrangement.
GENERATIVE_LAYOUT_PROB = 0.62

SCENE_APP_WEIGHTS: Dict[str, float] = {
    "chromium-browser": 4.0,
    "vscode": 4.5,
    "homebank": 3.8,
    "mousepad": 3.5,
    "qalculate-gtk": 3.0,
    "filezilla": 2.9,
    "bluefish": 2.8,
    "nautilus": 2.7,
    "file-roller": 2.6,
    "xarchiver": 2.4,
    "thunderbird": 2.2,
    "thunar": 2.0,
    "zim": 2.0,
    "seahorse": 1.9,
    "gnome-calculator": 1.9,
    "gnome-logs": 1.8,
    "gnome-text-editor": 1.7,
    "transmission-gtk": 1.6,
    "pluma": 1.6,
    "baobab": 1.5,
    "eog": 1.2,
}

APP_VISUAL_PRIORITY: Dict[str, int] = {
    "chromium-browser": 10,
    "vscode": 9,
    "thunderbird": 8,
    "homebank": 8,
    "filezilla": 8,
    "bluefish": 7,
    "zim": 7,
    "qalculate-gtk": 7,
    "nautilus": 6,
    "thunar": 6,
    "mousepad": 6,
    "seahorse": 6,
    "file-roller": 5,
    "xarchiver": 5,
    "gnome-logs": 5,
    "transmission-gtk": 5,
    "gnome-text-editor": 4,
    "pluma": 4,
    "baobab": 4,
    "eog": 4,
    "gnome-calculator": 3,
}

LAYOUT_WEIGHTS: Dict[int, List[tuple[str, int]]] = {
    2: [
        ("side_by_side_balanced", 32),
        ("side_by_side_offset", 24),
        ("primary_left", 17),
        ("primary_right", 13),
        ("cascade_light", 10),
        ("overlap_light", 4),
    ],
    3: [
        ("focus_left_stack_right", 26),
        ("focus_right_stack", 20),
        ("triple_column", 16),
        ("two_up_one_down", 16),
        ("side_by_side_plus_float", 14),
        ("cascade_light", 6),
        ("overlap_light", 2),
    ],
    4: [
        ("quad_grid", 52),
        ("primary_left_triple_right", 30),
        ("staggered_grid", 18),
    ],
}

CURATED_APP_STATES: Dict[str, List[str]] = {
    "baobab": [
        "default",
    ],
    # Added 2026-08-18 after an AT-SPI health audit of the 25 configured apps
    # that were not in the pool: it comes up, holds a real window and publishes
    # its contents - 165 elements in a scene, 157 of them table cells, at 1.2%
    # uncovered ink with no phantoms, drift or blanks.
    "gnome-system-monitor": [
        "default",
        "next_tab",
    ],
    # gucharmap is deliberately absent. It publishes 222 nodes, but its 157
    # table cells are the script list in the left pane - the character table
    # itself is a `drawing area` with **kids=0**, so the app's entire point is
    # an unannotatable canvas. Adding it on a 1.0% uncovered-ink reading was my
    # mistake: a big box "covers" text while saying nothing about it, which is
    # exactly the blind spot `audit_text_coverage.py` now exists to catch.
    "bluefish": [
        "default",
        "audit_probe",
        "find_dialog",
    ],
    "chromium-browser": [
        "single_python_downloads_scrolled",
        "chain:python_progressive:python_home",
        "chain:python_progressive:scrolled",
    ],
    "firefox-pdf": [
        "default",
        "page_down",
        "find_bar",
        "chain:pdf_progressive:page1",
        "chain:pdf_progressive:page2",
        "chain:pdf_progressive:find_bar",
    ],
    "vscode": [
        "default",
        "explorer_focus",
        "quick_open",
        "search_panel",
        "command_palette",
        "integrated_terminal",
        "problems_panel",
        "source_control",
        "settings_page",
        "chain:workspace_progressive:workspace_loaded",
        "chain:workspace_progressive:quick_open",
        "chain:workspace_progressive:search_panel",
        "chain:workspace_panels:command_palette",
        "chain:workspace_panels:problems_panel",
        "chain:workspace_panels:source_control",
        "chain:workspace_panels:integrated_terminal",
    ],
    "thunar": [
        "menu_open",
        "location_bar",
        "search_mode",
        "search_typed",
        "chain:search_progressive:search_mode",
        "chain:workspace_progressive:location_bar",
    ],
    "eog": [
        "default",
        "properties",
        "about_dialog",
        "chain:image_progressive:image_loaded",
        "chain:image_progressive:properties_open",
        "chain:image_progressive:gallery_toggle",
    ],
    "filezilla": [
        "default",
    ],
    "file-roller": [
        "default",
        "menu_open",
        "heuristic_probe",
        "chain:archive_progressive:menu_open",
        "chain:archive_progressive:heuristic_click",
    ],
    "gnome-calculator": [
        "default",
        "basic_input",
        "keyboard_mode",
        "menu_open",
        "chain:progressive_usage:empty",
        "chain:progressive_usage:result_shown",
        "chain:progressive_usage:menu_open",
    ],
    "gnome-text-editor": [
        "default",
        "open_dialog",
        "find_bar",
        "typed_text",
    ],
    "gnome-logs": [
        "default",
        "search",
    ],
    "homebank": [
        "default",
        "file_menu",
    ],
    "mousepad": [
        "default",
        "audit_probe",
        "find_dialog",
        "file_menu",
        "replace_dialog",
        "goto_line",
        "preferences_dialog",
        "context_menu",
        "chain:editor_progressive:search_dialog",
        "chain:editor_progressive:file_menu",
        "chain:editor_dialogs:replace_dialog",
        "chain:editor_dialogs:goto_line",
    ],
    "nautilus": [
        "default",
        "search",
        "context_menu",
        "grid_view",
        "sidebar_places",
    ],
    "pluma": [
        "default",
        "audit_probe",
        "find_dialog",
    ],
    "qalculate-gtk": [
        "default",
        "expression",
    ],
    "seahorse": [
        "default",
        "search",
        "app_menu",
    ],
    "thunderbird": [
        "default",
        "local_inbox",
    ],
    "transmission-gtk": [
        "default",
    ],
    "zim": [
        "default",
        "search",
        "file_menu",
    ],
    "xarchiver": [
        "default",
        "menu_open",
        "heuristic_probe",
        "chain:archive_progressive:menu_open",
        "chain:archive_progressive:heuristic_click",
    ],
}

DEFAULT_SCENE_APP_POOL = [
    "chromium-browser",
    "thunar",
    "vscode",
    "homebank",
    "mousepad",
    "qalculate-gtk",
    # filezilla is deliberately absent. Its local and remote file panes - the
    # bulk of its window - never reach AT-SPI: a full walk of a running instance
    # found 235 nodes and *zero* of role table, tree, list, list item, table row
    # or table cell, with the panes appearing as anonymous empty panels and
    # scroll panes. wxWidgets does not publish those controls on this stack, so
    # every capture containing FileZilla contributes a large region of visible
    # content that no annotation can cover: it measured 12-13% uncovered ink,
    # the worst in the pool, and unlike the others it is not a bug we can fix.
    # The manifest in configs/apps/filezilla.yaml is kept so it can be probed
    # again if the toolkit ever exposes them.
    "bluefish",
    # xarchiver, pluma and gnome-system-monitor are deliberately absent, and
    # unlike the others this is a capitulation rather than a diagnosis.
    #
    # They exit 0 without ever showing a window, and only they do: across a
    # 22,000-scene run they account for 295, 135 and 69 launch failures while
    # the other sixteen apps account for **zero**. That survived every fix
    # aimed at it - waiting for the window after a clean exit, killing leftover
    # instances by name, one job per host, staggered worker startup - and each
    # of them individually works when launched alone on an idle node. The
    # inotify exhaustion that produces the same GIO error is real and was
    # reproduced, but it is evidently not the whole cause, because spreading the
    # load did not move the rate.
    #
    # They were failing about a third of the time they were used, and a scene
    # that loses an app is a scene the quality gate rejects, so three apps were
    # costing far more samples than the diversity of three apps is worth. Their
    # manifests and curated states are kept: this is worth another look with a
    # session to interact with, not a permanent verdict.
    "nautilus",
    "file-roller",
    "zim",
    "seahorse",
    "gnome-logs",
    "gnome-text-editor",
    "transmission-gtk",
    # baobab is deliberately absent. It is a disk-usage analyser, so its window
    # is a list of the host's storage: it drew the cluster's GPFS mount and the
    # hostname `login2` as plain labels, 14 occurrences across one 86-capture
    # run. That is not a bug to fix - showing the machine's filesystem is the
    # app's entire purpose - so it is structurally incompatible with a corpus
    # that must not identify the machine. Same reasoning as gucharmap.
    #
    # thunderbird is deliberately absent too, and this one is recoverable.
    # Thunderbird 128's account-setup dialog pre-fills "Your full name" from the
    # passwd GECOS field, which on this host is a work email address and an
    # employee serial number; it also builds `<account>@<hostname>`. Prefs that
    # should stop the dialog opening (mail.provider.suppress_dialog_on_startup,
    # a fully defined account1/server1/id1) did not, in 3 of 86 captures. An
    # email client is a app class worth having, so this is worth another attempt
    # - but not at the cost of publishing somebody's name and address.
    "eog",
    "gnome-calculator",
]


@dataclass(frozen=True)
class WindowRect:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class SceneApp:
    app_name: str
    state_ref: str
    rect: WindowRect


@dataclass(frozen=True)
class SceneConfig:
    scene_id: str
    seed: int
    theme_preset: str
    display_preset: str
    panel_variant: str
    desktop_profile: str
    desktop_layout_template: str
    desktop_content_pack: str
    wallpaper_seed: int
    desktop_seed: int
    layout: str
    apps: List[SceneApp]


@dataclass(frozen=True)
class PlannedScene:
    scene: SceneConfig
    signature: str


@dataclass(frozen=True)
class ScenePlanRejection:
    scene: SceneConfig
    signature: str
    reason: str
    similarity_score: int = 0
    similar_to_seed: Optional[int] = None
    similar_to_scene_id: str = ""


def list_scene_app_pool() -> List[str]:
    return list(DEFAULT_SCENE_APP_POOL)


def _weighted_pick(rng: random.Random, weighted: Dict[str, int]) -> str:
    keys = list(weighted)
    weights = [max(1, int(weighted[key])) for key in keys]
    return rng.choices(keys, weights=weights, k=1)[0]


def _weighted_sample_without_replacement(
    pool: List[str],
    weights: Dict[str, float],
    rng: random.Random,
    count: int,
) -> List[str]:
    remaining = list(pool)
    chosen: List[str] = []
    for _ in range(min(count, len(remaining))):
        pick = rng.choices(
            remaining,
            weights=[max(0.1, float(weights.get(name, 1.0))) for name in remaining],
            k=1,
        )[0]
        chosen.append(pick)
        remaining.remove(pick)
    return chosen


def _choose_panel_variant(theme_preset: str, seed: int) -> str:
    rng = random.Random(seed)
    if theme_preset == "windows_redmond":
        options = [("bottom_tall", 54), ("bottom", 28), ("top", 10), ("top_slim", 8)]
    elif theme_preset in {"macos_tahoe_like", "macos_tahoe_glass", "quartz_night", "quartz_night_nord"}:
        options = [("top_slim_dock", 70), ("top_dock", 18), ("top_slim", 8), ("top", 4)]
    elif theme_preset == "ubuntu_like":
        options = [("left_dock", 58), ("left_slim_dock", 18), ("top", 14), ("top_slim", 6), ("bottom", 4)]
    else:
        options = [("top", 42), ("top_slim", 20), ("bottom", 20), ("bottom_tall", 10), ("top_tall", 8)]
    return rng.choices([name for name, _weight in options], weights=[weight for _name, weight in options], k=1)[0]


def _choose_scene_app_count(rng: random.Random, pool_size: int) -> int:
    supported = {count: weight for count, weight in SCENE_APP_COUNT_WEIGHTS.items() if count <= pool_size}
    if pool_size < 2 or not supported:
        return min(pool_size, 1)
    return int(_weighted_pick(rng, supported))


def _generative_layout_name(rng: random.Random, count: int) -> str:
    """A layout family name, tagged so it is distinguishable from the fixed ones.

    The count is passed through because what arrangement is *likely* depends on
    it: one window is usually maximised, two are usually snapped to halves, and
    eight are neither. See `layouts.FAMILY_WEIGHTS_BY_COUNT`.
    """
    return f"family:{choose_family(rng, count)}"


def _choose_scene_apps(pool: List[str], rng: random.Random, count: int) -> List[str]:
    candidates = sorted(set(pool))
    chosen: List[str] = []
    if "chromium-browser" in candidates and count >= 2 and rng.random() < SCENE_BROWSER_PRESENCE_PROB:
        chosen.append("chromium-browser")
        candidates.remove("chromium-browser")
    chosen.extend(
        _weighted_sample_without_replacement(
            candidates,
            SCENE_APP_WEIGHTS,
            rng,
            count - len(chosen),
        )
    )
    return chosen[:count]


def _order_scene_apps(apps: List[str], *, seed: int) -> List[str]:
    rng = random.Random(seed + 91_177)
    tie_breakers = {app: rng.random() for app in apps}
    return sorted(
        apps,
        key=lambda app: (-APP_VISUAL_PRIORITY.get(app, 1), tie_breakers[app], app),
    )


def _choose_layout(rng: random.Random, count: int) -> str:
    layouts = LAYOUT_WEIGHTS.get(count)
    if not layouts:
        # Five windows and up have no hand-written table; the families cover any
        # count, and cover it with more size variation than a fixed list does.
        return _generative_layout_name(rng, count)
    # Even where a table exists, a share of scenes use a family instead, so the
    # common counts are not limited to the handful of arrangements written down.
    if rng.random() < GENERATIVE_LAYOUT_PROB:
        return _generative_layout_name(rng, count)
    return rng.choices(
        [name for name, _weight in layouts],
        weights=[weight for _name, weight in layouts],
        k=1,
    )[0]


def compose_scene(
    seed: int,
    *,
    available_apps: Optional[List[str]] = None,
    session_envelope_seed: Optional[int] = None,
    force_app_count: Optional[int] = None,
    uniform_pages: bool = False,
) -> SceneConfig:
    """Compose one deterministic mixed-app scene from the curated qualified pool.

    `force_app_count` overrides the weighted draw. It exists so a batch can be
    planned for one window count on purpose - the bare desktop especially, which
    is 3% of the natural distribution and therefore impractical to collect by
    filtering: 5,000 of them would need 167,000 planned scenes.
    """
    rng = random.Random(seed)
    pool = [name for name in (available_apps or list_scene_app_pool()) if name in CURATED_APP_STATES]
    # Two apps are needed to *mix* them. A batch that pins the window count to
    # one - a single-application diversity set - has nothing to mix and one app
    # is the whole point, so the floor only applies above that.
    floor = 2 if (force_app_count is None or force_app_count > 1) else 1
    if len(pool) < floor:
        raise ValueError(
            f"scene composition needs at least {floor} curated app(s), got {len(pool)}"
        )

    if force_app_count is None:
        num_apps = _choose_scene_app_count(rng, len(pool))
    else:
        num_apps = max(0, min(int(force_app_count), len(pool)))
        # The draw is skipped, but the RNG must still advance by the same amount
        # or every downstream choice shifts and the seed stops describing the
        # scene it described before.
        _choose_scene_app_count(rng, len(pool))
    chosen_apps = _choose_scene_apps(pool, rng, num_apps) if num_apps else []
    env_seed = seed if session_envelope_seed is None else session_envelope_seed
    env_rng = random.Random(env_seed)
    theme_preset = _weighted_pick(env_rng, SCENE_THEME_WEIGHTS)
    display_preset = _weighted_pick(env_rng, SCENE_DISPLAY_WEIGHTS)
    panel_variant = _choose_panel_variant(theme_preset, env_seed + 101)
    desktop_profile = _weighted_pick(env_rng, SCENE_DESKTOP_PROFILE_WEIGHTS)
    if not num_apps and desktop_profile == "empty":
        # A bare desktop with an empty icon field is the one scene that has
        # nothing in it: wallpaper, a panel, and two leaf elements. It fails
        # `min_elements` every time, so the pair is not a rare bad draw - it is a
        # guaranteed rejection, and it wastes a whole session to produce it.
        #
        # The zero-application scene exists to show the desktop *unobstructed*,
        # which needs something on the desktop to show. Re-draw from the profiles
        # that put icons there; the wallpaper is still fully visible in all of
        # them, and `empty` keeps its weight for every scene that has a window
        # on top.
        desktop_profile = _weighted_pick(
            env_rng,
            {k: v for k, v in SCENE_DESKTOP_PROFILE_WEIGHTS.items() if k != "empty"},
        )
    desktop_layout_template = sample_desktop_layout_template(env_seed + 211)
    desktop_content_pack = sample_desktop_content_pack(env_seed + 307)
    layout = _choose_layout(rng, num_apps) if num_apps else BARE_DESKTOP_LAYOUT
    display = get_display_preset(display_preset)
    rects = (
        _layout_rects(
            layout,
            width=display.width,
            height=display.height,
            count=num_apps,
            seed=seed,
        )
        if num_apps
        else []
    )
    chosen_apps = _order_scene_apps(chosen_apps, seed=seed)

    apps = [
        SceneApp(
            app_name=app_name,
            state_ref=_choose_scene_state(
                app_name, seed + i * 17, uniform_pages=uniform_pages
            ),
            rect=rects[i],
        )
        for i, app_name in enumerate(chosen_apps)
    ]
    scene_id = stable_hash(f"scene-{seed}-{theme_preset}-{display_preset}-{layout}-{'-'.join(chosen_apps)}")
    return SceneConfig(
        scene_id=scene_id,
        seed=seed,
        theme_preset=theme_preset,
        display_preset=display_preset,
        panel_variant=panel_variant,
        desktop_profile=desktop_profile,
        desktop_layout_template=desktop_layout_template,
        desktop_content_pack=desktop_content_pack,
        wallpaper_seed=env_seed + 401,
        desktop_seed=env_seed + 701,
        layout=layout,
        apps=apps,
    )


def compose_scene_batch(
    *,
    start_seed: int,
    count: int,
    available_apps: Optional[List[str]] = None,
    session_group_size: int = 1,
) -> List[SceneConfig]:
    """Compose a deterministic sequence of scenes from a contiguous seed range."""
    if count <= 0:
        return []
    session_group_size = max(1, int(session_group_size or 1))
    scenes: List[SceneConfig] = []
    for offset in range(count):
        seed = start_seed + offset
        group_index = offset // session_group_size
        env_seed = start_seed + group_index * 100_003 if session_group_size > 1 else seed
        scenes.append(
            compose_scene(
                seed,
                available_apps=available_apps,
                session_envelope_seed=env_seed,
            )
        )
    return scenes


def compose_diverse_scene_batch(
    *,
    start_seed: int,
    count: int,
    available_apps: Optional[List[str]] = None,
    max_attempts: Optional[int] = None,
    existing_scenes: Optional[List[SceneConfig]] = None,
    session_group_size: int = 1,
) -> Tuple[List[PlannedScene], List[ScenePlanRejection], int]:
    """Compose a deterministic batch while rejecting near-duplicate plans."""
    if count <= 0:
        return [], [], start_seed

    max_attempts = max_attempts or max(count * 12, count + 12)
    comparison_pool = list(existing_scenes or [])
    accepted: List[PlannedScene] = []
    rejected: List[ScenePlanRejection] = []
    exact_signatures: set[str] = {scene_signature(scene) for scene in comparison_pool}
    pool = [name for name in (available_apps or list_scene_app_pool()) if name in CURATED_APP_STATES]

    seed = start_seed
    attempts = 0
    while len(accepted) < count and attempts < max_attempts:
        accepted_index = len(comparison_pool)
        group_index = accepted_index // max(1, int(session_group_size or 1))
        env_seed = start_seed + group_index * 100_003 if session_group_size > 1 else seed
        scene = compose_scene(
            seed,
            available_apps=available_apps,
            session_envelope_seed=env_seed,
        )
        signature = scene_signature(scene)
        attempts += 1
        seed += 1

        if signature in exact_signatures:
            rejected.append(
                ScenePlanRejection(
                    scene=scene,
                    signature=signature,
                    reason="duplicate_signature",
                )
            )
            continue

        nearest_seed: Optional[int] = None
        nearest_scene_id = ""
        best_score = -1
        for planned_scene in comparison_pool:
            score, _reasons = scene_similarity(scene, planned_scene)
            if score > best_score:
                best_score = score
                nearest_seed = planned_scene.seed
                nearest_scene_id = planned_scene.scene_id

        if best_score >= 10:
            rejected.append(
                ScenePlanRejection(
                    scene=scene,
                    signature=signature,
                    reason="too_similar_prelaunch",
                    similarity_score=best_score,
                    similar_to_seed=nearest_seed,
                    similar_to_scene_id=nearest_scene_id,
                )
            )
            continue

        coverage_penalty, coverage_reason = scene_coverage_penalty(
            scene,
            comparison_pool,
            pool=pool,
        )
        if coverage_penalty >= 4:
            rejected.append(
                ScenePlanRejection(
                    scene=scene,
                    signature=signature,
                    reason=coverage_reason or "overused_apps_prelaunch",
                    similarity_score=coverage_penalty,
                )
            )
            continue

        exact_signatures.add(signature)
        planned = PlannedScene(scene=scene, signature=signature)
        accepted.append(planned)
        comparison_pool.append(scene)

    return accepted, rejected, seed



#: How many recently-planned scenes a candidate is compared against for
#: near-duplicate rejection. `compose_diverse_scene_batch` compares against
#: every scene accepted so far, which is O(n^2): 300 scenes take 3.3s and 30,000
#: would take hours - unusable for planning a corpus in one pass. Near-duplicates
#: matter most when they are close together (a reviewer scrolling a shard sees
#: them side by side), and global balance is handled separately by the coverage
#: counters, which are exact and cost nothing to keep. So the window is bounded
#: and the counters are not.
DEFAULT_SIMILARITY_WINDOW = 256


@dataclass
class _CorpusLedger:
    """Running state for planning a corpus in one pass.

    Everything here is incremental. The counters are global and exact; only the
    similarity comparison is windowed.
    """

    pool: List[str]
    window: int = DEFAULT_SIMILARITY_WINDOW
    signatures: set = field(default_factory=set)
    #: Kept for the whole corpus, not a window: the point of a coarse key is
    #: that it is cheap enough to enforce globally, which turns "we measured no
    #: look-alikes in a sample" into "there cannot be one at any size".
    visual_keys: set = field(default_factory=set)
    recent: List[SceneConfig] = field(default_factory=list)
    app_counts: Dict[str, int] = field(default_factory=dict)
    combo_counts: Dict[Tuple[str, ...], int] = field(default_factory=dict)
    #: Scenes and distinct app-sets seen, **per window count**. The combo rule
    #: has to compare like with like: there is exactly one possible app set for
    #: a zero-window scene and thousands for a four-window one.
    scenes_by_count: Dict[int, int] = field(default_factory=dict)
    combos_by_count: Dict[int, set] = field(default_factory=dict)
    total_assignments: int = 0
    num_scenes: int = 0

    def __post_init__(self) -> None:
        for name in self.pool:
            self.app_counts.setdefault(name, 0)

    def _expected(self, app_name: str) -> float:
        total_weight = sum(SCENE_APP_WEIGHTS.get(n, 1.0) for n in self.pool) or 1.0
        return self.total_assignments * (SCENE_APP_WEIGHTS.get(app_name, 1.0) / total_weight)

    def coverage_penalty(self, scene: SceneConfig) -> Tuple[int, str]:
        """The same balance rule as `scene_coverage_penalty`, done in O(1)."""
        if not self.total_assignments:
            return 0, ""
        chosen = [app.app_name for app in scene.apps]
        penalty = 0
        for app_name in chosen:
            count = self.app_counts.get(app_name, 0)
            expected = self._expected(app_name)
            # Proportional slack, not absolute. `expected + 1.25` is a sensible
            # bar for a batch of thirty and a nonsensical one for a corpus: at
            # 46,000 scenes an app's expected count is around 2,000, and
            # demanding it land within 1.25 of that rejects almost everything.
            # Measured: asking for 60,000 scenes delivered 46,350 because the
            # planner exhausted its attempt budget at 15.5 tries per scene,
            # 558,381 of them refused on this rule alone.
            if count > expected + max(2.0, expected * 0.05):
                penalty += max(1, int(round((count - expected) / max(1.0, expected * 0.05))))
        # `scene_coverage_penalty` charges 2 per previous use of an app combo,
        # which is right for a batch of thirty and fatal for a corpus: with 21
        # apps there are only 21 one-app combos and ~210 two-app ones, so past a
        # few thousand scenes every combo has been used twice, every candidate
        # scores >= 4, and planning stalls. Measured: 6,000 scenes cost 51,712
        # rejected attempts and the rate was still climbing.
        #
        # A combo repeating is not the defect - an identical *scene* is, and the
        # signature check already catches that. So the charge is only for using
        # a combo more than its fair share, where fair share is the running mean
        # over the combos seen so far. That is scale-free.
        # The allowance is the mean reuse *among scenes with the same number of
        # windows*, because that is the only comparison that means anything. The
        # previous version averaged over every combo seen at any size, which
        # made small scenes look permanently overused: a zero-window scene has
        # one possible app set, a one-window scene has 21, a four-window scene
        # has thousands. Measured over 3,000 planned scenes, that rejected 98%
        # of zero-window candidates, 83% of one-window and 60% of two-window
        # against 15% of four-window - so the window-count distribution came out
        # as 7.6% two-window where its weight asks for 22%.
        combo = tuple(sorted(chosen))
        reuse = self.combo_counts.get(combo, 0)
        size = len(chosen)
        seen_here = len(self.combos_by_count.get(size, ()))
        # Half again over the mean, because a combo being used a bit more than
        # average is not a defect - the app weights guarantee some will be - and
        # rejecting on it costs window counts their share.
        mean_reuse = self.scenes_by_count.get(size, 0) / max(1, seen_here)
        allowance = max(2, round(mean_reuse * 1.5))
        if reuse > allowance:
            penalty += (reuse - allowance) * 2
        # There used to be a second rule here: +2 if none of the chosen apps was
        # currently underused. It distorted the window-count distribution badly,
        # because whether a scene contains an underused app depends on how many
        # apps it has. A two-window scene draws two names and usually misses,
        # an eight-window scene draws eight and usually hits - so two-window
        # scenes were rejected far more often. Measured over 8,000 planned
        # scenes: two-window came out at 6.6% against the 22% its weight asks
        # for, which quietly overrode the window-count distribution.
        #
        # It was also redundant. Penalising an *overused* app already frees room
        # for the rest by conservation, and the measured balance without it is
        # within 0.7 percentage points of every app's weighted share.
        reason = "overused_apps_prelaunch" if penalty else ""
        return penalty, reason

    def nearest(self, scene: SceneConfig) -> Tuple[int, Optional[int], str]:
        best, seed, scene_id = -1, None, ""
        for other in self.recent:
            score, _reasons = scene_similarity(scene, other)
            if score > best:
                best, seed, scene_id = score, other.seed, other.scene_id
        return best, seed, scene_id

    def accept(self, scene: SceneConfig, signature: str) -> None:
        self.signatures.add(signature)
        self.visual_keys.add(scene_visual_key(scene))
        self.recent.append(scene)
        if len(self.recent) > self.window:
            del self.recent[: len(self.recent) - self.window]
        self.num_scenes += 1
        combo = tuple(sorted(app.app_name for app in scene.apps))
        size = len(scene.apps)
        self.scenes_by_count[size] = self.scenes_by_count.get(size, 0) + 1
        self.combos_by_count.setdefault(size, set()).add(combo)
        self.combo_counts[combo] = self.combo_counts.get(combo, 0) + 1
        for app in scene.apps:
            if app.app_name in self.app_counts:
                self.app_counts[app.app_name] += 1
                self.total_assignments += 1


def compose_corpus_plan(
    *,
    start_seed: int,
    count: int,
    available_apps: Optional[List[str]] = None,
    window: int = DEFAULT_SIMILARITY_WINDOW,
    max_attempts: Optional[int] = None,
    session_group_size: int = 1,
    force_app_count: Optional[int] = None,
    uniform_pages: bool = False,
) -> Tuple[List[PlannedScene], List[ScenePlanRejection], int]:
    """Plan a whole corpus in one pass, at a cost linear in its size.

    Same acceptance rules as `compose_diverse_scene_batch` - exact signature,
    near-duplicate score, app-balance penalty - but the near-duplicate check
    looks at a bounded window rather than the entire corpus, and the balance
    counters are updated incrementally instead of recomputed per candidate.

    Planning the corpus once, before any capture runs, is what makes shards
    independent: each worker takes a slice of the plan, and no two shards can
    duplicate each other because the deduplication happened before the split.
    """
    if count <= 0:
        return [], [], start_seed

    pool = [name for name in (available_apps or list_scene_app_pool()) if name in CURATED_APP_STATES]
    ledger = _CorpusLedger(pool=pool, window=max(1, int(window)))
    accepted: List[PlannedScene] = []
    rejected: List[ScenePlanRejection] = []
    max_attempts = max_attempts or max(count * 12, count + 12)
    group_size = max(1, int(session_group_size or 1))

    seed = start_seed
    attempts = 0
    while len(accepted) < count and attempts < max_attempts:
        group_index = len(accepted) // group_size
        env_seed = start_seed + group_index * 100_003 if group_size > 1 else seed
        scene = compose_scene(
            seed,
            available_apps=available_apps,
            session_envelope_seed=env_seed,
            force_app_count=force_app_count,
            uniform_pages=uniform_pages,
        )
        signature = scene_signature(scene)
        attempts += 1
        seed += 1

        if signature in ledger.signatures:
            rejected.append(ScenePlanRejection(scene, signature, "duplicate_signature"))
            continue

        # The signature is exact, so two scenes a pixel apart both pass it. This
        # is the check that actually asks "would these look the same".
        if scene_visual_key(scene) in ledger.visual_keys:
            rejected.append(ScenePlanRejection(scene, signature, "duplicate_appearance"))
            continue

        score, near_seed, near_id = ledger.nearest(scene)
        if score >= 10:
            rejected.append(
                ScenePlanRejection(
                    scene, signature, "too_similar_prelaunch",
                    similarity_score=score,
                    similar_to_seed=near_seed,
                    similar_to_scene_id=near_id,
                )
            )
            continue

        penalty, reason = ledger.coverage_penalty(scene)
        if penalty >= 4:
            rejected.append(
                ScenePlanRejection(scene, signature, reason or "overused_apps_prelaunch",
                                   similarity_score=penalty)
            )
            continue

        ledger.accept(scene, signature)
        accepted.append(PlannedScene(scene=scene, signature=signature))

    return accepted, rejected, seed


def _load_observation_elements(result: Optional[Dict[str, object]]) -> List[Dict[str, Any]]:
    """Read back the leaf elements an observation just wrote to disk."""
    if not isinstance(result, dict):
        return []
    path = result.get("elements_leaf") or result.get("elements")
    if not isinstance(path, str):
        return []
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Could not read observation elements from %s", path, exc_info=True)
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        elements = data.get("elements")
        return elements if isinstance(elements, list) else []
    return []


class DesktopEnv:
    """A desktop session you can observe more than once.

    Static single-frame collection is the degenerate case of this API rather
    than a separate mode, so there is only one code path to maintain and the
    static behaviour cannot drift:

        env = DesktopEnv(config, output_dir=out)
        obs = env.reset(scene)   # exactly today's single capture
        env.close()

    Holding the session across observations also amortises startup, which
    dominates per-scene cost, so even purely static batches get cheaper.
    """

    def __init__(
        self,
        config: PipelineConfig,
        *,
        output_dir: Path,
        include_desktop_chrome: bool = True,
        app_configs_dir: Optional[Path] = None,
        preloaded_manifests: Optional[Dict[str, AppManifest]] = None,
    ) -> None:
        self.config = config
        self.output_dir = output_dir
        self.include_desktop_chrome = include_desktop_chrome
        self._manifests = preloaded_manifests or {
            m.app_name: m for m in load_manifests(app_configs_dir or config.apps_config_dir)
        }
        self._session: Optional[DesktopSession] = None
        self._session_signature: Optional[str] = None
        self._launched: Optional[LaunchedScene] = None
        self._step_index = 0
        self._last_elements: List[Dict[str, Any]] = []
        self.action_settle_sec = 1.2
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def __enter__(self) -> "DesktopEnv":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _require_manifests(self, scene: SceneConfig) -> None:
        missing = [a.app_name for a in scene.apps if a.app_name not in self._manifests]
        if missing:
            raise ValueError(f"Scene references unknown manifests: {missing}")

    def reset(self, scene: SceneConfig) -> Optional[Dict[str, object]]:
        """Bring up the scene and return its first observation."""
        self._require_manifests(scene)
        self._teardown_apps()

        scene_config = _build_scene_runtime_config(scene, self.config)
        signature = scene_session_signature(scene)
        # Only restart the session when the envelope actually differs; reusing a
        # matching one is what makes multi-scene runs cheap.
        if self._session is not None and self._session_signature != signature:
            self._close_session()
        if self._session is None:
            self._session = DesktopSession(scene_config.session)
            self._session.__enter__()
            self._session_signature = signature

        self._launched = launch_scene_apps(
            scene, scene_config=scene_config, manifests=self._manifests
        )
        self._step_index = 0
        # Not a fixed sleep: a dense scene has more windows still drawing, and
        # capturing during a redraw is what puts elements in the ground truth
        # that the screenshot never showed.
        self._readiness = wait_for_apps_to_settle(self._launched.app_targets)
        return self.observe()

    def observe(self) -> Optional[Dict[str, object]]:
        """Capture the current screen state."""
        if self._launched is None:
            raise RuntimeError("DesktopEnv.reset() must be called before observe()")
        result = capture_launched_scene(
            self._launched,
            out=self.output_dir,
            include_desktop_chrome=self.include_desktop_chrome,
            step_index=self._step_index,
        )
        self._last_elements = _load_observation_elements(result)
        return result

    @property
    def last_elements(self) -> List[Dict[str, Any]]:
        """Elements from the most recent observation."""
        return list(self._last_elements)

    def step(self, action: SceneAction) -> Dict[str, object]:
        """Apply one action and observe the result.

        Returns the new observation plus the element-level diff against the
        previous one, which is the actual supervision signal: what the action
        changed, not merely what the screen looks like afterwards.
        """
        if self._launched is None:
            raise RuntimeError("DesktopEnv.reset() must be called before step()")

        before = list(self._last_elements)
        # Where the action actually landed, and on what. Recorded because an
        # action dataset needs the point and the target's box in the *previous*
        # frame's coordinates - "click at (x, y)" is the supervision, and
        # recovering it later from a uid means hoping the uid survived into the
        # exported element list.
        target = next(
            (e for e in before if e.get("uid") == action.target_uid), None
        ) if action.target_uid else None
        point = None
        if action.type in ("click", "scroll"):
            try:
                point = resolve_action_point(action, before)
            except Exception:
                point = None

        self._apply_action(action, before)
        time.sleep(self.action_settle_sec)

        self._step_index += 1
        observation = self.observe()
        after = list(self._last_elements)
        record = action.to_dict()
        if point is not None:
            record["point"] = [int(point[0]), int(point[1])]
        if target is not None:
            record["target"] = {
                "role": target.get("role"),
                "kind": target.get("kind"),
                "name": target.get("name"),
                "visible_text": target.get("visible_text"),
                "rect": target.get("rect"),
                "app_name": target.get("app_name"),
            }
        return {
            "action": record,
            "observation": observation,
            "diff": diff_states(before, after),
            "step_index": self._step_index,
        }

    def _apply_action(self, action: SceneAction, elements: List[Dict[str, Any]]) -> None:
        kind = action.type
        if kind == "wait":
            time.sleep(max(0.0, action.seconds))
            return
        if kind == "key":
            xdo_key(action.value)
            return
        if kind == "type":
            xdo_type_text(action.text)
            return
        if kind in ("click", "scroll"):
            point = resolve_action_point(action, elements)
            if point is None:
                raise ValueError(f"Action {kind} has no resolvable target")
            if kind == "click":
                self._click_releasing_grabs(point[0], point[1])
            else:
                # Buttons 4/5 are wheel up/down.
                button = 4 if action.dy > 0 else 5
                for _ in range(max(1, abs(action.dy))):
                    xdo_click(point[0], point[1], button=button)
            return
        raise ValueError(f"Unknown action type: {kind}")

    def act(
        self,
        action: SceneAction,
        state: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Apply an action without capturing the result.

        Deciding *what* to do often needs only the accessibility tree - which
        menus an app offers, whether a dialog opened - and a full observation
        costs a screenshot plus the whole annotation pipeline. Probing an app
        with `step()` would spend that on frames nobody keeps. Trajectory steps
        still go through `step()`, because those frames are the dataset.

        `state` is the state the action was chosen from, and it matters: an
        action naming an element that only exists in a peeked tree cannot be
        resolved against the last observation, so it silently has nowhere to
        click.
        """
        self._apply_action(action, self._last_elements if state is None else state)
        time.sleep(self.action_settle_sec)

    def _click_releasing_grabs(self, x: int, y: int) -> None:
        """Click, recovering from a pointer grab left by the previous action.

        Clicking a widget can put the app into a state that grabs the pointer -
        measured on Thunar's sidebar, where clicking a section heading left every
        subsequent `mousemove --sync` blocking until its ten-second timeout. The
        grab is invisible in the accessibility tree, so it cannot be predicted;
        it can only be cleared and retried.
        """
        for attempt in range(3):
            try:
                xdo_click(x, y)
                return
            except XdotoolTimeout:
                logger.info(
                    "click at (%d,%d) blocked (attempt %d); releasing grabs",
                    x, y, attempt + 1,
                )
                self.release_grabs()
        xdo_click(x, y)

    def release_grabs(self) -> None:
        """Dismiss anything holding a pointer grab, such as an open menu."""
        # Escape twice with a pause: one press dismisses a menu, and a second
        # clears whatever that menu was itself nested inside. The pause matters
        # because the grab is released asynchronously, and a click issued in the
        # same instant blocks again.
        for _ in range(2):
            try:
                xdo_key("Escape")
            except Exception:
                logger.warning("could not release pointer grabs", exc_info=True)
                return
            time.sleep(0.4)

    def peek(self) -> List[Dict[str, Any]]:
        """Read the accessibility tree only: no screenshot, no annotation.

        Elements get the same `uid` and `interaction` block an observation
        carries, so a skill or predicate written against observed state works
        unchanged against peeked state - there is no second representation to
        keep in step. What they do *not* get is occlusion: nothing here has been
        compared against pixels, so a peeked element is a control signal and
        never ground truth.
        """
        from deskshot.extraction.atspi_walker import walk_application
        from deskshot.extraction.interaction_state import annotate_interaction_state
        from deskshot.extraction.text_visibility import _element_uid

        if self._launched is None:
            raise RuntimeError("DesktopEnv.reset() must be called before peek()")
        display = self.config.session.display
        elements: List[Dict[str, Any]] = []
        for atspi_name, app_name in self._launched.app_targets:
            try:
                walked = walk_application(atspi_name, display.width, display.height)
            except Exception:
                logger.warning("peek: could not walk %s", atspi_name, exc_info=True)
                continue
            for elem in walked:
                elem["app_name"] = app_name
                elem["_atspi_app_name"] = atspi_name
                elem["uid"] = _element_uid(atspi_name, elem.get("_atspi_path") or [])
            elements.extend(walked)
        annotate_interaction_state(elements)
        return elements

    def _teardown_apps(self) -> None:
        if self._launched is not None:
            teardown_launched_scene(self._launched)
            self._launched = None

    def _close_session(self) -> None:
        if self._session is not None:
            try:
                self._session.__exit__(None, None, None)
            finally:
                self._session = None
                self._session_signature = None

    def close(self) -> None:
        try:
            self._teardown_apps()
        finally:
            self._close_session()


def run_scene_extraction(
    scene: SceneConfig,
    config: PipelineConfig,
    *,
    output_dir: Optional[Path] = None,
    include_desktop_chrome: bool = True,
    app_configs_dir: Optional[Path] = None,
    preloaded_manifests: Optional[Dict[str, AppManifest]] = None,
) -> Optional[Dict[str, object]]:
    """Run a composed mixed-app scene inside one desktop session."""
    out = resolve_capture_dir(output_dir or config.output_dir, scene)
    out.mkdir(parents=True, exist_ok=True)

    manifests = preloaded_manifests or {
        m.app_name: m for m in load_manifests(app_configs_dir or config.apps_config_dir)
    }
    missing = [app.app_name for app in scene.apps if app.app_name not in manifests]
    if missing:
        raise ValueError(f"Scene references unknown manifests: {missing}")

    # A static capture is a zero-action episode; same code path as stepping.
    with DesktopEnv(
        config,
        output_dir=out,
        include_desktop_chrome=include_desktop_chrome,
        preloaded_manifests=manifests,
    ) as env:
        return env.reset(scene)


#: Consecutive failed actions before an episode is abandoned. One failure is
#: usually a single unreachable target; several in a row means the session is
#: wedged and further frames would be the same picture.
MAX_CONSECUTIVE_STEP_FAILURES = 3


def run_scene_episode(
    scene: SceneConfig,
    config: PipelineConfig,
    *,
    steps: int,
    output_dir: Optional[Path] = None,
    include_desktop_chrome: bool = True,
    app_configs_dir: Optional[Path] = None,
    preloaded_manifests: Optional[Dict[str, AppManifest]] = None,
    candidate_pool: int = 12,
) -> Optional[Dict[str, object]]:
    """Capture one scene as an episode: observe, act, observe again.

    Returns the *first* observation, so a caller that only wants a row back gets
    the same shape as `run_scene_extraction`. The episode itself is written
    alongside the captures as `episode-<scene_id>.json`, keyed by the capture
    stems, which are deterministic in (seed, step) - that is what lets a step be
    matched back to its observation later.

    Zero steps is a static capture, on the same code path, so the static
    behaviour cannot drift away from the stepped one.
    """
    from deskshot.generation.actions import sample_actionable_targets

    out = resolve_capture_dir(output_dir or config.output_dir, scene, is_episode=True)
    out.mkdir(parents=True, exist_ok=True)
    manifests = preloaded_manifests or {
        m.app_name: m for m in load_manifests(app_configs_dir or config.apps_config_dir)
    }
    rng = random.Random(scene.seed ^ 0xE9150DE)
    trace: List[Dict[str, Any]] = []
    consecutive_failures = 0

    with DesktopEnv(
        config,
        output_dir=out,
        include_desktop_chrome=include_desktop_chrome,
        preloaded_manifests=manifests,
    ) as env:
        first = env.reset(scene)
        if first is None:
            return None
        trace.append({"step_index": 0, "action": None,
                      "observation_stem": first.get("stem")})

        for _ in range(max(0, int(steps))):
            targets = sample_actionable_targets(env.last_elements, limit=candidate_pool)
            if not targets:
                logger.info("[episode:%s] no actionable targets left", scene.scene_id)
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
                # One failed action is not the end of the trajectory. Ending the
                # episode here threw away every remaining frame of work whose
                # session was already paid for: 1,807 step failures in one run
                # cut roughly half of all episodes short, and the corpus came
                # out at 64% of its planned size. Most failures are a single
                # unreachable target, and the next action is usually fine.
                consecutive_failures += 1
                logger.warning(
                    "[episode:%s] an action failed (%d in a row)",
                    scene.scene_id,
                    consecutive_failures,
                    exc_info=True,
                )
                if consecutive_failures >= MAX_CONSECUTIVE_STEP_FAILURES:
                    logger.warning(
                        "[episode:%s] giving up after %d consecutive failures",
                        scene.scene_id,
                        consecutive_failures,
                    )
                    break
                continue
            consecutive_failures = 0
            observation = result.get("observation") or {}
            trace.append(
                {
                    "step_index": result.get("step_index"),
                    "action": result.get("action"),
                    "diff": result.get("diff"),
                    "observation_stem": observation.get("stem"),
                }
            )

    episode_name = "episode.json" if corpus_layout_enabled() else f"episode-{scene.scene_id}.json"
    (out / episode_name).write_text(
        json.dumps(
            {
                "scene_id": scene.scene_id,
                "seed": scene.seed,
                "requested_steps": int(steps),
                "captured_steps": len(trace),
                "scene": asdict(scene),
                "steps": trace,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return first


def run_scene_group(
    scenes: List[SceneConfig],
    config: PipelineConfig,
    *,
    output_dir: Path,
    include_desktop_chrome: bool = True,
    app_configs_dir: Optional[Path] = None,
) -> List[tuple[SceneConfig, Optional[Dict[str, object]]]]:
    """Run a consecutive group of scenes inside one shared desktop session."""
    if not scenes:
        return []

    manifests = {m.app_name: m for m in load_manifests(app_configs_dir or config.apps_config_dir)}
    missing = sorted(
        {
            app.app_name
            for scene in scenes
            for app in scene.apps
            if app.app_name not in manifests
        }
    )
    if missing:
        raise ValueError(f"Scene references unknown manifests: {missing}")

    output_dir.mkdir(parents=True, exist_ok=True)
    first = scenes[0]
    session_signature = scene_session_signature(first)
    scene_config = _build_scene_runtime_config(first, config)
    rows: List[tuple[SceneConfig, Optional[Dict[str, object]]]] = []
    # Scenes here share one session envelope, so the env reuses the session
    # across resets instead of paying startup per scene.
    with DesktopEnv(
        config,
        output_dir=output_dir,
        include_desktop_chrome=include_desktop_chrome,
        preloaded_manifests=manifests,
    ) as env:
        for scene in scenes:
            if scene_session_signature(scene) != session_signature:
                raise ValueError("run_scene_group requires all scenes to share one session envelope")
            try:
                result = env.reset(scene)
            except Exception:
                logger.exception("[scene-group] Scene failed: seed=%d scene_id=%s", scene.seed, scene.scene_id)
                result = None
            rows.append((scene, result))
    return rows


def run_scene_batch(
    config: PipelineConfig,
    *,
    start_seed: int,
    count: int,
    output_dir: Path,
    include_desktop_chrome: bool = True,
    available_apps: Optional[List[str]] = None,
    session_group_size: int = 1,
) -> List[Dict[str, object]]:
    """Run a deterministic scene batch sequentially and save a machine-readable summary."""
    output_dir.mkdir(parents=True, exist_ok=True)
    scenes = compose_scene_batch(
        start_seed=start_seed,
        count=count,
        available_apps=available_apps,
        session_group_size=session_group_size,
    )
    (output_dir / "scene_batch.json").write_text(
        json.dumps([asdict(scene) for scene in scenes], indent=2),
        encoding="utf-8",
    )

    results: List[Dict[str, object]] = []
    rows: List[tuple[SceneConfig, Optional[Dict[str, object]]]] = []
    for scene in scenes:
        logger.info("[scene-batch] Running seed=%d scene_id=%s", scene.seed, scene.scene_id)
        try:
            result = run_scene_extraction(
                scene,
                config,
                output_dir=output_dir,
                include_desktop_chrome=include_desktop_chrome,
            )
        except Exception:
            logger.exception("[scene-batch] Scene failed: seed=%d scene_id=%s", scene.seed, scene.scene_id)
            result = None
        rows.append((scene, result))
        if result is not None:
            results.append(result)

    summary_lines = [
        f"start_seed: {start_seed}",
        f"count: {count}",
        f"completed: {len(results)}",
        "",
    ]
    for scene, result in rows:
        summary_lines.extend(
            [
                f"- seed={scene.seed} scene_id={scene.scene_id}",
                f"  layout={scene.layout} theme={scene.theme_preset} display={scene.display_preset}",
                f"  apps={', '.join(f'{app.app_name}:{app.state_ref}' for app in scene.apps)}",
                (
                    f"  stem={result['stem']} num_elements={result['num_elements']}"
                    if result is not None
                    else "  status=failed"
                ),
            ]
        )
    (output_dir / "summary.md").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    return results


#: Set to route captures into a sharded corpus tree instead of one flat
#: directory. See `corpus_relative_dir`.
CORPUS_LAYOUT_ENV = "DESKSHOT_CORPUS_LAYOUT"


def scene_capture_stem(scene: SceneConfig, step_index: int = 0) -> str:
    """The file stem for one capture of one scene, at one step.

    Deterministic in the plan rather than in the wall clock. The stem used to
    carry `stable_hash(str(time.time()))`, which meant a re-run of the same seed
    produced differently-named files: nothing downstream could tell that two
    captures were the same scene, a resumed shard could not skip work it had
    already done, and a sample could not be traced back to its plan entry.

    The step is spelled out rather than hashed, so an episode's frames sort into
    the order they happened in and `step00 -> action -> step01` is legible on
    disk. That matters for deriving an action dataset later: the observation
    before and after an action are `stepNN` and `stepNN+1` of the same scene.
    """
    return f"scene-{scene.scene_id}-step{int(step_index):02d}"


def corpus_relative_dir(scene: SceneConfig, *, is_episode: bool = False) -> str:
    """Where one scene's files live inside a corpus root.

    A million samples in one directory is unusable - `ls` never returns and
    every stat is a full scan - so the tree fans out. Two rules:

    - **Episodes are clustered.** All frames of one episode sit together in
      `ep/<scene_id>/`, next to the `episode.json` that records the action
      between each pair. That is the unit anyone building a long-horizon or
      action dataset needs to read, so it is the unit on disk.
    - **Static captures fan out by the first two characters of the scene id**,
      giving 256 directories per shard. With 64 shards and a million samples
      that is a few dozen files per directory.

    Shards are separate directories written by exactly one process, so two
    writers never touch the same directory and nothing can be overwritten.
    """
    if is_episode:
        return f"ep/{scene.scene_id}"
    return f"st/{scene.scene_id[:2]}"


def corpus_layout_enabled() -> bool:
    return os.environ.get(CORPUS_LAYOUT_ENV, "").strip().lower() in {"1", "true", "yes"}


def resolve_capture_dir(root: Path, scene: SceneConfig, *, is_episode: bool = False) -> Path:
    """The directory one scene's captures go in, honouring the corpus layout."""
    if not corpus_layout_enabled():
        return root
    out = root / corpus_relative_dir(scene, is_episode=is_episode)
    out.mkdir(parents=True, exist_ok=True)
    return out


def scene_signature(scene: SceneConfig) -> str:
    """Exact deterministic signature for one composed scene."""
    payload = {
        "theme_preset": scene.theme_preset,
        "display_preset": scene.display_preset,
        "panel_variant": scene.panel_variant,
        "desktop_profile": scene.desktop_profile,
        "desktop_layout_template": scene.desktop_layout_template,
        "desktop_content_pack": scene.desktop_content_pack,
        "layout": scene.layout,
        "apps": [
            {
                "app_name": app.app_name,
                "state_ref": app.state_ref,
                "rect": asdict(app.rect),
            }
            for app in scene.apps
        ],
    }
    return stable_hash(json.dumps(payload, sort_keys=True))


#: How coarsely window rectangles are compared when asking "do these two scenes
#: look the same". Twelfths of the screen: a window moved by a few pixels is the
#: same picture, a window moved by a tenth of the screen is not.
VISUAL_GRID = 12


def scene_visual_key(scene: SceneConfig) -> str:
    """A key that collides exactly when two scenes would look the same.

    `scene_signature` is an *exact* key - it contains raw pixel coordinates, so
    two scenes whose windows differ by one pixel get different signatures and
    both survive deduplication while being, to a reader and to a model, the same
    sample. That is the gap this closes.

    Everything a viewer would notice is in here: the apps and which state each
    is in, the arrangement quantised to twelfths of the screen, and the visual
    envelope (theme, resolution, panel, wallpaper seed, desktop contents).
    Nothing else is, so it stays an O(1) set membership test and can therefore
    be enforced across the whole corpus rather than over a window of recent
    scenes.

    Deliberately *not* included: `wallpaper_seed` and `desktop_seed`. They are
    derived from the scene seed and so are unique per scene, which would make
    every key distinct and the whole check vacuous - the first version of this
    function had exactly that bug and reported a flawless zero. The wallpaper is
    also the weakest thing to lean on, being mostly covered in any scene with
    windows in it, and it is already well spread on its own (332 distinct images
    over 400 scenes).
    """
    display = get_display_preset(scene.display_preset)
    cell_w = max(1, display.width // VISUAL_GRID)
    cell_h = max(1, display.height // VISUAL_GRID)
    payload = {
        "envelope": [
            scene.theme_preset, scene.display_preset, scene.panel_variant,
            scene.desktop_profile, scene.desktop_layout_template,
            scene.desktop_content_pack,
        ],
        "windows": [
            [
                app.app_name,
                app.state_ref,
                app.rect.x // cell_w,
                app.rect.y // cell_h,
                app.rect.width // cell_w,
                app.rect.height // cell_h,
            ]
            for app in scene.apps
        ],
    }
    return stable_hash(json.dumps(payload, sort_keys=True))


def scene_session_signature(scene: SceneConfig) -> str:
    """Signature for the reusable desktop session envelope of one scene."""
    payload = {
        "theme_preset": scene.theme_preset,
        "display_preset": scene.display_preset,
        "panel_variant": scene.panel_variant,
        "desktop_profile": scene.desktop_profile,
        "desktop_layout_template": scene.desktop_layout_template,
        "desktop_content_pack": scene.desktop_content_pack,
        "wallpaper_seed": scene.wallpaper_seed,
        "desktop_seed": scene.desktop_seed,
    }
    return stable_hash(json.dumps(payload, sort_keys=True))


def group_scenes_by_session_envelope(scenes: List[SceneConfig]) -> List[List[SceneConfig]]:
    """Group consecutive scenes that can safely share one desktop session."""
    groups: List[List[SceneConfig]] = []
    current: List[SceneConfig] = []
    current_sig = ""
    for scene in scenes:
        sig = scene_session_signature(scene)
        if current and sig != current_sig:
            groups.append(current)
            current = []
        if not current:
            current_sig = sig
        current.append(scene)
    if current:
        groups.append(current)
    return groups


def scene_similarity(scene_a: SceneConfig, scene_b: SceneConfig) -> Tuple[int, List[str]]:
    """Return a coarse similarity score used to reject near-duplicate plans."""
    score = 0
    reasons: List[str] = []

    apps_a = tuple(app.app_name for app in scene_a.apps)
    apps_b = tuple(app.app_name for app in scene_b.apps)
    app_set_a = tuple(sorted(apps_a))
    app_set_b = tuple(sorted(apps_b))

    if apps_a == apps_b:
        score += 3
        reasons.append("same_app_order")
    elif app_set_a == app_set_b:
        score += 2
        reasons.append("same_app_set")

    if scene_a.layout == scene_b.layout:
        score += 2
        reasons.append("same_layout")
    if scene_a.theme_preset == scene_b.theme_preset:
        score += 1
        reasons.append("same_theme")
    if scene_a.display_preset == scene_b.display_preset:
        score += 1
        reasons.append("same_display")
    if scene_a.panel_variant == scene_b.panel_variant:
        score += 1
        reasons.append("same_panel")
    if scene_a.desktop_profile == scene_b.desktop_profile:
        score += 1
        reasons.append("same_desktop_profile")
    if scene_a.desktop_layout_template == scene_b.desktop_layout_template:
        score += 1
        reasons.append("same_desktop_layout")
    if scene_a.desktop_content_pack == scene_b.desktop_content_pack:
        score += 1
        reasons.append("same_desktop_content_pack")
    if len(scene_a.apps) == len(scene_b.apps):
        score += 1
        reasons.append("same_app_count")

    # How much of the overlap is in the same state, as a bounded contribution
    # rather than a sum. Summing +2 per shared app made the score grow with
    # window count, so the fixed threshold of 10 was effectively stricter for
    # dense scenes: eight-window candidates were rejected as "too similar"
    # roughly three times as often as two-window ones, and the dense tail came
    # out at half the share its weight asks for. Those are the occlusion-heavy
    # scenes the pipeline most needs, so the bias was exactly backwards.
    b_by_app = {app.app_name: app for app in scene_b.apps}
    state_points = 0
    overlaps = 0
    for app_a in scene_a.apps:
        app_b = b_by_app.get(app_a.app_name)
        if app_b is None:
            continue
        overlaps += 1
        if app_a.state_ref == app_b.state_ref:
            state_points += 2
            reasons.append(f"same_state:{app_a.app_name}")
        elif _state_family(app_a.state_ref) == _state_family(app_b.state_ref):
            state_points += 1
            reasons.append(f"same_state_family:{app_a.app_name}")
    if overlaps:
        score += int(round(3 * state_points / (2 * overlaps)))

    return score, reasons


def scene_coverage_penalty(
    scene: SceneConfig,
    comparison_pool: List[SceneConfig],
    *,
    pool: List[str],
) -> Tuple[int, str]:
    """Return a lightweight penalty for overused apps/combos within a batch.

    This keeps mixed-scene planning from repeatedly reusing the same small subset
    of qualified apps even when the scenes are otherwise layout/theme-diverse.
    """
    if not comparison_pool:
        return 0, ""

    app_counts: Dict[str, int] = {name: 0 for name in pool}
    combo_counts: Dict[Tuple[str, ...], int] = {}
    for planned_scene in comparison_pool:
        combo = tuple(sorted(app.app_name for app in planned_scene.apps))
        combo_counts[combo] = combo_counts.get(combo, 0) + 1
        for app in planned_scene.apps:
            if app.app_name in app_counts:
                app_counts[app.app_name] += 1

    chosen_apps = [app.app_name for app in scene.apps]
    combo = tuple(sorted(chosen_apps))
    penalty = 0
    total_assignments = sum(app_counts.values())
    total_weight = sum(SCENE_APP_WEIGHTS.get(name, 1.0) for name in pool) or 1.0

    for app_name in chosen_apps:
        count = app_counts.get(app_name, 0)
        expected = total_assignments * (SCENE_APP_WEIGHTS.get(app_name, 1.0) / total_weight)
        if count > expected + 1.25:
            penalty += max(1, int(round(count - expected)))

    combo_reuse = combo_counts.get(combo, 0)
    if combo_reuse:
        penalty += combo_reuse * 2

    underused_pool_apps = [
        name
        for name, count in app_counts.items()
        if count + 0.75 < total_assignments * (SCENE_APP_WEIGHTS.get(name, 1.0) / total_weight)
    ]
    if underused_pool_apps and not any(name in underused_pool_apps for name in chosen_apps):
        penalty += 2

    if penalty >= 4:
        if combo_reuse:
            return penalty, "overused_app_combo_prelaunch"
        return penalty, "overused_apps_prelaunch"
    return penalty, ""


#: How close two screenshots' average hashes may be before the later one is
#: called a duplicate. **Zero**, i.e. identical, and that is a deliberate
#: tightening from 4.
#:
#: An average hash reduces an image to a 64-bit brightness pattern, and desktop
#: screenshots all share a shape - wallpaper behind rectangular windows - so
#: distinct desktops collide readily. The evidence that 4 was wrong: among 1,513
#: rejections the distances were 181 at 0, 174 at 1, 287 at 2, 344 at 3 and 527
#: at 4. A real duplicate population concentrates at 0; one that *grows* with
#: the threshold is counting random collisions. And of 60 rejected pairs
#: examined, **60 had entirely different application sets** - different scenes,
#: thrown away for looking alike to an 8x8 thumbnail.
#:
#: The planner already guarantees no two scenes share an appearance key, so this
#: check is not the primary defence against duplication. Its job is the case
#: planning cannot see: two different plans that *render* the same, which in
#: practice means both degenerated. Those are identical, not merely close.
NEAR_DUPLICATE_MAX_DISTANCE = 0


def _capture_dir_for(output_dir: Path, meta: Dict[str, Any], stem: str) -> Path:
    """Which directory a finished capture's files are actually in."""
    for key in ("elements_leaf", "elements", "screenshot", "png", "meta"):
        value = meta.get(key)
        if isinstance(value, str) and value:
            parent = Path(value).parent
            if (parent / f"{stem}.elements.unfiltered.json").is_file():
                return parent
    if (output_dir / f"{stem}.elements.unfiltered.json").is_file():
        return output_dir
    found = next(output_dir.rglob(f"{stem}.elements.unfiltered.json"), None)
    return found.parent if found is not None else output_dir


def evaluate_scene_capture(
    scene: SceneConfig,
    *,
    output_dir: Path,
    meta: Dict[str, Any],
    accepted_hashes: Dict[str, str],
) -> Dict[str, Any]:
    """Evaluate one completed scene capture for quality and duplicate risk."""
    stem = str(meta["stem"])
    # The capture is not necessarily in `output_dir` itself: with the corpus
    # layout it lives in `st/<xx>/` or `ep/<scene_id>/`. Prefer the path the
    # observation reported, fall back to searching, and only then to the root -
    # assuming the root aborted the whole shard on its first episode.
    here = _capture_dir_for(output_dir, meta, stem)
    unfiltered_elements = json.loads((here / f"{stem}.elements.unfiltered.json").read_text(encoding="utf-8"))
    elements = json.loads((here / f"{stem}.elements.json").read_text(encoding="utf-8"))
    leaf_elements = json.loads((here / f"{stem}.elements.leaf.json").read_text(encoding="utf-8"))

    viewport = meta.get("viewport", {})
    viewport_w = int(viewport.get("width", 0) or 0)
    viewport_h = int(viewport.get("height", 0) or 0)
    quality_checks = run_quality_checks(
        elements,
        leaf_elements=leaf_elements,
        viewport_w=viewport_w,
        viewport_h=viewport_h,
    )
    quality_ok = all(ok for _name, ok, _msg in quality_checks)
    zero_area_count = sum(
        1
        for elem in elements
        if int(elem.get("rect", {}).get("w", 0)) <= 0 or int(elem.get("rect", {}).get("h", 0)) <= 0
    )
    app_names_present = sorted(
        {
            (elem.get("app_name") or "").strip()
            for elem in elements
            if elem.get("source") == "app" and (elem.get("app_name") or "").strip()
        }
    )
    expected_apps = sorted(app.app_name for app in scene.apps)
    # An app contributing no elements means one of two very different things,
    # and treating them alike threw away good samples: either it never started,
    # or it started and is **entirely behind another window**. The second is not
    # a fault, it is a layout we generate on purpose - `maximized` with four
    # windows hides three of them by definition - and measured over 2,500
    # rejected samples it was **100%** of the cases, with zero real failures.
    #
    # `launched_apps` is what actually came up, so it separates them.
    launched_apps = {
        str(name) for name in (meta.get("launched_apps") or []) if str(name).strip()
    }
    absent = [name for name in expected_apps if name not in app_names_present]
    missing_apps = [name for name in absent if name not in launched_apps]
    occluded_apps = [name for name in absent if name in launched_apps]
    leaf_window_apps = sorted(
        {
            (elem.get("app_name") or "").strip()
            for elem in leaf_elements
            if elem.get("source") == "app"
            and (elem.get("role") or "").strip().lower() in {"frame", "window", "dialog", "alert", "desktop frame"}
        }
    )

    screenshot_path = here / f"{stem}.png"
    screenshot_hash = compute_image_ahash(screenshot_path)
    near_duplicate_of = ""
    near_duplicate_distance: Optional[int] = None
    for prev_stem, prev_hash in accepted_hashes.items():
        distance = hamming_distance_hex(screenshot_hash, prev_hash)
        if distance <= NEAR_DUPLICATE_MAX_DISTANCE:
            near_duplicate_of = prev_stem
            near_duplicate_distance = distance
            break

    accepted = quality_ok and zero_area_count == 0 and not missing_apps and not near_duplicate_of
    status = "accepted" if accepted else "rejected_post"
    reasons: List[str] = []
    if not quality_ok:
        reasons.append("quality_checks_failed")
    if zero_area_count:
        reasons.append("zero_area_boxes")
    if missing_apps:
        reasons.append("missing_apps")
    if near_duplicate_of:
        reasons.append("near_duplicate_capture")

    return {
        "status": status,
        "accepted": accepted,
        "reasons": reasons,
        "signature": scene_signature(scene),
        "screenshot_hash": screenshot_hash,
        "quality_ok": quality_ok,
        "quality_checks": [
            {"name": name, "ok": ok, "message": msg}
            for name, ok, msg in quality_checks
        ],
        "zero_area_count": zero_area_count,
        "expected_apps": expected_apps,
        "app_names_present": app_names_present,
        # Recorded, never a rejection: a window can be legitimately covered.
        "occluded_apps": occluded_apps,
        "missing_apps": missing_apps,
        "leaf_window_apps": leaf_window_apps,
        "near_duplicate_of": near_duplicate_of,
        "near_duplicate_distance": near_duplicate_distance,
        "num_elements_filtered": len(elements),
        "num_elements_unfiltered": len(unfiltered_elements),
        "num_elements_leaf": len(leaf_elements),
        "num_type_diversity": len({elem.get("type", "unknown") for elem in elements}),
        "stage_survival": build_stage_survival_summary(
            unfiltered_elements,
            elements,
            leaf_elements,
        ),
    }


def compute_image_ahash(path: Path, size: int = 8) -> str:
    """Compute a small average hash for duplicate detection."""
    with Image.open(path) as img:
        resized = img.convert("L").resize((size, size), Image.Resampling.LANCZOS)
        pixels = list(resized.getdata())
    avg = sum(pixels) / max(1, len(pixels))
    bits = "".join("1" if px >= avg else "0" for px in pixels)
    return f"{int(bits, 2):0{size * size // 4}x}"


def hamming_distance_hex(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def build_batch_distribution(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """Build compact coverage/distribution stats for accepted scene rows."""
    def bump(bucket: Dict[str, int], key: str) -> None:
        bucket[key] = bucket.get(key, 0) + 1

    distributions: Dict[str, Dict[str, int]] = {
        "themes": {},
        "layouts": {},
        "displays": {},
        "panels": {},
        "desktop_profiles": {},
        "desktop_layouts": {},
        "desktop_content_packs": {},
        "app_combos": {},
        "apps": {},
        "browser_domains": {},
    }
    for row in rows:
        scene = row["scene"]
        bump(distributions["themes"], scene["theme_preset"])
        bump(distributions["layouts"], scene["layout"])
        bump(distributions["displays"], scene["display_preset"])
        bump(distributions["panels"], scene["panel_variant"])
        bump(distributions["desktop_profiles"], scene["desktop_profile"])
        bump(distributions["desktop_layouts"], scene["desktop_layout_template"])
        bump(distributions["desktop_content_packs"], scene["desktop_content_pack"])
        combo = "+".join(sorted(app["app_name"] for app in scene["apps"]))
        bump(distributions["app_combos"], combo)
        for app in scene["apps"]:
            bump(distributions["apps"], app["app_name"])
            if app["app_name"] == "chromium-browser" and str(app["state_ref"]).startswith("catalog:"):
                bump(distributions["browser_domains"], str(app["state_ref"]).split(":", 2)[1])
    return distributions


def build_batch_element_coverage(
    output_dir: Path,
    rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build element/role/app coverage stats from saved scene exports."""
    def bump(bucket: Dict[str, int], key: str) -> None:
        key = str(key or "").strip() or "unknown"
        bucket[key] = bucket.get(key, 0) + 1

    coverage: Dict[str, Any] = {
        "accepted_scenes": len(rows),
        "filtered_type_counts": {},
        "leaf_type_counts": {},
        "filtered_role_counts": {},
        "leaf_role_counts": {},
        "filtered_source_counts": {},
        "leaf_source_counts": {},
        "filtered_app_counts": {},
        "leaf_app_counts": {},
        "leaf_window_app_counts": {},
        "filtered_type_scene_frequency": {},
        "leaf_type_scene_frequency": {},
        "filtered_role_scene_frequency": {},
        "leaf_role_scene_frequency": {},
        "per_scene": [],
    }

    window_roles = {"frame", "window", "dialog", "alert", "desktop frame"}
    for row in rows:
        stem = str(row["stem"])
        # Same reason as `evaluate_scene_capture`: the corpus layout nests
        # captures, so the directory has to be resolved rather than assumed.
        here = _capture_dir_for(output_dir, row, stem)
        filtered_path = here / f"{stem}.elements.json"
        leaf_path = here / f"{stem}.elements.leaf.json"
        if not filtered_path.is_file() or not leaf_path.is_file():
            continue

        filtered = json.loads(filtered_path.read_text(encoding="utf-8"))
        leaf = json.loads(leaf_path.read_text(encoding="utf-8"))
        filtered_types_seen: set[str] = set()
        filtered_roles_seen: set[str] = set()
        leaf_types_seen: set[str] = set()
        leaf_roles_seen: set[str] = set()

        for elem in filtered:
            elem_type = str(elem.get("type", "")).strip() or "unknown"
            role = str(elem.get("role", "")).strip() or "unknown"
            source = str(elem.get("source", "")).strip() or "unknown"
            app_name = str(elem.get("app_name", "")).strip() or "unknown"
            bump(coverage["filtered_type_counts"], elem_type)
            bump(coverage["filtered_role_counts"], role)
            bump(coverage["filtered_source_counts"], source)
            bump(coverage["filtered_app_counts"], app_name)
            filtered_types_seen.add(elem_type)
            filtered_roles_seen.add(role)

        for elem in leaf:
            elem_type = str(elem.get("type", "")).strip() or "unknown"
            role = str(elem.get("role", "")).strip() or "unknown"
            source = str(elem.get("source", "")).strip() or "unknown"
            app_name = str(elem.get("app_name", "")).strip() or "unknown"
            bump(coverage["leaf_type_counts"], elem_type)
            bump(coverage["leaf_role_counts"], role)
            bump(coverage["leaf_source_counts"], source)
            bump(coverage["leaf_app_counts"], app_name)
            if source == "app" and role.lower() in window_roles:
                bump(coverage["leaf_window_app_counts"], app_name)
            leaf_types_seen.add(elem_type)
            leaf_roles_seen.add(role)

        for elem_type in filtered_types_seen:
            bump(coverage["filtered_type_scene_frequency"], elem_type)
        for elem_type in leaf_types_seen:
            bump(coverage["leaf_type_scene_frequency"], elem_type)
        for role in filtered_roles_seen:
            bump(coverage["filtered_role_scene_frequency"], role)
        for role in leaf_roles_seen:
            bump(coverage["leaf_role_scene_frequency"], role)

        coverage["per_scene"].append(
            {
                "stem": stem,
                "scene_id": row.get("scene", {}).get("scene_id", ""),
                "filtered_elements": len(filtered),
                "leaf_elements": len(leaf),
                "filtered_type_diversity": len(filtered_types_seen),
                "leaf_type_diversity": len(leaf_types_seen),
                "filtered_role_diversity": len(filtered_roles_seen),
                "leaf_role_diversity": len(leaf_roles_seen),
            }
        )

    return coverage


def _state_family(state_ref: str) -> str:
    if state_ref.startswith("catalog:"):
        return browser_catalog_state_family(state_ref)
    if state_ref.startswith("chain:"):
        parts = state_ref.split(":", 2)
        if len(parts) >= 2:
            return ":".join(parts[:2])
    return state_ref


def _choose_scene_state(
    app_name: str, seed: int, *, uniform_pages: bool = False
) -> str:
    if app_name == "chromium-browser":
        rng = random.Random(seed)
        if uniform_pages or rng.random() < 0.74:
            # A page-diversity batch always takes a catalog page, and takes it
            # flat: the default prior turned a 1,000-site list into 50 observed
            # domains with google.com at 27.5%.
            return sample_browser_catalog_state(seed, uniform=uniform_pages)
    states = CURATED_APP_STATES[app_name]
    rng = random.Random(seed)
    return states[rng.randrange(len(states))]


def _build_scene_runtime_config(scene: SceneConfig, config: PipelineConfig) -> PipelineConfig:
    """Materialize the session/runtime config for one scene envelope."""
    scene_config = deepcopy(config)
    scene_config.session.desktop_env = "xfce"
    scene_config.session.theme = get_theme_preset(scene.theme_preset, resolve=False)
    scene_config.session.theme.panel_variant = scene.panel_variant
    scene_config.session.theme.wallpaper_seed = scene.wallpaper_seed
    display = get_display_preset(
        scene.display_preset,
        display_number=scene_config.session.display.display_number,
        depth=scene_config.session.display.depth,
    )
    scene_config.session.display.width = display.width
    scene_config.session.display.height = display.height
    scene_config.session.desktop_fixture.profile = scene.desktop_profile
    scene_config.session.desktop_fixture.seed = scene.desktop_seed
    scene_config.session.desktop_fixture.layout_template = scene.desktop_layout_template
    scene_config.session.desktop_fixture.content_pack = scene.desktop_content_pack
    return scene_config


def _wait_for_scene_targets_to_clear(app_targets: List[tuple[str, str]]) -> None:
    """Wait for launched app AT-SPI roots to disappear before reusing the session."""
    seen: set[str] = set()
    for atspi_name, _app_name in reversed(app_targets):
        name = (atspi_name or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        if wait_for_app_absent_from_atspi(name):
            logger.debug("[scene] Cleared AT-SPI target: %s", name)
        else:
            logger.warning("[scene] AT-SPI target still present after cleanup: %s", name)


def _find_interaction(manifest: AppManifest, interaction_name: str) -> InteractionSequence:
    for interaction in manifest.interactions:
        if interaction.name == interaction_name:
            return interaction
    raise ValueError(f"Interaction not found: {manifest.app_name}:{interaction_name}")


def _find_chain(manifest: AppManifest, chain_name: str) -> InteractionChain:
    for chain in manifest.chains:
        if chain.name == chain_name:
            return chain
    raise ValueError(f"Chain not found: {manifest.app_name}:{chain_name}")


_BROWSER_SCENE_APPS = {"chromium-browser", "firefox", "firefox-pdf", "chromium-pdf"}

# Browsers used to get *less* time than everything else, which is backwards: a
# browser is the heaviest thing in the pool. Measured in a four-worker batch,
# 16 of 16 Chromium launches hit the 24s ceiling and lost their whole scene -
# 28 of 32 scenes failed, almost all of them for this. It now matches the
# general floor, which the same batch showed to be adequate for other apps.
_BROWSER_LAUNCH_TIMEOUT_FLOOR = 90.0

#: Launching N scenes at once makes every one of them slower: they share a
#: filesystem, a page cache and a CPU. The floors above were measured on an idle
#: node, so running in parallel needs them scaled or the batch fails in exactly
#: the conditions it exists to test.
_LAUNCH_TIMEOUT_PARALLEL_SCALE_ENV = "DESKSHOT_LAUNCH_TIMEOUT_SCALE"
_MAX_LAUNCH_TIMEOUT_SCALE = 3.0


def launch_timeout_scale_for_workers(workers: int) -> float:
    """How much to stretch launch timeouts when `workers` scenes run at once."""
    if workers <= 1:
        return 1.0
    return min(_MAX_LAUNCH_TIMEOUT_SCALE, 1.0 + 0.4 * (workers - 1))


def _launch_timeout_scale() -> float:
    raw = os.environ.get(_LAUNCH_TIMEOUT_PARALLEL_SCALE_ENV)
    if not raw:
        return 1.0
    try:
        return max(1.0, min(_MAX_LAUNCH_TIMEOUT_SCALE, float(raw)))
    except ValueError:
        logger.warning("Ignoring invalid %s=%r", _LAUNCH_TIMEOUT_PARALLEL_SCALE_ENV, raw)
        return 1.0
# Compute nodes start apps from a cold page cache over a shared filesystem, so
# first-launch AT-SPI registration is much slower than on a warm login node.
# 12s was enough locally but timed out in LSF jobs and failed whole scenes.
# Measured cold first-launch times (scripts/check_app_pool_health.py): most apps
# under 1s, but transmission-gtk 19s, vscode 19s, zim 72s. A whole scene is lost
# when any one app is starved, so the floor is set above the slow tail rather
# than the median. DESKSHOT_SCENE_LAUNCH_TIMEOUT overrides it.
_SCENE_LAUNCH_TIMEOUT_FLOOR = 90.0


def _scene_launch_timeout_floor(app_name: str) -> float:
    """Minimum AT-SPI wait for a scene app, overridable for slow nodes."""
    override = os.environ.get("DESKSHOT_SCENE_LAUNCH_TIMEOUT")
    if override:
        try:
            return max(float(override), 1.0)
        except ValueError:
            logger.warning("Ignoring invalid DESKSHOT_SCENE_LAUNCH_TIMEOUT=%r", override)
    base = (
        _BROWSER_LAUNCH_TIMEOUT_FLOOR
        if app_name in _BROWSER_SCENE_APPS
        else _SCENE_LAUNCH_TIMEOUT_FLOOR
    )
    return base * _launch_timeout_scale()


def _launch_scene_app(manifest: AppManifest, *, timeout: float) -> object:
    """Launch one scene app with a small retry budget for AT-SPI flakiness."""
    effective_timeout = max(float(timeout), _scene_launch_timeout_floor(manifest.app_name))
    last_error: Optional[Exception] = None
    for attempt in range(1, 3):
        try:
            return launch_app(manifest, timeout=effective_timeout)
        except AppHandoffFailed as exc:
            # The handoff already happened and produced no window, so an
            # identical second request goes to the same place. Retrying this is
            # pure dead time - and at the full launch timeout it was most of it.
            logger.warning(
                "[scene] %s handed off without a window; not retrying: %s",
                manifest.app_name,
                exc,
            )
            raise
        except Exception as exc:
            last_error = exc
            logger.warning(
                "[scene] launch retry %d/2 for %s after error: %s",
                attempt,
                manifest.app_name,
                exc,
            )
            time.sleep(0.8)
    assert last_error is not None
    raise last_error


def _execute_scene_state(manifest: AppManifest, state_ref: str) -> None:
    """Reach one scene state through either a direct interaction or a chain prefix."""
    if state_ref.startswith("catalog:"):
        execute_sequence(
            build_browser_catalog_sequence(state_ref),
            app_name=manifest.atspi_name,
        )
        return
    if not state_ref.startswith("chain:"):
        interaction = _find_interaction(manifest, state_ref)
        if interaction.actions:
            execute_sequence(interaction, app_name=manifest.atspi_name)
        return

    _prefix, chain_name, step_name = state_ref.split(":", 2)
    chain = _find_chain(manifest, chain_name)
    for step in chain.steps:
        if step.companion_apps:
            raise ValueError(
                f"Scene state {state_ref} is invalid: companion-bearing chain steps are not supported"
            )
        seq = InteractionSequence(
            name=f"{chain_name}:{step.name}",
            description=step.description,
            actions=list(step.actions),
        )
        if seq.actions:
            execute_sequence(seq, app_name=manifest.atspi_name)
        if step.name == step_name:
            return
    raise ValueError(f"Chain step not found: {manifest.app_name}:{state_ref}")


def _position_app_window(manifest: AppManifest, rect: WindowRect) -> None:
    titles = [title for title in manifest.window_titles if title] + [
        manifest.app_name,
        manifest.atspi_name,
    ]
    for title in titles:
        if not focus_window_by_name(title):
            continue
        resized = resize_active_window(rect.width, rect.height)
        time.sleep(0.1)
        moved = move_active_window(rect.x, rect.y)
        time.sleep(0.2)
        if not resized or not moved:
            logger.warning(
                "Window positioning degraded for %s using title=%s resized=%s moved=%s rect=%s",
                manifest.app_name,
                title,
                resized,
                moved,
                rect,
            )
        return
    logger.warning("Could not focus window for %s using titles=%s", manifest.app_name, titles)


@dataclass
class LaunchedScene:
    """Apps of one scene, running in an already-active session."""

    scene: SceneConfig
    scene_config: PipelineConfig
    manifests: Dict[str, AppManifest]
    launched_apps: List[str]
    app_targets: List[Tuple[str, str]]
    procs: List[object]
    app_launch_durations: Dict[str, float]
    launch_started: float
    missing_apps: List[Dict[str, str]] = field(default_factory=list)


def launch_scene_apps(
    scene: SceneConfig,
    *,
    scene_config: PipelineConfig,
    manifests: Dict[str, AppManifest],
) -> LaunchedScene:
    """Launch a scene's apps and reach their states, without capturing.

    Split out from the capture so a session can outlive a single frame: a static
    capture is then just one observation, and an episode is several.
    """
    launched = LaunchedScene(
        scene=scene,
        scene_config=scene_config,
        manifests=manifests,
        launched_apps=[],
        app_targets=[],
        procs=[],
        app_launch_durations={},
        launch_started=time.monotonic(),
        missing_apps=[],
    )
    for app_spec in scene.apps:
        manifest = manifests[app_spec.app_name]
        logger.info(
            "[scene:%s] Launching %s (%s)",
            scene.scene_id,
            manifest.app_name,
            app_spec.state_ref,
        )
        app_started = time.monotonic()
        try:
            proc = _launch_scene_app(
                manifest,
                timeout=max(scene_config.session.app_launch_timeout, 12.0),
            )
        except Exception as exc:
            # Same tolerance as the single-session path. This is the loop the
            # batch's persistent workers actually use, and patching only the
            # other one left Chromium still costing four whole scenes.
            logger.warning(
                "[scene:%s] %s did not come up (%s); continuing without it",
                scene.scene_id, manifest.app_name, exc,
            )
            launched.missing_apps.append(
                {"app_name": manifest.app_name, "error": str(exc)}
            )
            continue
        launched.procs.append(proc)
        launched.launched_apps.append(manifest.app_name)
        launched.app_targets.append((manifest.atspi_name, manifest.app_name))
        _execute_scene_state(manifest, app_spec.state_ref)
        _position_app_window(manifest, app_spec.rect)
        launched.app_launch_durations[manifest.app_name] = round(
            time.monotonic() - app_started, 3
        )
    if not launched.launched_apps and scene.apps:
        raise RuntimeError(
            f"no app in scene {scene.scene_id} came up: "
            + ", ".join(m["app_name"] for m in launched.missing_apps)
        )
    return launched


def capture_launched_scene(
    launched: LaunchedScene,
    *,
    out: Path,
    include_desktop_chrome: bool,
    step_index: int = 0,
    extra_meta: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, object]]:
    """Capture the current screen state of an already-launched scene."""
    scene = launched.scene
    # A bare desktop has no app to speak for it. The manifest is only used for
    # naming and document semantics, so a placeholder is enough and keeps the
    # zero-window case on the same code path as every other capture.
    primary_manifest = (
        launched.manifests[scene.apps[0].app_name]
        if scene.apps
        else AppManifest(app_name="desktop", binary="", atspi_name="")
    )
    stem = scene_capture_stem(scene, step_index)
    scene_timing = {
        "app_launch_total_sec": round(time.monotonic() - launched.launch_started, 3),
        "app_launch_sec": dict(launched.app_launch_durations),
    }
    meta: Dict[str, Any] = {"scene": asdict(scene), "scene_timing": scene_timing}
    if step_index:
        meta["step_index"] = step_index
    if extra_meta:
        meta.update(extra_meta)
    return _capture_current_state(
        manifest=primary_manifest,
        capture_name=f"scene:{scene.scene_id}",
        capture_description=f"Mixed-app scene ({scene.layout})",
        stem=stem,
        config=launched.scene_config,
        out=out,
        include_desktop_chrome=include_desktop_chrome,
        launched_apps=launched.launched_apps,
        app_targets=launched.app_targets,
        extra_meta=meta,
    )


def teardown_launched_scene(launched: LaunchedScene) -> None:
    """Kill a scene's apps and wait for them to leave the AT-SPI tree.

    Killing the launcher process is not enough. A single-instance app's launcher
    exits as soon as it has handed off, so `kill_app` finds a dead process and
    returns without touching the window that is actually on screen. That window
    then survives into the next scene, whose own launcher hands off to it and
    exits 0 in turn - which is how one app failing to be killed became a run of
    scenes reporting it missing.
    """
    for proc in reversed(launched.procs):
        kill_app(proc)
    kill_apps_by_atspi_name([name for name, _app in launched.app_targets])
    _wait_for_scene_targets_to_clear(launched.app_targets)


def _run_scene_in_active_session(
    scene: SceneConfig,
    *,
    scene_config: PipelineConfig,
    manifests: Dict[str, AppManifest],
    out: Path,
    include_desktop_chrome: bool,
) -> Optional[Dict[str, object]]:
    """Run one scene assuming its desktop session is already active."""
    launched_apps: List[str] = []
    app_targets: List[tuple[str, str]] = []
    procs: List[object] = []
    missing_apps: List[Dict[str, str]] = []
    app_launch_durations: Dict[str, float] = {}
    scene_started = time.monotonic()
    primary_manifest = manifests[scene.apps[0].app_name]
    stem = scene_capture_stem(scene)
    try:
        for app_spec in scene.apps:
            manifest = manifests[app_spec.app_name]
            state_label = app_spec.state_ref
            logger.info(
                "[scene:%s] Launching %s (%s)",
                scene.scene_id,
                manifest.app_name,
                state_label,
            )
            app_started = time.monotonic()
            try:
                proc = _launch_scene_app(
                    manifest,
                    timeout=max(scene_config.session.app_launch_timeout, 12.0),
                )
            except Exception as exc:
                # One app that will not register should not cost the whole
                # scene. Measured in a three-worker batch: Chromium failed to
                # appear in the AT-SPI tree in half the scenes, and raising its
                # budget from 90s to 162s did not change that - so this is not a
                # slow start, it is an app that sometimes never registers. Every
                # other app in those scenes had launched and drawn correctly, and
                # all of it was thrown away.
                #
                # The scene is captured with the apps that did come up, and the
                # ones that did not are recorded, so a sample is never silently
                # short of what its plan asked for.
                logger.warning(
                    "[scene:%s] %s did not come up (%s); capturing without it",
                    scene.scene_id, manifest.app_name, exc,
                )
                missing_apps.append({"app_name": manifest.app_name, "error": str(exc)})
                continue
            procs.append(proc)
            launched_apps.append(manifest.app_name)
            app_targets.append((manifest.atspi_name, manifest.app_name))
            _execute_scene_state(manifest, app_spec.state_ref)
            _position_app_window(manifest, app_spec.rect)
            app_launch_durations[manifest.app_name] = round(
                time.monotonic() - app_started, 3
            )

        readiness = wait_for_apps_to_settle(app_targets)
        # Must go in via extra_meta: _capture_current_state writes meta.json
        # itself, so anything added to its return value never reaches the file.
        if not launched_apps:
            raise RuntimeError(
                f"no app in scene {scene.scene_id} came up: "
                + ", ".join(m["app_name"] for m in missing_apps)
            )
        scene_timing = {
            "app_launch_total_sec": round(time.monotonic() - scene_started, 3),
            "app_launch_sec": dict(app_launch_durations),
            "readiness": readiness,
            "missing_apps": missing_apps,
        }
        return _capture_current_state(
            manifest=primary_manifest,
            capture_name=f"scene:{scene.scene_id}",
            capture_description=f"Mixed-app scene ({scene.layout})",
            stem=stem,
            config=scene_config,
            out=out,
            include_desktop_chrome=include_desktop_chrome,
            launched_apps=launched_apps,
            app_targets=app_targets,
            extra_meta={"scene": asdict(scene), "scene_timing": scene_timing},
        )
    finally:
        for proc in reversed(procs):
            kill_app(proc)
        _wait_for_scene_targets_to_clear(app_targets)


def _layout_rects(
    layout: str, *, width: int, height: int, count: int, seed: int = 0
) -> List[WindowRect]:
    if layout.startswith("family:"):
        family = layout.split(":", 1)[1]
        # Two bugs lived in the line this replaces, and both mattered:
        #
        #   seed = abs(hash((family, count, width, height))) % (2 ** 31)
        #
        # `hash()` of a tuple containing a string is salted per process, so the
        # same scene got *different* window rectangles in every process. The
        # plan recorded one signature and the capture worker produced another,
        # which made "regenerate from seed" false for every generative layout -
        # 45% of two-to-four-window scenes and all of the larger ones - and made
        # the planner's deduplication partly fictitious.
        #
        # The scene seed was also absent, so every scene sharing a family, a
        # window count and a viewport got byte-identical geometry. That is a
        # duplication source in its own right.
        #
        # The repair is simply to stop shadowing `seed`: the scene's own seed
        # reaches `build_layout` and it is unique per scene, so nothing further
        # needs mixing in. An earlier version of this fix also computed a
        # `stable_hash` of family/count/viewport and then never used it, which
        # left a comment promising more than the code delivered.
        return [
            WindowRect(r.x, r.y, r.width, r.height)
            for r in build_layout(family, count, width=width, height=height, seed=seed)
        ]

    top_margin = max(70, height // 18)
    side_margin = max(110, width // 18)
    gap = max(28, width // 90)
    bottom_margin = max(90, height // 12)
    usable_h = height - top_margin - bottom_margin
    inner_w = width - side_margin * 2

    if count == 2 and layout == "side_by_side_balanced":
        window_w = (inner_w - gap) // 2
        return [
            WindowRect(side_margin, top_margin, window_w, usable_h - 18),
            WindowRect(side_margin + window_w + gap, top_margin + 12, window_w, usable_h - 30),
        ]

    if count == 2 and layout == "side_by_side_offset":
        window_w = (inner_w - gap) // 2
        return [
            WindowRect(side_margin + 24, top_margin + 8, window_w - 24, usable_h - 34),
            WindowRect(side_margin + window_w + gap, top_margin + 36, window_w, usable_h - 66),
        ]

    if count == 2 and layout == "primary_left":
        primary_w = int(inner_w * 0.58)
        secondary_w = inner_w - primary_w - gap
        return [
            WindowRect(side_margin, top_margin, primary_w, usable_h - 8),
            WindowRect(side_margin + primary_w + gap, top_margin + 26, secondary_w, usable_h - 54),
        ]

    if count == 2 and layout == "primary_right":
        primary_w = int(inner_w * 0.58)
        secondary_w = inner_w - primary_w - gap
        return [
            WindowRect(side_margin + secondary_w + gap, top_margin, primary_w, usable_h - 8),
            WindowRect(side_margin, top_margin + 24, secondary_w, usable_h - 48),
        ]

    if count == 2 and layout == "cascade_light":
        back_w = int(inner_w * 0.56)
        front_w = int(inner_w * 0.54)
        return [
            WindowRect(side_margin + 18, top_margin + 12, back_w, usable_h - 46),
            WindowRect(side_margin + inner_w - front_w - 18, top_margin + 64, front_w, usable_h - 98),
        ]

    if count == 2 and layout == "overlap_light":
        back_w = int(inner_w * 0.58)
        front_w = int(inner_w * 0.52)
        return [
            WindowRect(side_margin + 20, top_margin + 10, back_w, usable_h - 34),
            WindowRect(side_margin + int(inner_w * 0.28), top_margin + 58, front_w, usable_h - 100),
        ]

    if count == 3 and layout == "focus_left_stack_right":
        left_w = int(inner_w * 0.57)
        right_w = inner_w - left_w - gap
        top_h = (usable_h - gap) // 2
        return [
            WindowRect(side_margin, top_margin, left_w, usable_h - 10),
            WindowRect(side_margin + left_w + gap, top_margin, right_w, top_h - 8),
            WindowRect(side_margin + left_w + gap, top_margin + top_h + gap, right_w, top_h - 18),
        ]

    if count == 3 and layout == "focus_right_stack":
        right_w = int(inner_w * 0.57)
        left_w = inner_w - right_w - gap
        top_h = (usable_h - gap) // 2
        return [
            WindowRect(side_margin + left_w + gap, top_margin, right_w, usable_h - 10),
            WindowRect(side_margin, top_margin, left_w, top_h - 8),
            WindowRect(side_margin, top_margin + top_h + gap, left_w, top_h - 18),
        ]

    if count == 3 and layout == "triple_column":
        col_w = (inner_w - gap * 2) // 3
        return [
            WindowRect(side_margin, top_margin + 10, col_w, usable_h - 30),
            WindowRect(side_margin + col_w + gap, top_margin, col_w, usable_h - 16),
            WindowRect(side_margin + (col_w + gap) * 2, top_margin + 18, col_w, usable_h - 42),
        ]

    if count == 3 and layout == "two_up_one_down":
        top_h = int((usable_h - gap) * 0.56)
        bottom_h = usable_h - top_h - gap
        top_w = (inner_w - gap) // 2
        return [
            WindowRect(side_margin, top_margin, top_w, top_h),
            WindowRect(side_margin + top_w + gap, top_margin + 8, top_w, top_h - 10),
            WindowRect(side_margin + width // 8, top_margin + top_h + gap, inner_w - width // 4, bottom_h - 12),
        ]

    if count == 3 and layout == "side_by_side_plus_float":
        left_w = int(inner_w * 0.5)
        right_w = inner_w - left_w - gap
        float_w = int(inner_w * 0.34)
        float_h = int(usable_h * 0.38)
        return [
            WindowRect(side_margin, top_margin, left_w, usable_h - 14),
            WindowRect(side_margin + left_w + gap, top_margin + 18, right_w, usable_h - 52),
            WindowRect(side_margin + left_w - float_w // 5, top_margin + usable_h - float_h - 20, float_w, float_h),
        ]

    if count == 3 and layout == "cascade_light":
        return [
            WindowRect(side_margin + 10, top_margin + 6, int(inner_w * 0.54), usable_h - 38),
            WindowRect(side_margin + int(inner_w * 0.18), top_margin + 44, int(inner_w * 0.5), usable_h - 88),
            WindowRect(side_margin + int(inner_w * 0.42), top_margin + 82, int(inner_w * 0.44), usable_h - 126),
        ]

    if count == 3 and layout == "overlap_light":
        return [
            WindowRect(side_margin + 18, top_margin + 10, int(inner_w * 0.52), usable_h - 40),
            WindowRect(side_margin + int(inner_w * 0.22), top_margin + 56, int(inner_w * 0.5), usable_h - 102),
            WindowRect(side_margin + int(inner_w * 0.46), top_margin + 102, int(inner_w * 0.42), usable_h - 156),
        ]

    if count == 4 and layout == "quad_grid":
        cell_w = (inner_w - gap) // 2
        cell_h = (usable_h - gap) // 2
        return [
            WindowRect(side_margin, top_margin, cell_w, cell_h),
            WindowRect(side_margin + cell_w + gap, top_margin + 8, cell_w, cell_h - 10),
            WindowRect(side_margin, top_margin + cell_h + gap, cell_w, cell_h - 16),
            WindowRect(side_margin + cell_w + gap, top_margin + cell_h + gap + 8, cell_w, cell_h - 24),
        ]

    if count == 4 and layout == "primary_left_triple_right":
        left_w = int(inner_w * 0.52)
        right_w = inner_w - left_w - gap
        right_h = (usable_h - gap * 2) // 3
        return [
            WindowRect(side_margin, top_margin, left_w, usable_h - 12),
            WindowRect(side_margin + left_w + gap, top_margin, right_w, right_h - 4),
            WindowRect(side_margin + left_w + gap, top_margin + right_h + gap, right_w, right_h - 8),
            WindowRect(side_margin + left_w + gap, top_margin + (right_h + gap) * 2, right_w, right_h - 16),
        ]

    if count == 4 and layout == "staggered_grid":
        cell_w = int((inner_w - gap * 2) / 2.15)
        cell_h = int((usable_h - gap * 2) / 2.15)
        return [
            WindowRect(side_margin, top_margin, cell_w, cell_h),
            WindowRect(side_margin + cell_w + gap, top_margin + 20, cell_w, cell_h),
            WindowRect(side_margin + 24, top_margin + cell_h + gap + 12, cell_w, cell_h),
            WindowRect(side_margin + cell_w + gap + 24, top_margin + cell_h + gap + 30, cell_w, cell_h),
        ]

    raise ValueError(f"Unsupported layout/count combination: {layout} ({count})")
