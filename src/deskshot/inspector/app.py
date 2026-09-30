"""The request/response layer, with no socket in it.

Routing, path validation, merging and serialisation all live here as plain
functions over strings and dicts, so the interesting behaviour - does a bad
`run` parameter escape the root, does an edit round-trip through the overlay -
is testable without binding a port or driving a browser. `server.py` is the
thin adapter that turns this into HTTP.

Two rules shape the responses:

- Screenshots are up to 3840x2160 and about 1MB each, so `/api/image` supports a
  crop and a size cap. The viewer fetches a fitted copy of the whole frame and
  full-resolution crops of whatever region is zoomed in on, instead of pushing
  the original over an SSH tunnel every time.
- Element JSON is large (739KB for one 379-element capture), so JSON responses
  are gzipped when the client accepts it.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import mimetypes
import os
import posixpath
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote

from . import audit as audit_mod
from . import corpus as corpus_mod
from . import overlay as overlay_mod
from . import samples as samples_mod
from . import screentag as screentag_mod

try:  # crops and thumbnails only; the inspector still serves whole PNGs without it
    from PIL import Image
except ImportError:  # pragma: no cover - Pillow is present in this environment
    Image = None

STATIC_DIR = Path(__file__).resolve().parent / "static"

#: Cap on a generated crop or thumbnail, in pixels of the *source* region. A
#: request for a full-resolution crop of a 4K frame is legitimate; a request
#: that would decode and re-encode far more than the frame itself is not.
MAX_REGION_PIXELS = 3840 * 2160

#: Generated images are memoised on disk because the list view asks for the
#: same thumbnails on every visit, and decoding a 4K PNG costs ~0.1s each.
THUMBNAIL_CACHE_DIRNAME = "images"

#: What `fmt` may ask for, measured on a 3840x2160 capture over this tunnel.
#:
#: - `png` is what the raw file is; a 2048px-wide copy of it is 681KB.
#: - `webp` is for a frame that has already been downscaled, where the resize
#:   lost more than the encoder will: the same copy is 86KB, 8x smaller.
#: - `webp_exact` is lossless, for a native-resolution crop where the point is
#:   to look at the actual pixels under a box. It is *also* 2.7x smaller than
#:   PNG on screenshot content (80KB against 221KB for a 1408x896 crop), so
#:   exactness costs nothing but encode time here.
IMAGE_FORMATS = {
    "png": ("PNG", "image/png", {"compress_level": 1}),
    "webp": ("WEBP", "image/webp", {"quality": 86, "method": 2}),
    "webp_exact": ("WEBP", "image/webp", {"lossless": True, "method": 1}),
}
DEFAULT_IMAGE_FORMAT = "png"

#: Decoded frames held in memory, newest first. ~25MB per 4K frame.
#:
#: Three was too few to be a cache at all: a browser opens up to six
#: connections, so six concurrent thumbnail or prefetch requests for six
#: different captures evicted each other before any of them was reused, and
#: every one paid its own ~105ms decode.
FRAME_CACHE_SIZE = int(os.environ.get("DESKSHOT_FRAME_CACHE") or 12)

#: Parsed element lists held in memory. One capture is 480KB of JSON and an
#: audit item is opened, prefetched and revisited.
LEAF_CACHE_SIZE = 8

#: How often the generated-image cache is measured and trimmed. Not on every
#: write: that would `iterdir` the whole cache directory once per image
#: request, which is the per-request cost this cache exists to avoid. So the
#: ceiling is a ceiling on what the cache settles at, and it can overshoot by
#: up to this many files between trims.
IMAGE_CACHE_PRUNE_EVERY = 64


class HttpError(Exception):
    def __init__(self, status: int, message: str, extra: Optional[Dict[str, Any]] = None):
        Exception.__init__(self, message)
        self.status = status
        self.message = message
        #: Merged into the error body. A 409 carries the overlay that won, so
        #: the client can show both sides instead of just refusing to save.
        self.extra = extra or {}


class Response(object):
    """A body, or a file to stream instead of buffering it."""

    __slots__ = ("status", "headers", "body", "path")

    def __init__(self, status=200, headers=None, body=b"", path=None):
        self.status = status
        self.headers = headers or {}
        self.body = body
        self.path = path

    def read(self) -> bytes:
        if self.path is not None:
            return Path(self.path).read_bytes()
        return self.body

    def json(self) -> Any:
        body = self.read()
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return json.loads(body.decode("utf-8"))


def _json_response(payload: Any, status: int = 200, accept_encoding: str = "",
                   cache: str = "no-store") -> Response:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8", "Cache-Control": cache}
    if "gzip" in (accept_encoding or "") and len(body) > 1024:
        body = gzip.compress(body, 6)
        headers["Content-Encoding"] = "gzip"
    return Response(status, headers, body)


def _json_body(body: Optional[bytes]) -> Dict[str, Any]:
    try:
        payload = json.loads((body or b"").decode("utf-8"))
    except ValueError:
        raise HttpError(400, "body must be JSON")
    if not isinstance(payload, dict):
        raise HttpError(400, "body must be a JSON object")
    return payload


def _label_answer(row: Dict[str, Any]) -> Dict[str, Any]:
    """One stored answer, reduced to what the page needs to re-render it."""
    answer = {key: row[key] for key in
              ("value", "values", "at", "rater", "dwell_ms", "note", "event_id")
              if key in row}
    return answer


def _int_param(query: Dict[str, List[str]], name: str) -> Optional[int]:
    raw = _param(query, name)
    if raw in (None, ""):
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        raise HttpError(400, "%s must be a number" % name)


def _float_param(query: Dict[str, List[str]], name: str) -> Optional[float]:
    raw = _param(query, name)
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise HttpError(400, "%s must be a number" % name)


def _param(query: Dict[str, List[str]], name: str, default: Optional[str] = None) -> Optional[str]:
    values = query.get(name)
    if not values:
        return default
    return values[0]


def _require(query: Dict[str, List[str]], name: str) -> str:
    value = _param(query, name)
    if not value:
        raise HttpError(400, "missing %s parameter" % name)
    return value


def build_tree(
    runs: List[Dict[str, Any]], overlays: Optional[Dict[str, Dict[str, int]]] = None
) -> List[Dict[str, Any]]:
    """Nest a flat list of run paths into folders, newest branch first.

    A pure function over the run listing so the shape of the sidebar can be
    tested without a filesystem. `samples` is what the folder itself holds;
    `total`, `marked` and `edited` are rolled up over the subtree, which is what
    makes a collapsed folder informative enough to leave collapsed.
    """
    overlays = overlays or {}
    roots: List[Dict[str, Any]] = []
    index: Dict[str, Dict[str, Any]] = {}

    for entry in runs:
        parts = [
            part
            for part in str(entry.get("run") or "").replace(os.sep, "/").split("/")
            if part and part != "."
        ]
        if not parts:
            continue
        path = ""
        siblings = roots
        node = None
        for part in parts:
            path = part if not path else path + "/" + part
            node = index.get(path)
            if node is None:
                node = {
                    "name": part, "path": path, "run": False, "listed": False,
                    "samples": 0, "total": 0, "marked": 0, "edited": 0,
                    "mtime": 0.0, "children": [],
                }
                index[path] = node
                siblings.append(node)
            siblings = node["children"]
        counts = overlays.get(entry.get("run")) or {}
        node["run"] = True
        node["listed"] = bool(entry.get("listed"))
        node["samples"] = entry.get("samples") or 0
        node["marked"] = counts.get("marked", 0)
        node["edited"] = counts.get("edited", 0)
        node["mtime"] = entry.get("mtime") or 0.0

    def rollup(node: Dict[str, Any]) -> None:
        total, marked, edited, mtime = (
            node["samples"], node["marked"], node["edited"], node["mtime"],
        )
        for child in node["children"]:
            rollup(child)
            total += child["total"]
            marked += child["marked"]
            edited += child["edited"]
            mtime = max(mtime, child["mtime"])
        node["total"], node["marked"], node["edited"], node["mtime"] = (
            total, marked, edited, mtime,
        )
        node["children"].sort(key=lambda child: (-child["mtime"], child["name"]))

    for node in roots:
        rollup(node)
    roots.sort(key=lambda node: (-node["mtime"], node["name"]))
    return roots


class InspectorApp(object):
    """Everything the inspector answers, keyed by method and path."""

    def __init__(
        self,
        root: Path,
        golden_root: Path,
        author: str = "unknown",
        cache_dir: Optional[Path] = None,
        cache_max_bytes: int = 4 << 30,
        project_root: Optional[Path] = None,
        read_only: bool = False,
        corpus_root: Optional[Path] = None,
        audit_root: Optional[Path] = None,
        rater: Optional[str] = None,
        audit_read_only: bool = False,
    ):
        self.root = Path(root).resolve()
        self.golden_root = Path(golden_root)
        self.author = author
        self.project_root = Path(project_root).resolve() if project_root else self.root.parent
        self.read_only = read_only
        self.cache_dir = Path(cache_dir) if cache_dir else None
        index_cache = (self.cache_dir / "index.json") if self.cache_dir else None
        self.index = samples_mod.SampleIndex(self.root, index_cache)
        self.audits = samples_mod.AuditIndex()
        # Decoding a 3840x2160 PNG costs ~105ms, and one visit to a sample asks
        # for the fitted frame plus a crop per zoom step, all of the same file.
        # Holding the last few decoded frames turns every request after the
        # first into a crop of memory. ~25MB per frame at 4K.
        self._frames: "OrderedDict[Tuple[str, int, int], Any]" = OrderedDict()
        self._frames_lock = threading.Lock()
        self._decodes: Dict[Tuple[str, int, int], "threading.Lock"] = {}
        self._cache_bytes_max = max(0, int(cache_max_bytes or 0))
        self._prune_counter = 0
        # The corpus is browsed, never edited: 1.2M captures under a path that
        # is not `self.root` and must never be walked. `None` when the
        # inspector was started without one, and every /api/corpus route then
        # answers 404 rather than pretending.
        self.corpus = (
            corpus_mod.CorpusBrowser(Path(corpus_root).resolve())
            if corpus_root else None
        )
        # Human-audit campaigns. Separate from `golden_root` on purpose: an
        # audit session must not be able to touch the curated dev-run set, and
        # a campaign is keyed by observation key rather than by run and stem.
        self.audit_root = Path(audit_root).resolve() if audit_root else None
        self.rater = rater or author
        self.audit_read_only = audit_read_only
        self._campaigns: Dict[str, "audit_mod.Campaign"] = {}
        self._campaigns_lock = threading.Lock()
        # Parsed element lists, keyed by file identity. An audit item is opened,
        # its neighbours are prefetched, and a rater steps back and forth; each
        # of those would otherwise re-read and re-parse the same 480KB file.
        self._leaves: "OrderedDict[Tuple[str, int, int], List[Dict[str, Any]]]" = OrderedDict()
        self._leaves_lock = threading.Lock()

    # -- path safety ------------------------------------------------------

    def run_dir(self, run: str) -> Path:
        """Resolve a run parameter under the root, or refuse.

        The inspector writes to disk and is reachable over a tunnel, so `run`
        is treated as hostile input even though the only client is a browser on
        the other end of it.
        """
        run = (run or "").strip().strip("/")
        if not run or run.startswith("."):
            raise HttpError(400, "invalid run")
        candidate = (self.root / run).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise HttpError(403, "run outside the inspected root")
        if not candidate.is_dir():
            raise HttpError(404, "no such run: %s" % run)
        return candidate

    @staticmethod
    def check_stem(stem: str) -> str:
        if not stem or "/" in stem or "\\" in stem or stem.startswith("."):
            raise HttpError(400, "invalid stem")
        return stem

    def rel_run(self, directory: Path) -> str:
        return os.path.relpath(str(directory), str(self.root))

    # -- dispatch ---------------------------------------------------------

    def handle(
        self,
        method: str,
        path: str,
        query: Optional[Dict[str, List[str]]] = None,
        body: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Response:
        query = query or {}
        headers = {key.lower(): value for key, value in (headers or {}).items()}
        try:
            return self._route(method.upper(), path, query, body, headers)
        except HttpError as error:
            payload = {"error": error.message}
            payload.update(error.extra)
            return _json_response(payload, error.status,
                                  headers.get("accept-encoding", ""))
        except overlay_mod.OverlayError as error:
            return _json_response({"error": str(error)}, 400,
                                  headers.get("accept-encoding", ""))

    def _route(self, method, path, query, body, headers):
        accept = headers.get("accept-encoding", "")
        if path in ("/", "/index.html"):
            return self._static("index.html", headers)
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):], headers)
        if path == "/favicon.ico":
            return Response(404, {"Content-Type": "text/plain"}, b"")

        if path == "/api/config" and method == "GET":
            return _json_response(self._config(), 200, accept)
        if path == "/api/runs" and method == "GET":
            return _json_response(
                {"runs": self.index.runs(refresh=_param(query, "refresh") == "1")}, 200, accept
            )
        if path == "/api/tree" and method == "GET":
            return _json_response(self._tree(query), 200, accept)
        if path == "/api/run" and method == "GET":
            return _json_response(self._run(query), 200, accept)
        if path == "/api/sample" and method == "GET":
            return _json_response(self._sample(query), 200, accept)
        if path == "/api/screentag" and method == "GET":
            return self._screentag(query, accept)
        if path == "/api/image" and method == "GET":
            return self._image(query, headers)
        if path.startswith("/api/corpus/") and method == "GET":
            return self._corpus_route(path[len("/api/corpus/"):], query, headers, accept)
        if path == "/corpus" and method == "GET":
            return self._static("corpus.html", headers)
        if path == "/audit" and method == "GET":
            return self._static("audit.html", headers)
        if path.startswith("/api/audit/"):
            return self._audit_route(path[len("/api/audit/"):], method, query,
                                     body, headers, accept)
        if path == "/api/golden" and method == "GET":
            return _json_response(self._golden(), 200, accept)
        if path == "/api/overlay":
            if method == "POST":
                return _json_response(self._save_overlay(body), 200, accept)
            if method == "DELETE":
                return _json_response(self._delete_overlay(query), 200, accept)
        raise HttpError(404, "no route for %s %s" % (method, path))

    # -- static -----------------------------------------------------------

    def _static(self, relative: str, headers_in: Optional[Dict[str, str]] = None) -> Response:
        headers_in = headers_in or {}
        relative = posixpath.normpath(unquote(relative)).lstrip("/")
        if relative.startswith("..") or not relative:
            raise HttpError(400, "invalid asset path")
        target = (STATIC_DIR / relative).resolve()
        if STATIC_DIR not in target.parents or not target.is_file():
            raise HttpError(404, "no such asset")
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript",):
            content_type += "; charset=utf-8"
        body = target.read_bytes()
        stat = target.stat()
        # `no-cache` on its own, with no validator, meant 85KB of uncompressed
        # HTML+CSS+JS crossed the tunnel on every single page load and could
        # not even 304. Revalidation plus gzip makes a reload two conditional
        # requests instead.
        etag = '"%s"' % hashlib.sha1(
            ("%s|%s|%s" % (target.name, stat.st_mtime_ns, stat.st_size)).encode("utf-8")
        ).hexdigest()[:24]
        headers = {"Content-Type": content_type, "Cache-Control": "no-cache", "ETag": etag}
        if headers_in.get("if-none-match") == etag:
            return Response(304, headers)
        if "gzip" in (headers_in.get("accept-encoding") or "") and len(body) > 1024:
            body = gzip.compress(body, 6)
            headers["Content-Encoding"] = "gzip"
        headers["Content-Length"] = str(len(body))
        return Response(200, headers, body)

    # -- payloads ---------------------------------------------------------

    def _config(self) -> Dict[str, Any]:
        return {
            "root": str(self.root),
            "golden_root": str(self.golden_root),
            "author": self.author,
            "read_only": self.read_only,
            "views": list(samples_mod.VIEWS),
            "image_kinds": list(samples_mod.IMAGE_KINDS),
            "editable_fields": sorted(overlay_mod.EDITABLE_FIELDS),
            "mark_statuses": list(overlay_mod.MARK_STATUSES),
            "crops": Image is not None,
            "image_formats": sorted(IMAGE_FORMATS) if Image is not None else ["png"],
        }

    def _tree(self, query) -> Dict[str, Any]:
        """The whole run hierarchy as folders, without opening a capture.

        `incremental_checks/` is 325 run directories under 255 top-level ones,
        so the sidebar has to be able to show the shape of it at once. It is
        built entirely from the cached run index - the same data `/api/runs`
        serves - which means no filesystem walk per request and no image or
        element file touched. Samples are the leaves and are fetched per run by
        `/api/run` when a run is actually opened.
        """
        runs = self.index.runs(refresh=_param(query, "refresh") == "1")
        overlays = self._overlay_counts()
        return {
            "tree": build_tree(runs, overlays),
            "runs": len(runs),
            "captures": sum(entry.get("samples") or 0 for entry in runs),
        }

    def _overlay_counts(self) -> Dict[str, Dict[str, int]]:
        """Marked/edited totals per run, so a folder can be badged as curated."""
        out: Dict[str, Dict[str, int]] = {}
        for _, document in overlay_mod.iter_overlays(self.golden_root):
            run = (document.get("sample") or {}).get("run")
            if not isinstance(run, str) or not run:
                continue
            bucket = out.setdefault(run, {"marked": 0, "edited": 0})
            if (document.get("mark") or {}).get("status", "unmarked") != "unmarked":
                bucket["marked"] += 1
            edits = document.get("edits") or {}
            if edits.get("modified") or edits.get("deleted") or edits.get("added"):
                bucket["edited"] += 1
        return out

    def _run(self, query) -> Dict[str, Any]:
        run = _require(query, "run")
        directory = self.run_dir(run)
        rel = self.rel_run(directory)
        cached = self.index.samples(rel, refresh=_param(query, "refresh") == "1")
        marks = self._marks_for_run(rel)
        # Copy: the index hands out its cached records, and stamping the
        # current mark onto them would persist it into the index file.
        listing = []
        for sample in cached:
            entry = dict(sample)
            entry["overlay"] = marks.get(entry["stem"])
            listing.append(entry)
        return {
            "run": rel,
            "samples": listing,
            "quality": samples_mod.load_json(directory / "quality.json"),
        }

    def _marks_for_run(self, run: str) -> Dict[str, Dict[str, Any]]:
        """One overlay summary per stem, so the list can badge curated samples."""
        out: Dict[str, Dict[str, Any]] = {}
        directory = Path(self.golden_root) / run
        if not directory.is_dir():
            return out
        for path in sorted(directory.glob("*.json")):
            document = samples_mod.load_json(path)
            if not isinstance(document, dict) or document.get("schema") != overlay_mod.SCHEMA:
                continue
            sample = document.get("sample") or {}
            stem = sample.get("stem")
            if not stem:
                continue
            edits = document.get("edits") or {}
            summary = {
                "view": sample.get("view"),
                "mark": document.get("mark") or {},
                "updated_at": document.get("updated_at"),
                "updated_by": document.get("updated_by"),
                "counts": {
                    "modified": len(edits.get("modified") or {}),
                    "deleted": len(edits.get("deleted") or []),
                    "added": len(edits.get("added") or []),
                },
            }
            existing = out.get(stem)
            if existing is None or (summary["updated_at"] or "") > (existing["updated_at"] or ""):
                out[stem] = summary
        return out

    def _resolve_sample(self, query) -> Tuple[Path, str, str, str, Path]:
        directory = self.run_dir(_require(query, "run"))
        stem = self.check_stem(_require(query, "stem"))
        view = _param(query, "view", "leaf")
        if view not in samples_mod.VIEWS:
            raise HttpError(400, "unknown view %r" % view)
        source = directory / (stem + samples_mod.VIEWS[view])
        if not source.is_file():
            raise HttpError(404, "no %s elements for %s" % (view, stem))
        return directory, self.rel_run(directory), stem, view, source

    def _sample(self, query) -> Dict[str, Any]:
        directory, run, stem, view, source = self._resolve_sample(query)
        elements = samples_mod.load_elements(source)
        path = overlay_mod.overlay_path(self.golden_root, run, stem, view)
        document = overlay_mod.load_overlay(path)
        merged, applied = overlay_mod.apply(elements, document, include_deleted=True)
        stamp = overlay_mod.file_stamp(source, self.project_root)
        png = directory / (stem + ".png")
        width, height = samples_mod.png_size(png)
        meta = samples_mod.load_json(directory / (stem + ".meta.json"))
        visible = [item for item in merged
                   if (item.get("_golden") or {}).get("status") != "deleted"]
        return {
            "run": run,
            "stem": stem,
            "view": view,
            "image": {
                "width": width,
                "height": height,
                "bytes": png.stat().st_size if png.is_file() else None,
                "kinds": [
                    name
                    for name, suffix in samples_mod.IMAGE_KINDS.items()
                    if (directory / (stem + suffix)).is_file()
                ],
            },
            "views": [
                name
                for name, suffix in samples_mod.VIEWS.items()
                if (directory / (stem + suffix)).is_file()
            ],
            "elements": merged,
            "source": stamp,
            "overlay": document,
            # The compare-and-set token: send it back on save and a write that
            # landed in between is refused instead of silently overwritten.
            "overlay_version": overlay_mod.document_version(document),
            "applied": applied,
            "stale": overlay_mod.is_stale(document, stamp),
            "stats": samples_mod.summarise(visible),
            "audit": self.audits.rows_for(directory, stem),
            "meta": meta,
            "has_screentag": (directory / (stem + ".screentag.txt")).is_file(),
        }

    def _screentag(self, query, accept: str = "") -> Response:
        directory = self.run_dir(_require(query, "run"))
        stem = self.check_stem(_require(query, "stem"))
        path = directory / (stem + ".screentag.txt")
        if not path.is_file():
            raise HttpError(404, "no screentag for %s" % stem)
        if _param(query, "spans") != "1":
            return Response(
                200,
                {"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-cache"},
                path.read_bytes(),
            )
        return _json_response(self._screentag_index(directory, stem, path), 200, accept)

    def _screentag_index(self, directory: Path, stem: str, path: Path) -> Dict[str, Any]:
        """The tag text plus which character range each element produced.

        Built here rather than in the browser because the link is only sound if
        it comes from the same data the serializer consumed: the element list of
        the view `meta.json` names as the tag's source (always `leaf` so far),
        and the viewport those `<loc_>` tokens were normalised against. A client
        re-parsing the string would have to guess both.

        `unlinked` carries the reason when the spans could not be built, so the
        UI can say why the two panes are not talking to each other instead of
        silently offering a dead tag view.
        """
        text = path.read_text(encoding="utf-8", errors="replace")
        meta = samples_mod.load_json(directory / (stem + ".meta.json"))
        meta = meta if isinstance(meta, dict) else {}
        view = meta.get("screentag_source")
        if not isinstance(view, str) or view not in samples_mod.VIEWS:
            view = "leaf"
        width, height = self._screentag_viewport(directory, stem, meta)
        payload: Dict[str, Any] = {
            "run": self.rel_run(directory),
            "stem": stem,
            "view": view,
            "text": text,
            "viewport": {"width": width, "height": height},
            "grid": screentag_mod.LOC_GRID,
            "spans": [],
            "stats": {},
        }
        source = directory / (stem + samples_mod.VIEWS[view])
        if not width or not height:
            payload["unlinked"] = "no viewport size in meta.json and no readable PNG header"
            return payload
        if not source.is_file():
            payload["unlinked"] = "the %s element list this tag was serialized from is missing" % view
            return payload
        elements = samples_mod.load_elements(source)
        payload.update(
            screentag_mod.build_index(
                text, elements, width, height, overlay_mod.element_keys(elements)
            )
        )
        return payload

    @staticmethod
    def _screentag_viewport(
        directory: Path, stem: str, meta: Dict[str, Any]
    ) -> Tuple[Optional[int], Optional[int]]:
        """What `<loc_>` was normalised against: `meta.json`, else the PNG.

        The pipeline normalises by the session display size and screenshots that
        display whole, so the two agree on every capture in `v235_final3` and
        `v224_verify` (59 of 59). The PNG header is the fallback for a capture
        written before `viewport` was recorded.
        """
        viewport = meta.get("viewport")
        if isinstance(viewport, dict):
            width, height = viewport.get("width"), viewport.get("height")
            if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
                return width, height
        return samples_mod.png_size(directory / (stem + ".png"))

    # -- human audit -------------------------------------------------------

    def _need_audit(self) -> Path:
        if self.audit_root is None:
            raise HttpError(
                404,
                "this inspector was started without --audit; restart it with "
                "--audit <dir of campaigns> to run a human audit",
            )
        return self.audit_root

    def _campaign(self, name: str) -> "audit_mod.Campaign":
        """One Campaign object per name, shared across requests.

        Held rather than rebuilt because the label log is the campaign's state:
        rebuilding would re-read it on every keystroke, and the whole point of
        the append-only log is that a save costs no filesystem scan at all.
        """
        root = self._need_audit()
        name = audit_mod.safe_name(name, "campaign")
        with self._campaigns_lock:
            campaign = self._campaigns.get(name)
            if campaign is None:
                campaign = audit_mod.Campaign(root / name)
                if not campaign.exists():
                    raise HttpError(404, "no campaign %r under %s" % (name, root))
                self._campaigns[name] = campaign
            return campaign

    def _audit_route(self, action, method, query, body, headers, accept) -> Response:
        try:
            if action == "campaigns" and method == "GET":
                return _json_response(
                    {"campaigns": audit_mod.list_campaigns(self._need_audit()),
                     "rater": self.rater, "read_only": self.audit_read_only},
                    200, accept)
            if action == "queue" and method == "GET":
                return _json_response(self._audit_queue(query), 200, accept)
            if action == "item" and method == "GET":
                return self._audit_item(query, headers, accept)
            if action == "state" and method == "GET":
                return self._audit_state(query, accept)
            if action == "image" and method == "GET":
                return self._audit_image(query, headers)
            if action == "label" and method == "POST":
                return _json_response(self._audit_label(body), 200, accept)
            if action == "correction" and method == "POST":
                return _json_response(self._audit_correction(body), 200, accept)
        except audit_mod.AuditError as error:
            raise HttpError(error.status, error.message, error.extra)
        raise HttpError(404, "no audit route %r" % action)

    def _audit_queue(self, query) -> Dict[str, Any]:
        campaign = self._campaign(_require(query, "campaign"))
        rater = self._audit_rater(query)
        only_mine = _param(query, "mine", "1") != "0"
        start = max(0, _int_param(query, "from") or 0)
        limit = max(1, min(_int_param(query, "limit") or 2000, 5000))
        state_rater = rater if only_mine else None
        rows = []
        for index in range(start, min(start + limit, len(campaign.queue))):
            item = campaign.queue[index]
            state = campaign.item_state(index, state_rater)
            rows.append({
                "index": index,
                "observation_key": item.get("observation_key"),
                "stratum": item.get("stratum"),
                "apps": item.get("apps"),
                "n_elements": item.get("n_elements"),
                "occluded_ratio": item.get("occluded_ratio"),
                "draw": item.get("draw"),
                "status": (state["item"] or {}).get("status"),
                "answered": len(state["screen"]) + len(state["elements"]),
                "missed": len(state["missed"]),
            })
        return {
            "campaign": campaign.name,
            "title": campaign.manifest.get("title"),
            "rubric": campaign.rubric,
            "rubric_ref": audit_mod.rubric_ref(campaign.rubric),
            "kind": campaign.kind,
            "mode": campaign.mode,
            "grounding": bool(campaign.manifest.get("grounding")),
            "element_sample": campaign.manifest.get("element_sample"),
            "rater": rater,
            "read_only": self.audit_read_only,
            "from": start,
            "items": rows,
            "progress": campaign.progress(state_rater),
            "torn_bytes": campaign.torn_bytes,
        }

    def _audit_rater(self, query) -> str:
        return audit_mod.safe_name(_param(query, "rater") or self.rater, "rater")

    def _audit_paths(self, campaign, index: int,
                     which: str = "before") -> Tuple[Path, Path, Path]:
        """The three files an item is made of, resolved under the corpus root.

        `which="after"` is the screen an instruction sample's click produced -
        the only way to check that an instruction promising something actually
        describes what happened.
        """
        browser = self._need_corpus()
        item = campaign.item(index)
        field = "after_source_path" if which == "after" else "source_path"
        relative = str(item.get(field) or "")
        if not relative:
            raise HttpError(404, "item %d has no %s screen" % (index, which))
        base = browser.capture_path(relative)
        return (Path(str(base) + ".png"),
                Path(str(base) + samples_mod.VIEWS["leaf"]),
                Path(str(base) + ".screentag.txt"))

    def _leaf(self, path: Path) -> List[Dict[str, Any]]:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        with self._leaves_lock:
            cached = self._leaves.get(key)
            if cached is not None:
                self._leaves.move_to_end(key)
                return cached
        elements = samples_mod.load_elements(path)
        with self._leaves_lock:
            self._leaves[key] = elements
            while len(self._leaves) > LEAF_CACHE_SIZE:
                self._leaves.popitem(last=False)
        return elements

    def _audit_item(self, query, headers, accept) -> Response:
        campaign = self._campaign(_require(query, "campaign"))
        index = _int_param(query, "index")
        if index is None:
            index = campaign.index_of(_require(query, "key"))
        rater = self._audit_rater(query)
        item = campaign.item(index)
        png, leaf, tag = self._audit_paths(campaign, index)
        if not leaf.is_file():
            raise HttpError(404, "no element list for %s" % item.get("observation_key"))

        # The item's identity is the capture's, so a browser may cache it and
        # revalidate. The *answers* are deliberately not in this payload: they
        # change on every keystroke, and mixing them in would make the whole
        # thing uncacheable and turn prefetching the next three items into
        # three downloads that can never be reused. `/api/audit/state` serves
        # them separately.
        stat = leaf.stat()
        etag = '"%s"' % hashlib.sha1(
            ("audit|%s|%s|%s|%s|%s" % (campaign.name, index, stat.st_mtime_ns,
                                       stat.st_size, audit_mod.rubric_ref(campaign.rubric))
             ).encode("utf-8")).hexdigest()[:24]
        if headers.get("if-none-match") == etag:
            return Response(304, {"ETag": etag, "Cache-Control": "no-cache"})

        elements = self._leaf(leaf)
        projected = audit_mod.project_elements(elements)
        sampled = [str(key) for key in (item.get("element_keys") or [])]
        by_key = {row["key"]: row for row in projected}
        missing = [key for key in sampled if key not in by_key]
        payload = {
            "campaign": campaign.name,
            "index": index,
            "kind": campaign.kind,
            "count": len(campaign.queue),
            "item": item,
            "rater": rater,
            "read_only": self.audit_read_only,
            "elements": projected,
            "sampled": sampled,
            "sampled_missing": missing,
            "screentag": self._read_text(tag),
            "automated": {
                "verdict": samples_mod.load_json(
                    png.with_name(png.name.replace(".png", ".verdict.json"))),
                "shard_pixel_audit": self._shard_pixel_audit(item),
            },
        }
        response = _json_response(payload, 200, accept, cache="no-cache")
        response.headers["ETag"] = etag
        return response

    def _audit_state(self, query, accept) -> Response:
        campaign = self._campaign(_require(query, "campaign"))
        rater = self._audit_rater(query)
        raw = _param(query, "indices", "") or ""
        try:
            indices = [int(part) for part in raw.split(",") if part.strip() != ""]
        except ValueError:
            raise HttpError(400, "indices must be a comma-separated list of integers")
        if not indices:
            index = _int_param(query, "index")
            indices = [index] if index is not None else []
        payload = {
            "campaign": campaign.name,
            "rater": rater,
            "states": {str(index): self._audit_state_payload(campaign, index, rater)
                       for index in indices},
            "progress": campaign.progress(rater),
        }
        return _json_response(payload, 200, accept)

    def _audit_state_payload(self, campaign, index: int, rater: str) -> Dict[str, Any]:
        mine = campaign.item_state(index, rater)
        everyone = campaign.item_state(index, None)
        others = sorted({
            str(row.get("rater")) for row in campaign.labels()
            if row.get("index") == index and row.get("rater") != rater
        })
        return {
            "screen": {key: _label_answer(row) for key, row in mine["screen"].items()},
            "elements": {key: _label_answer(row) for key, row in mine["elements"].items()},
            "missed": mine["missed"],
            "sweep": mine["sweep"],
            "ground": mine["ground"],
            "item": mine["item"],
            "other_raters": others,
            "any_rater_answers": len(everyone["screen"]) + len(everyone["elements"]),
        }

    def _shard_pixel_audit(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The shard's automated pixel audit, trimmed to its headline rates.

        Shown collapsed in the UI. It is the automated estimate of the same
        thing the human is judging, so putting it in front of them before they
        answer would anchor the judgement instead of testing it.
        """
        browser = self.corpus
        shard = item.get("shard")
        if browser is None or not shard:
            return None
        path = browser.root / "shards" / str(shard) / "audit" / "pixel_audit.json"
        payload = samples_mod.load_json(path)
        if not isinstance(payload, dict):
            return None
        return {key: payload[key] for key in
                ("captures", "text_elements", "text_phantoms", "text_drifted",
                 "phantom_rate", "drift_rate", "ink_uncovered_mean", "ink_uncovered_p90")
                if key in payload}

    @staticmethod
    def _read_text(path: Path) -> Optional[str]:
        try:
            return path.read_text(encoding="utf-8") if path.is_file() else None
        except OSError:
            return None

    def _audit_image(self, query, headers) -> Response:
        """The screenshot for one queue item, through the cached image path.

        Deliberately addressed by campaign and index rather than by path: the
        client never names a file, so there is nothing to escape with, and the
        bytes are the same ETag-and-cache path the runs page already uses -
        86KB of WebP for a 4K frame rather than the 2.9MB the corpus figure
        route sends and forbids caching.
        """
        campaign = self._campaign(_require(query, "campaign"))
        index = _int_param(query, "index")
        if index is None:
            index = campaign.index_of(_require(query, "key"))
        which = _param(query, "which", "before") or "before"
        if which not in ("before", "after"):
            raise HttpError(400, "which is before or after")
        png, _, _ = self._audit_paths(campaign, index, which)
        if not png.is_file():
            raise HttpError(404, "no %s screenshot for item %d" % (which, index))
        return self._image_response(png, query, headers,
                                    default_format="webp")

    def _audit_label(self, body: Optional[bytes]) -> Dict[str, Any]:
        """One answer, or several in one request.

        `answers` exists because accepting a sample is five judgements and an
        item-done row, and six sequential round trips per item is exactly the
        latency this page is built to avoid. The log still gets one row per
        answer - the data model does not change, so the reductions, the
        statistics and the agreement code need no special case for it.
        """
        if self.audit_read_only:
            raise HttpError(403, "audit labelling is disabled (--audit-read-only)")
        payload = _json_body(body)
        campaign = self._campaign(str(payload.get("campaign") or ""))
        index = payload.get("index")
        if not isinstance(index, int):
            raise HttpError(400, "index must be an integer")
        rater = audit_mod.safe_name(str(payload.get("rater") or self.rater), "rater")
        expected = payload.get("rubric_ref")
        actual = audit_mod.rubric_ref(campaign.rubric)
        if expected and expected != actual:
            raise HttpError(409, "this page was loaded against rubric %s but the "
                                 "campaign is on %s; reload before answering"
                            % (expected, actual))

        answers = payload.get("answers")
        if answers is None:
            answers = [payload]
        elif not isinstance(answers, list) or not answers:
            raise HttpError(400, "answers must be a non-empty list")
        if len(answers) > 64:
            raise HttpError(400, "at most 64 answers in one request")

        rows = []
        for position, answer in enumerate(answers):
            if not isinstance(answer, dict):
                raise HttpError(400, "answer %d is not an object" % position)
            merged = dict(answer)
            merged.setdefault("index", index)
            if merged.get("index") != index:
                raise HttpError(400, "answer %d is for item %r, not %r"
                                % (position, merged.get("index"), index))
            # Validate every one before writing any: a batch that failed
            # halfway would leave an item answered in part, which is worse
            # than an error.
            rows.append(audit_mod.build_label_row(campaign, index, merged, rater))

        stored = 0
        duplicate = 0
        for row in rows:
            result = campaign.append_label(row)
            stored += 1 if result["stored"] else 0
            duplicate += 1 if result["duplicate"] else 0
        return {
            "stored": stored > 0,
            "written": stored,
            "duplicate": duplicate,
            "index": index,
            "state": self._audit_state_payload(campaign, index, rater),
            "progress": campaign.progress(rater),
        }

    def _audit_correction(self, body: Optional[bytes]) -> Dict[str, Any]:
        """Store a hand correction beside the judgement, never in the corpus.

        Reuses the `deskshot.golden/1` document and its whitelist so a
        correction made here is the same artefact the runs page produces, and
        so a corrected annotation can later be exported as authoritative
        without a second format to reconcile.
        """
        if self.audit_read_only:
            raise HttpError(403, "audit labelling is disabled (--audit-read-only)")
        payload = _json_body(body)
        campaign = self._campaign(str(payload.get("campaign") or ""))
        index = payload.get("index")
        if not isinstance(index, int):
            raise HttpError(400, "index must be an integer")
        item = campaign.item(index)
        rater = audit_mod.safe_name(str(payload.get("rater") or self.rater), "rater")
        _, leaf, _ = self._audit_paths(campaign, index)
        edits = overlay_mod.normalise_edits(payload.get("edits"))
        mark = overlay_mod.normalise_mark(payload.get("mark"))
        path = campaign.correction_path(str(item.get("observation_key")))
        existing = overlay_mod.load_overlay(path)
        self._check_version(payload, existing, path)
        cleared = overlay_mod.clear_stale_fragments(edits, self._leaf(leaf))
        stamp = {
            "path": str(item.get("source_path")) + samples_mod.VIEWS["leaf"],
            "sha256": ((item.get("digests") or {}).get("leaf") or {}).get("sha256"),
            "size": ((item.get("digests") or {}).get("leaf") or {}).get("bytes"),
        }
        document = overlay_mod.build_document(
            campaign.name, str(item.get("observation_key")), "leaf", stamp,
            edits, mark, author=rater, existing=existing,
        )
        if overlay_mod.is_empty(document):
            removed = overlay_mod.delete_overlay(path, campaign.corrections_root)
            return {"saved": False, "removed": removed, "path": str(path),
                    "overlay_version": None, "fragments_cleared": cleared}
        overlay_mod.save_overlay(path, document)
        return {"saved": True, "path": str(path), "overlay": document,
                "overlay_version": overlay_mod.document_version(document),
                "fragments_cleared": cleared}

    # -- corpus -----------------------------------------------------------

    def _need_corpus(self) -> "corpus_mod.CorpusBrowser":
        if self.corpus is None:
            raise HttpError(
                404,
                "this inspector was started without --corpus; restart it with "
                "--corpus <corpus root> to browse a generated corpus",
            )
        return self.corpus

    def _corpus_route(self, action, query, headers, accept) -> Response:
        browser = self._need_corpus()
        try:
            if action == "ls":
                return _json_response(browser.ls(
                    _param(query, "path", "") or "",
                    _int_param(query, "offset") or 0,
                    _int_param(query, "limit") or corpus_mod.DEFAULT_LIMIT,
                ), 200, accept)
            if action == "sample":
                return _json_response(browser.sample(
                    _require(query, "path"), _param(query, "view", "leaf") or "leaf",
                ), 200, accept)
            if action == "facets":
                return _json_response(browser.facets(), 200, accept)
            if action == "search":
                return _json_response(browser.search(
                    split=_param(query, "split") or None,
                    app=_param(query, "app") or None,
                    theme=_param(query, "theme") or None,
                    resolution=_param(query, "resolution") or None,
                    min_occlusion=_float_param(query, "min_occlusion"),
                    min_windows=_int_param(query, "min_windows"),
                    limit=_int_param(query, "limit") or 60,
                    random_order=_param(query, "random") == "1",
                    seed=_int_param(query, "seed") or 0,
                ), 200, accept)
            if action == "figure":
                return self._corpus_figure(browser, query, headers)
        except corpus_mod.CorpusError as error:
            raise HttpError(error.status, error.message)
        raise HttpError(404, "no corpus route %r" % action)

    def _corpus_figure(self, browser, query, headers) -> Response:
        """Render a capture on demand and hand back the bytes.

        Nothing is written: a corpus of 1.2M captures cannot have a rendered
        copy of itself sitting beside it, and a figure that exists only for as
        long as somebody is looking at it cannot go stale against the
        annotation it was drawn from.
        """
        from deskshot.figures.render import STYLES, figure as build_figure, load_capture

        target = browser.capture_path(_require(query, "path"))
        mode = _param(query, "mode", "overlay") or "overlay"
        if mode not in ("overlay", "pair", "zoom", "tag"):
            raise HttpError(400, "unknown figure mode %r" % mode)
        style = _param(query, "style", "paper") or "paper"
        if style not in STYLES:
            raise HttpError(400, "unknown style %r" % style)
        view = _param(query, "view", "leaf") or "leaf"
        width = max(320, min(_int_param(query, "width") or 1600, 4000))
        labels = _param(query, "labels", "auto") or "auto"
        if labels not in ("none", "auto", "all"):
            raise HttpError(400, "unknown labels %r" % labels)

        stat = target.with_name(target.name + ".png").stat()
        signature = "%s|%s|%s|%s|%s|%s|%s|%s" % (
            target, stat.st_mtime_ns, mode, style, view, width, labels,
            _param(query, "family", "") or "")
        etag = '"%s"' % hashlib.sha1(signature.encode("utf-8")).hexdigest()[:24]
        if headers.get("if-none-match") == etag:
            return Response(304, {"ETag": etag, "Cache-Control": "no-store"})

        try:
            capture = load_capture(target, view=view)
        except (FileNotFoundError, OSError) as error:
            raise HttpError(404, str(error))
        families = [f for f in (_param(query, "family", "") or "").split(",") if f]
        image = build_figure(
            [capture], mode=mode, style=STYLES[style], width=width,
            labels=labels, max_labels=_int_param(query, "max_labels") or 40,
            show_occluded=_param(query, "occlusion", "1") != "0",
            families=families or None,
            legend=_param(query, "legend", "1") != "0",
            zoom_factor=_float_param(query, "zoom_factor") or 3.0,
        )
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", compress_level=1)
        body = buffer.getvalue()
        return Response(200, {
            "Content-Type": "image/png",
            "Content-Length": str(len(body)),
            "ETag": etag,
            # Deliberately not cached to disk anywhere: this is a view of the
            # annotation, and the annotation is the artefact.
            "Cache-Control": "no-store",
        }, body)

    def _golden(self) -> Dict[str, Any]:
        entries = []
        for path, document in overlay_mod.iter_overlays(self.golden_root):
            sample = document.get("sample") or {}
            edits = document.get("edits") or {}
            entries.append(
                {
                    "path": os.path.relpath(str(path), str(Path(self.golden_root).parent)),
                    "run": sample.get("run"),
                    "stem": sample.get("stem"),
                    "view": sample.get("view"),
                    "mark": document.get("mark") or {},
                    "updated_at": document.get("updated_at"),
                    "updated_by": document.get("updated_by"),
                    "counts": {
                        "modified": len(edits.get("modified") or {}),
                        "deleted": len(edits.get("deleted") or []),
                        "added": len(edits.get("added") or []),
                    },
                }
            )
        entries.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return {"overlays": entries, "root": str(self.golden_root)}

    # -- writes -----------------------------------------------------------

    def _save_overlay(self, body: Optional[bytes]) -> Dict[str, Any]:
        if self.read_only:
            raise HttpError(403, "server started read-only")
        try:
            payload = json.loads((body or b"").decode("utf-8"))
        except ValueError:
            raise HttpError(400, "body must be JSON")
        if not isinstance(payload, dict):
            raise HttpError(400, "body must be a JSON object")
        query = {key: [str(payload.get(key) or "")] for key in ("run", "stem", "view")}
        if not query["view"][0]:
            query["view"] = ["leaf"]
        directory, run, stem, view, source = self._resolve_sample(query)

        edits = overlay_mod.normalise_edits(payload.get("edits"))
        mark = overlay_mod.normalise_mark(payload.get("mark"))
        path = overlay_mod.overlay_path(self.golden_root, run, stem, view)
        existing = overlay_mod.load_overlay(path)
        self._check_version(payload, existing, path)

        # An edited rect makes the fragments computed for the old one wrong,
        # and the coverage audit reads `rect` UNION `visible_fragments`. Doing
        # it here rather than in the browser means a curl client cannot write a
        # self-contradicting overlay either.
        cleared = overlay_mod.clear_stale_fragments(
            edits, samples_mod.load_elements(source)
        )
        stamp = overlay_mod.file_stamp(source, self.project_root)
        document = overlay_mod.build_document(
            run, stem, view, stamp, edits, mark,
            author=str(payload.get("author") or self.author),
            existing=existing,
        )
        if overlay_mod.is_empty(document):
            # Storing "no opinion" as a file would fill golden/ with noise and
            # make a curated set indistinguishable from a browsed one.
            removed = overlay_mod.delete_overlay(path, self.golden_root)
            return {"saved": False, "removed": removed, "path": str(path),
                    "overlay_version": None, "fragments_cleared": cleared}
        overlay_mod.save_overlay(path, document)
        return {"saved": True, "path": str(path), "overlay": document,
                "overlay_version": overlay_mod.document_version(document),
                "fragments_cleared": cleared}

    @staticmethod
    def _check_version(payload, existing, path) -> None:
        """Refuse a save built on an overlay somebody else has already replaced.

        `base_version` is what `/api/sample` handed out when the editor opened
        the sample. If the file on disk no longer hashes to that, a second
        editor saved in between and accepting this write would delete their work
        with no trace. A request that carries no `base_version` at all is a
        script rather than the UI and is allowed through - the response always
        reports the resulting version, so a script can adopt the protocol.
        """
        if "base_version" not in payload:
            return
        base = payload.get("base_version")
        current = overlay_mod.document_version(existing)
        if base == current:
            return
        if current is None:
            message = ("this overlay was deleted while you were editing it; "
                       "your edits are unsaved")
        else:
            who = (existing or {}).get("updated_by") or "someone"
            when = (existing or {}).get("updated_at") or "?"
            message = ("%s saved this overlay at %s while you were editing it - "
                       "nothing was written" % (who, when))
        raise HttpError(409, message, {
            "conflict": {
                "base_version": base,
                "current_version": current,
                "current": existing,
                "path": str(path),
            }
        })

    def _delete_overlay(self, query) -> Dict[str, Any]:
        if self.read_only:
            raise HttpError(403, "server started read-only")
        directory = self.run_dir(_require(query, "run"))
        stem = self.check_stem(_require(query, "stem"))
        view = _param(query, "view", "leaf")
        path = overlay_mod.overlay_path(self.golden_root, self.rel_run(directory), stem, view)
        return {"removed": overlay_mod.delete_overlay(path, self.golden_root), "path": str(path)}

    # -- images -----------------------------------------------------------

    def _image(self, query, headers) -> Response:
        directory = self.run_dir(_require(query, "run"))
        stem = self.check_stem(_require(query, "stem"))
        kind = _param(query, "kind", "screenshot")
        if kind not in samples_mod.IMAGE_KINDS:
            raise HttpError(400, "unknown image kind %r" % kind)
        path = directory / (stem + samples_mod.IMAGE_KINDS[kind])
        if not path.is_file():
            raise HttpError(404, "no %s image for %s" % (kind, stem))
        return self._image_response(path, query, headers)

    def _image_response(self, path: Path, query, headers,
                        default_format: str = DEFAULT_IMAGE_FORMAT) -> Response:
        """Crop, cap and re-encode one file, with an ETag it can be cached by.

        Shared by the runs page and the audit page so there is one image path
        with one cache, rather than the audit flow growing a second one that
        would have to be optimised separately.
        """
        region = [
            _int_param(query, "x"), _int_param(query, "y"),
            _int_param(query, "w"), _int_param(query, "h"),
        ]
        cap = _int_param(query, "max")
        fmt = (_param(query, "fmt", default_format) or default_format).lower()
        if fmt not in IMAGE_FORMATS:
            raise HttpError(400, "unknown image format %r" % fmt)
        stat = path.stat()
        signature = "%s|%s|%s|%s|%s|%s" % (path, stat.st_mtime_ns, stat.st_size, region, cap, fmt)
        etag = '"%s"' % hashlib.sha1(signature.encode("utf-8")).hexdigest()[:24]
        if headers.get("if-none-match") == etag:
            return Response(304, {"ETag": etag, "Cache-Control": "max-age=31536000"})

        common = {
            "Content-Type": IMAGE_FORMATS[fmt][1],
            "ETag": etag,
            # Every generated variant has the file's mtime in its ETag, so a
            # regenerated capture gets a new URL identity rather than a stale hit.
            "Cache-Control": "max-age=31536000",
        }
        if all(value is None for value in region) and cap is None and fmt == "png":
            common["Content-Length"] = str(stat.st_size)
            return Response(200, common, path=path)

        body = self._render(path, region, cap, etag, fmt)
        common["Content-Length"] = str(len(body))
        return Response(200, common, body)

    def _frame(self, path: Path):
        """The decoded frame, from memory when the same file was just used.

        The decode happens under a lock *for that file* rather than under the
        cache lock: holding the cache lock would serialise every request in the
        process behind one 105ms decode, while holding nothing at all means six
        parallel requests for the same capture - which is exactly what the
        viewer does when it asks for a frame and its crops - each decode it
        independently and then overwrite each other in the cache.
        """
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        with self._frames_lock:
            frame = self._frames.get(key)
            if frame is not None:
                self._frames.move_to_end(key)
                return frame
            decode_lock = self._decodes.get(key)
            if decode_lock is None:
                decode_lock = self._decodes[key] = threading.Lock()
        with decode_lock:
            with self._frames_lock:
                frame = self._frames.get(key)
                if frame is not None:
                    self._frames.move_to_end(key)
                    return frame
            with Image.open(str(path)) as handle:
                # Convert eagerly: every consumer wants RGB, and doing it once
                # here keeps the cached frame directly croppable.
                frame = handle.convert("RGB")
        with self._frames_lock:
            self._frames[key] = frame
            self._decodes.pop(key, None)
            while len(self._frames) > FRAME_CACHE_SIZE:
                self._frames.popitem(last=False)
        return frame

    def _render(self, path: Path, region, cap: Optional[int], etag: str, fmt: str) -> bytes:
        if Image is None:
            raise HttpError(501, "Pillow is not available; crops and thumbnails are disabled")
        cached = self._cache_path(etag, fmt)
        if cached is not None and cached.is_file():
            return cached.read_bytes()

        image = self._frame(path)
        width, height = image.size
        x, y, w, h = region
        if any(value is not None for value in region):
            x = max(0, min(int(x or 0), width - 1))
            y = max(0, min(int(y or 0), height - 1))
            w = int(w) if w else width - x
            h = int(h) if h else height - y
            w = max(1, min(w, width - x))
            h = max(1, min(h, height - y))
            if w * h > MAX_REGION_PIXELS:
                raise HttpError(400, "requested region is larger than a full frame")
            image = image.crop((x, y, x + w, y + h))
        if cap:
            cap = max(16, min(int(cap), 8192))
            longest = max(image.size)
            if longest > cap:
                scale = float(cap) / longest
                image = image.resize(
                    (max(1, int(image.size[0] * scale)), max(1, int(image.size[1] * scale))),
                    Image.BILINEAR,
                )
        buffer = io.BytesIO()
        pillow_format, _, options = IMAGE_FORMATS[fmt]
        image.save(buffer, pillow_format, **options)
        body = buffer.getvalue()
        if cached is not None:
            try:
                cached.parent.mkdir(parents=True, exist_ok=True)
                temporary = cached.with_name(cached.name + ".tmp%d" % os.getpid())
                temporary.write_bytes(body)
                os.replace(str(temporary), str(cached))
                self._prune_image_cache(cached.parent)
            except OSError:
                pass
        return body

    def _prune_image_cache(self, directory: Path) -> None:
        """Keep the generated-image cache under its ceiling, oldest out first.

        Nothing pruned this before. One variant is ~72KB and a campaign over a
        1.2M-capture corpus generates one per item per zoom step, so an
        unbounded cache is a slow disk leak on a shared filesystem rather than
        a cache.
        """
        if not self._cache_bytes_max:
            return
        self._prune_counter += 1
        if self._prune_counter % IMAGE_CACHE_PRUNE_EVERY != 0:
            return
        entries = []
        total = 0
        for entry in directory.iterdir():
            if not entry.is_file() or entry.name.startswith("."):
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, entry))
            total += stat.st_size
        if total <= self._cache_bytes_max:
            return
        entries.sort()
        for _, size, entry in entries:
            if total <= self._cache_bytes_max:
                break
            try:
                entry.unlink()
            except OSError:
                continue
            total -= size

    def _cache_path(self, etag: str, fmt: str = DEFAULT_IMAGE_FORMAT) -> Optional[Path]:
        if not self.cache_dir:
            return None
        return self.cache_dir / THUMBNAIL_CACHE_DIRNAME / ("%s.%s" % (etag.strip('"'), fmt))


def parse_target(target: str) -> Tuple[str, Dict[str, List[str]]]:
    """Split a request target into path and query, keeping repeated keys."""
    if "?" in target:
        path, _, raw = target.partition("?")
        return unquote(path), parse_qs(raw, keep_blank_values=True)
    return unquote(target), {}
