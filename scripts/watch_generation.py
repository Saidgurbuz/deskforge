#!/usr/bin/env python3
"""Watch a running corpus generation and say when it needs a human.

A multi-day run across dozens of nodes fails in ways a final report cannot
help with: a shard wedges and stops producing, a node fills its disk, an app
starts crashing after an update, or - worst - it keeps producing samples that
are quietly wrong. All of those are cheap to detect while they are happening
and expensive to discover afterwards.

So this reports two different things and keeps them apart:

**Progress**, from the job logs. Cheap: counting lines in a few dozen files,
no filesystem walk. Gives per-shard position, throughput, ETA, and which
shards have gone quiet.

**Quality**, from a bounded sample of the newest captures. The full audits over
a million samples are not something to run every ten minutes, so it takes the
most recently written captures and checks those - enough to catch a change in
behaviour within one polling interval.

Thresholds are deliberately conservative: this is meant to be believed, so it
would rather stay quiet than cry wolf. FAIL means stop and look; WARN means
look when convenient.

    PYTHONPATH=src python scripts/watch_generation.py --root <corpus root>
    PYTHONPATH=src python scripts/watch_generation.py --root <corpus root> --watch
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

#: A shard that has not started a new scene in this long has probably wedged.
#: Generous, because one dense scene with Chromium in it can legitimately take
#: several minutes and the scene timeout itself is 1200s.
STALL_SECONDS = 2400

#: Below this much free space, stop: a truncated capture is worse than none.
MIN_FREE_GB = 500.0

#: Quality bars. These are not the gate's - they are looser, because they run on
#: a handful of recent captures and a small sample is noisy. They exist to catch
#: a *change in behaviour*, not to certify quality; `qa.py gate` does that.
MAX_BLANK_RATE = 0.02
MAX_PHANTOM_RATE = 0.02
MAX_UNCOVERED_INK = 0.08
MIN_ELEMENTS_PER_CAPTURE = 40.0

_SEED_LINE = re.compile(r"^(\d\d:\d\d:\d\d).*running seed=(\d+)", re.M)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _shard_dirs(root: Path) -> List[Path]:
    shards = root / "shards"
    if not shards.is_dir():
        return [root]
    return sorted(p for p in shards.iterdir() if p.is_dir())


def _read_progress(root: Path) -> Dict[str, Any]:
    """Per-shard position, taken from the job logs rather than the filesystem."""
    # Both streams. `scene-batch` logs progress through `logging`, which writes
    # to **stderr**, so an LSF job's `.out` is empty and reading only that made
    # the watcher report zero shards while 64 were working.
    log_dir = root / "logs"
    logs = sorted(log_dir.glob("*.out")) + sorted(log_dir.glob("*.err")) if log_dir.is_dir() else []
    # Only the most recent submission. A run that was stopped and resubmitted
    # leaves the old array's logs in place, and they never move again - so they
    # were counted as shards and would have been reported as 64 stalled ones
    # within the hour. LSF names logs `<jobid>.<index>.{out,err}`; the highest
    # job id is the live array.
    job_ids = set()
    for log in logs:
        head = log.name.split(".", 1)[0]
        if head.isdigit():
            job_ids.add(int(head))
    if job_ids:
        current = str(max(job_ids))
        logs = [log for log in logs if log.name.split(".", 1)[0] == current]
    shards: List[Dict[str, Any]] = []
    for log in logs:
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Only logs a shard actually wrote. Anything else in the directory - this
        # watcher's own output, for instance - was being counted as a shard, and
        # a phantom shard that never starts a scene reads as a stall.
        if "scene-batch" not in text:
            continue
        seeds = _SEED_LINE.findall(text)
        # Either phrasing means the shard is done: a full batch reports the
        # first, a plan-driven shard that ran out of scenes reports the second.
        # Matching only the first meant every finished shard looked live, and a
        # finished run would eventually be reported as 64 stalled shards.
        finished = (
            "accepted scene captures" in text or "plan shard exhausted" in text
        )
        shards.append({
            "log": log.name,
            "scenes_started": len(seeds),
            "last_seed": seeds[-1][1] if seeds else None,
            "mtime": log.stat().st_mtime,
            "quiet_for_sec": round(time.time() - log.stat().st_mtime),
            "finished": finished,
        })
    return {"shards": shards}


def _count_samples(root: Path, *, full: bool) -> Tuple[int, Optional[Path]]:
    """Samples on disk, and the most recently written capture directory.

    `scandir` per shard rather than one `rglob` over the corpus: at a million
    samples an rglob is minutes of GPFS metadata traffic, and the number is only
    needed to two significant figures.
    """
    total = 0
    newest_dir: Optional[Path] = None
    newest_mtime = -1.0
    shards = _shard_dirs(root)
    if not full:
        # Only the recently-touched shards; this value is a hint for the report,
        # not a measurement worth 64 shards of GPFS metadata traffic.
        try:
            shards = sorted(shards, key=lambda p: -p.stat().st_mtime)[:6]
        except OSError:
            shards = shards[:6]
    for shard in shards:
        for kind in ("st", "ep"):
            base = shard / kind
            if not base.is_dir():
                continue
            try:
                entries = list(os.scandir(base))
            except OSError:
                continue
            for entry in entries:
                if not entry.is_dir():
                    continue
                if entry.stat().st_mtime > newest_mtime:
                    newest_mtime = entry.stat().st_mtime
                    newest_dir = Path(entry.path)
                if full:
                    try:
                        total += sum(
                            1 for f in os.scandir(entry.path)
                            if f.name.endswith(".meta.json")
                        )
                    except OSError:
                        pass
    return total, newest_dir


def _newest_captures(root: Path, limit: int) -> List[Path]:
    """Recently written leaf-element files, from a sample of shards.

    A **sample**, not a survey. Walking every shard's every bucket took longer
    than the polling interval once 64 shards were live - each `stat` is a GPFS
    round trip - and the quality check only needs a handful of recent captures.
    So it looks at the shards that changed most recently and stops as soon as it
    has enough.
    """
    shards = _shard_dirs(root)
    try:
        shards = sorted(shards, key=lambda p: -p.stat().st_mtime)
    except OSError:
        pass
    found: List[Tuple[float, Path]] = []
    for shard in shards[:6]:
        for kind in ("st", "ep"):
            base = shard / kind
            if not base.is_dir():
                continue
            try:
                buckets = sorted(os.scandir(base), key=lambda e: -e.stat().st_mtime)[:4]
            except OSError:
                continue
            for bucket in buckets:
                try:
                    for f in os.scandir(bucket.path):
                        if f.name.endswith(".elements.leaf.json"):
                            found.append((f.stat().st_mtime, Path(f.path)))
                except OSError:
                    continue
        if len(found) >= limit * 4:
            break
    found.sort(key=lambda pair: -pair[0])
    return [path for _mtime, path in found[:limit]]


def _check_quality(captures: List[Path]) -> Dict[str, Any]:
    """Cheap audits over a bounded sample of recent captures."""
    if not captures:
        return {"checked": 0}
    import importlib.util

    def _load(name: str, filename: str):
        spec = importlib.util.spec_from_file_location(
            name, str(Path(__file__).resolve().parent / filename)
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    blanks = _load("abw", "audit_blank_widgets.py")
    pixels = _load("aap", "audit_annotation_pixels.py")
    coverage = _load("aec", "audit_element_coverage.py")
    from deskshot.privacy import scan_text

    checked = elements = 0
    blank_checked = blank_hit = 0
    text_total = phantom_hit = 0
    ink = uncovered = 0.0
    identifying: Dict[str, int] = {}

    for leaf in captures:
        png = leaf.with_name(leaf.name.replace(".elements.leaf.json", ".png"))
        if not png.is_file():
            continue
        try:
            data = json.loads(leaf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        els = data if isinstance(data, list) else data.get("elements", [])
        checked += 1
        elements += len(els)
        try:
            b = blanks.audit_capture(png, els)
            blank_checked += b["num_checked"]
            blank_hit += b["num_blank"]
            p = pixels.audit_capture(png, els)
            text_total += p["num_text_elements"]
            phantom_hit += p["num_text_phantoms"]
            c = coverage.audit_capture(png, els)
            for win in c["windows"]:
                ink += win["ink"]
                uncovered += win["uncovered_ink"]
        except Exception as exc:  # an audit crash is itself worth reporting
            identifying.setdefault(f"audit_error:{type(exc).__name__}", 0)
            identifying[f"audit_error:{type(exc).__name__}"] += 1
        for el in els:
            for field in ("name", "inner_text", "visible_text"):
                value = el.get(field)
                if isinstance(value, str):
                    for leak in scan_text(value, include_brand=False):
                        identifying[leak.literal] = identifying.get(leak.literal, 0) + 1

    return {
        "checked": checked,
        "elements_per_capture": round(elements / max(1, checked), 1),
        "blank_rate": round(blank_hit / max(1, blank_checked), 5),
        "phantom_rate": round(phantom_hit / max(1, text_total), 5),
        "uncovered_ink": round(uncovered / max(1.0, ink), 5),
        "identifying": identifying,
    }


def _free_gb(path: Path) -> float:
    usage = shutil.disk_usage(str(path))
    return usage.free / 1e9


def _leaked_daemons() -> int:
    script = Path(__file__).resolve().parent / "cleanup_stale_sessions.py"
    try:
        out = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True, timeout=120
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return -1
    match = re.search(r"orphan session daemons:\s*(\d+)", out)
    return int(match.group(1)) if match else 0


def poll(root: Path, *, sample: int, full_count: bool) -> Dict[str, Any]:
    progress = _read_progress(root)
    total, newest_dir = _count_samples(root, full=full_count)
    quality = _check_quality(_newest_captures(root, sample))

    problems: List[str] = []
    warnings: List[str] = []

    live = [s for s in progress["shards"] if not s["finished"]]
    stalled = [s for s in live if s["quiet_for_sec"] > STALL_SECONDS]
    if stalled:
        problems.append(
            f"{len(stalled)} shard(s) silent for over {STALL_SECONDS // 60} min: "
            + ", ".join(s["log"] for s in stalled[:6])
        )

    free = _free_gb(root)
    if free < MIN_FREE_GB:
        problems.append(f"only {free:.0f} GB free (floor {MIN_FREE_GB:.0f} GB)")

    if quality.get("checked"):
        if quality["blank_rate"] > MAX_BLANK_RATE:
            problems.append(f"blank widgets {quality['blank_rate']*100:.2f}% on recent captures")
        if quality["phantom_rate"] > MAX_PHANTOM_RATE:
            problems.append(f"text phantoms {quality['phantom_rate']*100:.2f}% on recent captures")
        if quality["uncovered_ink"] > MAX_UNCOVERED_INK:
            warnings.append(f"uncovered ink {quality['uncovered_ink']*100:.2f}% on recent captures")
        if quality["elements_per_capture"] < MIN_ELEMENTS_PER_CAPTURE:
            problems.append(
                f"only {quality['elements_per_capture']} elements per capture - "
                "apps may be failing to come up"
            )
        errors = {k: v for k, v in quality["identifying"].items() if k.startswith("audit_error:")}
        if errors:
            problems.append(f"audits raised: {errors}")
    else:
        warnings.append("no recent captures found to check")

    daemons = _leaked_daemons()
    if daemons > 8:
        warnings.append(f"{daemons} orphan session daemons on this node")

    verdict = "FAIL" if problems else ("WARN" if warnings else "PASS")
    return {
        "at": _now(),
        "root": str(root),
        "verdict": verdict,
        "problems": problems,
        "warnings": warnings,
        "shards_total": len(progress["shards"]),
        "shards_running": len(live),
        "shards_finished": len(progress["shards"]) - len(live),
        "scenes_started": sum(s["scenes_started"] for s in progress["shards"]),
        "samples_on_disk": total if full_count else None,
        "free_gb": round(free, 1),
        "newest_capture_dir": str(newest_dir) if newest_dir else None,
        "quality": quality,
    }


def render(status: Dict[str, Any]) -> str:
    lines = [
        f"[{status['at']}]  {status['verdict']}",
        f"  shards      {status['shards_running']} running, "
        f"{status['shards_finished']} finished of {status['shards_total']}",
        f"  scenes      {status['scenes_started']} started"
        + (f", {status['samples_on_disk']} samples on disk"
           if status["samples_on_disk"] is not None else ""),
        f"  disk        {status['free_gb']} GB free",
    ]
    q = status["quality"]
    if q.get("checked"):
        ident = {k: v for k, v in q["identifying"].items() if not k.startswith("audit_error:")}
        lines += [
            f"  recent {q['checked']:>3}   {q['elements_per_capture']} elements/capture,"
            f" blank {q['blank_rate']*100:.2f}%,"
            f" phantom {q['phantom_rate']*100:.2f}%,"
            f" uncovered {q['uncovered_ink']*100:.2f}%",
        ]
        if ident:
            lines.append(f"  identifying {ident}")
    for problem in status["problems"]:
        lines.append(f"  FAIL  {problem}")
    for warning in status["warnings"]:
        lines.append(f"  warn  {warning}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--watch", action="store_true", help="keep polling")
    parser.add_argument("--interval", type=int, default=900, help="seconds between polls")
    parser.add_argument("--sample", type=int, default=6,
                        help="how many recent captures to audit each poll")
    parser.add_argument("--full-count", action="store_true",
                        help="count every sample on disk (slow at corpus scale)")
    args = parser.parse_args()

    if not args.root.is_dir():
        print(f"no such corpus root: {args.root}")
        return 2

    status_dir = args.root / "status"
    status_dir.mkdir(parents=True, exist_ok=True)

    while True:
        status = poll(args.root, sample=args.sample, full_count=args.full_count)
        print(render(status), flush=True)
        (status_dir / "latest.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
        with (status_dir / "history.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(status, sort_keys=True) + "\n")
        if not args.watch:
            return 1 if status["verdict"] == "FAIL" else 0
        time.sleep(max(60, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
