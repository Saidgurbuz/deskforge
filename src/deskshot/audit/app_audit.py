"""App qualification helpers for dense desktop UI extraction.

The goal is to reject apps that expose too little usable AT-SPI structure
before we spend time writing rich manifests or generating large datasets.
"""

from __future__ import annotations

import json
import logging
import shlex
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from deskshot.automation.app_launcher import find_binary
from deskshot.config import (
    ASSETS_DIR,
    Action,
    AppManifest,
    EXTRACTED_DIR,
    InteractionSequence,
    PipelineConfig,
)
from deskshot.environment.session import DesktopSession
from deskshot.extraction.run_extraction import run_single_extraction
from deskshot.pipeline.orchestrator import load_manifests
from deskshot.postprocessing.quality import run_quality_checks

logger = logging.getLogger(__name__)


INTERACTIVE_TYPES = {
    "Button",
    "Checkbox",
    "Radiobox",
    "Select",
    "Slider",
    "Steppers",
    "Tab",
    "Toggles",
    "Text Input",
    "Menu",
    "Link",
    "Calendar",
}

GENERIC_TEXTS = {
    "",
    "content view",
    "desktop",
    "window",
}
TEXT_PLACEHOLDER_CHARS = str.maketrans("", "", "\u200b\u200c\u200d\u2060\ufeff\ufffc")


@dataclass(frozen=True)
class AuditThresholds:
    """Thresholds for deciding whether an app is worth onboarding."""

    min_elements: int = 14
    min_type_diversity: int = 4
    min_interactive_elements: int = 6
    min_leaf_interactive_elements: int = 4
    min_named_elements: int = 4
    max_same_area_clusters: int = 3
    max_largest_same_area_cluster: int = 2
    max_zero_area_elements: int = 0


@dataclass(frozen=True)
class ProbeSpec:
    """Fallback probe spec for binaries that do not yet have manifests."""

    app_name: str
    binary: str
    atspi_name: str
    window_titles: List[str] = field(default_factory=list)
    args: List[str] = field(default_factory=list)
    wait_seconds: float = 2.0

    def to_manifest(self) -> AppManifest:
        return AppManifest(
            app_name=self.app_name,
            binary=self.binary,
            atspi_name=self.atspi_name,
            args=list(self.args),
            window_titles=list(self.window_titles),
            interactions=[
                InteractionSequence(
                    name="default_probe",
                    description="Qualification probe",
                    actions=[Action(type="wait", value=f"{self.wait_seconds:.1f}")],
                )
            ],
        )


FALLBACK_PROBES: Dict[str, ProbeSpec] = {
    "thunar": ProbeSpec(
        app_name="thunar",
        binary="thunar",
        atspi_name="thunar",
        window_titles=["Thunar", "File Manager"],
        wait_seconds=2.0,
    ),
}

