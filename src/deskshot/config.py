"""Configuration dataclasses for DeskShot pipeline."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # repository root
TOOLS_DIR = PROJECT_ROOT / "tools"
EXTRACTED_DIR = TOOLS_DIR / "extracted"
EXTRACTED_BIN = EXTRACTED_DIR / "usr" / "bin"
EXTRACTED_LIB = EXTRACTED_DIR / "usr" / "lib64"
CONFIGS_DIR = PROJECT_ROOT / "configs"
XFCE_CONFIG_DIR = TOOLS_DIR / "xfce_config"
ASSETS_DIR = PROJECT_ROOT / "assets"

#: App manifests name in-repo files as `${DESKFORGE_ROOT}/...`, resolved to this
#: checkout when a manifest is loaded, so a clone works wherever it lives.
PROJECT_ROOT_TOKEN = "${DESKFORGE_ROOT}"

#: Absolute checkout paths that older manifests or captures may still carry,
#: rewritten to the current root on load. Set DESKSHOT_LEGACY_ROOTS to a
#: `os.pathsep`-separated list after moving a checkout.
LEGACY_PROJECT_ROOTS = tuple(
    Path(root) for root in os.environ.get("DESKSHOT_LEGACY_ROOTS", "").split(os.pathsep) if root
)

# ScreenParse's optional `webshot` element filter. Off unless DESKSHOT_WEBSHOT_SRC
# points at a webshot checkout; DeskForge-1M was generated without it.
WEBSHOT_SRC = Path(os.environ["DESKSHOT_WEBSHOT_SRC"]) if os.environ.get("DESKSHOT_WEBSHOT_SRC") else None


def runtime_tmp_dir() -> Path:
    """Return the scratch root for per-session runtime side copies."""
    return Path(os.environ.get("TMPDIR") or "/tmp")


def bin_fix_dir() -> Path:
    """Return the writable binary side-copy directory.

    Large LSF runs set DESKSHOT_BIN_FIX_DIR under each job's TMPDIR so
    permission-fixed binaries are not shared across concurrent jobs on a node.
    """
    return Path(os.environ.get("DESKSHOT_BIN_FIX_DIR") or runtime_tmp_dir() / "deskshot_bin_fix")


def ensure_webshot_importable() -> None:
    """Add an external webshot source to sys.path when one is configured."""
    if WEBSHOT_SRC is None:
        return
    src = str(WEBSHOT_SRC)
    if src not in sys.path:
        sys.path.insert(0, src)


def _rebase_project_path_value(value: Any) -> Any:
    """Resolve `${DESKFORGE_ROOT}` and legacy absolute roots to the current root."""
    if isinstance(value, str):
        current = str(PROJECT_ROOT)
        if PROJECT_ROOT_TOKEN in value:
            return value.replace(PROJECT_ROOT_TOKEN, current)
        for legacy_root in LEGACY_PROJECT_ROOTS:
            legacy = str(legacy_root)
            if value.startswith(f"file://{legacy}"):
                return f"file://{current}{value[len(f'file://{legacy}'):]}"
            if value.startswith(legacy):
                return f"{current}{value[len(legacy):]}"
        return value
    if isinstance(value, list):
        return [_rebase_project_path_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _rebase_project_path_value(item) for key, item in value.items()}
    return value


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

@dataclass
class DisplayConfig:
    """Xvfb virtual display settings."""
    display_number: int = 99
    width: int = 1920
    height: int = 1080
    depth: int = 24

    @property
    def display_str(self) -> str:
        return f":{self.display_number}"

    @property
    def screen_str(self) -> str:
        return f"{self.width}x{self.height}x{self.depth}"


@dataclass
class ThemeConfig:
    """Desktop theme settings."""
    gtk_theme: str = "Adwaita"
    icon_theme: str = "Adwaita"
    wm_theme: str = "Default"
    cursor_theme: str = "Adwaita"
    wallpaper: str = ""  # empty = use first available
    wallpaper_seed: int = 0
    font: str = "Sans 10"
    desktop_style: str = "linux"  # linux, windows, macos, ubuntu
    panel_variant: str = ""  # empty = style default, else top/bottom[/slim|tall]


@dataclass
class DesktopFixtureConfig:
    """Managed desktop-content settings for session diversity."""
    enabled: bool = True
    seed: int = 0
    profile: str = "balanced"  # sparse, balanced, dense
    num_folders: int = 0  # 0 = derive from profile
    num_files: int = 0  # 0 = derive from profile
    layout_template: str = ""  # empty = seed-sampled desktop icon layout template
    content_pack: str = ""  # empty = seed-sampled semantic desktop content mix


@dataclass
class SessionConfig:
    """Desktop session configuration."""
    display: DisplayConfig = field(default_factory=DisplayConfig)
    tools_dir: Path = field(default_factory=lambda: TOOLS_DIR)
    startup_timeout: float = 5.0
    app_launch_timeout: float = 10.0
    atspi_poll_interval: float = 0.3
    desktop_env: str = "metacity"  # "none", "metacity", or "xfce"
    theme: ThemeConfig = field(default_factory=ThemeConfig)
    desktop_fixture: DesktopFixtureConfig = field(default_factory=DesktopFixtureConfig)
    xfce_startup_timeout: float = 15.0


# ---------------------------------------------------------------------------
# App Manifests
# ---------------------------------------------------------------------------

@dataclass
class Action:
    """Single interaction action."""
    type: str  # "key", "click", "wait", "type_text"
    value: str = ""
    delay: float = 0.5  # seconds to wait after action


@dataclass
class InteractionSequence:
    """Named sequence of actions to reach a specific app state."""
    name: str
    description: str = ""
    actions: List[Action] = field(default_factory=list)
    companion_apps: List[str] = field(default_factory=list)
    actions_after_companions: bool = False


@dataclass
class ChainStep:
    """One step in a multi-capture interaction chain."""
    name: str = ""
    description: str = ""
    actions: List[Action] = field(default_factory=list)
    capture: bool = True
    companion_apps: List[str] = field(default_factory=list)
    actions_after_companions: bool = False


@dataclass
class InteractionChain:
    """Named sequence of steps captured from one long-lived app session."""
    name: str
    description: str = ""
    steps: List[ChainStep] = field(default_factory=list)


@dataclass
class AppManifest:
    """Per-app configuration defining launch and interaction sequences."""
    app_name: str
    binary: str
    atspi_name: str  # Name as it appears in AT-SPI tree
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    document_path: str = ""
    document_semantics: str = ""
    window_titles: List[str] = field(default_factory=list)
    interactions: List[InteractionSequence] = field(default_factory=list)
    chains: List[InteractionChain] = field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: Path) -> "AppManifest":
        """Load manifest from a YAML file."""
        with open(path) as f:
            data = yaml.safe_load(f)
        data = _rebase_project_path_value(data)

        def _parse_actions(raw_actions: List[Dict[str, Any]]) -> List[Action]:
            return [
                Action(
                    type=a["type"],
                    value=a.get("value", ""),
                    delay=a.get("delay", 0.5),
                )
                for a in raw_actions
            ]

        interactions = []
        for seq_data in data.get("interactions", []):
            interactions.append(InteractionSequence(
                name=seq_data["name"],
                description=seq_data.get("description", ""),
                actions=_parse_actions(seq_data.get("actions", [])),
                companion_apps=seq_data.get("companion_apps", []),
                actions_after_companions=seq_data.get("actions_after_companions", False),
            ))

        chains = []
        for chain_data in data.get("chains", []):
            steps = []
            for step_idx, step_data in enumerate(chain_data.get("steps", []), start=1):
                steps.append(ChainStep(
                    name=step_data.get("name", f"step{step_idx:02d}"),
                    description=step_data.get("description", ""),
                    actions=_parse_actions(step_data.get("actions", [])),
                    capture=step_data.get("capture", True),
                    companion_apps=step_data.get("companion_apps", []),
                    actions_after_companions=step_data.get("actions_after_companions", False),
                ))
            chains.append(InteractionChain(
                name=chain_data["name"],
                description=chain_data.get("description", ""),
                steps=steps,
            ))

        if data.get("scene_templates"):
            from deskshot.browser_scenes import expand_browser_scene_templates

            interactions.extend(
                expand_browser_scene_templates(
                    app_name=data["app_name"],
                    binary=data["binary"],
                    templates=data.get("scene_templates", []),
                )
            )

        return cls(
            app_name=data["app_name"],
            binary=data["binary"],
            atspi_name=data["atspi_name"],
            args=data.get("args", []),
            env=data.get("env", {}),
            cwd=data.get("cwd", ""),
            document_path=data.get("document_path", ""),
            document_semantics=data.get("document_semantics", ""),
            window_titles=data.get("window_titles", []),
            interactions=interactions,
            chains=chains,
        )


@dataclass(frozen=True)
class WorkItem:
    """One pipeline work item: either a single interaction or a chain."""

    manifest: AppManifest
    interaction: Optional[InteractionSequence] = None
    chain: Optional[InteractionChain] = None

    @property
    def name(self) -> str:
        if self.interaction is not None:
            return self.interaction.name
        if self.chain is not None:
            return self.chain.name
        raise ValueError("WorkItem requires either interaction or chain")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

@dataclass
class PipelineConfig:
    """Global pipeline configuration."""
    session: SessionConfig = field(default_factory=SessionConfig)
    output_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "raw")
    viz_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "viz")
    apps_config_dir: Path = field(default_factory=lambda: CONFIGS_DIR / "apps")

    # Filtering
    min_box_size: int = 4
    max_box_size: int = 1000
    iou_threshold: float = 0.95
    containment_threshold: float = 0.98

    # Quality thresholds
    min_elements: int = 5
    min_type_diversity: int = 3
    min_coverage_ratio: float = 0.10
    min_tree_depth: int = 3
    occlusion_clipping: bool = True
    occlusion_partial_clipping: bool = True
    occlusion_min_visible_ratio: float = 0.15

    @classmethod
    def from_yaml(cls, path: Path) -> "PipelineConfig":
        """Load pipeline config from YAML."""
        with open(path) as f:
            data = yaml.safe_load(f)

        display_cfg = DisplayConfig(**data.get("display", {}))

        # Parse theme config
        theme_data = data.get("theme", {})
        theme_cfg = ThemeConfig(**{k: v for k, v in theme_data.items()
                                   if k in ThemeConfig.__dataclass_fields__}) if theme_data else ThemeConfig()

        fixture_data = data.get("desktop_fixture", {})
        desktop_fixture_cfg = DesktopFixtureConfig(
            **{
                k: v for k, v in fixture_data.items()
                if k in DesktopFixtureConfig.__dataclass_fields__
            }
        ) if fixture_data else DesktopFixtureConfig()

        session_cfg = SessionConfig(
            display=display_cfg,
            theme=theme_cfg,
            desktop_fixture=desktop_fixture_cfg,
        )

        if "tools_dir" in data:
            session_cfg.tools_dir = Path(data["tools_dir"])
        if "startup_timeout" in data:
            session_cfg.startup_timeout = data["startup_timeout"]
        if "app_launch_timeout" in data:
            session_cfg.app_launch_timeout = data["app_launch_timeout"]
        if "desktop_env" in data:
            session_cfg.desktop_env = data["desktop_env"]
        if "xfce_startup_timeout" in data:
            session_cfg.xfce_startup_timeout = data["xfce_startup_timeout"]

        kwargs: Dict[str, Any] = {"session": session_cfg}

        if "output_dir" in data:
            kwargs["output_dir"] = Path(data["output_dir"])
        if "viz_dir" in data:
            kwargs["viz_dir"] = Path(data["viz_dir"])
        if "apps_config_dir" in data:
            kwargs["apps_config_dir"] = Path(data["apps_config_dir"])

        for key in ("min_box_size", "max_box_size", "iou_threshold",
                     "containment_threshold", "min_elements",
                     "min_type_diversity", "min_coverage_ratio",
                     "min_tree_depth", "occlusion_clipping",
                     "occlusion_partial_clipping", "occlusion_min_visible_ratio"):
            if key in data:
                kwargs[key] = data[key]

        return cls(**kwargs)
