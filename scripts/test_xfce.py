#!/usr/bin/env python3
"""Integration test: XFCE desktop session with desktop chrome extraction.

Usage:
    python scripts/test_xfce.py [--display 96]

Steps:
1. Start XFCE session (display :96)
2. Run health checks (all 6)
3. Take screenshot → tmp/xfce_samples/xfce_desktop.png
4. Extract desktop chrome elements, print summary
5. Launch gnome-calculator, extract with chrome, save visualization
6. Clean up
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

# Ensure project root is importable
project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root / "src"))

from deskshot.config import SessionConfig, DisplayConfig, ThemeConfig, PipelineConfig, AppManifest, InteractionSequence

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_xfce")


def main() -> int:
    parser = argparse.ArgumentParser(description="Test XFCE desktop session")
    parser.add_argument("--display", type=int, default=96,
                        help="Display number (default: 96)")
    args = parser.parse_args()

    # Output directory
    out_dir = project_root / "tmp" / "xfce_samples"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Configure session
    display_cfg = DisplayConfig(display_number=args.display)
    theme_cfg = ThemeConfig()  # use defaults (Adwaita)
    session_cfg = SessionConfig(
        display=display_cfg,
        theme=theme_cfg,
        desktop_env="xfce",
    )

    print("=" * 60)
    print("  XFCE Desktop Integration Test")
    print("=" * 60)

    # ── Step 1: Start XFCE session ──
    print(f"\n── Step 1: Start XFCE session on :{args.display} ──")
    from deskshot.environment.session import DesktopSession

    try:
        session = DesktopSession(session_cfg)
        session.__enter__()
    except Exception as e:
        print(f"FAIL: Could not start session: {e}")
        return 1

    print(f"Session started on display {session.display}")
    print(f"Running processes: {len(session._procs)}")

    try:
        # ── Step 2: Health checks ──
        print("\n── Step 2: Health checks ──")
        from deskshot.environment.health import run_all_checks, print_health_report

        checks = run_all_checks(
            desktop_env="xfce",
            tracked_pids=session.tracked_pids,
        )
        all_ok = print_health_report(checks)
        if not all_ok:
            print("\nWARN: Some health checks failed (continuing anyway)")

        # ── Step 3: Take screenshot ──
        print("\n── Step 3: Capture XFCE desktop screenshot ──")
        from deskshot.extraction.screenshot import capture_screenshot

        screenshot_path = out_dir / "xfce_desktop.png"
        img = capture_screenshot(output_path=screenshot_path)
        print(f"Screenshot saved: {screenshot_path} ({img.size[0]}x{img.size[1]})")

        # ── Step 4: Extract desktop chrome ──
        print("\n── Step 4: Extract desktop chrome elements ──")
        from deskshot.extraction.atspi_walker import walk_desktop_chrome

        chrome_elems = walk_desktop_chrome(
            viewport_w=display_cfg.width,
            viewport_h=display_cfg.height,
        )
        print(f"Desktop chrome elements: {len(chrome_elems)}")
        if chrome_elems:
            # Print type distribution
            type_counts: dict[str, int] = {}
            for e in chrome_elems:
                t = e.get("type", "unknown")
                type_counts[t] = type_counts.get(t, 0) + 1
            print("  Type distribution:")
            for t, c in sorted(type_counts.items(), key=lambda x: -x[1])[:10]:
                print(f"    {t}: {c}")

            # Save chrome elements
            chrome_path = out_dir / "xfce_chrome.elements.json"
            chrome_path.write_text(
                json.dumps(chrome_elems, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            print(f"  Saved: {chrome_path}")

        # ── Step 5: Launch app + extract with chrome ──
        print("\n── Step 5: Launch gnome-calculator + extract with chrome ──")
        from deskshot.automation.app_launcher import launch_app, kill_app
        from deskshot.extraction.atspi_walker import walk_application

        manifest = AppManifest(
            app_name="gnome-calculator",
            binary="gnome-calculator",
            atspi_name="gnome-calculator",
        )

        proc = None
        try:
            proc = launch_app(manifest, timeout=10.0)
            time.sleep(1.0)

            # Walk app elements
            app_elems = walk_application(
                "gnome-calculator",
                viewport_w=display_cfg.width,
                viewport_h=display_cfg.height,
            )
            print(f"App elements: {len(app_elems)}")

            # Walk chrome again (with app running)
            chrome_elems2 = walk_desktop_chrome(
                viewport_w=display_cfg.width,
                viewport_h=display_cfg.height,
            )
            print(f"Chrome elements (with app): {len(chrome_elems2)}")

            # Capture screenshot with app
            app_screenshot = out_dir / "xfce_with_calculator.png"
            img2 = capture_screenshot(output_path=app_screenshot)
            print(f"Screenshot: {app_screenshot}")

            # Merge and save
            all_elems = app_elems + chrome_elems2
            all_path = out_dir / "xfce_calculator_all.elements.json"
            all_path.write_text(
                json.dumps(all_elems, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            print(f"Total elements (app + chrome): {len(all_elems)}")

            # Source distribution
            app_count = sum(1 for e in all_elems if e.get("source") == "app")
            chrome_count = sum(1 for e in all_elems if e.get("source") == "desktop_chrome")
            print(f"  source='app': {app_count}")
            print(f"  source='desktop_chrome': {chrome_count}")

            # Basic visualization
            try:
                from PIL import Image, ImageDraw
                viz_img = img2.copy()
                draw = ImageDraw.Draw(viz_img, "RGBA")
                for elem in all_elems:
                    rect = elem.get("rect", {})
                    x, y = rect.get("x", 0), rect.get("y", 0)
                    w, h = rect.get("w", 0), rect.get("h", 0)
                    if w < 2 or h < 2:
                        continue
                    color = (0, 255, 0) if elem.get("source") == "app" else (255, 100, 0)
                    draw.rectangle([x, y, x + w, y + h], outline=color, width=2)
                viz_path = out_dir / "xfce_calculator_viz.png"
                viz_img.save(str(viz_path))
                print(f"Visualization: {viz_path}")
            except ImportError:
                print("  (PIL not available for visualization)")

        finally:
            if proc:
                kill_app(proc)

        # ── Step 6: Summary ──
        print("\n── Step 6: Summary ──")
        print(f"Output directory: {out_dir}")
        for f in sorted(out_dir.iterdir()):
            size = f.stat().st_size
            print(f"  {f.name} ({size:,} bytes)")

        print("\nAll steps completed successfully!")
        return 0

    except Exception as e:
        logger.exception(f"Test failed: {e}")
        return 1

    finally:
        # ── Clean up ──
        print("\n── Cleanup ──")
        session.__exit__(None, None, None)
        print("Session terminated.")


if __name__ == "__main__":
    sys.exit(main())
