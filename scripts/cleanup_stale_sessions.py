#!/usr/bin/env python
"""Reclaim what killed scene runs leave behind.

A DesktopSession that exits normally cleans up after itself - verified: bringing
one up and tearing it down leaves zero orphan daemons and no lock file. What
leaks is runs that are *killed*: a batch that times out, an LSF job that dies, a
`kill -9` on the driver. The session's daemons are re-parented to init and keep
running, the Xvfb lock file stays behind, and the per-run profile dirs under
/tmp are never removed.

Left alone it accumulates. Before this script was written the box was carrying
21 orphan daemons (~312MB RSS, oldest four days), 15 stale display locks and
397MB of /tmp - and three wait-loops still sleeping on a scene that had died
five days earlier.

What makes this safe to run at any time is that a display is only considered
dead if connecting to it fails. Anything belonging to a live session is left
alone, so it can run while a batch is in flight.

Deliberately not `pkill -f`: that matches the calling shell's own command line
and has killed the in-flight command three times in this project. Everything
here resolves PIDs first and kills by PID.

Usage:
    PYTHONPATH=src python scripts/cleanup_stale_sessions.py            # report only
    PYTHONPATH=src python scripts/cleanup_stale_sessions.py --apply
"""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

#: Session daemons that get re-parented to init when a run is killed. Matched
#: against `comm`, which the kernel truncates to 15 characters.
DAEMON_PREFIXES = (
    "at-spi2-registr",
    "at-spi-bus-laun",
    "dbus-daemon",
    "gvfsd-metadata",
    "dconf-service",
    "xdg-desktop-por",
    "xdg-permission-",
)

#: Don't touch anything younger than this. A session brings its X server up
#: before its daemons, so the liveness check below is what actually protects a
#: running batch; this is a second line of defence against odd startup orders.
DEFAULT_MIN_AGE_SECONDS = 300

#: The staging cache is meant to persist - it is what `_ensure_binaries_executable`
#: populates, and deleting it just forces every binary to be re-copied.
def _keep_dirs() -> Set[str]:
    from deskshot.config import bin_fix_dir
    return {bin_fix_dir().name, "deskshot_bin_fix"}


def _ps(fields: str) -> List[List[str]]:
    out = subprocess.run(
        ["ps", "-u", os.environ.get("USER") or getpass.getuser(), "-o", fields, "--no-headers"],
        capture_output=True, text=True,
    ).stdout
    return [line.split(None, len(fields.split(",")) - 1) for line in out.splitlines() if line.strip()]


