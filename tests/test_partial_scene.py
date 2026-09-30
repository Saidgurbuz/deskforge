"""A scene must survive one app failing to come up.

Measured in a three-worker batch: Chromium failed to register in the AT-SPI
tree in half the scenes, and raising its budget from 90s to 162s did not change
that - so it is not a slow start, it is an app that sometimes never registers.
Every other app in those scenes had launched and drawn correctly, and the whole
capture was discarded.
"""

import pytest

from deskshot.generation import scene_composer


class _Proc:
    """Just enough of Popen for the teardown path."""

    pid = 4242

    def poll(self):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


class _Manifest:
    def __init__(self, name):
        self.app_name = name
        self.atspi_name = name


def _patch(monkeypatch, failing, captured):
    """Stub out everything but the launch loop."""
    monkeypatch.setattr(scene_composer, "_execute_scene_state", lambda *a, **k: None)
    monkeypatch.setattr(scene_composer, "_position_app_window", lambda *a, **k: None)
    monkeypatch.setattr(scene_composer, "wait_for_apps_to_settle", lambda *a, **k: {"settled": True})

    def _launch(manifest, *, timeout):
        if manifest.app_name in failing:
            raise RuntimeError(f"App '{manifest.app_name}' did not appear in AT-SPI tree")
        return _Proc()

    monkeypatch.setattr(scene_composer, "_launch_scene_app", _launch)

    def _capture(*args, **kwargs):
        captured.append(kwargs.get("extra_meta") or {})
        return {"stem": "scene-x"}

    monkeypatch.setattr(scene_composer, "_capture_current_state", _capture)


def _scene(names):
    from deskshot.generation.scene_composer import SceneApp, SceneConfig, WindowRect

    apps = [
        SceneApp(app_name=n, state_ref="default", rect=WindowRect(0, 0, 400, 400))
        for n in names
    ]
    return SceneConfig(
        scene_id="test", seed=1, theme_preset="linux_classic",
        display_preset="hdplus_1600x900", panel_variant="top",
        desktop_profile="balanced", desktop_layout_template="right_stack",
        desktop_content_pack="mixed_default", wallpaper_seed=1, desktop_seed=1,
        layout="family:grid", apps=apps,
    )


def test_the_scene_is_captured_without_the_app_that_failed(monkeypatch, tmp_path) -> None:
    captured: list = []
    _patch(monkeypatch, {"chromium-browser"}, captured)
    manifests = {n: _Manifest(n) for n in ("chromium-browser", "mousepad", "thunar")}

    row = scene_composer._run_scene_in_active_session(
        _scene(["chromium-browser", "mousepad", "thunar"]),
        scene_config=scene_composer.PipelineConfig(),
        manifests=manifests, out=tmp_path, include_desktop_chrome=False,
    )

    assert row is not None
    timing = captured[0]["scene_timing"]
    assert [m["app_name"] for m in timing["missing_apps"]] == ["chromium-browser"]


def test_a_scene_where_nothing_came_up_still_fails(monkeypatch, tmp_path) -> None:
    """Capturing an empty desktop and calling it a sample would be worse than
    failing: the plan asked for apps and none of them are there."""
    captured: list = []
    _patch(monkeypatch, {"mousepad", "thunar"}, captured)
    manifests = {n: _Manifest(n) for n in ("mousepad", "thunar")}

    with pytest.raises(RuntimeError, match="no app in scene"):
        scene_composer._run_scene_in_active_session(
            _scene(["mousepad", "thunar"]),
            scene_config=scene_composer.PipelineConfig(),
            manifests=manifests, out=tmp_path, include_desktop_chrome=False,
        )


def test_a_healthy_scene_records_no_missing_apps(monkeypatch, tmp_path) -> None:
    captured: list = []
    _patch(monkeypatch, set(), captured)
    manifests = {n: _Manifest(n) for n in ("mousepad", "thunar")}

    scene_composer._run_scene_in_active_session(
        _scene(["mousepad", "thunar"]),
        scene_config=scene_composer.PipelineConfig(),
        manifests=manifests, out=tmp_path, include_desktop_chrome=False,
    )

    assert captured[0]["scene_timing"]["missing_apps"] == []


def test_the_worker_launch_path_is_tolerant_too(monkeypatch) -> None:
    """The batch's persistent workers use `launch_scene_apps`, not the
    single-session path - patching only the latter left Chromium still costing
    four whole scenes in a live batch."""
    from deskshot.generation.scene_composer import launch_scene_apps

    monkeypatch.setattr(scene_composer, "_execute_scene_state", lambda *a, **k: None)
    monkeypatch.setattr(scene_composer, "_position_app_window", lambda *a, **k: None)

    def _launch(manifest, *, timeout):
        if manifest.app_name == "chromium-browser":
            raise RuntimeError("App 'Chromium' did not appear in AT-SPI tree")
        return _Proc()

    monkeypatch.setattr(scene_composer, "_launch_scene_app", _launch)

    launched = launch_scene_apps(
        _scene(["chromium-browser", "mousepad"]),
        scene_config=scene_composer.PipelineConfig(),
        manifests={n: _Manifest(n) for n in ("chromium-browser", "mousepad")},
    )

    assert launched.launched_apps == ["mousepad"]
    assert [m["app_name"] for m in launched.missing_apps] == ["chromium-browser"]


def test_the_worker_path_still_fails_when_nothing_came_up(monkeypatch) -> None:
    from deskshot.generation.scene_composer import launch_scene_apps

    monkeypatch.setattr(scene_composer, "_execute_scene_state", lambda *a, **k: None)
    monkeypatch.setattr(scene_composer, "_position_app_window", lambda *a, **k: None)
    monkeypatch.setattr(
        scene_composer, "_launch_scene_app",
        lambda manifest, *, timeout: (_ for _ in ()).throw(RuntimeError("nope")),
    )

    with pytest.raises(RuntimeError, match="no app in scene"):
        launch_scene_apps(
            _scene(["mousepad"]),
            scene_config=scene_composer.PipelineConfig(),
            manifests={"mousepad": _Manifest("mousepad")},
        )