DISCOVERY_ALLOWED_CATEGORIES = {
    "2DGraphics",
    "Calculator",
    "Development",
    "FileManager",
    "FileTools",
    "Graphics",
    "IDE",
    "Network",
    "Office",
    "Programming",
    "RasterGraphics",
    "TextEditor",
    "Utility",
    "VectorGraphics",
    "Viewer",
    "WebBrowser",
}
DISCOVERY_EXCLUDED_CATEGORIES = {
    "DesktopSettings",
    "Settings",
    "X-XFCE-SettingsDialog",
}
DISCOVERY_EXCLUDED_BINARIES = {
    "caja",
    "caja-autorun-software",
    "caja-file-management-properties",
    "dbus-daemon",
    "exo-open",
    "mate-panel",
    "metacity",
    "nautilus-autorun-software",
    "thunar-settings",
    "xfce4-panel",
    "xfce4-session",
    "xfce4-session-logout",
    "xfdesktop",
    "xfdesktop-settings",
    "xfsettingsd",
    "xfwm4",
    "xfwm4-settings",
    "xfwm4-tweaks-settings",
    "xfwm4-workspace-settings",
}
DISCOVERY_BROWSER_EXTRA_ARGS: Dict[str, List[str]] = {
    "chromium-browser": [
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--password-store=basic",
        "--user-data-dir=/tmp/session_chromium_profile",
        "about:blank",
    ],
}
DISCOVERY_BROWSER_ATSPI_NAMES: Dict[str, str] = {
    "chromium-browser": "Chromium",
}
DISCOVERY_BROWSER_EXTRA_ENV: Dict[str, Dict[str, str]] = {
    "chromium-browser": {
        "ACCESSIBILITY_ENABLED": "1",
        "GNOME_ACCESSIBILITY": "1",
    },
}
BROWSER_AUDIT_BINARIES = {"chromium-browser", "firefox", "firefox-bin"}
BROWSER_AUDIT_PAGE = ASSETS_DIR / "audit" / "browser_dense_probe.html"
BROWSER_AUDIT_TITLE = "DeskShot Browser Audit"
BROWSER_AUDIT_EXTRA_ENV: Dict[str, Dict[str, str]] = {
    "chromium-browser": {
        "ACCESSIBILITY_ENABLED": "1",
        "GNOME_ACCESSIBILITY": "1",
    },
    "firefox": {
        "ACCESSIBILITY_ENABLED": "1",
        "GNOME_ACCESSIBILITY": "1",
        "MOZ_ACCESSIBILITY_FORCE_DISABLED": "0",
    },
    "firefox-bin": {
        "ACCESSIBILITY_ENABLED": "1",
        "GNOME_ACCESSIBILITY": "1",
        "MOZ_ACCESSIBILITY_FORCE_DISABLED": "0",
    },
}
BROWSER_AUDIT_EXTRA_ARGS: Dict[str, List[str]] = {
    "chromium-browser": ["--force-renderer-accessibility", "--test-type"],
}
DEFAULT_LIVE_BROWSER_WAIT_SECONDS = 5.0


@dataclass(frozen=True)
class DiscoveredProbeSpec:
    """Probe spec derived conservatively from a desktop entry."""

    app_name: str
    binary: str
    atspi_name: str
    display_name: str
    desktop_id: str
    desktop_file: Path
    window_titles: List[str] = field(default_factory=list)
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    categories: List[str] = field(default_factory=list)
    startup_wm_class: str = ""
    wait_seconds: float = 2.0

    def to_manifest(self) -> AppManifest:
        return AppManifest(
            app_name=self.app_name,
            binary=self.binary,
            atspi_name=self.atspi_name,
            args=list(self.args),
            env=dict(self.env),
            window_titles=list(self.window_titles),
            interactions=[
                InteractionSequence(
                    name="default_probe",
                    description=f"Qualification probe for {self.display_name}",
                    actions=[Action(type="wait", value=f"{self.wait_seconds:.1f}")],
                )
            ],
        )


def _normalized_text(value: str) -> str:
    cleaned = (value or "").translate(TEXT_PLACEHOLDER_CHARS)
    return " ".join(cleaned.strip().split()).lower()


def _normalized_alias(value: str) -> str:
    return _normalized_text(value).replace("_", "-").replace(".desktop", "")


def _desktop_bool(value: str) -> bool:
    return _normalized_text(value) in {"1", "true", "yes"}


def _parse_desktop_entry(path: Path) -> Dict[str, str]:
    """Parse the main [Desktop Entry] section only, ignoring localized keys."""
    data: Dict[str, str] = {}
    in_section = False
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            if line == "[Desktop Entry]":
                in_section = True
                continue
            if in_section:
                break
            continue
        if not in_section or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        key = key.strip()
        if "[" in key:
            continue
        data.setdefault(key, value.strip())
    return data


def _parse_exec_command(exec_value: str) -> tuple[Optional[str], List[str]]:
    """Parse a desktop entry Exec command into binary + args."""
    if not exec_value:
        return None, []
    try:
        tokens = shlex.split(exec_value, posix=True)
    except ValueError:
        tokens = exec_value.split()
    if not tokens:
        return None, []

    # Support simple "env KEY=VAL app ..." launchers.
    idx = 0
    if tokens and tokens[0] == "env":
        idx = 1
        while idx < len(tokens) and "=" in tokens[idx] and not tokens[idx].startswith("%"):
            idx += 1
    if idx >= len(tokens):
        return None, []

    binary = Path(tokens[idx]).name
    args = [tok for tok in tokens[idx + 1:] if not tok.startswith("%")]
    return binary, args


