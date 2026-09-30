#!/usr/bin/env python
"""Verify every scene-pool app still launches and registers in AT-SPI.

The pool is only as good as its weakest app: a single app that stops registering
silently poisons every scene that samples it, and nothing else in the pipeline
notices. `cli preflight` only proves the binary resolves, not that the app comes
up and exposes an accessibility tree.

Runs each app in one shared headless session and reports launch + AT-SPI
registration + element count, so a regression shows up as a table row rather
than as a mysterious drop in batch acceptance.

Usage:
    PYTHONPATH=src python scripts/check_app_pool_health.py \
        --output incremental_checks/vXXX/run_YYYY_health --display-number 610
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from deskshot.automation.app_launcher import build_app_env, find_binary  # noqa: E402
from deskshot.config import CONFIGS_DIR, DisplayConfig, SessionConfig  # noqa: E402
from deskshot.environment.session import DesktopSession  # noqa: E402
from deskshot.generation.scene_composer import DEFAULT_SCENE_APP_POOL  # noqa: E402
from deskshot.pipeline.orchestrator import load_manifests  # noqa: E402

import gi  # noqa: E402

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402


def _atspi_names() -> List[str]:
    desktop = Atspi.get_desktop(0)
    names = []
    for i in range(desktop.get_child_count()):
        child = desktop.get_child_at_index(i)
        if child is not None:
            names.append(child.get_name() or "")
    return names


def _atspi_element_count(atspi_name: str, limit: int = 4000) -> int:
    """Rough subtree size for the app, as a density signal."""
    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        child = desktop.get_child_at_index(i)
        if child is None or atspi_name.lower() not in (child.get_name() or "").lower():
            continue
        total = 0
        stack = [child]
        while stack and total < limit:
            node = stack.pop()
            total += 1
            try:
                for j in range(node.get_child_count()):
                    grandchild = node.get_child_at_index(j)
                    if grandchild is not None:
                        stack.append(grandchild)
            except Exception:
                continue
        return total
    return 0


def check_app(manifest, *, settle: float, timeout: float) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "app": manifest.app_name,
        "binary": manifest.binary,
        "status": "fail",
        "registered": False,
        "window": False,
        "elements": 0,
        "launch_sec": None,
        "message": "",
    }
    try:
        binary = find_binary(manifest.binary)
    except Exception as exc:
        row["message"] = f"binary not found: {exc}"
        return row

    cmd = [binary] + [str(a) for a in (manifest.args or [])]
    env = build_app_env(binary, manifest)
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd, env=env, cwd=manifest.cwd or None,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        row["message"] = f"launch failed: {exc}"
        return row

    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                row["message"] = f"exited with code {proc.returncode}"
                return row
            if any(
                manifest.atspi_name.lower() in (n or "").lower() for n in _atspi_names()
            ):
                row["registered"] = True
                row["launch_sec"] = round(time.monotonic() - started, 1)
                break
            time.sleep(0.5)

        if not row["registered"]:
            row["message"] = f"did not register in AT-SPI within {timeout}s"
            return row

        time.sleep(settle)
        row["elements"] = _atspi_element_count(manifest.atspi_name)
        try:
            xdotool = find_binary("xdotool")
            wins = subprocess.run(
                [xdotool, "search", "--onlyvisible", "--name", "."],
                capture_output=True, text=True, timeout=20,
            ).stdout.split()
            for wid in wins:
                name = subprocess.run(
                    [xdotool, "getwindowname", wid],
                    capture_output=True, text=True, timeout=10,
                ).stdout.strip()
                if manifest.atspi_name.lower() in name.lower() or any(
                    t.lower() in name.lower() for t in (manifest.window_titles or [])
                ):
                    row["window"] = True
                    break
        except Exception:
            pass

        row["status"] = "ok" if row["elements"] > 1 else "sparse"
        return row
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        time.sleep(1.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apps", help="Comma-separated app subset (default: scene pool)")
    parser.add_argument("--output", required=True, help="Directory for the report")
    parser.add_argument("--display-number", type=int, default=610)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--settle", type=float, default=1.5)
    args = parser.parse_args()

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    names = args.apps.split(",") if args.apps else sorted(DEFAULT_SCENE_APP_POOL)
    manifests = {m.app_name: m for m in load_manifests(CONFIGS_DIR / "apps")}
    missing = [n for n in names if n not in manifests]

    config = SessionConfig()
    config.display = DisplayConfig(display_number=args.display_number, width=1280, height=720)
    config.desktop_env = "xfce"

    rows: List[Dict[str, Any]] = []
    with DesktopSession(config):
        for name in names:
            if name not in manifests:
                continue
            row = check_app(manifests[name], settle=args.settle, timeout=args.timeout)
            rows.append(row)
            print(
                f"{row['app']:20} {row['status']:7} registered={str(row['registered']):5} "
                f"window={str(row['window']):5} elements={row['elements']:5} "
                f"launch={row['launch_sec']} {row['message']}",
                flush=True,
            )

    report = {
        "checked": len(rows),
        "ok": sum(1 for r in rows if r["status"] == "ok"),
        "sparse": sum(1 for r in rows if r["status"] == "sparse"),
        "fail": sum(1 for r in rows if r["status"] == "fail"),
        "missing_manifests": missing,
        "rows": rows,
    }
    (out / "app_health.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(
        f"\nchecked={report['checked']} ok={report['ok']} "
        f"sparse={report['sparse']} fail={report['fail']}"
    )
    if missing:
        print(f"missing manifests: {missing}")
    return 1 if report["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
