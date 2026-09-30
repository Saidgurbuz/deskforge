from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path

from deskshot.config import AppManifest
from deskshot.generation.scene_composer import (
    build_batch_element_coverage,
    CURATED_APP_STATES,
    DEFAULT_SCENE_APP_POOL,
    SceneApp,
    SceneConfig,
    WindowRect,
    compose_scene,
    compose_scene_batch,
    compose_diverse_scene_batch,
    evaluate_scene_capture,
    group_scenes_by_session_envelope,
    hamming_distance_hex,
    scene_coverage_penalty,
    scene_session_signature,
    scene_signature,
    scene_similarity,
    _launch_scene_app,
)


def test_compose_scene_is_deterministic() -> None:
    a = compose_scene(123, available_apps=["chromium-browser", "vscode", "thunar"])
    b = compose_scene(123, available_apps=["chromium-browser", "vscode", "thunar"])

    assert asdict(a) == asdict(b)


def test_compose_scene_uses_curated_pool_and_valid_rects() -> None:
    scene = compose_scene(77, available_apps=["chromium-browser", "vscode", "thunar", "eog"])

    assert len(scene.apps) in {2, 3, 4}
    assert len({app.app_name for app in scene.apps}) == len(scene.apps)
    assert scene.theme_preset
    assert scene.display_preset
    assert scene.panel_variant
    assert scene.desktop_layout_template
    assert scene.desktop_content_pack

    for app in scene.apps:
        if app.app_name == "chromium-browser":
            assert app.state_ref in CURATED_APP_STATES[app.app_name] or app.state_ref.startswith("catalog:")
        else:
            assert app.state_ref in CURATED_APP_STATES[app.app_name]
        assert app.rect.width > 100
        assert app.rect.height > 100
        assert app.rect.x >= 0
        assert app.rect.y >= 0


def test_compose_scene_can_pick_chain_backed_states() -> None:
    scene = compose_scene(6, available_apps=["chromium-browser", "vscode", "thunar", "gnome-calculator"])

    assert any(app.state_ref.startswith("chain:") for app in scene.apps)
    for app in scene.apps:
        if app.state_ref.startswith("chain:"):
            _prefix, chain_name, step_name = app.state_ref.split(":", 2)
            assert chain_name
            assert step_name


def test_compose_scene_batch_uses_contiguous_seeds() -> None:
    scenes = compose_scene_batch(
        start_seed=10,
        count=3,
        available_apps=["chromium-browser", "vscode", "thunar"],
    )

    assert [scene.seed for scene in scenes] == [10, 11, 12]
    assert len({scene.scene_id for scene in scenes}) == 3


def test_compose_scene_batch_can_share_session_envelopes() -> None:
    scenes = compose_scene_batch(
        start_seed=10,
        count=5,
        available_apps=["chromium-browser", "vscode", "thunar", "gnome-calculator"],
        session_group_size=2,
    )

    assert [scene.seed for scene in scenes] == [10, 11, 12, 13, 14]
    assert scene_session_signature(scenes[0]) == scene_session_signature(scenes[1])
    assert scene_session_signature(scenes[1]) != scene_session_signature(scenes[2])
    assert scene_session_signature(scenes[2]) == scene_session_signature(scenes[3])


def test_group_scenes_by_session_envelope_preserves_consecutive_groups() -> None:
    scenes = compose_scene_batch(
        start_seed=20,
        count=5,
        available_apps=["chromium-browser", "vscode", "thunar", "gnome-calculator"],
        session_group_size=2,
    )

    groups = group_scenes_by_session_envelope(scenes)

    assert [len(group) for group in groups] == [2, 2, 1]
    assert len({scene_session_signature(group[0]) for group in groups}) == 3


def test_scene_signature_changes_when_state_changes() -> None:
    base = SceneConfig(
        scene_id="a",
        seed=1,
        theme_preset="linux_classic",
        display_preset="fhd_1920x1080",
        panel_variant="top",
        desktop_profile="balanced",
        desktop_layout_template="upper_left_two_col",
        desktop_content_pack="engineering_dev",
        wallpaper_seed=10,
        desktop_seed=20,
        layout="split",
        apps=[
            SceneApp("vscode", "default", WindowRect(0, 0, 800, 600)),
            SceneApp("chromium-browser", "default", WindowRect(900, 0, 800, 600)),
        ],
    )
    changed = SceneConfig(
        scene_id="a",
        seed=1,
        theme_preset="linux_classic",
        display_preset="fhd_1920x1080",
        panel_variant="top",
        desktop_profile="balanced",
        desktop_layout_template="upper_left_two_col",
        desktop_content_pack="engineering_dev",
        wallpaper_seed=10,
        desktop_seed=20,
        layout="split",
        apps=[
            SceneApp("vscode", "search_panel", WindowRect(0, 0, 800, 600)),
            SceneApp("chromium-browser", "default", WindowRect(900, 0, 800, 600)),
        ],
    )

    assert scene_signature(base) != scene_signature(changed)