def _binary_is_available(binary_name: str) -> bool:
    try:
        find_binary(binary_name)
    except FileNotFoundError:
        return False
    return True


def browser_audit_page_uri() -> str:
    """Return the local deterministic browser audit page URI."""
    return BROWSER_AUDIT_PAGE.resolve().as_uri()


def _is_browser_manifest(manifest: AppManifest) -> bool:
    names = {
        manifest.app_name,
        manifest.binary,
        Path(manifest.binary).name,
        Path(manifest.binary).stem,
    }
    return any(name in BROWSER_AUDIT_BINARIES for name in names if name)


def _replace_or_append_browser_target(args: List[str], target_uri: str) -> List[str]:
    out: List[str] = []
    replaced = False
    for arg in args:
        if (
            arg.startswith("about:")
            or arg.startswith("http://")
            or arg.startswith("https://")
            or arg.startswith("file://")
            or arg.endswith(".html")
            or arg.endswith(".htm")
        ):
            if not replaced:
                out.append(target_uri)
                replaced = True
            continue
        out.append(arg)

    if not replaced:
        out.append(target_uri)
    return out


def prepare_manifest_for_audit(
    manifest: AppManifest,
    *,
    browser_url: Optional[str] = None,
) -> AppManifest:
    """Apply deterministic audit-time adjustments without changing source manifests."""
    if not _is_browser_manifest(manifest):
        return manifest

    binary_key = Path(manifest.binary).name
    target_uri = browser_url or browser_audit_page_uri()
    args = _replace_or_append_browser_target(list(manifest.args), target_uri)
    for extra_arg in BROWSER_AUDIT_EXTRA_ARGS.get(binary_key, []):
        if extra_arg not in args:
            args.insert(0, extra_arg)

    env = dict(manifest.env)
    for key, value in BROWSER_AUDIT_EXTRA_ENV.get(binary_key, {}).items():
        env.setdefault(key, value)

    window_titles = list(manifest.window_titles)
    if browser_url is None:
        for title in (BROWSER_AUDIT_TITLE,):
            if title not in window_titles:
                window_titles.append(title)

    return replace(
        manifest,
        args=args,
        env=env,
        window_titles=window_titles,
    )


def prepare_interaction_for_audit(
    interaction: InteractionSequence,
    manifest: AppManifest,
    *,
    browser_wait_seconds: Optional[float] = None,
) -> InteractionSequence:
    """Extend browser probe waits for live URL targets when requested."""
    if browser_wait_seconds is None or not _is_browser_manifest(manifest):
        return interaction

    actions = [replace(action) for action in interaction.actions]
    for action in reversed(actions):
        if action.type != "wait":
            continue
        try:
            current = float(action.value)
        except ValueError:
            current = 0.0
        if current < browser_wait_seconds:
            action.value = f"{browser_wait_seconds:.1f}"
        return replace(interaction, actions=actions)

    actions.append(Action(type="wait", value=f"{browser_wait_seconds:.1f}"))
    return replace(interaction, actions=actions)


def _manifest_aliases(manifest: AppManifest, *, extra_aliases: Optional[List[str]] = None) -> set[str]:
    aliases = {
        _normalized_alias(manifest.app_name),
        _normalized_alias(manifest.binary),
        _normalized_alias(Path(manifest.binary).stem),
    }
    aliases.update(_normalized_alias(title) for title in manifest.window_titles if title)
    if extra_aliases:
        aliases.update(_normalized_alias(alias) for alias in extra_aliases if alias)
    return {alias for alias in aliases if alias}


