#!/usr/bin/env python
"""How many of an episode's frames are genuinely different screens?

A long episode is worth its wall clock twice over if every frame is also a usable
parsing sample. That only holds if the frames differ - and repetition is exactly
how the episode planner reaches a long horizon, so it is the assumption most
likely to be false.

Frames are compared on what a parser would have to produce for them, not on
pixels: the set of `(role, text, quantised position)` signatures. Position is
quantised to an 8px grid so a one-pixel redraw is not counted as a new screen,
and text is normalised the same way predicates normalise it.

Three numbers, because they answer different questions:

*Exact*      - identical ScreenTag. Two frames that serialise the same string are
               the same training target, whatever else differs.
*Near*       - element signatures overlapping above `--threshold`. A frame that
               differs only by which menu row is highlighted is not a second
               parsing problem.
*Novel work* - how much of each frame is content no earlier frame contained. A
               dataset of 200 frames that revisit 18 screens has the parsing
               value of 18 frames, not 200.

Usage:
    PYTHONPATH=src python scripts/measure_frame_diversity.py \
        --episode incremental_checks/v185_long_horizon/task-* --output <out>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

#: Position quantisation, in pixels. Coarse enough to absorb redraw jitter,
#: fine enough that a widget moving a visible distance reads as a change.
GRID = 8


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("_", "")).strip().lower()


def _text(elem: Dict[str, Any]) -> str:
    for key in ("visible_text", "inner_text", "name"):
        val = elem.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def signature(elements: Sequence[Dict[str, Any]]) -> Set[Tuple]:
    """What a parser would have to emit for this frame, as a comparable set."""
    out: Set[Tuple] = set()
    for elem in elements:
        rect = elem.get("rect") or {}
        out.add((
            str(elem.get("role") or "").strip().lower(),
            _norm(_text(elem)),
            int(rect.get("x", 0)) // GRID,
            int(rect.get("y", 0)) // GRID,
        ))
    return out


def jaccard(a: Set[Tuple], b: Set[Tuple]) -> float:
    if not a and not b:
        return 1.0
    union = len(a | b)
    return len(a & b) / union if union else 1.0


def load_frames(episode: Path) -> List[Dict[str, Any]]:
    task = json.loads((episode / "task.json").read_text(encoding="utf-8"))
    frames: List[Dict[str, Any]] = []
    for step in task["steps"]:
        stem = step.get("observation_stem")
        if not stem:
            continue
        leaf = episode / f"{stem}.elements.leaf.json"
        tag = episode / f"{stem}.screentag.txt"
        if not leaf.is_file():
            continue
        data = json.loads(leaf.read_text(encoding="utf-8"))
        elements = data if isinstance(data, list) else data.get("elements", [])
        frames.append({
            "step_index": step["step_index"],
            "intent": step["intent"],
            "stem": stem,
            "elements": elements,
            "screentag": tag.read_text(encoding="utf-8") if tag.is_file() else "",
            "signature": signature(elements),
        })
    return frames


def cluster(frames: List[Dict[str, Any]], threshold: float) -> List[int]:
    """Assign each frame to the first cluster it is near-identical to.

    Greedy and order-dependent by design: it answers "how much did this episode
    add as it went", which is the question a collection run cares about.
    """
    reps: List[Set[Tuple]] = []
    labels: List[int] = []
    for frame in frames:
        for i, rep in enumerate(reps):
            if jaccard(frame["signature"], rep) >= threshold:
                labels.append(i)
                break
        else:
            reps.append(frame["signature"])
            labels.append(len(reps) - 1)
    return labels


def analyse(frames: List[Dict[str, Any]], threshold: float) -> Dict[str, Any]:
    n = len(frames)
    exact = len({f["screentag"] for f in frames})
    labels = cluster(frames, threshold)
    distinct = len(set(labels))

    seen: Set[Tuple] = set()
    novel_fractions: List[float] = []
    for frame in frames:
        sig = frame["signature"]
        novel = len(sig - seen)
        novel_fractions.append(novel / max(1, len(sig)))
        seen |= sig

    consecutive = [
        jaccard(frames[i]["signature"], frames[i + 1]["signature"])
        for i in range(n - 1)
    ]

    members: Dict[int, List[int]] = {}
    for frame, label in zip(frames, labels):
        members.setdefault(label, []).append(frame["step_index"])

    return {
        "num_frames": n,
        "distinct_screentags": exact,
        "distinct_screens": distinct,
        "distinct_screen_rate": round(distinct / max(1, n), 4),
        "threshold": threshold,
        "mean_consecutive_similarity": round(sum(consecutive) / max(1, len(consecutive)), 4),
        "mean_novel_element_fraction": round(sum(novel_fractions) / max(1, n), 4),
        "novel_fraction_after_first_quarter": round(
            sum(novel_fractions[n // 4:]) / max(1, n - n // 4), 4
        ),
        "total_distinct_elements": len(seen),
        "clusters": [
            {
                "size": len(steps),
                "steps": steps[:12],
                "intent": frames[steps[0]]["intent"],
            }
            for _, steps in sorted(members.items(), key=lambda kv: -len(kv[1]))
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--episode", required=True, nargs="+", help="Episode directories")
    ap.add_argument("--threshold", type=float, default=0.95)
    ap.add_argument("--output", default=None)
    args = ap.parse_args()

    reports = []
    for pattern in args.episode:
        for path in sorted(Path().glob(pattern)) or [Path(pattern)]:
            if not (path / "task.json").is_file():
                continue
            frames = load_frames(path)
            if not frames:
                continue
            report = analyse(frames, args.threshold)
            report["episode"] = str(path)
            reports.append(report)

            print(f"\n=== {path}")
            print(f"  frames                    {report['num_frames']}")
            print(f"  distinct ScreenTags       {report['distinct_screentags']}")
            print(f"  distinct screens (>={args.threshold})  {report['distinct_screens']}"
                  f"  ({report['distinct_screen_rate']:.1%})")
            print(f"  mean consecutive overlap  {report['mean_consecutive_similarity']:.3f}")
            print(f"  mean novel elements/frame {report['mean_novel_element_fraction']:.3f}")
            print(f"  novel after first quarter {report['novel_fraction_after_first_quarter']:.3f}")
            print("  largest repeated screens:")
            for c in report["clusters"][:6]:
                if c["size"] < 2:
                    continue
                print(f"    x{c['size']:<3} {c['intent'][:56]}")

    if args.output:
        out = Path(args.output)
        out.mkdir(parents=True, exist_ok=True)
        (out / "frame_diversity.json").write_text(
            json.dumps(reports, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