def test_scene_similarity_prefers_same_apps_layout_and_states() -> None:
    a = SceneConfig(
        scene_id="a",
        seed=1,
        theme_preset="linux_classic",
        display_preset="fhd_1920x1080",
        panel_variant="top_slim",
        desktop_profile="sparse",
        desktop_layout_template="center_cluster",
        desktop_content_pack="business_ops",
        wallpaper_seed=11,
        desktop_seed=22,
        layout="split",
        apps=[
            SceneApp("vscode", "chain:workspace_progressive:search_panel", WindowRect(0, 0, 800, 600)),
            SceneApp("gnome-calculator", "chain:progressive_usage:menu_open", WindowRect(820, 0, 500, 500)),
        ],
    )
    b = SceneConfig(
        scene_id="b",
        seed=2,
        theme_preset="linux_classic",
        display_preset="fhd_1920x1080",
        panel_variant="top_slim",
        desktop_profile="sparse",
        desktop_layout_template="center_cluster",
        desktop_content_pack="business_ops",
        wallpaper_seed=12,
        desktop_seed=23,
        layout="split",
        apps=[
            SceneApp("vscode", "chain:workspace_progressive:quick_open", WindowRect(0, 0, 800, 600)),
            SceneApp("gnome-calculator", "chain:progressive_usage:result_shown", WindowRect(820, 0, 500, 500)),
        ],
    )
    c = SceneConfig(
        scene_id="c",
        seed=3,
        theme_preset="ubuntu_like",
        display_preset="qhd_2560x1440",
        panel_variant="bottom_tall",
        desktop_profile="dense",
        desktop_layout_template="right_stack",
        desktop_content_pack="creative_media",
        wallpaper_seed=33,
        desktop_seed=44,
        layout="overlap",
        apps=[
            SceneApp("chromium-browser", "single_python_downloads_scrolled", WindowRect(0, 0, 1200, 900)),
            SceneApp("thunar", "search_mode", WindowRect(400, 200, 900, 700)),
        ],
    )

    score_ab, _ = scene_similarity(a, b)
    score_ac, _ = scene_similarity(a, c)
    assert score_ab > score_ac


def test_compose_diverse_scene_batch_returns_unique_signatures() -> None:
    accepted, rejected, next_seed = compose_diverse_scene_batch(
        start_seed=10,
        count=4,
        available_apps=["chromium-browser", "vscode", "thunar", "gnome-calculator"],
        max_attempts=40,
    )

    assert len(accepted) == 4
    signatures = [planned.signature for planned in accepted]
    assert len(set(signatures)) == len(signatures)
    assert next_seed > 10
    assert len(rejected) >= 0


def test_compose_diverse_scene_batch_accepts_session_grouping() -> None:
    accepted, _rejected, _next_seed = compose_diverse_scene_batch(
        start_seed=10,
        count=4,
        available_apps=["chromium-browser", "vscode", "thunar", "gnome-calculator"],
        max_attempts=40,
        session_group_size=2,
    )

    assert len(accepted) == 4
    signatures = [scene_session_signature(planned.scene) for planned in accepted]
    assert signatures[0] == signatures[1]


def test_hamming_distance_hex_counts_bit_difference() -> None:
    assert hamming_distance_hex("0", "0") == 0
    assert hamming_distance_hex("0", "f") == 4


def test_compose_diverse_scene_batch_respects_existing_scenes() -> None:
    existing = [
        compose_scene(
            10,
            available_apps=["chromium-browser", "vscode", "thunar", "gnome-calculator"],
        )
    ]

    accepted, rejected, next_seed = compose_diverse_scene_batch(
        start_seed=10,
        count=2,
        available_apps=["chromium-browser", "vscode", "thunar", "gnome-calculator"],
        max_attempts=20,
        existing_scenes=existing,
    )

    assert len(accepted) == 2
    assert next_seed > 10
    existing_signature = scene_signature(existing[0])
    assert all(planned.signature != existing_signature for planned in accepted)
    assert any(row.reason in {"duplicate_signature", "too_similar_prelaunch"} for row in rejected)


