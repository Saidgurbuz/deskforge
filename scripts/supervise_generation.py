#!/usr/bin/env python
"""Keep a corpus run alive until every shard has finished.

A run of this size does not fail all at once. It loses a shard here and there -
a walltime expires, a node is evicted, an out-of-memory killer fires, a host
goes into an advance reservation and the job pends behind it forever. Each loss
is silent: LSF reports EXIT or simply stops listing the job, and the corpus ends
up short by however many scenes that shard owned. On the previous run four
shards sat PEND for six hours behind a reservation and nobody noticed until the
whole array was inspected by hand.

This watches the array and resubmits whatever is neither finished nor alive.
That is safe to do repeatedly because every element runs `scene-batch --resume`:
a resubmitted shard skips the captures already on disk and continues from where
it stopped, so a shard can be interrupted any number of times and still
converge.

**How a shard is known to be finished:** `run_shard.sh` writes
`status/done/shard-NNNN` only on a clean exit. Its absence is the signal to
resubmit; its presence is never overwritten. Nothing here parses LSF exit codes
to decide completion, because a job can vanish from `bjobs` for reasons that
have nothing to do with whether the work is done.

    supervise_generation.py --root <root> --name dsv3        # follow an existing array
    supervise_generation.py --root <root> --name dsv3 --once # one pass, then exit

The supervisor is itself restartable: it holds no state that is not on disk.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Set

#: Give up on a shard that has been resubmitted this many times. A shard that
#: cannot get through its slice after this is a bug, not bad luck, and quietly
#: resubmitting it forever would hide that.
DEFAULT_MAX_ATTEMPTS = 6

#: LSF states that mean the work is still in the system and must not be
#: duplicated. Anything else - EXIT, ZOMBI, absent - is a candidate to resubmit.
LIVE_STATES = frozenset({"PEND", "RUN", "PROV", "WAIT", "SSUSP", "USUSP", "PSUSP"})


def _run(cmd: List[str], timeout: int = 120) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout or ""


def live_shard_indices(job_name: str) -> Set[int]:
    """Shard indices that LSF is currently holding, from the job names.

    Jobs are submitted one per shard as `<name>-<index>` rather than as an array
    element, because a resubmission of one index has to be its own job. The name
    is therefore the only link back to the shard.
    """
    text = _run(["bjobs", "-u", os.environ.get("USER", ""), "-o",
                 "job_name stat", "-noheader"])
    live: Set[int] = set()
    prefix = f"{job_name}-"
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name, state = parts[0], parts[1]
        if not name.startswith(prefix):
            continue
        if state not in LIVE_STATES:
            continue
        try:
            live.add(int(name[len(prefix):]))
        except ValueError:
            continue
    return live


def finished_shard_indices(root: Path, job_name: str = "") -> Set[int]:
    """Markers for one run.

    They live under the job name because a corpus receives more than one run and
    `status/done` shared between them meant a new run inherited the previous
    run's 300 completions. A run submitted before this change kept its markers
    directly in `status/done`, so that path is still read when the per-job
    directory does not exist.
    """
    done_dir = root / "status" / "done" / job_name if job_name else \
        root / "status" / "done"
    if job_name and not done_dir.is_dir():
        done_dir = root / "status" / "done"
    if not done_dir.is_dir():
        return set()
    out: Set[int] = set()
    for marker in done_dir.iterdir():
        name = marker.name
        if not name.startswith("shard-"):
            continue
        try:
            out.add(int(name[len("shard-"):]))
        except ValueError:
            continue
    return out


def _attempts_path(root: Path) -> Path:
    return root / "status" / "supervisor_attempts.json"


def load_attempts(root: Path) -> Dict[str, int]:
    try:
        return json.loads(_attempts_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_attempts(root: Path, attempts: Dict[str, int]) -> None:
    path = _attempts_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(attempts, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def resubmit(
    root: Path, index: int, *, job_name: str, cores: int, queue: str,
    group: str, walltime: str = "",
) -> bool:
    """Put one shard back on the cluster. No host is named on purpose.

    Pinning a job to a host with `-m` is what left four shards pending for six
    hours behind an advance reservation. Reserving enough cores that only a few
    of our jobs fit on a node bounds the per-node session count just as well -
    which is the constraint that actually matters, because
    `fs.inotify.max_user_instances` is per user per node - and it lets LSF put
    the work wherever there is room.
    """
    runner = root / "run_shard.sh"
    if not runner.is_file():
        print(f"  no runner at {runner}", file=sys.stderr)
        return False
    cmd = [
        "bsub", "-J", f"{job_name}-{index}", "-n", str(cores), "-q", queue,
        *(["-G", group] if group else []), "-R", "span[hosts=1]",
        "-o", str(root / "logs" / f"{job_name}-{index}.%J.out"),
        "-e", str(root / "logs" / f"{job_name}-{index}.%J.err"),
    ]
    if walltime:
        cmd += ["-W", walltime]
    cmd += [str(runner), str(index)]
    out = _run(cmd)
    ok = "is submitted" in out
    if not ok:
        print(f"  resubmit of shard {index} failed: {out.strip()[:160]}", file=sys.stderr)
    return ok


def free_gb(path: Path) -> float:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return -1.0
    return usage.free / 1e9


def one_pass(args: argparse.Namespace) -> Dict[str, object]:
    root: Path = args.root
    finished = finished_shard_indices(root, args.name)
    live = live_shard_indices(args.name)
    attempts = load_attempts(root)

    every = set(range(args.shards))
    stalled = sorted(every - finished - live)

    exhausted = [i for i in stalled if attempts.get(str(i), 0) >= args.max_attempts]
    resubmittable = [i for i in stalled if i not in set(exhausted)]

    submitted = 0
    for index in resubmittable[: args.max_resubmits]:
        if args.dry_run:
            print(f"  would resubmit shard {index} "
                  f"(attempt {attempts.get(str(index), 0) + 1})")
            submitted += 1
            continue
        if resubmit(root, index, job_name=args.name, cores=args.cores,
                    queue=args.queue, group=args.group, walltime=args.walltime):
            attempts[str(index)] = attempts.get(str(index), 0) + 1
            submitted += 1

    if submitted and not args.dry_run:
        save_attempts(root, attempts)

    return {
        "finished": len(finished),
        "live": len(live),
        "stalled": len(stalled),
        "resubmitted": submitted,
        "exhausted": exhausted,
        "free_gb": round(free_gb(root), 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--name", required=True, help="job-name prefix used at submission")
    ap.add_argument("--shards", type=int, default=0,
                    help="defaults to the shard count recorded in run.json")
    ap.add_argument("--cores", type=int, default=0)
    ap.add_argument("--queue", default="")
    ap.add_argument("--group", default=os.environ.get("LSB_DEFAULT_USERGROUP", ""),
                    help="LSF user group (-G); omitted when empty")
    ap.add_argument("--walltime", default="")
    ap.add_argument("--poll", type=int, default=600, help="seconds between passes")
    ap.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    ap.add_argument("--max-resubmits", type=int, default=64,
                    help="most shards to put back in one pass, so a cluster-wide "
                         "outage does not turn into a submission storm")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    manifest_path = args.root / "run.json"
    manifest = {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    args.shards = args.shards or int(manifest.get("shards") or 0)
    args.cores = args.cores or int(manifest.get("cores_per_job") or 24)
    args.queue = args.queue or str(manifest.get("queue") or "normal")
    if not args.shards:
        print("need --shards, and run.json does not record one", file=sys.stderr)
        return 2

    print(f"supervising {args.shards} shards of {args.root} as '{args.name}-N'")
    print(f"  {args.cores} cores/job, queue {args.queue}, poll {args.poll}s, "
          f"give up after {args.max_attempts} attempts")

    while True:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        state = one_pass(args)
        print(f"[{stamp}] finished {state['finished']}/{args.shards}  "
              f"live {state['live']}  stalled {state['stalled']}  "
              f"resubmitted {state['resubmitted']}  free {state['free_gb']} GB",
              flush=True)
        if state["exhausted"]:
            print(f"  GIVING UP on shards {state['exhausted']} after "
                  f"{args.max_attempts} attempts each - look at their logs",
                  flush=True)
        if state["finished"] >= args.shards:
            print("every shard has finished", flush=True)
            return 0
        if args.once:
            return 0
        time.sleep(max(60, args.poll))


if __name__ == "__main__":
    raise SystemExit(main())
