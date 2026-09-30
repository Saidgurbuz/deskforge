#!/usr/bin/env python
"""Test whether the same seed really reproduces the same scene.

The project claims any sample can be regenerated exactly from its seed. That has
never been tested, and several things now make it doubtful: app launch times
span 0.5s to 72s across the pool, interaction sequences are timing-dependent,
and a session is held across observations. If the claim is false, it is far
better to find that here than in review.

Runs each seed twice and compares the outputs at three levels, because they fail
for different reasons and the distinction matters:

*Plan*   - the composed SceneConfig. Pure function of the seed; any difference
           here is a bug in composition, not a timing artefact.
*Structure* - element count, roles, and the ScreenTag skeleton with text and
           coordinates removed. Survives small layout jitter.
*Exact*  - the full ScreenTag string, which is what a training target actually is.

Reporting all three separates "the pipeline is nondeterministic" from "rendering
jitters by a pixel", which need different answers.

Usage:
    PYTHONPATH=src python scripts/check_scene_determinism.py \
        --seeds 3 --start-seed 990000 --output incremental_checks/vXXX/determinism
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

_LOC = re.compile(r"<loc_\d+>")
_TEXT_BETWEEN_TAGS = re.compile(r">([^<]*)<")


def screentag_skeleton(tag: str) -> str:
    """ScreenTag with coordinates and text stripped, leaving structure only."""
    without_loc = _LOC.sub("<loc>", tag)
    return _TEXT_BETWEEN_TAGS.sub("><", without_loc)


def _load(run: Path) -> Optional[Dict[str, Any]]:
    metas = sorted(run.glob("*.meta.json"))
    if not metas:
        return None
    meta = json.loads(metas[-1].read_text(encoding="utf-8"))
    stem = meta.get("stem")
    leaf = run / f"{stem}.elements.leaf.json"
    tag = run / f"{stem}.screentag.txt"
    elements = []
    if leaf.is_file():
        data = json.loads(leaf.read_text(encoding="utf-8"))
        elements = data if isinstance(data, list) else data.get("elements", [])
    return {
        "meta": meta,
        "elements": elements,
        "screentag": tag.read_text(encoding="utf-8") if tag.is_file() else "",
    }


def _roles(elements: List[Dict[str, Any]]) -> Counter:
    return Counter(str(e.get("role")) for e in elements)


def compare(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    ma, mb = a["meta"], b["meta"]
    plan_a = json.dumps(ma.get("scene"), sort_keys=True)
    plan_b = json.dumps(mb.get("scene"), sort_keys=True)

    roles_a, roles_b = _roles(a["elements"]), _roles(b["elements"])
    skel_a = screentag_skeleton(a["screentag"])
    skel_b = screentag_skeleton(b["screentag"])

    role_diff = {
        role: [roles_a.get(role, 0), roles_b.get(role, 0)]
        for role in set(roles_a) | set(roles_b)
        if roles_a.get(role, 0) != roles_b.get(role, 0)
    }
    return {
        "plan_identical": plan_a == plan_b,
        "counts": {
            "elements": [len(a["elements"]), len(b["elements"])],
            "filtered": [ma.get("num_elements_filtered"), mb.get("num_elements_filtered")],
            "leaf": [ma.get("num_elements_leaf"), mb.get("num_elements_leaf")],
            "chrome": [ma.get("num_chrome_elements"), mb.get("num_chrome_elements")],
        },
        "element_count_identical": len(a["elements"]) == len(b["elements"]),
        "roles_identical": roles_a == roles_b,
        "role_diff": role_diff,
        "structure_identical": skel_a == skel_b,
        "screentag_identical": a["screentag"] == b["screentag"],
        "screentag_len": [len(a["screentag"]), len(b["screentag"])],
    }


def run_scene(seed: int, out: Path, display: int, apps: Optional[str]) -> int:
    cmd = [
        sys.executable, "-m", "deskshot.cli", "scene",
        "--seed", str(seed), "--output", str(out),
        "--display-number", str(display), "--include-chrome",
    ]
    if apps:
        cmd += ["--apps", apps]
    log = out / "scene.log"
    out.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as fh:
        return subprocess.run(cmd, cwd=str(PROJECT_ROOT), stdout=fh,
                              stderr=subprocess.STDOUT, timeout=2400).returncode


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--start-seed", type=int, default=990000)
    ap.add_argument("--output", required=True)
    ap.add_argument("--display", type=int, default=700)
    ap.add_argument("--apps", default=None)
    args = ap.parse_args()

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    results: List[Dict[str, Any]] = []

    for i in range(args.seeds):
        seed = args.start_seed + i
        pair: List[Optional[Dict[str, Any]]] = []
        for rep in (1, 2):
            run_dir = out / f"seed{seed}_run{rep}"
            rc = run_scene(seed, run_dir, args.display + i * 4 + rep, args.apps)
            loaded = _load(run_dir) if rc == 0 else None
            pair.append(loaded)
            print(f"  seed={seed} run{rep} rc={rc} "
                  f"{'ok' if loaded else 'NO OUTPUT'}", flush=True)

        if not (pair[0] and pair[1]):
            results.append({"seed": seed, "error": "one or both runs produced nothing"})
            continue
        cmp = compare(pair[0], pair[1])
        cmp["seed"] = seed
        results.append(cmp)
        print(
            f"    plan={cmp['plan_identical']} counts={cmp['counts']['leaf']} "
            f"roles={cmp['roles_identical']} structure={cmp['structure_identical']} "
            f"exact={cmp['screentag_identical']}",
            flush=True,
        )

    ok = [r for r in results if "error" not in r]
    summary = {
        "seeds": len(results),
        "compared": len(ok),
        "plan_identical": sum(1 for r in ok if r["plan_identical"]),
        "element_count_identical": sum(1 for r in ok if r["element_count_identical"]),
        "roles_identical": sum(1 for r in ok if r["roles_identical"]),
        "structure_identical": sum(1 for r in ok if r["structure_identical"]),
        "screentag_identical": sum(1 for r in ok if r["screentag_identical"]),
        "results": results,
    }
    (out / "determinism.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(
        f"\ncompared={summary['compared']}/{summary['seeds']}  "
        f"plan={summary['plan_identical']}  counts={summary['element_count_identical']}  "
        f"roles={summary['roles_identical']}  structure={summary['structure_identical']}  "
        f"exact={summary['screentag_identical']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
