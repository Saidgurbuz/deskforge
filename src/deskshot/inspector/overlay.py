"""Annotation edits that survive the next pipeline run.

`incremental_checks/` is regenerated and gitignored, so a correction written
back into a run's JSON is gone the next time that batch runs and was never in
git to begin with. Corrections therefore live in a separate overlay document
under `golden/`, one per sample, and reading a sample means "pipeline output
with the overlay applied on top". The overlay is the artifact that gets
committed; the capture it describes is disposable.

Each overlay carries a provenance stamp of the file it was made against - path,
sha256, size, mtime. It deliberately does *not* try to re-attach edits when the
capture underneath changes: it reports itself stale and leaves the judgement to
a person, because a rect drawn against different pixels is not obviously still
correct.

Elements are addressed by `uid` where the pipeline emits one and by
(app, dom index, source) where it does not - 705 of 5,201 leaf elements in
`v224_verify` carry no uid at all. That pair is unique in every leaf and amodal
file measured; the unfiltered view repeats it for desktop-chrome duplicates, so
a repeat gets an occurrence ordinal appended rather than silently colliding.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

SCHEMA = "deskshot.golden/1"

#: Fields an edit may set. The whitelist is not paranoia about a hostile client
#: - it is what keeps an overlay reviewable in a diff. Anything outside it is a
#: derived or bookkeeping field (`_dom_index`, reading order) that a human edit
#: cannot keep consistent by hand.
EDITABLE_FIELDS = frozenset({
    "rect",
    "visible_fragments",
    "role",
    "tag",
    "type",
    "name",
    "inner_text",
    "visible_text",
    "visible_text_status",
    "is_occluded",
    "app_name",
    "golden_note",
})

#: Fields an added element must carry: without a box and a role it is not an
#: annotation, it is a note.
REQUIRED_ADD_FIELDS = ("rect", "role")

MARK_STATUSES = ("unmarked", "verified", "needs_work", "rejected")

#: History is a breadcrumb trail for a curated set, not an audit log. Keeping
#: it bounded stops a session of nudging one box from producing a 4MB overlay.
HISTORY_LIMIT = 50


class OverlayError(ValueError):
    """A rejected edit payload. Carries a message meant for the user."""


# --------------------------------------------------------------------------
# element identity


def element_key(element: Dict[str, Any]) -> str:
    """Address one element in a capture, stably across regeneration."""
    uid = element.get("uid")
    if uid:
        return "uid:%s" % uid
    return "dom:%s:%s:%s" % (
        element.get("app_name") or "?",
        element.get("_dom_index"),
        element.get("source") or "?",
    )


def element_keys(elements: Sequence[Dict[str, Any]]) -> List[str]:
    """Keys for a whole capture, disambiguated by occurrence where they repeat."""
    seen: Dict[str, int] = {}
    keys: List[str] = []
    for element in elements:
        base = element_key(element)
        count = seen.get(base, 0)
        seen[base] = count + 1
        keys.append(base if count == 0 else "%s#%d" % (base, count))
    return keys


# --------------------------------------------------------------------------
# provenance


def file_stamp(path: Path, base: Optional[Path] = None) -> Dict[str, Any]:
    """Identify a source file well enough to detect that it was regenerated."""
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    shown = str(path)
    if base is not None:
        try:
            shown = str(path.resolve().relative_to(Path(base).resolve()))
        except ValueError:
            pass
    return {
        "path": shown,
        "sha256": digest.hexdigest(),
        "size": stat.st_size,
        "mtime": round(stat.st_mtime, 3),
    }


def is_stale(overlay: Optional[Dict[str, Any]], stamp: Dict[str, Any]) -> bool:
    """True when the capture changed under an overlay that has something to say."""
    if not overlay:
        return False
    recorded = (overlay.get("source") or {}).get("sha256")
    if not recorded:
        return False
    return recorded != stamp.get("sha256")


# --------------------------------------------------------------------------
# storage


def overlay_path(golden_root: Path, run: str, stem: str, view: str) -> Path:
    """Mirror the run's directory layout so overlays are findable by hand."""
    return Path(golden_root) / run / ("%s.%s.json" % (stem, view))