def test_ubuntu_like_theme_prefers_left_dock_variant() -> None:
    scenes = [
        compose_scene(seed, available_apps=["chromium-browser", "vscode", "bluefish"])
        for seed in range(120, 150)
    ]
    ubuntu_scenes = [scene for scene in scenes if scene.theme_preset == "ubuntu_like"]

    assert ubuntu_scenes
    assert sum(scene.panel_variant.startswith("left") for scene in ubuntu_scenes) >= max(
        1,
        len(ubuntu_scenes) // 2,
    )


def test_default_scene_pool_excludes_apps_that_cannot_be_annotated_or_de_identified() -> None:
    """Three apps are deliberately absent, for two different reasons.

    **Cannot be annotated.** `filezilla`'s file panes never reach AT-SPI - a
    full walk found 235 nodes and zero of role table/tree/list/list item/table
    row/table cell - so every capture carries a large region of visible content
    no annotation can cover. `gucharmap`'s character table is a `drawing area`
    with no children, which is the app's entire point.

    **Cannot be de-identified.** `baobab` is a disk-usage analyser, so its
    window is a list of the host's storage: it drew the cluster's GPFS mount and the
    hostname as plain labels. `thunderbird`'s account-setup dialog pre-fills
    "Your full name" from the passwd GECOS field, which on this host is a work
    email address and an employee serial number.

    Manifests are kept for all of them; only the sampling pool drops them.
    """
    for app in ("filezilla", "gucharmap", "baobab", "thunderbird",
                "xarchiver", "pluma", "gnome-system-monitor"):
        assert app not in DEFAULT_SCENE_APP_POOL, app
    # Every pooled app must have curated states, or composition silently skips it.
    assert set(DEFAULT_SCENE_APP_POOL) <= set(CURATED_APP_STATES)
    assert len(DEFAULT_SCENE_APP_POOL) >= 15


def test_compose_scene_prefers_browser_presence_in_realistic_pool() -> None:
    pool = ["chromium-browser", "vscode", "thunar", "mousepad", "xarchiver", "gnome-calculator"]
    scenes = [compose_scene(seed, available_apps=pool) for seed in range(40, 80)]
    browser_count = sum(1 for scene in scenes if any(app.app_name == "chromium-browser" for app in scene.apps))

    assert browser_count >= 24


def test_compose_scene_uses_catalog_backed_browser_states() -> None:
    pool = ["chromium-browser", "vscode", "thunar", "mousepad"]
    scenes = [compose_scene(seed, available_apps=pool) for seed in range(100, 130)]

    assert any(
        app.app_name == "chromium-browser" and app.state_ref.startswith("catalog:")
        for scene in scenes
        for app in scene.apps
    )


def test_launch_scene_app_applies_atspi_timeout_floors(monkeypatch) -> None:
    """Both app classes get a floor; 12s starved cold compute nodes."""
    seen: list[tuple[str, float]] = []

    def fake_launch(manifest: AppManifest, *, timeout: float):
        seen.append((manifest.app_name, timeout))
        return object()

    monkeypatch.delenv("DESKSHOT_SCENE_LAUNCH_TIMEOUT", raising=False)
    monkeypatch.setattr("deskshot.generation.scene_composer.launch_app", fake_launch)

    _launch_scene_app(AppManifest("chromium-browser", "chromium-browser", "Chromium"), timeout=12.0)
    _launch_scene_app(AppManifest("mousepad", "mousepad", "Mousepad"), timeout=12.0)

    # A browser gets the same floor as anything else: measured in a four-worker
    # batch, the old 24s ceiling failed 16 of 16 Chromium launches.
    assert seen == [("chromium-browser", 90.0), ("mousepad", 90.0)]


def test_launch_scene_app_keeps_configured_timeout_when_higher(monkeypatch) -> None:
    seen: list[tuple[str, float]] = []

    def fake_launch(manifest: AppManifest, *, timeout: float):
        seen.append((manifest.app_name, timeout))
        return object()

    monkeypatch.delenv("DESKSHOT_SCENE_LAUNCH_TIMEOUT", raising=False)
    monkeypatch.setattr("deskshot.generation.scene_composer.launch_app", fake_launch)

    _launch_scene_app(AppManifest("mousepad", "mousepad", "Mousepad"), timeout=120.0)

    assert seen == [("mousepad", 120.0)]


