#!/usr/bin/env python3
"""Run deterministic PDF-reader probes on a chosen display number."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from deskshot.config import CONFIGS_DIR, PipelineConfig
from deskshot.environment.session import DesktopSession
from deskshot.extraction.run_extraction import run_chain_extraction, run_single_extraction
from deskshot.pipeline.orchestrator import load_manifests


DEFAULT_APPS = ["firefox-pdf", "chromium-pdf", "evince-pdf"]
EXPECTED_STRINGS = [
    "Quarterly Systems Reliability Report",
    "Table 1. Release Validation Metrics",
    "Figure 1. Service topology sketch",
    "Appendix A. Deployment Notes",
]


def _pick_probe(manifest):
    for chain in manifest.chains:
        if chain.name == "pdf_progressive":
            return ("chain", chain)
    for interaction in manifest.interactions:
        if interaction.name == "default":
            return ("interaction", interaction)
    if manifest.interactions:
        return ("interaction", manifest.interactions[0])
    raise ValueError(f"No interaction found for {manifest.app_name}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apps", default=",".join(DEFAULT_APPS))
    parser.add_argument("--display-number", type=int, default=240)
    parser.add_argument("--output", required=True)
    parser.add_argument("--include-chrome", action="store_true")
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    config_path = CONFIGS_DIR / "pipeline.yaml"
    config = PipelineConfig.from_yaml(config_path) if config_path.exists() else PipelineConfig()
    config.session.display.display_number = args.display_number
    config.output_dir = output_dir

    manifests = {m.app_name: m for m in load_manifests(config.apps_config_dir)}
    selected = []
    for name in [part.strip() for part in args.apps.split(",") if part.strip()]:
        manifest = manifests.get(name)
        if manifest is None:
            raise SystemExit(f"Unknown manifest: {name}")
        selected.append(manifest)

    rows = []
    with DesktopSession(config.session):
        for manifest in selected:
            probe_type, probe = _pick_probe(manifest)
            captures = []
            if probe_type == "chain":
                captures = run_chain_extraction(
                    manifest,
                    probe,
                    config,
                    output_dir=output_dir,
                    include_desktop_chrome=args.include_chrome,
                    manifest_lookup=manifests,
                )
                result = captures[-1] if captures else None
            else:
                result = run_single_extraction(
                    manifest,
                    probe,
                    config,
                    output_dir=output_dir,
                    include_desktop_chrome=args.include_chrome,
                    manifest_lookup=manifests,
                )
                captures = [result] if result is not None else []

            if result is None:
                rows.append({"app_name": manifest.app_name, "status": "launch_failed"})
                continue

            texts = set()
            leaf_texts = set()
            screentag_text = ""
            capture_rows = []
            for capture in captures:
                if capture is None:
                    continue
                elements = json.loads(Path(capture["elements"]).read_text(encoding="utf-8"))
                leaf_elements = json.loads(Path(capture["elements_leaf"]).read_text(encoding="utf-8"))
                screentag = Path(capture["screentag"]).read_text(encoding="utf-8")
                texts.update((e.get("inner_text") or "").strip() for e in elements if (e.get("inner_text") or "").strip())
                leaf_texts.update((e.get("inner_text") or "").strip() for e in leaf_elements if (e.get("inner_text") or "").strip())
                screentag_text += "\n" + screentag
                capture_rows.append(
                    {
                        "stem": capture["stem"],
                        "num_elements": capture["num_elements"],
                        "num_leaf_elements": len(leaf_elements),
                    }
                )
            rows.append(
                {
                    "app_name": manifest.app_name,
                    "status": "ok",
                    "result": result,
                    "captures": capture_rows,
                    "num_elements": max((row["num_elements"] for row in capture_rows), default=0),
                    "num_leaf_elements": max((row["num_leaf_elements"] for row in capture_rows), default=0),
                    "expected_strings": {
                        needle: {
                            "filtered": needle in texts,
                            "leaf": needle in leaf_texts,
                            "screentag": needle in screentag_text,
                        }
                        for needle in EXPECTED_STRINGS
                    },
                }
            )

    (output_dir / "summary.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    for row in rows:
        print(json.dumps(row, indent=2))


if __name__ == "__main__":
    main()