def document_version(document: Optional[Dict[str, Any]]) -> Optional[str]:
    """A short content hash of a stored overlay, or None when there is none.

    This is the compare-and-set token: a client reads a sample, is told the
    version it is editing, and sends it back when it saves. Content addressing
    rather than an mtime because two saves inside the same second are exactly
    the case that loses work, and because a rewritten-but-identical file should
    not be reported as somebody else's change.
    """
    if not document:
        return None
    payload = json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def load_overlay(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_overlay(path: Path, document: Dict[str, Any]) -> None:
    """Write atomically - a half-written overlay would lose real curation work."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=".tmp-", suffix=".json", delete=False
    )
    try:
        json.dump(document, handle, indent=2, ensure_ascii=False, sort_keys=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(handle.name, str(path))
    except BaseException:
        handle.close()
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def delete_overlay(path: Path, golden_root: Path) -> bool:
    """Remove an overlay and any directories it was the last occupant of."""
    if not path.is_file():
        return False
    path.unlink()
    root = Path(golden_root).resolve()
    parent = path.parent.resolve()
    while parent != root and root in parent.parents:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
    return True


def iter_overlays(golden_root: Path) -> Iterator[Tuple[Path, Dict[str, Any]]]:
    root = Path(golden_root)
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(document, dict) and document.get("schema") == SCHEMA:
            yield path, document


# --------------------------------------------------------------------------
# validation


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def normalise_rect(value: Any) -> Dict[str, int]:
    if not isinstance(value, dict):
        raise OverlayError("rect must be an object with x, y, w, h")
    out = {}
    for field in ("x", "y", "w", "h"):
        raw = value.get(field)
        if not _is_number(raw):
            raise OverlayError("rect.%s must be a number" % field)
        out[field] = int(round(raw))
    if out["w"] <= 0 or out["h"] <= 0:
        raise OverlayError("rect must have positive width and height")
    return out


def normalise_fragments(value: Any) -> List[Dict[str, int]]:
    """The visible pieces of a partly occluded element, as a list of rects.

    An empty list is meaningful and is the common stored value: it says "this
    element has no fragment geometry beyond its rect", which is what a
    hand-drawn box asserts.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise OverlayError("visible_fragments must be a list of rects")
    return [normalise_rect(item) for item in value]


def _normalise_fields(raw: Any, where: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise OverlayError("%s must be an object" % where)
    out: Dict[str, Any] = {}
    for field, value in raw.items():
        if field not in EDITABLE_FIELDS:
            raise OverlayError("%s: field %r is not editable" % (where, field))
        if field == "rect":
            out[field] = normalise_rect(value)
        elif field == "visible_fragments":
            out[field] = normalise_fragments(value)
        elif field == "is_occluded":
            out[field] = bool(value)
        elif value is None or isinstance(value, str) or _is_number(value):
            out[field] = value
        else:
            raise OverlayError("%s: field %r must be a string, number or null" % (where, field))
    return out


def normalise_edits(raw: Any) -> Dict[str, Any]:
    """Coerce a client edit payload into the stored shape, or reject it."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise OverlayError("edits must be an object")

    modified_raw = raw.get("modified") or {}
    if not isinstance(modified_raw, dict):
        raise OverlayError("edits.modified must be an object keyed by element key")
    modified: Dict[str, Any] = {}
    for key, fields in modified_raw.items():
        if not isinstance(key, str) or not key:
            raise OverlayError("edits.modified keys must be non-empty strings")
        clean = _normalise_fields(fields, "edits.modified[%s]" % key)
        if clean:
            modified[key] = clean

    deleted_raw = raw.get("deleted") or []
    if not isinstance(deleted_raw, list):
        raise OverlayError("edits.deleted must be a list of element keys")
    deleted: List[str] = []
    for key in deleted_raw:
        if not isinstance(key, str) or not key:
            raise OverlayError("edits.deleted entries must be non-empty strings")
        if key not in deleted:
            deleted.append(key)

    added_raw = raw.get("added") or []
    if not isinstance(added_raw, list):
        raise OverlayError("edits.added must be a list of elements")
    added: List[Dict[str, Any]] = []
    used_ids = set()
    for position, element in enumerate(added_raw):
        if not isinstance(element, dict):
            raise OverlayError("edits.added entries must be objects")
        fields = dict(element)
        golden_id = fields.pop("golden_id", None)
        clean = _normalise_fields(fields, "edits.added[%d]" % position)
        for required in REQUIRED_ADD_FIELDS:
            if required not in clean:
                raise OverlayError("edits.added[%d] needs a %s" % (position, required))
        if not isinstance(golden_id, str) or not golden_id:
            golden_id = "add-%d" % (position + 1)
        if golden_id in used_ids:
            raise OverlayError("edits.added: duplicate golden_id %r" % golden_id)
        used_ids.add(golden_id)
        clean["golden_id"] = golden_id
        added.append(clean)

    return {"modified": modified, "deleted": deleted, "added": added}


def clear_stale_fragments(
    edits: Dict[str, Any], elements: Sequence[Dict[str, Any]]
) -> List[str]:
    """A hand-moved rect invalidates the fragments computed for the old one.

    `visible_fragments` is where the occlusion stage puts the *other* visible
    pieces of a partly hidden element, and `rect` is only the largest of them.
    The coverage audit therefore measures `rect` UNION `visible_fragments`, so
    leaving the old fragments beside a new rect does not merely look wrong - it
    keeps crediting the annotation with area a person just said it does not
    cover, and does it silently.

    Clipping the fragments to the new rect would be worse than useless: the
    fragments that matter are precisely the ones *outside* the rect, so a 1px
    nudge would delete them. So the rule is invalidation, not repair - editing a
    rect means "this box is what is visible", and any fragment geometry that
    came from the old box is dropped. The original list is kept in
    `_golden.original` by :func:`apply`, so the edit is still reversible, and an
    edit that sets `visible_fragments` explicitly is left exactly as sent.

    Returns the keys it touched, so a caller can report them.
    """
    modified = edits.get("modified") or {}
    if not modified:
        return []
    by_key = dict(zip(element_keys(elements), elements))
    touched: List[str] = []
    for key, fields in modified.items():
        if "rect" not in fields or "visible_fragments" in fields:
            continue
        element = by_key.get(key)
        if element is None:  # a stale edit matching nothing: nothing to invalidate
            continue
        if element.get("visible_fragments"):
            fields["visible_fragments"] = []
            touched.append(key)
    return sorted(touched)


def normalise_mark(raw: Any) -> Dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise OverlayError("mark must be an object")
    status = raw.get("status") or "unmarked"
    if status not in MARK_STATUSES:
        raise OverlayError("mark.status must be one of %s" % ", ".join(MARK_STATUSES))
    tags_raw = raw.get("tags") or []
    if not isinstance(tags_raw, list):
        raise OverlayError("mark.tags must be a list of strings")
    tags = []
    for tag in tags_raw:
        if not isinstance(tag, str):
            raise OverlayError("mark.tags entries must be strings")
        tag = tag.strip()
        if tag and tag not in tags:
            tags.append(tag)
    note = raw.get("note") or ""
    if not isinstance(note, str):
        raise OverlayError("mark.note must be a string")
    return {"status": status, "tags": tags, "note": note}


def is_empty(document: Dict[str, Any]) -> bool:
    """An overlay with no edits, no mark and no note is worth deleting, not storing."""
    mark = document.get("mark") or {}
    edits = document.get("edits") or {}
    return (
        mark.get("status", "unmarked") == "unmarked"
        and not mark.get("tags")
        and not (mark.get("note") or "").strip()
        and not edits.get("modified")
        and not edits.get("deleted")
        and not edits.get("added")
    )


# --------------------------------------------------------------------------
# documents


def _now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def build_document(
    run: str,
    stem: str,
    view: str,
    stamp: Dict[str, Any],
    edits: Dict[str, Any],
    mark: Dict[str, Any],
    author: str,
    existing: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Fold a validated edit payload into a new or existing overlay document."""
    now = _now()
    history = list((existing or {}).get("history") or [])
    history.append(
        {
            "at": now,
            "author": author,
            "mark": mark.get("status", "unmarked"),
            "counts": {
                "modified": len(edits.get("modified") or {}),
                "deleted": len(edits.get("deleted") or []),
                "added": len(edits.get("added") or []),
            },
        }
    )
    return {
        "schema": SCHEMA,
        "sample": {"run": run, "stem": stem, "view": view},
        "source": stamp,
        "created_at": (existing or {}).get("created_at") or now,
        "created_by": (existing or {}).get("created_by") or author,
        "updated_at": now,
        "updated_by": author,
        "mark": mark,
        "edits": edits,
        "history": history[-HISTORY_LIMIT:],
    }


def apply(
    elements: Sequence[Dict[str, Any]],
    document: Optional[Dict[str, Any]],
    include_deleted: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Pipeline output with the overlay applied. This is what "the sample" means.

    Deleted elements are dropped by default; the inspector asks for them so a
    deletion can be seen and undone, which is the only reason the flag exists.
    """
    keys = element_keys(elements)
    edits = (document or {}).get("edits") or {}
    modified = edits.get("modified") or {}
    deleted = set(edits.get("deleted") or [])
    added = edits.get("added") or []

    merged: List[Dict[str, Any]] = []
    matched_modified = set()
    matched_deleted = set()

    for key, element in zip(keys, elements):
        item = dict(element)
        item["_key"] = key
        change = modified.get(key)
        if change:
            matched_modified.add(key)
            item.update(change)
            item["_golden"] = {
                "status": "modified",
                "fields": sorted(change),
                "original": {field: element.get(field) for field in change},
            }
        if key in deleted:
            matched_deleted.add(key)
            # Keep the modification's `original` when an element is both edited
            # and deleted, so a client can still reconstruct the source values.
            marker = dict(item.get("_golden") or {})
            marker["status"] = "deleted"
            item["_golden"] = marker
            if not include_deleted:
                continue
        merged.append(item)

    for element in added:
        item = dict(element)
        item["_key"] = "add:%s" % element.get("golden_id")
        item["_golden"] = {"status": "added"}
        item.setdefault("name", "")
        item.setdefault("visible_text", "")
        item.setdefault("source", "golden")
        merged.append(item)

    unmatched = sorted(
        (set(modified) - matched_modified) | (deleted - matched_deleted)
    )
    summary = {
        "modified": len(matched_modified),
        "deleted": len(matched_deleted),
        "added": len(added),
        "unmatched": unmatched,
    }
    return merged, summary
