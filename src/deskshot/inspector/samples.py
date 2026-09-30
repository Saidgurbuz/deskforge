"""Finding captures and reading one, without reading them all.

`incremental_checks/` is 6,445 directories and 65,535 files; a cold walk takes
5.1s and there are 862 captures in it. Listing a run must not cost a JSON parse
per sample - the leaf file for a single capture is 739KB - so the list is built
from the PNG header (24 bytes) plus `<stem>.meta.json` (8KB), and the result is
cached per run directory. A run whose directory mtime and file count are
unchanged is reused from cache rather than rescanned, which is why browsing is
instant after the first visit and a "rescan" button exists for when it is not.

Sample *detail* is always read fresh from disk. The cache is a listing index,
never a source of annotation truth.
"""

from __future__ import annotations

import json
import os
import struct
import tempfile
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: The element lists a capture writes, in the order a person wants them offered.
#: `leaf` is what ScreenTag serializes and therefore the default.
VIEWS = OrderedDict(
    (
        ("leaf", ".elements.leaf.json"),
        ("filtered", ".elements.json"),
        ("unfiltered", ".elements.unfiltered.json"),
        ("amodal", ".elements.amodal.json"),
    )
)

#: Images a capture may carry. The screenshot is the only required one; the
#: rest are the pipeline's own renderings, useful for cross-checking a box
#: against what `visualize` thought it drew.
IMAGE_KINDS = OrderedDict(
    (
        ("screenshot", ".png"),
        ("boxes_leaf", ".elements.leaf.x_viz.png"),
        ("boxes_filtered", ".x_viz.png"),
        ("occlusion", ".occlusion.x_viz.png"),
        ("reading_order_leaf", ".readingorder.leaf.x_viz.png"),
        ("reading_order_filtered", ".readingorder.filtered.x_viz.png"),
    )
)

INDEX_VERSION = 2

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_size(path: Path) -> Tuple[Optional[int], Optional[int]]:
    """Read the dimensions out of the IHDR chunk, not by decoding the image."""
    try:
        with path.open("rb") as handle:
            header = handle.read(24)
    except OSError:
        return None, None
    if len(header) < 24 or header[:8] != _PNG_SIGNATURE or header[12:16] != b"IHDR":
        return None, None
    width, height = struct.unpack(">II", header[16:24])
    return int(width), int(height)


def load_elements(path: Path) -> List[Dict[str, Any]]:
    """Both shapes are in the wild: a bare list and {"elements": [...]}."""
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict):
        payload = payload.get("elements") or []
    if not isinstance(payload, list):
        raise ValueError("%s does not contain an element list" % path)
    return [element for element in payload if isinstance(element, dict)]


