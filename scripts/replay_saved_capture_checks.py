#!/usr/bin/env python3
"""Replay quality checks over saved capture artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from deskshot.postprocessing.quality import build_stage_survival_summary, run_quality_checks


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _iter_meta_paths(inputs: Iterable[str]) -> List[Path]:
    meta_paths: List[Path] = []
    for raw in inputs:
        path = Path(raw)
        if path.is_dir():
            meta_paths.extend(sorted(path.glob("*.meta.json")))
            continue
        if path.name.endswith(".meta.json"):
            meta_paths.append(path)
            continue
        raise ValueError(f"Unsupported input: {path}. Use a capture directory or *.meta.json file.")
    return sorted(set(meta_paths))


def replay_capture(meta_path: Path) -> Dict[str, Any]:
    stem = meta_path.name.removesuffix(".meta.json")
    base_dir = meta_path.parent
    meta = _load_json(meta_path)
    unfiltered = _load_json(base_dir / f"{stem}.elements.unfiltered.json")
    filtered = _load_json(base_dir / f"{stem}.elements.json")
    leaf = _load_json(base_dir / f"{stem}.elements.leaf.json")
    viewport = meta.get("viewport", {})
    checks = run_quality_checks(
        filtered,
        leaf_elements=leaf,
        viewport_w=int(viewport.get("width", 0) or 0),
        viewport_h=int(viewport.get("height", 0) or 0),
    )
    return {
        "stem": stem,
        "app_name": meta.get("app_name", ""),
        "quality_ok": all(ok for _name, ok, _msg in checks),
        "quality_checks": [
            {"name": name, "ok": ok, "message": message}
            for name, ok, message in checks
        ],
        "stage_survival": build_stage_survival_summary(unfiltered, filtered, leaf),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay quality checks on saved capture artifacts.")
    parser.add_argument("inputs", nargs="+", help="Capture directory and/or *.meta.json files")
    parser.add_argument("--output", "-o", help="Optional summary JSON output path")
    args = parser.parse_args()

    meta_paths = _iter_meta_paths(args.inputs)
    if not meta_paths:
        raise SystemExit("No capture metadata found.")

    summaries = [replay_capture(path) for path in meta_paths]
    payload = {
        "num_captures": len(summaries),
        "num_quality_ok": sum(1 for row in summaries if row["quality_ok"]),
        "captures": summaries,
    }
    text = json.dumps(payload, indent=2)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if all(row["quality_ok"] for row in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
