#!/usr/bin/env python
"""Hand out X display numbers so two runs never collide on one.

Display numbers are passed by hand (`--display-number`, `--base-display-number`)
and the same values appear in every runbook, so two agents working in parallel
pick 1850 within seconds of each other. The second Xvfb fails with "Server is
already active", the scene dies, and the failure looks like flakiness rather
than a collision.

A lease is a lock file under `.deskshot/displays/` held by a live process. A
number is free when no lock is held, no `/tmp/.X<n>-lock` exists, and no Xvfb is
running on it - three independent checks, because each on its own has a stale
mode: a lease outlives a `kill -9`, an X lock outlives its server, and a running
Xvfb may predate the lease directory entirely.

    # hold a block for as long as a command runs
    scripts/lease_display.py --count 3 -- python -m deskshot.cli scene-batch \
        --base-display-number '{display}' ...

    # or just ask for one and manage it yourself
    scripts/lease_display.py --count 1 --print-only

With `--print-only` the lease is released immediately and the number is only a
suggestion; the wrapper form is the one that actually guarantees exclusivity,
because the lock is held for the child's lifetime.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import getpass
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, TextIO, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _lease_dir() -> Path:
    """Where display leases live - **node-local, on purpose**.

    An X display is a per-machine resource: `:1501` on one compute node has
    nothing to do with `:1501` on another, and the thing that actually proves it
    is free is `/tmp/.X1501-lock`, which is node-local too. The lease directory
    used to sit in the shared checkout, which was wrong in two ways once jobs
    run on more than one node: 64 jobs would contend on GPFS locks for a
    resource they do not share, and they would carve up one 500-display range
    between them instead of each having the whole of it.

    Overridable with DESKSHOT_LEASE_DIR, for a single-node run that wants the
    leases somewhere it can inspect them.
    """
    override = os.environ.get("DESKSHOT_LEASE_DIR", "").strip()
    if override:
        return Path(override)
    tmp = Path(os.environ.get("TMPDIR") or "/tmp")
    return tmp / "deskshot_displays"


LEASE_DIR = _lease_dir()

#: Range to allocate from. Below 1000 collides with the interactive sessions in
#: scripts/start_desktop.sh; above 2000 collides with nothing but is capped so a
#: runaway loop cannot walk forever.
FIRST_DISPLAY = 1500
LAST_DISPLAY = 1999


def _xvfb_displays() -> set[int]:
    out = subprocess.run(
        ["ps", "-u", os.environ.get("USER") or getpass.getuser(), "-o", "comm,args", "--no-headers"],
        capture_output=True, text=True,
    ).stdout
    live: set[int] = set()
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2 or parts[0] != "Xvfb":
            continue
        for token in parts[1].split():
            if token.startswith(":") and token[1:].split(".")[0].isdigit():
                live.add(int(token[1:].split(".")[0]))
                break
    return live


def _try_lease(number: int, busy: set[int]) -> Optional[TextIO]:
    """Take the lock for `number`, or None if it is not free."""
    if number in busy:
        return None
    if Path(f"/tmp/.X{number}-lock").exists():
        return None
    LEASE_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(LEASE_DIR / f"{number}.lock", "w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EAGAIN, errno.EACCES):
            return None
        raise
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def acquire(count: int) -> Tuple[int, List[TextIO]]:
    """Lease `count` consecutive display numbers. Returns (first, handles)."""
    busy = _xvfb_displays()
    for start in range(FIRST_DISPLAY, LAST_DISPLAY - count + 2):
        handles: List[TextIO] = []
        for offset in range(count):
            handle = _try_lease(start + offset, busy)
            if handle is None:
                break
            handles.append(handle)
        if len(handles) == count:
            return start, handles
        for handle in handles:
            handle.close()
    raise SystemExit(
        f"lease_display: no run of {count} free displays in "
        f"{FIRST_DISPLAY}..{LAST_DISPLAY}. Stale leases? "
        f"`python scripts/cleanup_stale_sessions.py --apply` then retry."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--count", type=int, default=1,
                    help="consecutive displays needed (one per parallel worker)")
    ap.add_argument("--print-only", action="store_true",
                    help="print a free number and release it immediately")
    ap.add_argument("command", nargs=argparse.REMAINDER,
                    help="after --, the command to run; '{display}' is substituted")
    args = ap.parse_args()

    first, handles = acquire(args.count)

    if args.print_only or not args.command:
        print(first)
        for handle in handles:
            handle.close()
        return 0

    command = [a for a in args.command if a != "--"]
    command = [a.replace("{display}", str(first)) for a in command]
    print(f"[lease] displays {first}..{first + args.count - 1} -> {' '.join(command[:6])} ...",
          file=sys.stderr)
    try:
        return subprocess.call(command)
    finally:
        for handle in handles:
            handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
