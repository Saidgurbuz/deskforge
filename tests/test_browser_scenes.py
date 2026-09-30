"""Tests for config-driven browser scene templates."""

import pytest

from deskshot.browser_scenes import expand_browser_scene_templates


def test_expand_chromium_live_windows_scene_builds_expected_actions() -> None:
    interactions = expand_browser_scene_templates(
        app_name="chromium-browser",
        binary="chromium-browser",
        templates=[
            {
                "type": "chromium_live_windows",
                "name": "split_python_docs",
                "description": "Two-window Python scene",
                "settle_seconds": 8.0,
                "final_wait_seconds": 0.8,
                "windows": [
                    {
                        "url": "https://www.python.org/",
                        "x": 150,
                        "y": 70,
                        "width": 820,
                        "height": 880,
                    },
                    {
                        "url": "https://docs.python.org/3/",
                        "x": 980,
                        "y": 85,
                        "width": 900,
                        "height": 860,
                        "post_actions": [
                            {"type": "key", "value": "Page_Down", "delay": 0.8},
                        ],
                    },
                ],
            }
        ],
    )

    assert len(interactions) == 1
    interaction = interactions[0]
    assert interaction.name == "split_python_docs"
    assert interaction.description == "Two-window Python scene"
    action_types = [action.type for action in interaction.actions]
    assert action_types.count("type_text") == 2
    assert action_types.count("window_resize_active") == 2
    assert action_types.count("window_move_active") == 2
    assert "Page_Down" in [action.value for action in interaction.actions]
    assert "ctrl+n" in [action.value for action in interaction.actions]
    assert interaction.actions[-1].type == "wait"
    assert interaction.actions[-1].value == "0.8"


def test_expand_chromium_live_windows_scene_rejects_empty_inline_action_type() -> None:
    with pytest.raises(ValueError, match="non-empty type"):
        expand_browser_scene_templates(
            app_name="chromium-browser",
            binary="chromium-browser",
            templates=[
                {
                    "type": "chromium_live_windows",
                    "name": "bad_actions",
                    "windows": [
                        {
                            "url": "https://www.python.org/",
                            "x": 0,
                            "y": 0,
                            "width": 100,
                            "height": 100,
                            "post_actions": [{"type": "", "value": "Page_Down"}],
                        }
                    ],
                }
            ],
        )


def test_expand_chromium_live_windows_scene_rejects_non_chromium() -> None:
    with pytest.raises(ValueError, match="Chromium"):
        expand_browser_scene_templates(
            app_name="firefox",
            binary="firefox-bin",
            templates=[
                {
                    "type": "chromium_live_windows",
                    "name": "bad",
                    "windows": [{"url": "https://www.python.org/", "x": 0, "y": 0, "width": 100, "height": 100}],
                }
            ],
        )