def _probe_aliases(spec: DiscoveredProbeSpec) -> set[str]:
    aliases = {
        _normalized_alias(spec.app_name),
        _normalized_alias(spec.binary),
        _normalized_alias(spec.atspi_name),
        _normalized_alias(spec.display_name),
        _normalized_alias(spec.desktop_id),
        _normalized_alias(Path(spec.desktop_id).stem),
        _normalized_alias(spec.startup_wm_class),
    }
    aliases.update(_normalized_alias(title) for title in spec.window_titles if title)
    return {alias for alias in aliases if alias}


def _should_consider_desktop_entry(
    entry: Dict[str, str],
    *,
    include_hidden: bool,
) -> bool:
    if _normalized_text(entry.get("Type", "")) != "application":
        return False
    if _desktop_bool(entry.get("Terminal", "")):
        return False
    if _desktop_bool(entry.get("NoDisplay", "")) and not include_hidden:
        return False

    categories = {c for c in entry.get("Categories", "").split(";") if c}
    if categories & DISCOVERY_EXCLUDED_CATEGORIES:
        return False
    if not categories & DISCOVERY_ALLOWED_CATEGORIES:
        return False
    return True


def _build_discovered_probe(
    desktop_file: Path,
    entry: Dict[str, str],
) -> Optional[DiscoveredProbeSpec]:
    binary, args = _parse_exec_command(entry.get("Exec", ""))
    if not binary or binary in DISCOVERY_EXCLUDED_BINARIES:
        return None
    if not _binary_is_available(binary):
        return None

    display_name = entry.get("Name") or Path(binary).stem
    startup_wm_class = entry.get("StartupWMClass", "")
    app_stem = Path(binary).stem or binary
    atspi_name = DISCOVERY_BROWSER_ATSPI_NAMES.get(binary, app_stem)
    window_titles = []
    for title in (display_name, startup_wm_class, atspi_name, app_stem, binary):
        if not title or title in window_titles:
            continue
        window_titles.append(title)

    categories = [c for c in entry.get("Categories", "").split(";") if c]
    probe_args = list(args)
    if binary in DISCOVERY_BROWSER_EXTRA_ARGS:
        probe_args = list(DISCOVERY_BROWSER_EXTRA_ARGS[binary])
    probe_env = dict(DISCOVERY_BROWSER_EXTRA_ENV.get(binary, {}))
    return DiscoveredProbeSpec(
        app_name=app_stem,
        binary=binary,
        atspi_name=atspi_name,
        display_name=display_name,
        desktop_id=desktop_file.stem,
        desktop_file=desktop_file,
        window_titles=window_titles,
        args=probe_args,
        env=probe_env,
        categories=categories,
    )


def discover_probe_specs(
    applications_dir: Path,
    *,
    app_names: Optional[List[str]] = None,
    include_hidden: bool = False,
    exclude_aliases: Optional[set[str]] = None,
    limit: Optional[int] = None,
) -> List[DiscoveredProbeSpec]:
    """Discover audit probe specs from extracted .desktop files."""
    requested = {_normalized_alias(name) for name in (app_names or []) if name}
    excluded = {_normalized_alias(alias) for alias in (exclude_aliases or set()) if alias}

    specs: List[DiscoveredProbeSpec] = []
    seen_apps: set[str] = set()
    for desktop_file in sorted(applications_dir.glob("*.desktop")):
        entry = _parse_desktop_entry(desktop_file)
        if not _should_consider_desktop_entry(entry, include_hidden=include_hidden):
            continue
        spec = _build_discovered_probe(desktop_file, entry)
        if spec is None:
            continue

        aliases = _probe_aliases(spec)
        if aliases & excluded:
            continue
        if requested and not aliases & requested:
            continue
        if spec.app_name in seen_apps:
            continue

        specs.append(spec)
        seen_apps.add(spec.app_name)
        if limit is not None and len(specs) >= limit:
            break

    return specs


def _rect_iou(a: Dict[str, int], b: Dict[str, int]) -> float:
    ax1, ay1 = a.get("x", 0), a.get("y", 0)
    ax2, ay2 = ax1 + a.get("w", 0), ay1 + a.get("h", 0)
    bx1, by1 = b.get("x", 0), b.get("y", 0)
    bx2, by2 = bx1 + b.get("w", 0), by1 + b.get("h", 0)
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter == 0:
        return 0.0
    aa = max(1, (ax2 - ax1) * (ay2 - ay1))
    ba = max(1, (bx2 - bx1) * (by2 - by1))
    return inter / max(1, aa + ba - inter)


