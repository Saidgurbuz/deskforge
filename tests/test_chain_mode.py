"""Focused tests for multi-capture chain mode."""

from pathlib import Path

from deskshot.config import (
    Action,
    AppManifest,
    ChainStep,
    DisplayConfig,
    InteractionChain,
    InteractionSequence,
    PipelineConfig,
    SessionConfig,
    WorkItem,
)
from deskshot.pipeline.orchestrator import generate_work_items
from deskshot.config import CONFIGS_DIR


def test_generate_work_items_includes_interactions_and_chains() -> None:
    manifest = AppManifest(
        app_name="gnome-calculator",
        binary="gnome-calculator",
        atspi_name="gnome-calculator",
        interactions=[InteractionSequence(name="default")],
        chains=[InteractionChain(name="progressive_usage")],
    )

    items = generate_work_items([manifest])

    assert [item.name for item in items] == ["default", "progressive_usage"]
    assert items[0].interaction is not None
    assert items[0].chain is None
    assert items[1].interaction is None
    assert items[1].chain is not None


def test_generate_work_items_filters_chain_names() -> None:
    manifest = AppManifest(
        app_name="gnome-calculator",
        binary="gnome-calculator",
        atspi_name="gnome-calculator",
        interactions=[InteractionSequence(name="default")],
        chains=[InteractionChain(name="progressive_usage")],
    )

    items = generate_work_items([manifest], interaction_names=["progressive_usage"])

    assert len(items) == 1
    assert items[0].name == "progressive_usage"
    assert items[0].chain is not None


def test_run_chain_extraction_captures_multiple_steps(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from deskshot.extraction import run_extraction as mod

    config = PipelineConfig(
        session=SessionConfig(display=DisplayConfig(width=1280, height=720))
    )
    manifest = AppManifest(
        app_name="gnome-calculator",
        binary="gnome-calculator",
        atspi_name="gnome-calculator",
    )
    chain = InteractionChain(
        name="progressive_usage",
        description="calculator progression",
        steps=[
            ChainStep(
                name="empty",
                actions=[Action(type="wait", value="1.0")],
                capture=True,
            ),
            ChainStep(
                name="typed",
                actions=[Action(type="key", value="1")],
                capture=True,
            ),
            ChainStep(
                name="uncaptured",
                actions=[Action(type="wait", value="0.1")],
                capture=False,
            ),
        ],
    )

    launched = []
    executed = []
    killed = []
    captures = []

    monkeypatch.setattr(mod, "launch_app", lambda *args, **kwargs: launched.append(args[0].app_name) or object())
    monkeypatch.setattr(mod, "kill_app", lambda proc: killed.append(proc))
    monkeypatch.setattr(
        mod,
        "execute_sequence",
        lambda seq, **kwargs: executed.append(seq.name),
    )
    monkeypatch.setattr(mod, "_launch_companion_apps", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod.time, "sleep", lambda *_args, **_kwargs: None)

    def _fake_capture_current_state(**kwargs):
        captures.append(kwargs)
        stem = kwargs["stem"]
        return {
            "stem": stem,
            "screenshot": str(tmp_path / f"{stem}.png"),
            "elements": str(tmp_path / f"{stem}.elements.json"),
            "elements_unfiltered": str(tmp_path / f"{stem}.elements.unfiltered.json"),
            "meta": str(tmp_path / f"{stem}.meta.json"),
            "screentag": str(tmp_path / f"{stem}.screentag.txt"),
            "viz": str(tmp_path / f"{stem}.x_viz.png"),
            "num_elements": 5,
        }

    monkeypatch.setattr(mod, "_capture_current_state", _fake_capture_current_state)

    results = mod.run_chain_extraction(
        manifest,
        chain,
        config,
        output_dir=tmp_path,
        include_desktop_chrome=True,
        manifest_lookup={},
    )

    assert launched == ["gnome-calculator"]
    assert executed == [
        "progressive_usage:empty",
        "progressive_usage:typed",
        "progressive_usage:uncaptured",
    ]
    assert len(results) == 2
    assert [r["stem"].split("-")[-1] for r in results] == ["step01", "step02"]
    assert len(captures) == 2
    assert captures[0]["capture_name"] == "progressive_usage"
    assert captures[0]["extra_meta"]["chain_step_name"] == "empty"
    assert captures[1]["extra_meta"]["chain_step_index"] == 2
    assert captures[0]["include_desktop_chrome"] is True
    assert len(killed) == 1


def test_real_chromium_and_vscode_manifests_expose_chains() -> None:
    chromium = AppManifest.from_yaml(CONFIGS_DIR / "apps" / "chromium-browser.yaml")
    vscode = AppManifest.from_yaml(CONFIGS_DIR / "apps" / "vscode.yaml")
    thunar = AppManifest.from_yaml(CONFIGS_DIR / "apps" / "thunar.yaml")
    eog = AppManifest.from_yaml(CONFIGS_DIR / "apps" / "eog.yaml")
    file_roller = AppManifest.from_yaml(CONFIGS_DIR / "apps" / "file-roller.yaml")
    mousepad = AppManifest.from_yaml(CONFIGS_DIR / "apps" / "mousepad.yaml")
    xarchiver = AppManifest.from_yaml(CONFIGS_DIR / "apps" / "xarchiver.yaml")

    assert {chain.name for chain in chromium.chains} >= {
        "python_progressive",
        "docs_dual_window_scatter",
    }
    assert {chain.name for chain in vscode.chains} >= {
        "workspace_progressive",
        "workspace_heuristic_explore",
    }
    assert {chain.name for chain in thunar.chains} >= {
        "search_progressive",
        "with_eog_scatter",
        "workspace_progressive",
    }
    assert {chain.name for chain in eog.chains} >= {
        "image_progressive",
    }
    assert {chain.name for chain in file_roller.chains} >= {
        "archive_progressive",
    }
    assert {chain.name for chain in mousepad.chains} >= {
        "editor_progressive",
    }
    assert {chain.name for chain in xarchiver.chains} >= {
        "archive_progressive",
    }