def test_scene_coverage_penalty_flags_overused_combo() -> None:
    pool = ["baobab", "chromium-browser", "vscode", "thunar", "gnome-calculator"]
    comparison_pool = [
        compose_scene(6, available_apps=pool),
        compose_scene(7, available_apps=pool),
        compose_scene(8, available_apps=pool),
    ]
    candidate = SceneConfig(
        scene_id="dup",
        seed=999,
        theme_preset="linux_classic",
        display_preset="fhd_1920x1080",
        panel_variant="top",
        desktop_profile="balanced",
        desktop_layout_template="left_and_mid",
        desktop_content_pack="mixed_default",
        wallpaper_seed=1,
        desktop_seed=2,
        layout="split",
        apps=[
            SceneApp("chromium-browser", "single_python_downloads_scrolled", WindowRect(0, 0, 900, 700)),
            SceneApp("vscode", "default", WindowRect(920, 0, 900, 700)),
        ],
    )

    penalty, reason = scene_coverage_penalty(candidate, comparison_pool, pool=pool)
    assert penalty >= 0
    if penalty >= 4:
        assert reason in {"overused_app_combo_prelaunch", "overused_apps_prelaunch"}


def test_build_batch_element_coverage_reads_filtered_and_leaf_exports(tmp_path: Path) -> None:
    stem = "scene-demo"
    filtered = [
        {"type": "Button", "role": "push button", "source": "app", "app_name": "vscode"},
        {"type": "Text Input", "role": "entry", "source": "app", "app_name": "vscode"},
        {"type": "Menu", "role": "menu", "source": "desktop_chrome", "app_name": ""},
    ]
    leaf = [
        {"type": "Window", "role": "frame", "source": "app", "app_name": "vscode"},
        {"type": "Button", "role": "push button", "source": "app", "app_name": "vscode"},
    ]
    (tmp_path / f"{stem}.elements.json").write_text(json.dumps(filtered), encoding="utf-8")
    (tmp_path / f"{stem}.elements.leaf.json").write_text(json.dumps(leaf), encoding="utf-8")

    coverage = build_batch_element_coverage(
        tmp_path,
        [{"stem": stem, "scene": {"scene_id": "demo"}}],
    )

    assert coverage["accepted_scenes"] == 1
    assert coverage["filtered_type_counts"]["Button"] == 1
    assert coverage["leaf_type_counts"]["Window"] == 1
    assert coverage["leaf_window_app_counts"]["vscode"] == 1
    assert coverage["filtered_type_scene_frequency"]["Button"] == 1
    assert coverage["leaf_role_scene_frequency"]["frame"] == 1


