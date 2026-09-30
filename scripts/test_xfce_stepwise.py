#!/usr/bin/env python3
"""Stepwise MATE chrome integration test: 10 modular steps with artifacts.

Usage:
    python scripts/test_xfce_stepwise.py [--display 97]
    python scripts/test_xfce_stepwise.py --start-step 3 --stop-step 5
    python scripts/test_xfce_stepwise.py --continue-on-fail

Steps:
  0: Download + extract RPMs           (no session)
  1: Verify MATE + XFCE binaries       (no session)
  2: ldd dependency check              (no session)
  3: Start session (xfwm4 + MATE)      (session starts here)
  4: Health checks (9)                 (inside session)
  5: Desktop screenshot                (inside session)
  6: Desktop chrome AT-SPI extraction  (inside session)
  7: App + chrome extraction + viz     (inside session)
  8: ScreenTag serialization           (inside session)
  9: v2 vs v3 comparison               (after session cleanup)

Output:
  - default: incremental_checks/v3_mate_chrome/run_YYYYmmdd_HHMMSS/
  - or:      incremental_checks/v3_mate_chrome/<--run-name>/
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_xfce_stepwise")

# Silence noisy sub-loggers
for name in ("deskshot.extraction.atspi_walker", "deskshot.environment.themes",
             "deskshot.environment.xfce_config", "deskshot.environment.mate_config"):
    logging.getLogger(name).setLevel(logging.WARNING)


# ── Result tracking ──────────────────────────────────────────────────────

class StepResult:
    """Result of a single test step."""
    def __init__(self, step: int, name: str, passed: bool, message: str,
                 duration: float = 0.0, artifacts: list[str] | None = None):
        self.step = step
        self.name = name
        self.passed = passed
        self.message = message
        self.duration = duration
        self.artifacts = artifacts or []


# ── Step implementations ─────────────────────────────────────────────────

def step0_download_rpms(out_dir: Path) -> StepResult:
    """Download and extract all RPMs including XFCE + MATE + themes."""
    t0 = time.monotonic()

    from deskshot.environment.setup import download_rpm, extract_rpms, compile_schemas, patch_xvfb_xkbcomp, ALL_RPMS
    from deskshot.config import TOOLS_DIR

    rpms_dir = TOOLS_DIR / "rpms"
    extracted_dir = TOOLS_DIR / "extracted"
    rpms_dir.mkdir(parents=True, exist_ok=True)
    extracted_dir.mkdir(parents=True, exist_ok=True)

    # Download all RPMs
    failed = []
    for pkg in ALL_RPMS:
        ok = download_rpm(pkg, rpms_dir)
        if not ok:
            failed.append(pkg)

    # Extract
    extract_rpms(rpms_dir, extracted_dir)
    compile_schemas(extracted_dir)
    patch_xvfb_xkbcomp(extracted_dir)

    # List RPMs
    rpm_list = sorted(f.name for f in rpms_dir.glob("*.rpm"))
    artifact = out_dir / "step0_rpm_list.txt"
    artifact.write_text("\n".join(rpm_list) + "\n", encoding="utf-8")

    duration = time.monotonic() - t0

    if failed:
        return StepResult(0, "Download RPMs", False,
                          f"Failed to download: {', '.join(failed)}",
                          duration, [str(artifact)])

    return StepResult(0, "Download RPMs", True,
                      f"{len(rpm_list)} RPMs in tools/rpms/",
                      duration, [str(artifact)])


def step1_verify_binaries(out_dir: Path) -> StepResult:
    """Verify all required XFCE + MATE binaries are present."""
    t0 = time.monotonic()

    from deskshot.config import EXTRACTED_BIN, EXTRACTED_DIR

    required = [
        "Xvfb", "dbus-daemon", "dbus-send",
        "xfwm4", "xfconfd", "xfsettingsd",
        "mate-panel", "caja",
        "gnome-calculator",
    ]

    results: Dict[str, str] = {}
    missing = []

    for name in required:
        path = EXTRACTED_BIN / name
        if path.is_file():
            results[name] = f"OK ({path})"
        else:
            # Search libexec
            matches = list(EXTRACTED_DIR.rglob(name))
            if matches:
                results[name] = f"OK ({matches[0]})"
            else:
                results[name] = "MISSING"
                missing.append(name)

    artifact = out_dir / "step1_binary_check.txt"
    lines = [f"{name}: {status}" for name, status in results.items()]
    artifact.write_text("\n".join(lines) + "\n", encoding="utf-8")

    duration = time.monotonic() - t0

    if missing:
        return StepResult(1, "Verify binaries", False,
                          f"Missing: {', '.join(missing)}",
                          duration, [str(artifact)])

    return StepResult(1, "Verify binaries", True,
                      f"All {len(required)} binaries found",
                      duration, [str(artifact)])


def step2_ldd_check(out_dir: Path) -> StepResult:
    """Check shared library resolution for key binaries."""
    t0 = time.monotonic()

    from deskshot.config import EXTRACTED_BIN, EXTRACTED_LIB, EXTRACTED_DIR
    import os

    binaries_to_check = ["Xvfb", "xfwm4", "mate-panel", "caja"]
    all_results: Dict[str, List[str]] = {}
    any_missing = False

    # Set up LD_LIBRARY_PATH for the check
    env = dict(os.environ)
    lib_path = str(EXTRACTED_LIB)
    existing = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = f"{lib_path}:{existing}" if existing else lib_path

    for name in binaries_to_check:
        binary = EXTRACTED_BIN / name
        if not binary.is_file():
            # Try rglob
            matches = list(EXTRACTED_DIR.rglob(name))
            if matches:
                binary = matches[0]
            else:
                all_results[name] = ["BINARY NOT FOUND"]
                any_missing = True
                continue

        result = subprocess.run(
            ["ldd", str(binary)],
            capture_output=True, text=True, env=env,
        )
        missing_libs = [
            line.strip()
            for line in result.stdout.splitlines()
            if "not found" in line
        ]
        if missing_libs:
            all_results[name] = missing_libs
            any_missing = True
        else:
            all_results[name] = ["All libraries resolved"]

    artifact = out_dir / "step2_ldd_results.txt"
    lines = []
    for name, results_list in all_results.items():
        lines.append(f"=== {name} ===")
        for r in results_list:
            lines.append(f"  {r}")
    artifact.write_text("\n".join(lines) + "\n", encoding="utf-8")

    duration = time.monotonic() - t0

    if any_missing:
        return StepResult(2, "ldd check", False,
                          "Missing libraries found (see step2_ldd_results.txt)",
                          duration, [str(artifact)])

    return StepResult(2, "ldd check", True,
                      f"All libraries resolved for {len(binaries_to_check)} binaries",
                      duration, [str(artifact)])


def step3_start_session(out_dir: Path, display_num: int) -> Tuple[StepResult, Any]:
    """Start session with xfwm4 + MATE panel + Caja desktop."""
    t0 = time.monotonic()

    from deskshot.config import SessionConfig, DisplayConfig, ThemeConfig
    from deskshot.environment.session import DesktopSession

    log_dir = out_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    display_cfg = DisplayConfig(display_number=display_num)
    theme_cfg = ThemeConfig()
    session_cfg = SessionConfig(
        display=display_cfg,
        theme=theme_cfg,
        desktop_env="xfce",
    )

    session = DesktopSession(session_cfg, log_dir=log_dir)

    try:
        session.__enter__()
    except Exception as e:
        duration = time.monotonic() - t0
        # Collect any log files as artifacts
        log_files = [str(f) for f in log_dir.glob("*.log")]
        return StepResult(3, "Start MATE chrome session", False,
                          f"Session failed: {e}",
                          duration, log_files), None

    duration = time.monotonic() - t0
    n_procs = len(session._procs)
    log_files = [str(f) for f in log_dir.glob("*.log")]

    return StepResult(3, "Start MATE chrome session", True,
                      f"Session started, {n_procs} processes, display :{display_num}",
                      duration, log_files), session


def step4_health_checks(out_dir: Path, session) -> StepResult:
    """Run all health checks (3 core + 6 desktop)."""
    t0 = time.monotonic()

    from deskshot.environment.health import run_all_checks

    checks = run_all_checks(
        desktop_env="xfce",
        tracked_pids=getattr(session, "tracked_pids", None),
    )

    artifact = out_dir / "step4_health_report.txt"
    lines = []
    all_pass = True
    for name, ok, msg in checks:
        status = "PASS" if ok else "FAIL"
        lines.append(f"[{status}] {name}: {msg}")
        if not ok:
            all_pass = False

    artifact.write_text("\n".join(lines) + "\n", encoding="utf-8")
    duration = time.monotonic() - t0

    n_pass = sum(1 for _, ok, _ in checks if ok)
    n_total = len(checks)

    if not all_pass:
        return StepResult(4, "Health checks", False,
                          f"{n_pass}/{n_total} passed (see step4_health_report.txt)",
                          duration, [str(artifact)])

    return StepResult(4, "Health checks", True,
                      f"{n_pass}/{n_total} health checks passed",
                      duration, [str(artifact)])


def step5_screenshot(out_dir: Path, display_cfg) -> StepResult:
    """Capture desktop screenshot and validate."""
    t0 = time.monotonic()

    from deskshot.extraction.screenshot import capture_screenshot
    import numpy as np

    screenshot_path = out_dir / "step5_mate_desktop.png"
    try:
        img = capture_screenshot(output_path=screenshot_path)
    except Exception as e:
        return StepResult(5, "Desktop screenshot", False,
                          f"Screenshot failed: {e}",
                          time.monotonic() - t0)

    # Validate: correct size and not all-black
    w, h = img.size
    expected_w, expected_h = display_cfg.width, display_cfg.height

    arr = np.array(img)
    is_all_black = arr.max() == 0
    mean_brightness = arr.mean()

    duration = time.monotonic() - t0
    issues = []

    if w != expected_w or h != expected_h:
        issues.append(f"Size {w}x{h} != expected {expected_w}x{expected_h}")
    if is_all_black:
        issues.append("Screenshot is all-black")

    if issues:
        return StepResult(5, "Desktop screenshot", False,
                          "; ".join(issues),
                          duration, [str(screenshot_path)])

    return StepResult(5, "Desktop screenshot", True,
                      f"{w}x{h}, mean brightness={mean_brightness:.1f}",
                      duration, [str(screenshot_path)])


def step6_chrome_atspi(out_dir: Path, display_cfg) -> StepResult:
    """Extract desktop chrome elements via AT-SPI (expect dense MATE annotations)."""
    t0 = time.monotonic()

    from deskshot.extraction.atspi_walker import walk_desktop_chrome

    chrome_elems = walk_desktop_chrome(
        viewport_w=display_cfg.width,
        viewport_h=display_cfg.height,
    )

    artifact = out_dir / "step6_chrome_elements.json"
    artifact.write_text(
        json.dumps(chrome_elems, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    duration = time.monotonic() - t0

    if not chrome_elems:
        return StepResult(6, "Chrome AT-SPI", False,
                          "No desktop chrome elements found",
                          duration, [str(artifact)])

    # Type distribution summary
    type_counts: Dict[str, int] = {}
    for e in chrome_elems:
        t = e.get("type", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1

    top_types = sorted(type_counts.items(), key=lambda x: -x[1])[:5]
    types_str = ", ".join(f"{t}:{c}" for t, c in top_types)

    # Dense check: MATE should give us >>2 elements
    dense = len(chrome_elems) > 2
    msg = f"{len(chrome_elems)} elements ({types_str})"
    if not dense:
        msg += " [WARN: expected >>2 dense chrome elements from MATE]"

    return StepResult(6, "Chrome AT-SPI", True,
                      msg, duration, [str(artifact)])


def step7_app_extraction(out_dir: Path, display_cfg) -> StepResult:
    """Launch gnome-calculator, extract app + chrome, save visualization."""
    t0 = time.monotonic()

    from deskshot.config import AppManifest
    from deskshot.automation.app_launcher import launch_app, kill_app
    from deskshot.extraction.atspi_walker import walk_application, walk_desktop_chrome
    from deskshot.extraction.screenshot import capture_screenshot

    manifest = AppManifest(
        app_name="gnome-calculator",
        binary="gnome-calculator",
        atspi_name="gnome-calculator",
    )

    proc = None
    artifacts = []

    try:
        proc = launch_app(manifest, timeout=10.0)
        time.sleep(1.0)

        # Walk app elements
        app_elems = walk_application(
            "gnome-calculator",
            viewport_w=display_cfg.width,
            viewport_h=display_cfg.height,
        )

        # Walk chrome
        chrome_elems = walk_desktop_chrome(
            viewport_w=display_cfg.width,
            viewport_h=display_cfg.height,
        )

        # Re-index chrome to follow app elements
        offset = len(app_elems)
        for elem in chrome_elems:
            elem["_dom_index"] += offset
            if elem["_parent_dom_index"] is not None:
                elem["_parent_dom_index"] += offset
            elem["parent_index"] = elem["_parent_dom_index"]
            elem["_children_dom_indices"] = [c + offset for c in elem["_children_dom_indices"]]
            elem["children_indices"] = list(elem["_children_dom_indices"])

        all_elems = app_elems + chrome_elems

        # Screenshot with app
        screenshot_path = out_dir / "step7_mate_with_app.png"
        img = capture_screenshot(output_path=screenshot_path)
        artifacts.append(str(screenshot_path))

        # Save elements
        elems_path = out_dir / "step7_elements.json"
        elems_path.write_text(
            json.dumps(all_elems, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        artifacts.append(str(elems_path))

        # Visualization: green=app, orange=chrome
        try:
            from PIL import Image, ImageDraw
            viz_img = img.copy()
            draw = ImageDraw.Draw(viz_img, "RGBA")
            for elem in all_elems:
                rect = elem.get("rect", {})
                x, y = rect.get("x", 0), rect.get("y", 0)
                w, h = rect.get("w", 0), rect.get("h", 0)
                if w < 2 or h < 2:
                    continue
                color = (0, 255, 0) if elem.get("source") == "app" else (255, 100, 0)
                draw.rectangle([x, y, x + w, y + h], outline=color, width=2)
            viz_path = out_dir / "step7_viz.png"
            viz_img.save(str(viz_path))
            artifacts.append(str(viz_path))
        except ImportError:
            pass

        duration = time.monotonic() - t0

        app_count = sum(1 for e in all_elems if e.get("source") == "app")
        chrome_count = sum(1 for e in all_elems if e.get("source") == "desktop_chrome")

        if not all_elems:
            return StepResult(7, "App + chrome extraction", False,
                              "No elements extracted",
                              duration, artifacts)

        return StepResult(7, "App + chrome extraction", True,
                          f"{len(all_elems)} elements (app={app_count}, chrome={chrome_count})",
                          duration, artifacts)

    except Exception as e:
        duration = time.monotonic() - t0
        return StepResult(7, "App + chrome extraction", False,
                          f"Extraction failed: {e}",
                          duration, artifacts)

    finally:
        if proc:
            kill_app(proc)


def step8_screentag(out_dir: Path, display_cfg) -> StepResult:
    """Serialize elements to ScreenTag format."""
    t0 = time.monotonic()

    # Read elements from step 7
    elems_path = out_dir / "step7_elements.json"
    if not elems_path.exists():
        return StepResult(8, "ScreenTag serialization", False,
                          "No step7_elements.json found (step 7 must pass first)",
                          time.monotonic() - t0)

    elems = json.loads(elems_path.read_text(encoding="utf-8"))

    from deskshot.extraction.run_extraction import _elements_to_screentag

    screentag_text = _elements_to_screentag(
        elems, display_cfg.width, display_cfg.height,
    )

    artifact = out_dir / "step8_screentag.txt"
    artifact.write_text(screentag_text, encoding="utf-8")

    duration = time.monotonic() - t0

    # Validate format
    if not screentag_text.startswith("<screentag>"):
        return StepResult(8, "ScreenTag serialization", False,
                          "Output doesn't start with <screentag>",
                          duration, [str(artifact)])

    n_tags = screentag_text.count("<loc_")
    n_lines = len(screentag_text.strip().splitlines())

    return StepResult(8, "ScreenTag serialization", True,
                      f"{n_lines} lines, {n_tags} location tokens",
                      duration, [str(artifact)])


def step9_comparison(out_dir: Path) -> StepResult:
    """Compare v2 (XFCE, sparse chrome) vs v3 (MATE, dense chrome)."""
    t0 = time.monotonic()

    v2_dir = PROJECT_ROOT / "incremental_checks" / "v2_xfce_desktop"
    artifact = out_dir / "step9_comparison.txt"

    lines = ["=== v2 vs v3 Comparison ===\n"]

    # Check v2 exists
    if not v2_dir.exists():
        lines.append("v2_xfce_desktop/ not found — skipping comparison")
        artifact.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return StepResult(9, "v2 vs v3 comparison", True,
                          "No v2 baseline to compare (informational)",
                          time.monotonic() - t0, [str(artifact)])

    # Load v2 elements
    v2_path = v2_dir / "step7_elements.json"
    v2_elems = []
    if v2_path.exists():
        v2_elems = json.loads(v2_path.read_text(encoding="utf-8"))

    # Load v3 elements
    v3_path = out_dir / "step7_elements.json"
    v3_elems = []
    if v3_path.exists():
        v3_elems = json.loads(v3_path.read_text(encoding="utf-8"))

    # Comparison metrics
    v2_sources = {}
    for e in v2_elems:
        s = e.get("source", "unknown")
        v2_sources[s] = v2_sources.get(s, 0) + 1

    v3_sources = {}
    for e in v3_elems:
        s = e.get("source", "unknown")
        v3_sources[s] = v3_sources.get(s, 0) + 1

    v2_types = set(e.get("type", "unknown") for e in v2_elems)
    v3_types = set(e.get("type", "unknown") for e in v3_elems)
    new_types = v3_types - v2_types

    lines.append("v2 (XFCE panel, sparse chrome):")
    lines.append(f"  Total elements: {len(v2_elems)}")
    lines.append(f"  Sources: {v2_sources}")
    lines.append(f"  Unique types: {len(v2_types)}")
    lines.append(f"  Chrome elements: {v2_sources.get('desktop_chrome', 0)}")
    lines.append("")
    lines.append("v3 (MATE panel + Caja, dense chrome):")
    lines.append(f"  Total elements: {len(v3_elems)}")
    lines.append(f"  Sources: {v3_sources}")
    lines.append(f"  Unique types: {len(v3_types)}")
    lines.append(f"  Chrome elements: {v3_sources.get('desktop_chrome', 0)}")
    lines.append("")

    v3_chrome_count = v3_sources.get("desktop_chrome", 0)
    v2_chrome_count = v2_sources.get("desktop_chrome", 0)
    chrome_delta = v3_chrome_count - v2_chrome_count
    lines.append(f"Chrome delta: {chrome_delta:+d} elements (v2={v2_chrome_count}, v3={v3_chrome_count})")
    if new_types:
        lines.append(f"New types in v3: {sorted(new_types)}")
    else:
        lines.append("No new types in v3")

    # Screentag comparison
    v2_st_path = v2_dir / "step8_screentag.txt"
    v3_st_path = out_dir / "step8_screentag.txt"

    if v2_st_path.exists() and v3_st_path.exists():
        v2_st = v2_st_path.read_text(encoding="utf-8")
        v3_st = v3_st_path.read_text(encoding="utf-8")
        v2_lines_count = len(v2_st.strip().splitlines())
        v3_lines_count = len(v3_st.strip().splitlines())
        lines.append("")
        lines.append(f"ScreenTag: v2={v2_lines_count} lines, v3={v3_lines_count} lines")

    artifact.write_text("\n".join(lines) + "\n", encoding="utf-8")

    duration = time.monotonic() - t0
    return StepResult(9, "v2 vs v3 comparison", True,
                      f"v2={len(v2_elems)} elems, v3={len(v3_elems)} elems, "
                      f"chrome delta={chrome_delta:+d}",
                      duration, [str(artifact)])


# ── Runner ───────────────────────────────────────────────────────────────

def print_summary(results: List[StepResult]) -> bool:
    """Print final summary table. Returns True if all passed."""
    print("\n" + "=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    print(f"  {'Step':<5} {'Name':<30} {'Status':<6} {'Time':>8}  Message")
    print("-" * 70)

    all_pass = True
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        if not r.passed:
            all_pass = False
        print(f"  {r.step:<5} {r.name:<30} {status:<6} {r.duration:>7.1f}s  {r.message}")

    print("-" * 70)
    total_time = sum(r.duration for r in results)
    n_pass = sum(1 for r in results if r.passed)
    n_total = len(results)
    overall = "ALL PASS" if all_pass else "SOME FAILED"
    print(f"  {overall}: {n_pass}/{n_total} steps passed in {total_time:.1f}s")
    print("=" * 70)

    return all_pass


def _next_version_output_root(incremental_dir: Path, suffix: str) -> Path:
    """Return next versioned output root: incremental_checks/vN_<suffix>."""
    incremental_dir.mkdir(parents=True, exist_ok=True)

    max_v = 0
    pat = re.compile(r"^v(\d+)_")
    for child in incremental_dir.iterdir():
        if not child.is_dir():
            continue
        m = pat.match(child.name)
        if not m:
            continue
        try:
            max_v = max(max_v, int(m.group(1)))
        except ValueError:
            continue

    return incremental_dir / f"v{max_v + 1}_{suffix}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stepwise MATE chrome integration test (10 steps)",
    )
    parser.add_argument("--display", type=int, default=97,
                        help="Display number (default: 97)")
    parser.add_argument("--start-step", type=int, default=0,
                        help="First step to run (default: 0)")
    parser.add_argument("--stop-step", type=int, default=9,
                        help="Last step to run (default: 9)")
    parser.add_argument("--continue-on-fail", action="store_true",
                        help="Keep running after a step fails")
    parser.add_argument(
        "--output-root",
        help="Root directory for run subfolders (default: auto next vN_mate_chrome)",
    )
    parser.add_argument(
        "--run-name",
        help="Optional run subfolder name (default: run_<UTC timestamp>)",
    )
    args = parser.parse_args()

    if args.output_root:
        output_root = Path(args.output_root)
    else:
        output_root = _next_version_output_root(
            PROJECT_ROOT / "incremental_checks",
            "mate_chrome",
        )
    run_name = args.run_name or f"run_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    out_dir = output_root / run_name
    out_dir.mkdir(parents=True, exist_ok=True)

    from deskshot.config import DisplayConfig
    display_cfg = DisplayConfig(display_number=args.display)

    print("=" * 70)
    print("  MATE Chrome Stepwise Integration Test (v3)")
    print(f"  Display: :{args.display}  Steps: {args.start_step}-{args.stop_step}")
    print(f"  Output root: {output_root}")
    print(f"  Run dir:     {out_dir}")
    print("=" * 70)

    results: List[StepResult] = []
    session = None

    def run_step(step: int, func, *func_args) -> bool:
        """Run a step, append result, return whether to continue."""
        if step < args.start_step or step > args.stop_step:
            return True

        print(f"\n── Step {step}: {func.__doc__.strip().split(chr(10))[0]} ──")
        result = func(*func_args)

        # Handle step3 returning (result, session) tuple
        nonlocal session
        if isinstance(result, tuple):
            result, session = result

        results.append(result)
        status = "PASS" if result.passed else "FAIL"
        print(f"  [{status}] {result.message} ({result.duration:.1f}s)")

        if result.artifacts:
            for a in result.artifacts:
                print(f"    -> {Path(a).name}")

        if not result.passed and not args.continue_on_fail:
            print(f"\n  Step {step} failed. Use --continue-on-fail to keep going.")
            return False
        return True

    try:
        # Steps 0-2: No session needed
        if not run_step(0, step0_download_rpms, out_dir):
            return print_summary(results) or 1
        if not run_step(1, step1_verify_binaries, out_dir):
            return print_summary(results) or 1
        if not run_step(2, step2_ldd_check, out_dir):
            return print_summary(results) or 1

        # Step 3: Start session (needed for steps 4-8)
        if 3 >= args.start_step and 3 <= args.stop_step:
            print(f"\n── Step 3: Start session (xfwm4 + MATE panel + Caja) on :{args.display} ──")
            result, session = step3_start_session(out_dir, args.display)
            results.append(result)
            status = "PASS" if result.passed else "FAIL"
            print(f"  [{status}] {result.message} ({result.duration:.1f}s)")
            if result.artifacts:
                for a in result.artifacts:
                    print(f"    -> {Path(a).name}")
            if not result.passed and not args.continue_on_fail:
                print(f"\n  Step 3 failed. Use --continue-on-fail to keep going.")
                print_summary(results)
                return 1
        elif args.start_step > 3 and args.stop_step >= 4 and args.start_step <= 8:
            # Need to start session for steps 4-8 even if step 3 is skipped
            print(f"\n── (Starting session for steps {args.start_step}-{min(args.stop_step, 8)}) ──")
            result, session = step3_start_session(out_dir, args.display)
            if not result.passed:
                print(f"  Session startup failed: {result.message}")
                if not args.continue_on_fail:
                    results.append(result)
                    print_summary(results)
                    return 1

        # Steps 4-8: Inside session
        if not run_step(4, step4_health_checks, out_dir, session):
            print_summary(results)
            return 1
        if not run_step(5, step5_screenshot, out_dir, display_cfg):
            print_summary(results)
            return 1
        if not run_step(6, step6_chrome_atspi, out_dir, display_cfg):
            print_summary(results)
            return 1
        if not run_step(7, step7_app_extraction, out_dir, display_cfg):
            print_summary(results)
            return 1
        if not run_step(8, step8_screentag, out_dir, display_cfg):
            print_summary(results)
            return 1

    finally:
        # Clean up session before step 9
        if session is not None:
            print("\n── Cleaning up session ──")
            session.__exit__(None, None, None)
            print("  Session terminated.")
            session = None

    # Step 9: After session cleanup
    if not run_step(9, step9_comparison, out_dir):
        print_summary(results)
        return 1

    # Final summary
    all_pass = print_summary(results)

    if all_pass:
        print(f"\nAll artifacts saved to: {out_dir}/")

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
