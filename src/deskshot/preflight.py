"""Compute-node portability preflight checks."""

from __future__ import annotations

import contextlib
import io
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from deskshot.automation.app_launcher import resolve_binary
from deskshot.config import CONFIGS_DIR, PROJECT_ROOT, TOOLS_DIR, bin_fix_dir
from deskshot.pipeline.orchestrator import load_manifests

CRITICAL_SESSION_BINARIES = [
    "Xvfb",
    "dbus-daemon",
    "dbus-run-session",
    "xdotool",
    "xfwm4",
    "mate-panel",
    "caja",
]

SYSTEM_PATH_ALLOWED = set()


@dataclass(frozen=True)
class PreflightResult:
    name: str
    binary: str
    path: str
    source: str
    status: str
    message: str

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _project_controlled_path(path: str) -> bool:
    p = Path(path)
    roots = [PROJECT_ROOT, TOOLS_DIR, bin_fix_dir()]
    return any(_path_is_under(p, root) for root in roots)


def _check_binary(name: str, binary: str, *, strict_compute: bool) -> PreflightResult:
    try:
        resolution = resolve_binary(binary)
    except FileNotFoundError as exc:
        return PreflightResult(name, binary, "", "missing", "fail", str(exc))

    path = Path(resolution.path)
    if not path.is_file():
        return PreflightResult(name, binary, resolution.path, resolution.source, "fail", "resolved path is not a file")
    if not path.stat().st_mode & 0o111:
        return PreflightResult(name, binary, resolution.path, resolution.source, "fail", "resolved path is not executable")
    if strict_compute and resolution.source == "system_path" and binary not in SYSTEM_PATH_ALLOWED:
        return PreflightResult(
            name,
            binary,
            resolution.path,
            resolution.source,
            "fail",
            "system PATH resolution is not compute-portable; stage this binary under tools/ or allow it explicitly",
        )
    if strict_compute and not resolution.compute_safe and not _project_controlled_path(resolution.path):
        return PreflightResult(
            name,
            binary,
            resolution.path,
            resolution.source,
            "fail",
            "resolved path is outside project-controlled runtime roots",
        )
    return PreflightResult(name, binary, resolution.path, resolution.source, "ok", "portable")


def _normalize_app_names(app_names: Optional[Iterable[str]]) -> Optional[List[str]]:
    if app_names is None:
        return None
    names = [name.strip() for name in app_names if name and name.strip()]
    return names or None


def run_app_binary_preflight(
    app_names: Optional[Iterable[str]] = None,
    app_configs_dir: Optional[Path] = None,
    *,
    strict_compute: bool = False,
    include_session_binaries: bool = True,
) -> List[PreflightResult]:
    """Check app and session binaries on the current node."""
    from deskshot.environment.setup import ensure_runtime_path_bridges

    with contextlib.redirect_stdout(io.StringIO()):
        ensure_runtime_path_bridges()

    names = _normalize_app_names(app_names)
    config_dir = app_configs_dir or (CONFIGS_DIR / "apps")
    all_manifests = load_manifests(config_dir)
    if names:
        manifests = [
            manifest
            for manifest in all_manifests
            if manifest.app_name in names or Path(manifest.app_name).stem in names
        ]
    else:
        manifests = all_manifests
    matched_names = {manifest.app_name for manifest in manifests}
    matched_names.update(path.stem for path in config_dir.glob("*.yaml") if path.stem in (names or []))
    results: List[PreflightResult] = []

    if names:
        for name in names:
            if name not in matched_names:
                results.append(PreflightResult(name, "", "", "missing", "fail", "app manifest not found"))

    for manifest in manifests:
        results.append(_check_binary(manifest.app_name, manifest.binary, strict_compute=strict_compute))

    if include_session_binaries:
        for binary in CRITICAL_SESSION_BINARIES:
            results.append(_check_binary(f"session:{binary}", binary, strict_compute=strict_compute))

    return results


def preflight_results_to_json(results: List[PreflightResult]) -> str:
    return json.dumps([asdict(result) for result in results], indent=2)


def format_preflight_results(results: List[PreflightResult]) -> str:
    lines = ["status\tname\tbinary\tsource\tpath\tmessage"]
    for result in results:
        lines.append(
            "\t".join(
                [
                    result.status,
                    result.name,
                    result.binary,
                    result.source,
                    result.path,
                    result.message,
                ]
            )
        )
    return "\n".join(lines)


def preflight_failed(results: List[PreflightResult]) -> bool:
    return any(not result.ok for result in results)