def load_json(path: Path) -> Optional[Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _sample_record(directory: Path, stem: str) -> Dict[str, Any]:
    png = directory / (stem + ".png")
    stat = png.stat()
    width, height = png_size(png)
    record: Dict[str, Any] = {
        "stem": stem,
        "width": width,
        "height": height,
        "png_bytes": stat.st_size,
        "mtime": round(stat.st_mtime, 3),
        "views": [name for name, suffix in VIEWS.items() if (directory / (stem + suffix)).is_file()],
        "images": [
            name for name, suffix in IMAGE_KINDS.items() if (directory / (stem + suffix)).is_file()
        ],
    }
    meta = load_json(directory / (stem + ".meta.json")) or {}
    if isinstance(meta, dict):
        scene = meta.get("scene") or {}
        record["counts"] = {
            "leaf": meta.get("num_elements_leaf"),
            "filtered": meta.get("num_elements_filtered"),
            "unfiltered": meta.get("num_elements_unfiltered"),
        }
        record["apps"] = meta.get("launched_apps") or []
        record["seed"] = scene.get("seed")
        record["theme"] = scene.get("theme_preset")
        record["layout"] = scene.get("layout")
        record["desktop_env"] = meta.get("desktop_env")
    return record


def scan_directory(directory: Path) -> List[Dict[str, Any]]:
    """Captures in one directory: a PNG and a leaf element list sharing a stem."""
    try:
        names = set(os.listdir(str(directory)))
    except OSError:
        return []
    stems = sorted(
        name[: -len(".elements.leaf.json")]
        for name in names
        if name.endswith(".elements.leaf.json")
    )
    records = []
    for stem in stems:
        if (stem + ".png") not in names:
            continue
        try:
            records.append(_sample_record(directory, stem))
        except OSError:
            continue
    return records


def _dir_signature(directory: Path) -> Tuple[int, int]:
    """Cheap "did anything change here" probe: dir mtime plus entry count.

    A file rewritten in place with the same name does not move the directory
    mtime, which is exactly why sample detail is never served from the cache.
    """
    stat = directory.stat()
    try:
        count = len(os.listdir(str(directory)))
    except OSError:
        count = -1
    return stat.st_mtime_ns, count


class SampleIndex:
    """A cached listing of runs and the captures in them."""

    def __init__(self, root: Path, cache_path: Optional[Path] = None):
        self.root = Path(root)
        self.cache_path = Path(cache_path) if cache_path else None
        self._data: Optional[Dict[str, Any]] = None

    # -- cache ------------------------------------------------------------

    def _load_cache(self) -> Optional[Dict[str, Any]]:
        if not self.cache_path or not self.cache_path.is_file():
            return None
        data = load_json(self.cache_path)
        if not isinstance(data, dict):
            return None
        if data.get("version") != INDEX_VERSION or data.get("root") != str(self.root.resolve()):
            return None
        if not isinstance(data.get("runs"), dict):
            return None
        return data

    def _save_cache(self, data: Dict[str, Any]) -> None:
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=str(self.cache_path.parent),
                prefix=".tmp-index-", delete=False,
            )
            with handle:
                json.dump(data, handle)
            os.replace(handle.name, str(self.cache_path))
        except OSError:
            pass  # a cache that cannot be written is a slow inspector, not a broken one

    # -- scanning ---------------------------------------------------------

    def _scan(self, previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Find the runs. Deliberately does not open a single capture.

        Building sample records for all 864 captures during the walk took 32s
        on this filesystem, because each one costs a dozen stats and a
        `meta.json` read. The walk alone is 5s, so the run list is built from
        directory listings and the per-sample records are filled in when a run
        is actually opened.
        """
        known = (previous or {}).get("runs") or {}
        runs: Dict[str, Any] = {}
        for dirpath, dirnames, filenames in os.walk(str(self.root)):
            # Count entries before pruning, so the signature matches the one
            # `_dir_signature` computes from a plain listdir.
            entries = len(dirnames) + len(filenames)
            dirnames[:] = [name for name in dirnames if not name.startswith(".")]
            captures = sum(1 for name in filenames if name.endswith(".elements.leaf.json"))
            if not captures:
                continue
            directory = Path(dirpath)
            rel = os.path.relpath(dirpath, str(self.root))
            try:
                mtime_ns, count = directory.stat().st_mtime_ns, entries
            except OSError:
                continue
            entry = {"mtime_ns": mtime_ns, "entries": count, "captures": captures}
            cached = known.get(rel)
            if cached and cached.get("mtime_ns") == mtime_ns and cached.get("entries") == count:
                entry["samples"] = cached.get("samples")
            runs[rel] = entry
        return {"version": INDEX_VERSION, "root": str(self.root.resolve()), "runs": runs}

    def refresh(self) -> Dict[str, Any]:
        self._data = self._scan(self._data or self._load_cache())
        self._save_cache(self._data)
        return self._data

    def _ensure(self) -> Dict[str, Any]:
        if self._data is None:
            self._data = self._load_cache()
        if self._data is None:
            self.refresh()
        return self._data or {"runs": {}}

    # -- queries ----------------------------------------------------------

    def runs(self, refresh: bool = False) -> List[Dict[str, Any]]:
        data = self.refresh() if refresh else self._ensure()
        out = []
        for rel, entry in data.get("runs", {}).items():
            samples = entry.get("samples")
            out.append(
                {
                    "run": rel,
                    "samples": len(samples) if samples is not None else entry.get("captures", 0),
                    "listed": samples is not None,
                    "mtime": round((entry.get("mtime_ns") or 0) / 1e9, 3),
                }
            )
        out.sort(key=lambda item: item["mtime"], reverse=True)
        return out

    def samples(self, run: str, refresh: bool = False) -> List[Dict[str, Any]]:
        data = self._ensure()
        entry = (data.get("runs") or {}).setdefault(run, {})
        directory = self.root / run
        if not directory.is_dir():
            return []
        try:
            mtime_ns, count = _dir_signature(directory)
        except OSError:
            mtime_ns, count = None, None
        stale = (
            refresh
            or entry.get("samples") is None
            or entry.get("mtime_ns") != mtime_ns
            or entry.get("entries") != count
        )
        if stale:
            entry["samples"] = scan_directory(directory)
            entry["mtime_ns"], entry["entries"] = mtime_ns, count
            entry["captures"] = len(entry["samples"])
            self._save_cache(data)
        return list(entry.get("samples") or [])


# --------------------------------------------------------------------------
# per-sample derived values


def summarise(elements: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """The numbers a person wants beside a capture, computed from what is shown.

    Everything here is derived from the merged element list rather than from
    `meta.json`, so an edit changes the statistics the way it changes the boxes.
    """
    by_role: Counter = Counter()
    by_kind: Counter = Counter()
    by_type: Counter = Counter()
    by_app: Counter = Counter()
    by_text_status: Counter = Counter()
    occluded = 0
    fragmented = 0
    actionable = 0
    with_text = 0
    characters = 0
    edited = Counter()
    areas = []

    for element in elements:
        by_role[element.get("role") or "?"] += 1
        # Both vocabularies, side by side: `type` is the legacy 55 ScreenTag
        # classes, `kind` the 26 that replace them. Comparing them on real
        # captures is the whole point of emitting both, so the stats tab has to
        # show both rather than one.
        by_kind[element.get("kind") or "-"] += 1
        by_type[element.get("type") or "?"] += 1
        by_app[element.get("app_name") or "?"] += 1
        status = element.get("visible_text_status")
        if status:
            by_text_status[status] += 1
        if element.get("is_occluded"):
            occluded += 1
        if len(element.get("visible_fragments") or []) > 1:
            fragmented += 1
        if ((element.get("interaction") or {}).get("actionable")):
            actionable += 1
        text = element.get("visible_text") or ""
        if text:
            with_text += 1
            characters += len(text)
        golden = element.get("_golden") or {}
        if golden.get("status"):
            edited[golden["status"]] += 1
        rect = element.get("rect") or {}
        if _numeric(rect.get("w")) and _numeric(rect.get("h")):
            areas.append(float(rect["w"]) * float(rect["h"]))

    areas.sort()
    return {
        "elements": len(elements),
        "occluded": occluded,
        "fragmented": fragmented,
        "actionable": actionable,
        "with_text": with_text,
        "characters": characters,
        "median_area": int(areas[len(areas) // 2]) if areas else 0,
        "by_role": by_role.most_common(),
        "by_kind": by_kind.most_common(),
        "by_type": by_type.most_common(),
        "by_app": by_app.most_common(),
        "by_text_status": by_text_status.most_common(),
        "edited": dict(edited),
    }


def _numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class AuditIndex:
    """Per-capture rows out of a run's `audit/*.json`, reloaded when they change.

    Every audit in this repo writes `{"rows": [{"capture": "<stem>.png", ...}]}`,
    so one loader covers pixel, coverage and structure audits without knowing
    which of them a run happened to produce.
    """

    def __init__(self) -> None:
        self._cache: Dict[str, Tuple[Tuple[float, int], Dict[str, Any]]] = {}

    def rows_for(self, run_dir: Path, stem: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        audit_dir = Path(run_dir) / "audit"
        if not audit_dir.is_dir():
            return out
        for path in sorted(audit_dir.glob("*.json")):
            table = self._table(path)
            row = table.get(stem + ".png") or table.get(stem)
            if row is not None:
                out[path.stem] = row
        return out

    def _table(self, path: Path) -> Dict[str, Any]:
        try:
            stat = path.stat()
        except OSError:
            return {}
        signature = (stat.st_mtime, stat.st_size)
        cached = self._cache.get(str(path))
        if cached and cached[0] == signature:
            return cached[1]
        payload = load_json(path)
        table: Dict[str, Any] = {}
        if isinstance(payload, dict):
            for row in payload.get("rows") or []:
                if isinstance(row, dict) and isinstance(row.get("capture"), str):
                    table[row["capture"]] = row
        self._cache[str(path)] = (signature, table)
        return table
