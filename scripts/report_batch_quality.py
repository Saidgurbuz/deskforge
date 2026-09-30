#!/usr/bin/env python
"""Aggregate a scene batch into quality, cost and robustness numbers.

A batch is worth running only if its output can be judged, and the judgement
that matters is not the mean - it is how the pipeline behaves as scenes get
harder. Density is the axis: more windows means more overlap, more popups, more
things still drawing, and every defect found so far got worse with it.

So everything is reported **per app count**:

*Quality*     - phantom and drift rates from `audit_annotation_pixels`, which
                compares annotations against the pixels they describe.
*Occlusion*   - how much partial and full covering each density actually
                produced, so a claim of "we cover the hard case" is measured.
*Cost*        - wall clock per scene, which is what job sizing needs.
*Robustness*  - scenes that failed, and scenes captured before they settled.

Usage:
    PYTHONPATH=src python scripts/report_batch_quality.py \
        --batch incremental_checks/vXXX --output incremental_checks/vXXX/report
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _load_metas(batch: Path) -> List[Dict[str, Any]]:
    out = []
    for path in sorted(batch.glob("*.meta.json")):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        meta["_path"] = str(path)
        out.append(meta)
    return out


def _audit_rows(batch: Path) -> Dict[str, Dict[str, Any]]:
    """Per-capture audit rows, keyed by capture filename."""
    report = batch / "audit" / "pixel_audit.json"
    if not report.is_file():
        return {}
    data = json.loads(report.read_text(encoding="utf-8"))
    return {row["capture"]: row for row in data.get("rows", [])}


def _occlusion_counts(batch: Path, stem: str) -> Dict[str, int]:
    path = batch / f"{stem}.elements.leaf.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    elements = data if isinstance(data, list) else data.get("elements", [])
    counts: Dict[str, int] = defaultdict(int)
    for elem in elements:
        counts[str(elem.get("occlusion_state") or "none")] += 1
    counts["total"] = len(elements)
    return dict(counts)


def build_report(batch: Path) -> Dict[str, Any]:
    metas = _load_metas(batch)
    audits = _audit_rows(batch)

    by_count: Dict[int, Dict[str, Any]] = defaultdict(
        lambda: {
            "scenes": 0, "elements": 0, "text": 0, "phantoms": 0, "drifted": 0,
            "occ_partial": 0, "occ_hidden": 0, "wall": [], "unsettled": 0,
        }
    )
    rows: List[Dict[str, Any]] = []

    for meta in metas:
        scene = meta.get("scene") or {}
        apps = scene.get("apps") or []
        count = len(apps)
        stem = meta.get("stem") or ""
        bucket = by_count[count]
        bucket["scenes"] += 1
        bucket["elements"] += int(meta.get("num_elements_leaf") or 0)

        timing = meta.get("scene_timing") or {}
        wall = timing.get("scene_wall_sec")
        if wall:
            bucket["wall"].append(float(wall))
        readiness = timing.get("readiness") or {}
        if readiness and not readiness.get("settled", True):
            bucket["unsettled"] += 1

        occ = _occlusion_counts(batch, stem)
        bucket["occ_partial"] += occ.get("partial", 0)
        bucket["occ_hidden"] += occ.get("hidden", 0)

        audit = audits.get(f"{stem}.png") or {}
        bucket["text"] += int(audit.get("num_text_elements") or 0)
        bucket["phantoms"] += int(audit.get("num_text_phantoms") or 0)
        bucket["drifted"] += int(audit.get("num_text_drifted") or 0)

        rows.append({
            "stem": stem, "num_apps": count,
            "layout": scene.get("layout"), "apps": [a.get("app_name") for a in apps],
            "elements": meta.get("num_elements_leaf"),
            "wall_sec": wall, "settled": readiness.get("settled"),
            "phantoms": audit.get("num_text_phantoms"),
            "text": audit.get("num_text_elements"),
            "occlusion": occ,
        })

    summary = {}
    for count, bucket in sorted(by_count.items()):
        text = max(1, bucket["text"])
        elements = max(1, bucket["elements"])
        summary[count] = {
            "scenes": bucket["scenes"],
            "mean_elements": round(bucket["elements"] / bucket["scenes"], 1),
            "phantom_rate": round(bucket["phantoms"] / text, 5),
            "drift_rate": round(bucket["drifted"] / text, 5),
            "phantoms": bucket["phantoms"],
            "text_elements": bucket["text"],
            "partial_occluded_share": round(bucket["occ_partial"] / elements, 4),
            "hidden_share": round(bucket["occ_hidden"] / elements, 4),
            "median_wall_sec": round(statistics.median(bucket["wall"]), 1) if bucket["wall"] else None,
            "unsettled_scenes": bucket["unsettled"],
        }
    return {"per_app_count": summary, "num_scenes": len(metas), "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--batch", required=True)
    ap.add_argument("--output", default=None)
    args = ap.parse_args()

    batch = Path(args.batch)
    report = build_report(batch)

    print(f"scenes captured: {report['num_scenes']}\n")
    header = (f"{'apps':>5}{'scenes':>8}{'elems':>8}{'text':>7}"
              f"{'phantom%':>10}{'drift%':>9}{'partial%':>10}{'hidden%':>9}"
              f"{'sec':>7}{'unsettled':>11}")
    print(header)
    print("-" * len(header))
    for count, row in report["per_app_count"].items():
        print(f"{count:>5}{row['scenes']:>8}{row['mean_elements']:>8.0f}"
              f"{row['text_elements']:>7}{row['phantom_rate'] * 100:>10.2f}"
              f"{row['drift_rate'] * 100:>9.2f}"
              f"{row['partial_occluded_share'] * 100:>10.2f}"
              f"{row['hidden_share'] * 100:>9.2f}"
              f"{(row['median_wall_sec'] or 0):>7.0f}{row['unsettled_scenes']:>11}")

    total_text = sum(r["text_elements"] for r in report["per_app_count"].values())
    total_ph = sum(r["phantoms"] for r in report["per_app_count"].values())
    print(f"\noverall phantom rate: {total_ph}/{total_text} "
          f"({total_ph / max(1, total_text):.2%})")

    if args.output:
        out = Path(args.output)
        out.mkdir(parents=True, exist_ok=True)
        (out / "batch_quality.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
