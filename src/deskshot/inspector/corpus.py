"""Browsing 1.2M captures without ever listing 1.2M captures.

`SampleIndex` walks its root and caches what it finds. That is right for
`incremental_checks/` - 6,445 directories, one 5s walk - and impossible for the
corpus, which is 300 shards holding roughly eleven million files. A single
`os.walk` there does not finish in a session.

So nothing here walks. Every request lists exactly one directory, and counts
the children of only the entries on the page being shown. Cost is bounded by
the page size, not by the size of the corpus, so `shards/` and
`shards/shard-0000/ep/` cost the same.

Two things make that navigable rather than merely possible:

* a shard directory holds ~2,000 per-scene `.log` files beside the three
  directories anyone wants, so files are summarised by extension instead of
  listed, and
* an optional SQLite index built from `plan/manifest.jsonl` turns "a test_app
  sample with heavy occlusion" into a query. Without it the browser still
  works; you just have to know where you are going.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from deskshot.inspector.samples import VIEWS, load_json, png_size

LEAF_SUFFIX = ".elements.leaf.json"

#: Files a capture is made of, so a directory listing can tell "part of a
#: sample" from "something else that happens to be here".
CAPTURE_SUFFIXES = tuple(VIEWS.values()) + (
    ".png", ".meta.json", ".screentag.txt", ".verdict.json",
)

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000


class CorpusError(Exception):
    def __init__(self, status: int, message: str):
        Exception.__init__(self, message)
        self.status = status
        self.message = message


class CorpusBrowser(object):
    """One directory at a time, plus an optional index for jumping."""

    def __init__(self, root: Path, index_path: Optional[Path] = None):
        self.root = Path(root).resolve()
        if index_path is not None:
            self.index_path: Optional[Path] = Path(index_path)
        else:
            candidate = self.root / "plan" / "index.sqlite"
            self.index_path = candidate if candidate.is_file() else None
        self._db: Optional[sqlite3.Connection] = None
        self._db_lock = threading.Lock()
        self._facets: Optional[Dict[str, Any]] = None

    # -- paths ------------------------------------------------------------

    def resolve(self, relative: str) -> Path:
        """Resolve under the root or refuse. The client is not trusted."""
        relative = (relative or "").strip().strip("/")
        if relative.startswith("."):
            raise CorpusError(400, "invalid path")
        candidate = (self.root / relative).resolve() if relative else self.root
        if candidate != self.root and self.root not in candidate.parents:
            raise CorpusError(403, "path outside the corpus root")
        return candidate

    def rel(self, path: Path) -> str:
        if path == self.root:
            return ""
        return os.path.relpath(str(path), str(self.root)).replace(os.sep, "/")

    # -- listing ----------------------------------------------------------

    def ls(self, relative: str = "", offset: int = 0, limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
        directory = self.resolve(relative)
        if not directory.is_dir():
            raise CorpusError(404, "not a directory: %s" % (relative or "/"))
        limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
        offset = max(0, int(offset or 0))

        dir_names: List[str] = []
        file_names: List[str] = []
        try:
            with os.scandir(str(directory)) as entries:
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    try:
                        (dir_names if entry.is_dir() else file_names).append(entry.name)
                    except OSError:
                        continue
        except OSError as error:
            raise CorpusError(404, "cannot list %s: %s" % (relative or "/", error))

        dir_names.sort()
        stems = sorted(name[: -len(LEAF_SUFFIX)]
                       for name in file_names if name.endswith(LEAF_SUFFIX))
        present = set(file_names)
        stems = [stem for stem in stems if (stem + ".png") in present]

        page_dirs = dir_names[offset:offset + limit]
        page_stems = stems[max(0, offset - len(dir_names)):][:max(0, limit - len(page_dirs))]

        return {
            "path": self.rel(directory),
            "parents": self._breadcrumb(directory),
            "dirs": [self._dir_record(directory / name) for name in page_dirs],
            "samples": [self._stem_record(directory, stem) for stem in page_stems],
            "total_dirs": len(dir_names),
            "total_samples": len(stems),
            "offset": offset,
            "limit": limit,
            "other_files": _summarise_files(file_names, stems),
            "indexed": self.index_path is not None,
        }

    def _breadcrumb(self, directory: Path) -> List[Dict[str, str]]:
        crumbs = [{"name": self.root.name or "corpus", "path": ""}]
        if directory == self.root:
            return crumbs
        parts = self.rel(directory).split("/")
        for depth in range(len(parts)):
            crumbs.append({"name": parts[depth], "path": "/".join(parts[: depth + 1])})
        return crumbs

    def _dir_record(self, path: Path) -> Dict[str, Any]:
        """One listdir per entry on the page - the only per-child cost."""
        record: Dict[str, Any] = {"name": path.name, "path": self.rel(path)}
        try:
            names = os.listdir(str(path))
        except OSError:
            record["children"] = None
            return record
        record["children"] = len(names)
        record["captures"] = sum(1 for name in names if name.endswith(LEAF_SUFFIX))
        record["dirs"] = sum(1 for name in names if "." not in name)
        return record

    def _stem_record(self, directory: Path, stem: str) -> Dict[str, Any]:
        png = directory / (stem + ".png")
        record: Dict[str, Any] = {
            "stem": stem,
            "path": "%s/%s" % (self.rel(directory), stem) if self.rel(directory) else stem,
        }
        try:
            record["bytes"] = png.stat().st_size
        except OSError:
            record["bytes"] = None
        width, height = png_size(png)
        record["width"], record["height"] = width, height
        facts = self.facts(record["path"])
        if facts:
            record.update(facts)
        return record

    # -- one sample -------------------------------------------------------

    def sample(self, relative: str, view: str = "leaf") -> Dict[str, Any]:
        """Enough to describe a capture, without reading its 739KB leaf list."""
        directory, stem = self._split(relative)
        meta = load_json(directory / (stem + ".meta.json")) or {}
        png = directory / (stem + ".png")
        if not png.is_file():
            raise CorpusError(404, "no screenshot for %s" % relative)
        width, height = png_size(png)
        available = [name for name, suffix in VIEWS.items()
                     if (directory / (stem + suffix)).is_file()]
        tag_path = directory / (stem + ".screentag.txt")
        try:
            screentag = tag_path.read_text(encoding="utf-8") if tag_path.is_file() else None
        except OSError:
            screentag = None
        return {
            "path": relative,
            "stem": stem,
            "dir": self.rel(directory),
            "width": width,
            "height": height,
            "views": available,
            "view": view if view in available else (available[0] if available else "leaf"),
            "meta": meta if isinstance(meta, dict) else {},
            "verdict": load_json(directory / (stem + ".verdict.json")),
            "episode": load_json(directory / "episode.json") if (directory / "episode.json").is_file() else None,
            "screentag": screentag,
            "facts": self.facts(relative),
        }

    def capture_path(self, relative: str) -> Path:
        directory, stem = self._split(relative)
        return directory / stem

    def _split(self, relative: str) -> Tuple[Path, str]:
        relative = (relative or "").strip().strip("/")
        if not relative or "/" not in relative:
            raise CorpusError(400, "a sample path is <dir>/<stem>")
        head, stem = relative.rsplit("/", 1)
        if not stem or stem.startswith("."):
            raise CorpusError(400, "invalid stem")
        directory = self.resolve(head)
        if not directory.is_dir():
            raise CorpusError(404, "no such directory: %s" % head)
        return directory, stem

    # -- the optional index ----------------------------------------------

    def _connect(self) -> Optional[sqlite3.Connection]:
        """One read-only connection, guarded.

        `check_same_thread=False` is necessary - the server is threaded - but
        on its own it is a promise the caller has to keep: a single sqlite3
        connection is not safe for concurrent use, and two raters or one page
        firing parallel requests is enough to reach it. `self._db_lock` is that
        promise, and every query below takes it.
        """
        if self._db is not None:
            return self._db
        if not self.index_path or not Path(self.index_path).is_file():
            return None
        with self._db_lock:
            if self._db is not None:
                return self._db
            try:
                self._db = sqlite3.connect("file:%s?mode=ro" % self.index_path,
                                           uri=True, check_same_thread=False)
                self._db.row_factory = sqlite3.Row
            except sqlite3.Error:
                self._db = None
        return self._db

    def facts(self, relative: str) -> Optional[Dict[str, Any]]:
        """Split, apps, occlusion for one capture - only if an index exists."""
        db = self._connect()
        if db is None:
            return None
        try:
            with self._db_lock:
                row = db.execute(
                    "SELECT split, apps, theme, resolution, occluded_ratio, n_elements,"
                    " n_windows, publishable, train_eligible FROM samples WHERE path = ?",
                    (relative,),
                ).fetchone()
        except sqlite3.Error:
            return None
        return _row_facts(row) if row else None

    def search(
        self,
        *,
        split: Optional[str] = None,
        app: Optional[str] = None,
        theme: Optional[str] = None,
        resolution: Optional[str] = None,
        min_occlusion: Optional[float] = None,
        min_windows: Optional[int] = None,
        limit: int = 60,
        random_order: bool = False,
        seed: int = 0,
    ) -> Dict[str, Any]:
        db = self._connect()
        if db is None:
            raise CorpusError(
                409,
                "no index at plan/index.sqlite - build it with "
                "scripts/build_corpus_index.py to search a corpus this size",
            )
        where: List[str] = []
        params: List[Any] = []
        if split:
            where.append("split = ?")
            params.append(split)
        if app:
            where.append("apps LIKE ?")
            params.append("%%|%s|%%" % app)
        if theme:
            where.append("theme = ?")
            params.append(theme)
        if resolution:
            where.append("resolution = ?")
            params.append(resolution)
        if min_occlusion is not None:
            where.append("occluded_ratio >= ?")
            params.append(float(min_occlusion))
        if min_windows is not None:
            where.append("n_windows >= ?")
            params.append(int(min_windows))
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        # A deterministic pseudo-shuffle: a seeded permutation of rowid, so the
        # same seed gives the same sample every time and a figure in a paper can
        # be reproduced from the query that made it.
        #
        # Two rounds, not one. `(rowid * K + seed) % M` adds the same offset to
        # every row, which rotates the cyclic order instead of reshuffling it -
        # seeds 5 and 6 returned the same rows. Multiplying again after the
        # modulus avalanches; the modulus comes first so 1.3M rows cannot
        # overflow int64 and silently fall back to float.
        #
        # A single `%`: this string is an *argument* to the %-format below, not
        # the format string, so `%%` reached SQLite verbatim and did not parse.
        order = (" ORDER BY (((rowid * 2654435761 + ? * 499979) % 1000003) * 40503) % 1000003"
                 if random_order else "")
        limit = max(1, min(int(limit or 60), 500))
        sql = "SELECT * FROM samples%s%s LIMIT ?" % (clause, order)
        if random_order:
            params.append(int(seed))
        params.append(limit)
        try:
            with self._db_lock:
                rows = db.execute(sql, params).fetchall()
                total = db.execute("SELECT COUNT(*) FROM samples%s" % clause,
                                   params[:len(where)]).fetchone()[0]
        except sqlite3.Error as error:
            raise CorpusError(500, "index query failed: %s" % error)
        return {
            "total": total,
            "results": [dict(_row_facts(row), path=row["path"], stem=row["stem"])
                        for row in rows],
        }

    def facets(self) -> Dict[str, Any]:
        """What is worth putting in a dropdown, straight from the index.

        Cached: the app tally reads every row's app list, which is 2.3s over
        1.27M rows, and the answer only changes when the index is rebuilt.
        """
        if self._facets is not None:
            return self._facets
        db = self._connect()
        if db is None:
            return {"indexed": False}
        with self._db_lock:
            return self._build_facets(db)

    def _build_facets(self, db: sqlite3.Connection) -> Dict[str, Any]:
        if self._facets is not None:
            return self._facets
        out: Dict[str, Any] = {"indexed": True}
        for column in ("split", "theme", "resolution"):
            try:
                out[column] = [
                    {"value": row[0], "count": row[1]}
                    for row in db.execute(
                        "SELECT %s, COUNT(*) FROM samples GROUP BY %s ORDER BY 2 DESC"
                        % (column, column))
                    if row[0]
                ]
            except sqlite3.Error:
                out[column] = []
        try:
            counter: Counter = Counter()
            for (apps,) in db.execute("SELECT apps FROM samples WHERE apps != '||'"):
                for name in (apps or "").strip("|").split("|"):
                    if name:
                        counter[name] += 1
            out["app"] = [{"value": name, "count": count}
                          for name, count in counter.most_common()]
        except sqlite3.Error:
            out["app"] = []
        try:
            out["total"] = db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        except sqlite3.Error:
            out["total"] = None
        self._facets = out
        return out


def _row_facts(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "split": row["split"],
        "apps": [name for name in (row["apps"] or "").strip("|").split("|") if name],
        "theme": row["theme"],
        "resolution": row["resolution"],
        "occluded_ratio": row["occluded_ratio"],
        "n_elements": row["n_elements"],
        "n_windows": row["n_windows"],
        "publishable": bool(row["publishable"]),
        "train_eligible": bool(row["train_eligible"]),
    }


def _summarise_files(names: List[str], stems: List[str]) -> List[Dict[str, Any]]:
    """A shard holds ~2,000 `.log` files. Count them; do not list them.

    Files belonging to a capture are left out entirely - they are represented
    by the sample they belong to, and listing them again triples the page for
    no information.
    """
    stem_set = set(stems)
    counter: Counter = Counter()
    for name in names:
        if any(name.endswith(suffix) and name[: -len(suffix)] in stem_set
               for suffix in CAPTURE_SUFFIXES):
            continue
        _, dot, extension = name.rpartition(".")
        counter["." + extension if dot else "(no extension)"] += 1
    return [{"extension": extension, "count": count}
            for extension, count in counter.most_common(12)]
