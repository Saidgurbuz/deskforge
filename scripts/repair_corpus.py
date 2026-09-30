#!/usr/bin/env python
"""Find and set aside the samples a killed run left half-written.

Until the capture pipeline was changed to publish the screenshot last, the PNG
was written to its final name *before* the annotations. A shard killed in
between - LSF timeout, node eviction, OOM - therefore left a perfectly good
screenshot with no ground truth beside it. There were 11,835 of them, 2.92% of
the corpus, and every audit missed them because the audits walk `*.meta.json`
and these have no meta to walk.

Two shapes of damage:

  blank     a zero-application capture from before the readiness fix: black
            screen, one or two leaf elements, nothing drawn (--drop-blank-desktops)
  orphan    a `<stem>.png` with no `<stem>.meta.json` - an image with no labels
  partial   a stem missing any artifact a sample owes, including the screenshot
            itself: now that the PNG is renamed into place last, a kill leaves a
            meta with no PNG rather than the other way round

Neither can be repaired: the accessibility tree that would have produced the
annotations is gone with the process that held it. So they are moved aside
rather than deleted, into `<root>/quarantine/`, preserving the shard path so a
mistake can be undone with `mv`. `--delete` removes them instead, and
`--apply` is required for either - the default only reports.

    repair_corpus.py --root <corpus root>
    repair_corpus.py --root <corpus root> --apply
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Set, Tuple

# What a finished sample owes. `.elements.amodal.json` is included because the
# pipeline always writes it; a stem missing it was interrupted.
REQUIRED_PARTS = (
    ".meta.json",
    ".elements.json",
    ".elements.leaf.json",
    ".elements.unfiltered.json",
    ".elements.amodal.json",
    ".screentag.txt",
)


#: A capture with no applications and this few published elements is the black
#: desktop the readiness bug produced: wallpaper never painted, so all that
#: survives is the panel's "Applications" label. Real bare desktops carry a
#: dozen icons; the verification run measured 12.
BLANK_DESKTOP_MAX_LEAF = 3


def find_blank_desktops(directory: Path) -> List[Path]:
    """Stems that are a zero-application capture with nothing drawn on them.

    Judged from the metadata alone - `launched_apps` and the published leaf
    count - so the scan never decodes an image. Every one of 40 sampled such
    captures was black across all four desktop profiles, and each carried a
    single leaf element.
    """
    out: List[Path] = []
    try:
        metas = [p for p in directory.iterdir() if p.name.endswith(".meta.json")]
    except OSError:
        return out
    for meta in metas:
        try:
            payload = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if payload.get("launched_apps"):
            continue
        if (payload.get("scene") or {}).get("apps"):
            continue
        leaf = payload.get("num_elements_leaf")
        if not isinstance(leaf, int) or leaf > BLANK_DESKTOP_MAX_LEAF:
            continue
        out.append(directory / meta.name[: -len(".meta.json")])
    return out


#: How recently a file may have been touched and still be considered damage.
#:
#: Now that the screenshot is renamed into place last, a capture that is *in
#: flight* looks exactly like a half sample: metadata on disk, no PNG yet. Run
#: this while a generation is going and it would quarantine work that was about
#: to finish. Anything younger than this is left alone, which costs nothing -
#: real damage does not heal - and makes the tool safe to run during a run.
MIN_AGE_SEC = 3600.0


def _older_than(path: Path, cutoff: float) -> bool:
    try:
        return path.stat().st_mtime < cutoff
    except OSError:
        return False


def scan_capture_dir(
    directory: Path, cutoff: float = 0.0
) -> Tuple[List[Path], List[Path], List[Path]]:
    """Return (orphan stems, partial stems, stale .partial files) for one dir.

    `cutoff` is an mtime before which a file counts as settled; anything newer
    is assumed to be a capture still being written.
    """
    try:
        names = set(p.name for p in directory.iterdir())
    except OSError:
        return [], [], []
    pngs = {n[: -len(".png")] for n in names if n.endswith(".png")}
    metas = {n[: -len(".meta.json")] for n in names if n.endswith(".meta.json")}
    orphans = [directory / f"{s}.png" for s in sorted(pngs - metas)]
    # Two failure modes, one per write order. Before the screenshot was made the
    # commit marker, a kill left a PNG with no meta; now it leaves a meta with no
    # PNG. Both are half samples and both have to be caught, because a corpus
    # spans runs from either side of that change.
    partials = [
        directory / s
        for s in sorted(pngs & metas)
        if any(f"{s}{part}" not in names for part in REQUIRED_PARTS)
    ] + [directory / s for s in sorted(metas - pngs)]

    stale = [
        directory / n
        for n in sorted(names)
        if n.startswith(".") and n.endswith(".png.partial")
    ]

    if cutoff:
        # A stem counts as settled only when *every* file it owns is settled -
        # one freshly written part means the capture is still in progress.
        def settled(stem: Path) -> bool:
            parts = list(directory.glob(f"{stem.name}.*"))
            return bool(parts) and all(_older_than(q, cutoff) for q in parts)

        orphans = [p for p in orphans if _older_than(p, cutoff)]
        partials = [p for p in partials if settled(p)]
        stale = [p for p in stale if _older_than(p, cutoff)]

    return orphans, partials, stale


def files_for_stem(stem_path: Path) -> List[Path]:
    """Every artifact belonging to one capture stem."""
    return sorted(stem_path.parent.glob(f"{stem_path.name}.*"))


def capture_dirs(root: Path) -> List[Path]:
    out: List[Path] = []
    shards = root / "shards"
    base = shards if shards.is_dir() else root
    for shard in sorted(p for p in base.iterdir() if p.is_dir()):
        for sub in ("ep", "st"):
            group = shard / sub
            if group.is_dir():
                out.extend(p for p in group.iterdir() if p.is_dir())
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--apply", action="store_true",
                    help="actually move (or delete); without it, only report")
    ap.add_argument("--delete", action="store_true",
                    help="delete instead of moving to <root>/quarantine")
    ap.add_argument("--min-age-sec", type=float, default=MIN_AGE_SEC,
                    help="leave anything touched more recently than this alone, "
                         "so a capture still being written is never quarantined. "
                         "0 disables the guard.")
    ap.add_argument("--drop-blank-desktops", action="store_true",
                    help="also take out zero-application captures with nothing "
                         "drawn on them - the black desktops from before the "
                         "readiness fix")
    ap.add_argument("--threads", type=int, default=24)
    args = ap.parse_args()

    if not (args.root / "shards").is_dir() and not args.root.is_dir():
        print(f"no corpus at {args.root}", file=sys.stderr)
        return 2

    dirs = capture_dirs(args.root)
    print(f"scanning {len(dirs)} capture directories under {args.root}")

    counts = Counter()
    victims: List[Tuple[str, Path]] = []
    cutoff = (time.time() - args.min_age_sec) if args.min_age_sec > 0 else 0.0
    if cutoff:
        print(f"ignoring anything touched in the last "
              f"{args.min_age_sec / 60:.0f} min (a capture in flight looks "
              f"exactly like a half sample)")
    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        for orphans, partials, stale in pool.map(
            lambda d: scan_capture_dir(d, cutoff), dirs
        ):
            for png in orphans:
                counts["orphan"] += 1
                victims.append(("orphan", png))
            for stem in partials:
                counts["partial"] += 1
                victims.append(("partial", stem))
            for tmp in stale:
                counts["stale_partial"] += 1
                victims.append(("stale_partial", tmp))

    if args.drop_blank_desktops:
        with ThreadPoolExecutor(max_workers=args.threads) as pool:
            for stems in pool.map(find_blank_desktops, dirs):
                for stem in stems:
                    counts["blank_desktop"] += 1
                    victims.append(("partial", stem))  # same removal shape

    print(f"  orphan screenshots (no annotations): {counts['orphan']}")
    print(f"  incomplete samples (missing a part): {counts['partial']}")
    print(f"  stale .png.partial from a kill      : {counts['stale_partial']}")
    if args.drop_blank_desktops:
        print(f"  blank zero-application desktops     : {counts['blank_desktop']}")

    if not victims:
        print("nothing to repair")
        return 0

    for kind, path in victims[:5]:
        print(f"  eg {kind}: {path}")

    if not args.apply:
        print("\ndry run - pass --apply to move these to "
              f"{args.root / 'quarantine'} (or --delete to remove them)")
        return 0

    quarantine = args.root / "quarantine"

    # Expand to individual files first, then move them concurrently. Serially,
    # this managed 0.5 files/sec: each move is a rename (both paths are on the
    # same filesystem) but the `mkdir -p` in front of it is a synchronous GPFS
    # metadata round trip, and there are ~12,000 of them. Directories are
    # created once, up front, for the same reason.
    # One path can be reached two ways - an orphan PNG is also matched by its
    # own stem's glob - and with 48 threads the loser of that race logs a
    # confusing ENOENT for a file the winner has already moved. Deduplicate
    # while preserving order so the log means what it says.
    work: List[Path] = []
    seen: Set[Path] = set()
    for kind, path in victims:
        for target in ([path] if kind != "partial" else files_for_stem(path)):
            if target in seen:
                continue
            seen.add(target)
            work.append(target)

    if not args.delete:
        for parent in {(quarantine / f.relative_to(args.root)).parent for f in work}:
            parent.mkdir(parents=True, exist_ok=True)

    def relocate(target: Path) -> int:
        """Move or delete one file. Returns its size, or -1 if it was skipped."""
        try:
            size = target.stat().st_size
        except OSError:
            return -1
        try:
            if args.delete:
                target.unlink()
            else:
                shutil.move(str(target), str(quarantine / target.relative_to(args.root)))
        except OSError as exc:
            print(f"  could not handle {target}: {exc}", file=sys.stderr)
            return -1
        return size

    done = 0
    bytes_freed = 0
    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        for size in pool.map(relocate, work):
            if size < 0:
                continue
            done += 1
            bytes_freed += size

    verb = "deleted" if args.delete else f"moved to {quarantine}"
    print(f"\n{done} files {verb} ({bytes_freed / 1e9:.2f} GB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