def _proc_env(pid: int, key: str) -> Optional[str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    for entry in raw.split(b"\0"):
        if entry.startswith(key.encode() + b"="):
            return entry.decode(errors="replace").split("=", 1)[1]
    return None


def display_is_live(display: str, xdpyinfo: Optional[str]) -> bool:
    """True if something answers on `display`.

    Falls back to the lock file's PID when xdpyinfo is unavailable, which is the
    case on a bare PATH - the extracted xdpyinfo is a stub that execs the staged
    copy, and that copy only exists once a session has run.
    """
    if xdpyinfo:
        try:
            rc = subprocess.run(
                [xdpyinfo, "-display", display],
                capture_output=True, timeout=5,
            ).returncode
            return rc == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    number = display.lstrip(":").split(".")[0]
    lock = Path(f"/tmp/.X{number}-lock")
    try:
        pid = int(lock.read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Someone else's process: not ours to judge, so treat it as live.
        return True
    # The PID is alive, but PIDs get recycled - on a box that has churned
    # through thousands of scene subprocesses, a dead display's lock can end up
    # naming an unrelated process, and then that display number is never
    # reclaimed. The server writes its own PID here, so require it to *be* one.
    try:
        comm = Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return True
    return comm in {"Xvfb", "Xorg", "X"}


def _find_xdpyinfo() -> Optional[str]:
    for candidate in (
        Path(os.environ.get("DESKSHOT_BIN_FIX_DIR") or "/tmp/deskshot_bin_fix") / "xdpyinfo",
        Path("/usr/bin/xdpyinfo"),
    ):
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate)
    return shutil.which("xdpyinfo")


def live_displays(xdpyinfo: Optional[str]) -> Set[str]:
    """Displays with a running Xvfb, taken from the process table not the locks."""
    live: Set[str] = set()
    for row in _ps("pid,comm,args"):
        if len(row) < 3 or row[1] != "Xvfb":
            continue
        for token in row[2].split():
            if token.startswith(":") and token[1:].split(".")[0].isdigit():
                live.add(token.split(".")[0])
                break
    return live


def find_orphan_daemons(live: Set[str], min_age: int, xdpyinfo: Optional[str]) -> List[Dict]:
    found: List[Dict] = []
    for row in _ps("ppid,pid,etimes,rss,comm"):
        if len(row) < 5 or row[0] != "1":
            continue
        pid, etimes, rss, comm = int(row[1]), int(row[2]), int(row[3]), row[4]
        if not comm.startswith(DAEMON_PREFIXES):
            continue
        if etimes < min_age:
            continue
        display = _proc_env(pid, "DISPLAY")
        if display is None:
            # No DISPLAY at all: cannot prove it is stale, so leave it.
            continue
        if display in live or display_is_live(display, xdpyinfo):
            continue
        found.append({"pid": pid, "comm": comm, "display": display,
                      "age": etimes, "rss_mb": rss / 1024.0})
    return found


def find_stale_locks(live: Set[str], xdpyinfo: Optional[str]) -> List[Tuple[Path, Path, str]]:
    stale: List[Tuple[Path, Path, str]] = []
    for lock in sorted(Path("/tmp").glob(".X*-lock")):
        number = lock.name[2:-5]
        if not number.isdigit():
            continue
        display = f":{number}"
        if display in live or display_is_live(display, xdpyinfo):
            continue
        stale.append((lock, Path(f"/tmp/.X11-unix/X{number}"), display))
    return stale


#: Environment variables through which a launched app points at its session's
#: scratch directory.
#:
#: This catches apps but NOT the session process itself: `/proc/<pid>/environ`
#: is the environment a process was *started* with, and DesktopSession sets
#: these in-process, after start. An idle session - one whose apps have exited,
#: or which has not launched any yet - is therefore invisible here. That gap is
#: why `find_stale_temp` also refuses to touch anything newer than the oldest
#: running session; this check is the finer-grained of the two, not the load
#: bearing one.
_SCRATCH_ENV_KEYS = (
    "HOME", "TMPDIR", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
    "XDG_DATA_HOME", "XDG_STATE_HOME", "CHROME_USER_DATA_DIR",
)


def paths_in_use(root: Path) -> Set[Path]:
    """Top-level `root/deskshot*` entries a running app process points at."""
    in_use: Set[Path] = set()

    def note(value: Optional[str]) -> None:
        if not value:
            return
        try:
            path = Path(value).resolve()
        except (OSError, ValueError):
            return
        try:
            rel = path.relative_to(root.resolve())
        except ValueError:
            return
        if rel.parts and rel.parts[0].startswith("deskshot"):
            in_use.add(root / rel.parts[0])

    for row in _ps("pid"):
        try:
            pid = int(row[0])
        except (ValueError, IndexError):
            continue
        for key in _SCRATCH_ENV_KEYS:
            note(_proc_env(pid, key))
        try:
            note(os.readlink(f"/proc/{pid}/cwd"))
        except OSError:
            pass
    return in_use


def oldest_session_start() -> Optional[float]:
    """When the longest-running live session started, as a unix timestamp.

    Every in-flight run has an Xvfb, and a run's scratch directories are created
    after its Xvfb. So nothing newer than this can be safely assumed dead - even
    if it belongs to a *different*, already-dead session, in which case it is
    simply reclaimed after the current batch finishes. Erring this way costs
    disk; erring the other way deletes a running batch's profile directory.
    """
    ages = []
    for row in _ps("pid,etimes,comm"):
        if len(row) >= 3 and row[2] == "Xvfb":
            try:
                ages.append(int(row[1]))
            except ValueError:
                pass
    if not ages:
        return None
    # A minute of slack for dirs created just before their Xvfb finished coming up.
    return time.time() - max(ages) - 60


SESSION_MARKER_FILE = ".deskshot_session"


def find_stale_temp(min_age_hours: float) -> List[Tuple[Path, float]]:
    keep = _keep_dirs()
    cutoff = time.time() - min_age_hours * 3600
    session_floor = oldest_session_start()
    if session_floor is not None:
        cutoff = min(cutoff, session_floor)
    root = Path(os.environ.get("TMPDIR") or "/tmp")
    busy = paths_in_use(root)
    stale: List[Tuple[Path, float]] = []
    # Two shapes: the historical `deskshot*` directories, and the neutrally
    # named `session-*` ones (the session HOME lives inside and its path is
    # drawn on screen, so the name cannot say "deskshot"). The latter are only
    # touched when they carry our marker file, since /tmp is shared.
    candidates = list(root.glob("deskshot*"))
    candidates += [
        entry for entry in root.glob("session-*")
        if (entry / SESSION_MARKER_FILE).exists()
    ]
    for entry in sorted(set(candidates)):
        if entry.name in keep or entry in busy:
            continue
        try:
            if entry.stat().st_mtime > cutoff:
                continue
            size = _du(entry)
        except OSError:
            continue
        stale.append((entry, size))
    return stale


def _du(path: Path) -> float:
    if path.is_file():
        return path.stat().st_size / 1e6
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total / 1e6


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="actually kill and delete (default is a report)")
    ap.add_argument("--min-age", type=int, default=DEFAULT_MIN_AGE_SECONDS,
                    help="skip processes younger than this many seconds")
    ap.add_argument("--temp-age-hours", type=float, default=6.0,
                    help="skip /tmp entries modified more recently than this")
    args = ap.parse_args()

    xdpyinfo = _find_xdpyinfo()
    live = live_displays(xdpyinfo)
    print(f"live displays (Xvfb running): {sorted(live) or 'none'}")
    print(f"mode: {'APPLY' if args.apply else 'report only (use --apply)'}\n")

    daemons = find_orphan_daemons(live, args.min_age, xdpyinfo)
    locks = find_stale_locks(live, xdpyinfo)
    temp = find_stale_temp(args.temp_age_hours)

    print(f"orphan session daemons: {len(daemons)}"
          f"  ({sum(d['rss_mb'] for d in daemons):.0f} MB RSS)")
    for d in sorted(daemons, key=lambda d: -d["age"])[:20]:
        print(f"  pid {d['pid']:<9} {d['comm']:<17} {d['display']:<8} "
              f"{d['age'] / 3600:.1f}h  {d['rss_mb']:.0f}MB")
    if len(daemons) > 20:
        print(f"  ... and {len(daemons) - 20} more")

    print(f"\nstale display locks: {len(locks)}")
    if locks:
        print("  " + " ".join(d for _, _, d in locks))

    print(f"\nstale /tmp entries: {len(temp)}  ({sum(s for _, s in temp):.0f} MB)")
    for path, size in sorted(temp, key=lambda t: -t[1])[:10]:
        print(f"  {size:8.1f} MB  {path}")
    if len(temp) > 10:
        print(f"  ... and {len(temp) - 10} more")

    if not args.apply:
        print("\nnothing changed.")
        return 0

    killed = 0
    for d in daemons:
        try:
            os.kill(d["pid"], signal.SIGKILL)
            killed += 1
        except OSError:
            pass
    removed_locks = 0
    for lock, sock, _ in locks:
        for path in (lock, sock):
            try:
                path.unlink()
            except OSError:
                pass
        removed_locks += 1
    freed = 0.0
    for path, size in temp:
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink()
            freed += size
        except OSError:
            pass

    print(f"\nkilled {killed} daemons, removed {removed_locks} locks, "
          f"freed {freed:.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
