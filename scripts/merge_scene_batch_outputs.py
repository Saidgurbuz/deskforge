#!/usr/bin/env python3
"""Merge per-job LSF scene-batch outputs into one top-level index."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _artifact_paths(job_dir: Path, stem: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for path in sorted(job_dir.glob(f"{stem}*")):
        if path.is_file():
            out[path.name] = str(path.resolve())
    return out


def build_index(output_root: Path) -> Dict[str, Any]:
    jobs_root = output_root / "jobs"
    done_dir = output_root / ".lsbatch" / "parts_done"
    failed_dir = output_root / ".lsbatch" / "parts_failed"

    accepted_rows: List[Dict[str, Any]] = []
    job_status: List[Dict[str, Any]] = []

    for job_dir in sorted(jobs_root.glob("part_*")):
        part_name = job_dir.name
        part = int(part_name.split("_")[-1])
        results_path = job_dir / "scene_batch_results.json"
        summary_path = job_dir / "summary.md"
        done_marker = done_dir / f"{part_name}.done"
        failed_marker = failed_dir / f"{part_name}.failed"

        rows: List[Dict[str, Any]] = []
        if results_path.is_file():
            rows = _load_json(results_path)

        accepted = 0
        completed = 0
        for item in rows:
            scene = item.get("scene") or {}
            result = item.get("result") or {}
            if result:
                completed += 1
            audit = result.get("scene_batch_audit") or {}
            if not audit.get("accepted"):
                continue
            stem = str(result.get("stem") or "")
            accepted += 1
            accepted_rows.append(
                {
                    "part": part,
                    "job_dir": str(job_dir.resolve()),
                    "scene": scene,
                    "result": result,
                    "artifacts": _artifact_paths(job_dir, stem) if stem else {},
                }
            )

        job_status.append(
            {
                "part": part,
                "job_dir": str(job_dir.resolve()),
                "has_results": results_path.is_file(),
                "has_summary": summary_path.is_file(),
                "done": done_marker.is_file(),
                "failed": failed_marker.is_file(),
                "completed_captures": completed,
                "accepted_captures": accepted,
            }
        )

    return {
        "output_root": str(output_root.resolve()),
        "num_jobs": len(job_status),
        "num_done_jobs": sum(1 for row in job_status if row["done"]),
        "num_failed_jobs": sum(1 for row in job_status if row["failed"]),
        "num_accepted_captures": len(accepted_rows),
        "accepted_rows": accepted_rows,
        "job_status": job_status,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", required=True, help="Top-level LSF batch output root")
    args = parser.parse_args()

    output_root = Path(args.output_root).expanduser().resolve()
    merged_dir = output_root / "merged"
    merged_dir.mkdir(parents=True, exist_ok=True)

    index = build_index(output_root)
    accepted_rows = index.pop("accepted_rows")
    job_status = index.pop("job_status")

    (merged_dir / "accepted_manifest.json").write_text(
        json.dumps(accepted_rows, indent=2),
        encoding="utf-8",
    )
    with (merged_dir / "accepted_manifest.jsonl").open("w", encoding="utf-8") as handle:
        for row in accepted_rows:
            handle.write(json.dumps(row) + "\n")

    (merged_dir / "job_status.json").write_text(
        json.dumps(job_status, indent=2),
        encoding="utf-8",
    )
    (merged_dir / "summary.json").write_text(
        json.dumps(index, indent=2),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