def test_evaluate_scene_capture_includes_stage_survival(tmp_path: Path) -> None:
    scene = SceneConfig(
        scene_id="demo",
        seed=1,
        theme_preset="linux_classic",
        display_preset="fhd_1920x1080",
        panel_variant="top",
        desktop_profile="balanced",
        desktop_layout_template="center_cluster",
        desktop_content_pack="engineering_dev",
        wallpaper_seed=1,
        desktop_seed=2,
        layout="split",
        apps=[SceneApp("vscode", "default", WindowRect(0, 0, 800, 600))],
    )
    stem = "scene-demo"
    filtered = [
        {
            "type": "Window",
            "role": "frame",
            "source": "app",
            "app_name": "vscode",
            "rect": {"x": 0, "y": 0, "w": 600, "h": 400},
            "visible_fragments": [{"x": 0, "y": 0, "w": 600, "h": 400}],
            "_visibility_source_rect": {"x": 0, "y": 0, "w": 600, "h": 400},
            "is_occluded": False,
            "parent_index": None,
            "children_indices": [1],
            "_children_dom_indices": [1],
            "_window_stack_index": 3,
        },
        {
            "type": "Button",
            "role": "push button",
            "source": "app",
            "app_name": "vscode",
            "rect": {"x": 10, "y": 10, "w": 100, "h": 30},
            "visible_fragments": [{"x": 10, "y": 10, "w": 100, "h": 30}],
            "_visibility_source_rect": {"x": 10, "y": 10, "w": 100, "h": 30},
            "is_occluded": False,
            "parent_index": 0,
            "children_indices": [],
            "_children_dom_indices": [],
            "_window_stack_index": 3,
        },
        {
            "type": "Text Input",
            "role": "entry",
            "source": "app",
            "app_name": "vscode",
            "rect": {"x": 20, "y": 60, "w": 120, "h": 24},
            "visible_fragments": [{"x": 20, "y": 60, "w": 120, "h": 24}],
            "_visibility_source_rect": {"x": 20, "y": 60, "w": 120, "h": 24},
            "is_occluded": False,
            "parent_index": 0,
            "children_indices": [],
            "_children_dom_indices": [],
            "_window_stack_index": 3,
        },
        {
            "type": "Heading",
            "role": "heading",
            "source": "app",
            "app_name": "vscode",
            "rect": {"x": 20, "y": 100, "w": 140, "h": 30},
            "visible_fragments": [{"x": 20, "y": 100, "w": 140, "h": 30}],
            "_visibility_source_rect": {"x": 20, "y": 100, "w": 140, "h": 30},
            "is_occluded": False,
            "parent_index": 0,
            "children_indices": [],
            "_children_dom_indices": [],
            "_window_stack_index": 3,
        },
        {
            "type": "Menu",
            "role": "menu",
            "source": "desktop_chrome",
            "app_name": "mate-panel",
            "rect": {"x": 0, "y": 0, "w": 100, "h": 20},
            "visible_fragments": [{"x": 0, "y": 0, "w": 100, "h": 20}],
            "_visibility_source_rect": {"x": 0, "y": 0, "w": 100, "h": 20},
            "is_occluded": False,
            "parent_index": None,
            "children_indices": [],
            "_children_dom_indices": [],
        },
    ]
    unfiltered = filtered + [
        {
            "type": "Text",
            "role": "static",
            "source": "app",
            "app_name": "vscode",
            "rect": {"x": 20, "y": 140, "w": 90, "h": 20},
            "visible_fragments": [{"x": 20, "y": 140, "w": 90, "h": 20}],
            "_visibility_source_rect": {"x": 20, "y": 140, "w": 90, "h": 20},
            "is_occluded": False,
            "parent_index": 0,
            "children_indices": [],
            "_children_dom_indices": [],
            "_window_stack_index": 3,
        }
    ]
    leaf = filtered[:4]
    (tmp_path / f"{stem}.elements.unfiltered.json").write_text(json.dumps(unfiltered), encoding="utf-8")
    (tmp_path / f"{stem}.elements.json").write_text(json.dumps(filtered), encoding="utf-8")
    (tmp_path / f"{stem}.elements.leaf.json").write_text(json.dumps(leaf), encoding="utf-8")
    (tmp_path / f"{stem}.png").write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de0000000c49444154789c63606060000000040001f61738550000000049454e44ae426082"
        )
    )

    meta = {
        "stem": stem,
        "viewport": {"width": 800, "height": 600},
    }
    audit = evaluate_scene_capture(
        scene,
        output_dir=tmp_path,
        meta=meta,
        accepted_hashes={},
    )

    assert audit["accepted"] is True
    assert audit["stage_survival"]["counts"] == {"unfiltered": 6, "filtered": 5, "leaf": 4}
    assert audit["stage_survival"]["retention"]["filtered_vs_unfiltered"] == 0.8333


def test_scene_launch_timeout_floor_defaults(monkeypatch) -> None:
    """Non-browser apps need a cold-node floor above the old 12s."""
    from deskshot.generation.scene_composer import _scene_launch_timeout_floor

    monkeypatch.delenv("DESKSHOT_SCENE_LAUNCH_TIMEOUT", raising=False)

    assert _scene_launch_timeout_floor("mousepad") == 90.0
    assert _scene_launch_timeout_floor("chromium-browser") == 90.0


def test_scene_launch_timeout_floor_env_override(monkeypatch) -> None:
    from deskshot.generation.scene_composer import _scene_launch_timeout_floor

    monkeypatch.setenv("DESKSHOT_SCENE_LAUNCH_TIMEOUT", "75")

    assert _scene_launch_timeout_floor("mousepad") == 75.0
    assert _scene_launch_timeout_floor("chromium-browser") == 75.0


def test_scene_launch_timeout_floor_ignores_invalid_override(monkeypatch) -> None:
    from deskshot.generation.scene_composer import _scene_launch_timeout_floor

    monkeypatch.setenv("DESKSHOT_SCENE_LAUNCH_TIMEOUT", "not-a-number")

    assert _scene_launch_timeout_floor("mousepad") == 90.0