def _same_area(a: Dict[str, int], b: Dict[str, int]) -> bool:
    ia = _rect_iou(a, b)
    if ia < 0.992:
        return False
    aa = max(1, a.get("w", 0) * a.get("h", 0))
    ba = max(1, b.get("w", 0) * b.get("h", 0))
    ratio = aa / ba
    return 0.96 <= ratio <= 1.04


def _count_same_area_clusters(elements: List[Dict[str, Any]]) -> tuple[int, int]:
    """Count same-area duplicate clusters within app elements."""
    groups: List[set[int]] = []
    used = set()

    for i, a in enumerate(elements):
        if i in used:
            continue
        cluster = {i}
        for j in range(i + 1, len(elements)):
            b = elements[j]
            if a.get("app_name") != b.get("app_name"):
                continue
            if a.get("source") != b.get("source"):
                continue
            if not _same_area(a.get("rect", {}), b.get("rect", {})):
                continue

            a_text = _normalized_text(a.get("inner_text", ""))
            b_text = _normalized_text(b.get("inner_text", ""))
            if a_text and b_text and a_text != b_text:
                continue
            cluster.add(j)

        if len(cluster) > 1:
            groups.append(cluster)
            used |= cluster

    return len(groups), max((len(g) for g in groups), default=1)


def summarize_app_elements(
    elements: Iterable[Dict[str, Any]],
    *,
    viewport_w: int = 1920,
    viewport_h: int = 1080,
    thresholds: AuditThresholds | None = None,
) -> Dict[str, Any]:
    """Summarize one app's filtered elements and classify support quality."""
    thresholds = thresholds or AuditThresholds()
    app_elements = [e for e in elements if e.get("source") != "desktop_chrome"]

    types = {e.get("type", "") for e in app_elements if e.get("type")}
    roles = {e.get("role", "") for e in app_elements if e.get("role")}
    named_elements = [
        e for e in app_elements
        if _normalized_text(e.get("inner_text", "")) not in GENERIC_TEXTS
    ]
    interactive_elements = [
        e for e in app_elements
        if e.get("type") in INTERACTIVE_TYPES
    ]
    leaf_interactive_elements = [
        e for e in interactive_elements
        if not e.get("children_indices") and not e.get("_children_dom_indices")
    ]
    zero_area_elements = [
        e for e in app_elements
        if e.get("rect", {}).get("w", 0) <= 0 or e.get("rect", {}).get("h", 0) <= 0
    ]
    same_area_cluster_count, largest_same_area_cluster = _count_same_area_clusters(app_elements)
    quality_checks = run_quality_checks(
        app_elements,
        viewport_w=viewport_w,
        viewport_h=viewport_h,
    )
    quality_ok = all(ok for _, ok, _ in quality_checks)

    metrics = {
        "num_elements": len(app_elements),
        "num_type_diversity": len(types),
        "num_role_diversity": len(roles),
        "num_named_elements": len(named_elements),
        "num_interactive_elements": len(interactive_elements),
        "num_leaf_interactive_elements": len(leaf_interactive_elements),
        "num_zero_area_elements": len(zero_area_elements),
        "same_area_cluster_count": same_area_cluster_count,
        "largest_same_area_cluster": largest_same_area_cluster,
        "quality_ok": quality_ok,
        "quality_checks": [
            {"name": name, "ok": ok, "message": message}
            for name, ok, message in quality_checks
        ],
    }

    hard_failures: List[str] = []
    review_flags: List[str] = []

    if metrics["num_elements"] < thresholds.min_elements:
        hard_failures.append(f"elements<{thresholds.min_elements}")
    if metrics["num_type_diversity"] < thresholds.min_type_diversity:
        hard_failures.append(f"type_diversity<{thresholds.min_type_diversity}")
    if metrics["num_interactive_elements"] < thresholds.min_interactive_elements:
        hard_failures.append(f"interactive<{thresholds.min_interactive_elements}")
    if metrics["num_leaf_interactive_elements"] < thresholds.min_leaf_interactive_elements:
        hard_failures.append(f"leaf_interactive<{thresholds.min_leaf_interactive_elements}")
    if metrics["num_zero_area_elements"] > thresholds.max_zero_area_elements:
        hard_failures.append(f"zero_area>{thresholds.max_zero_area_elements}")

    if metrics["num_named_elements"] < thresholds.min_named_elements:
        review_flags.append(f"named<{thresholds.min_named_elements}")
    if metrics["same_area_cluster_count"] > thresholds.max_same_area_clusters:
        review_flags.append(f"same_area_clusters>{thresholds.max_same_area_clusters}")
    if metrics["largest_same_area_cluster"] > thresholds.max_largest_same_area_cluster:
        review_flags.append(
            f"largest_same_area_cluster>{thresholds.max_largest_same_area_cluster}"
        )
    if not quality_ok:
        review_flags.append("quality_checks_partial_fail")

    if hard_failures:
        status = "reject"
    elif review_flags:
        status = "review"
    else:
        status = "pass"

    return {
        "status": status,
        "hard_failures": hard_failures,
        "review_flags": review_flags,
        "metrics": metrics,
    }


def select_probe_interaction(manifest: AppManifest) -> InteractionSequence:
    """Prefer a deterministic default interaction for app qualification."""
    for preferred_name in ("audit_probe", "default"):
        for interaction in manifest.interactions:
            if interaction.name == preferred_name:
                return interaction
    if manifest.interactions:
        return manifest.interactions[0]
    return InteractionSequence(
        name="default_probe",
        description="Qualification probe",
        actions=[Action(type="wait", value="1.5")],
    )


def resolve_probe_manifests(
    config_dir: Path,
    app_names: Optional[List[str]] = None,
    *,
    discover: bool = False,
    include_hidden: bool = False,
    applications_dir: Optional[Path] = None,
    limit: Optional[int] = None,
) -> List[AppManifest]:
    """Resolve requested manifests, falling back to built-in probe specs."""
    existing = load_manifests(config_dir)
    stem_entries = {
        _normalized_alias(path.stem): AppManifest.from_yaml(path)
        for path in sorted(config_dir.glob("*.yaml"))
    }
    fallback_manifests = {name: spec.to_manifest() for name, spec in FALLBACK_PROBES.items()}

    manifest_alias_map: Dict[str, AppManifest] = {}
    for manifest in existing:
        for alias in _manifest_aliases(manifest):
            manifest_alias_map.setdefault(alias, manifest)
    for alias, manifest in stem_entries.items():
        manifest_alias_map.setdefault(alias, manifest)
        for extra in _manifest_aliases(manifest, extra_aliases=[alias]):
            manifest_alias_map.setdefault(extra, manifest)
    for name, manifest in fallback_manifests.items():
        for alias in _manifest_aliases(manifest, extra_aliases=[name]):
            manifest_alias_map.setdefault(alias, manifest)

    if app_names:
        manifests: List[AppManifest] = []
        seen: set[str] = set()
        unresolved: List[str] = []
        for raw_name in app_names:
            manifest = manifest_alias_map.get(_normalized_alias(raw_name))
            if manifest is None:
                unresolved.append(raw_name)
                continue
            if manifest.app_name in seen:
                continue
            manifests.append(manifest)
            seen.add(manifest.app_name)

        if discover and unresolved:
            probe_specs = discover_probe_specs(
                applications_dir or (EXTRACTED_DIR / "usr" / "share" / "applications"),
                app_names=unresolved,
                include_hidden=include_hidden,
                exclude_aliases=set(manifest_alias_map),
                limit=limit,
            )
            for spec in probe_specs:
                manifest = spec.to_manifest()
                if manifest.app_name in seen:
                    continue
                manifests.append(manifest)
                seen.add(manifest.app_name)
        return manifests

    if discover:
        probe_specs = discover_probe_specs(
            applications_dir or (EXTRACTED_DIR / "usr" / "share" / "applications"),
            include_hidden=include_hidden,
            exclude_aliases=set(manifest_alias_map),
            limit=limit,
        )
        return [spec.to_manifest() for spec in probe_specs]

    requested = sorted({_normalized_alias(m.app_name) for m in existing} | set(FALLBACK_PROBES))
    manifests = []
    seen: set[str] = set()
    for name in requested:
        manifest = manifest_alias_map.get(_normalized_alias(name))
        if manifest is None or manifest.app_name in seen:
            continue
        manifests.append(manifest)
        seen.add(manifest.app_name)
    return manifests


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=True), encoding="utf-8")


def _write_summary_markdown(path: Path, rows: List[Dict[str, Any]]) -> None:
    lines = [
        "# App Audit Summary",
        "",
        "| App | Status | Elements | Types | Interactive | Leaf Interactive | Same-area Clusters |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        audit = row.get("audit")
        if audit is None:
            lines.append(
                f"| `{row['app_name']}` | {row.get('status', 'unknown')} | 0 | 0 | 0 | 0 | 0 |"
            )
            continue
        metrics = audit["metrics"]
        lines.append(
            f"| `{row['app_name']}` | {audit['status']} | "
            f"{metrics['num_elements']} | {metrics['num_type_diversity']} | "
            f"{metrics['num_interactive_elements']} | {metrics['num_leaf_interactive_elements']} | "
            f"{metrics['same_area_cluster_count']} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_app_audit(
    config: PipelineConfig,
    manifests: List[AppManifest],
    *,
    output_dir: Path,
    include_desktop_chrome: bool = True,
    thresholds: AuditThresholds | None = None,
    browser_url: Optional[str] = None,
    browser_wait_seconds: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Run qualification probe for each app inside one desktop session."""
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: List[Dict[str, Any]] = []
    manifest_lookup = {m.app_name: m for m in manifests}

    with DesktopSession(config.session) as session:
        logger.info("Audit session started on %s", session.display)
        for manifest in manifests:
            audit_manifest = prepare_manifest_for_audit(manifest, browser_url=browser_url)
            interaction = prepare_interaction_for_audit(
                select_probe_interaction(audit_manifest),
                audit_manifest,
                browser_wait_seconds=(
                    browser_wait_seconds
                    if browser_wait_seconds is not None
                    else (DEFAULT_LIVE_BROWSER_WAIT_SECONDS if browser_url else None)
                ),
            )
            result = run_single_extraction(
                audit_manifest,
                interaction,
                config,
                output_dir=output_dir,
                include_desktop_chrome=include_desktop_chrome,
                manifest_lookup=manifest_lookup,
            )
            if result is None:
                rows.append(
                    {
                        "app_name": manifest.app_name,
                        "binary": manifest.binary,
                        "interaction": interaction.name,
                        "status": "launch_failed",
                    }
                )
                continue

            elements = json.loads(Path(result["elements"]).read_text(encoding="utf-8"))
            meta = json.loads(Path(result["meta"]).read_text(encoding="utf-8"))
            audit = summarize_app_elements(
                elements,
                viewport_w=meta["viewport"]["width"],
                viewport_h=meta["viewport"]["height"],
                thresholds=thresholds,
            )
            audit_path = output_dir / f"{result['stem']}.audit.json"
            _write_json(
                audit_path,
                {
                    "app_name": manifest.app_name,
                    "binary": manifest.binary,
                    "interaction": interaction.name,
                    "result": result,
                    "audit": audit,
                    "browser_url": browser_url or "",
                },
            )
            rows.append(
                {
                    "app_name": manifest.app_name,
                    "binary": manifest.binary,
                    "interaction": interaction.name,
                    "result": result,
                    "audit": audit,
                    "browser_url": browser_url or "",
                }
            )

    _write_json(output_dir / "audit_summary.json", rows)
    _write_summary_markdown(output_dir / "summary.md", rows)
    return rows
