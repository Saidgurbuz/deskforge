"""Helpers for visible-fragment geometry and serialization metadata."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


Rect = Dict[str, int]


def rect_area(rect: Rect) -> int:
    return max(0, int(rect.get("w", 0))) * max(0, int(rect.get("h", 0)))


def clone_rect(rect: Dict[str, Any]) -> Rect:
    return {
        "x": int(rect.get("x", 0)),
        "y": int(rect.get("y", 0)),
        "w": int(rect.get("w", 0)),
        "h": int(rect.get("h", 0)),
    }


def is_valid_rect(rect: Optional[Dict[str, Any]], *, min_visible_size: int = 1) -> bool:
    if not isinstance(rect, dict):
        return False
    return int(rect.get("w", 0)) >= min_visible_size and int(rect.get("h", 0)) >= min_visible_size


def intersect_rect(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[Rect]:
    x1 = max(int(a.get("x", 0)), int(b.get("x", 0)))
    y1 = max(int(a.get("y", 0)), int(b.get("y", 0)))
    x2 = min(int(a.get("x", 0)) + int(a.get("w", 0)), int(b.get("x", 0)) + int(b.get("w", 0)))
    y2 = min(int(a.get("y", 0)) + int(a.get("h", 0)), int(b.get("y", 0)) + int(b.get("h", 0)))
    if x2 <= x1 or y2 <= y1:
        return None
    return {"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1}


def union_rect(rects: List[Rect]) -> Optional[Rect]:
    if not rects:
        return None
    x1 = min(r["x"] for r in rects)
    y1 = min(r["y"] for r in rects)
    x2 = max(r["x"] + r["w"] for r in rects)
    y2 = max(r["y"] + r["h"] for r in rects)
    if x2 <= x1 or y2 <= y1:
        return None
    return {"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1}


def best_fragment(rects: List[Rect]) -> Optional[Rect]:
    if not rects:
        return None
    return max(rects, key=rect_area)


def _merge_pair(a: Rect, b: Rect) -> Optional[Rect]:
    if a["y"] == b["y"] and a["h"] == b["h"]:
        ax2 = a["x"] + a["w"]
        bx2 = b["x"] + b["w"]
        if ax2 == b["x"] or bx2 == a["x"]:
            x1 = min(a["x"], b["x"])
            x2 = max(ax2, bx2)
            return {"x": x1, "y": a["y"], "w": x2 - x1, "h": a["h"]}
    if a["x"] == b["x"] and a["w"] == b["w"]:
        ay2 = a["y"] + a["h"]
        by2 = b["y"] + b["h"]
        if ay2 == b["y"] or by2 == a["y"]:
            y1 = min(a["y"], b["y"])
            y2 = max(ay2, by2)
            return {"x": a["x"], "y": y1, "w": a["w"], "h": y2 - y1}
    return None


def normalize_fragments(rects: List[Rect], *, min_visible_size: int = 1) -> List[Rect]:
    deduped: List[Rect] = []
    seen: set[tuple[int, int, int, int]] = set()
    for rect in rects:
        if not is_valid_rect(rect, min_visible_size=min_visible_size):
            continue
        clean = clone_rect(rect)
        key = (clean["x"], clean["y"], clean["w"], clean["h"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(clean)

    deduped.sort(key=lambda r: (r["y"], r["x"], r["h"], r["w"]))
    changed = True
    while changed and len(deduped) > 1:
        changed = False
        merged: List[Rect] = []
        used = [False] * len(deduped)
        for i, rect in enumerate(deduped):
            if used[i]:
                continue
            merged_rect = rect
            for j in range(i + 1, len(deduped)):
                if used[j]:
                    continue
                candidate = _merge_pair(merged_rect, deduped[j])
                if candidate is None:
                    continue
                used[j] = True
                merged_rect = candidate
                changed = True
            used[i] = True
            merged.append(merged_rect)
        deduped = sorted(merged, key=lambda r: (r["y"], r["x"], r["h"], r["w"]))
    return deduped


def clip_fragments_to_rect(
    fragments: List[Rect],
    clip_rect: Dict[str, Any],
    *,
    min_visible_size: int = 1,
) -> List[Rect]:
    if not is_valid_rect(clip_rect, min_visible_size=min_visible_size):
        return []
    clipped: List[Rect] = []
    for frag in fragments:
        inter = intersect_rect(frag, clip_rect)
        if inter is not None:
            clipped.append(inter)
    return normalize_fragments(clipped, min_visible_size=min_visible_size)


def clip_fragments_to_fragments(
    fragments: List[Rect],
    clip_regions: List[Rect],
    *,
    min_visible_size: int = 1,
) -> List[Rect]:
    if not clip_regions:
        return []
    clipped: List[Rect] = []
    for frag in fragments:
        for region in clip_regions:
            inter = intersect_rect(frag, region)
            if inter is not None:
                clipped.append(inter)
    return normalize_fragments(clipped, min_visible_size=min_visible_size)


def init_visibility_metadata(elements: List[Dict[str, Any]]) -> None:
    for elem in elements:
        source_rect = clone_rect(elem.get("rect", {}))
        elem["_visibility_source_rect"] = source_rect
        elem["_is_occluded_by_overlap"] = False
        set_visible_fragments(elem, [source_rect] if is_valid_rect(source_rect) else [])


def set_visible_fragments(
    elem: Dict[str, Any],
    fragments: List[Rect],
    *,
    min_visible_size: int = 1,
) -> List[Rect]:
    normalized = normalize_fragments(fragments, min_visible_size=min_visible_size)
    elem["_visible_fragments"] = [clone_rect(r) for r in normalized]
    elem["visible_fragments"] = [clone_rect(r) for r in normalized]
    elem["is_occluded"] = bool(elem.get("_is_occluded_by_overlap", False))
    return normalized


def get_visible_fragments(elem: Dict[str, Any]) -> List[Rect]:
    fragments = elem.get("_visible_fragments")
    if isinstance(fragments, list):
        return normalize_fragments([clone_rect(r) for r in fragments])
    rect = elem.get("rect", {})
    if is_valid_rect(rect):
        return [clone_rect(rect)]
    return []


def visible_union_rect(elem: Dict[str, Any]) -> Optional[Rect]:
    fragments = get_visible_fragments(elem)
    if not fragments:
        return None
    return union_rect(fragments)


def strip_internal_visibility_state(elements: List[Dict[str, Any]]) -> None:
    for elem in elements:
        elem.pop("_visible_fragments", None)


OCCLUSION_STATE_NONE = "none"
OCCLUSION_STATE_PARTIAL = "partial"
OCCLUSION_STATE_HIDDEN = "hidden"


def annotate_occlusion_state(elements) -> dict:
    """Stamp an explicit three-state `occlusion_state` on each element.

    `is_occluded` alone conflates "partly covered" with "entirely covered", and
    the fully covered case was only recoverable by noticing the element had been
    dropped from the filtered output. Making the state explicit keeps that
    distinction available to consumers without changing what gets exported.
    """
    counts = {
        OCCLUSION_STATE_NONE: 0,
        OCCLUSION_STATE_PARTIAL: 0,
        OCCLUSION_STATE_HIDDEN: 0,
    }
    for elem in elements:
        # Read the exported key, not get_visible_fragments: that one falls back to
        # the element rect when no fragments are set, which would report a fully
        # covered element as partial, and it reads an internal key that does not
        # survive export - so this would give different answers on loaded JSON.
        fragments = elem.get("visible_fragments")
        if not isinstance(fragments, list):
            fragments = elem.get("_visible_fragments")
        has_visible = bool(fragments) if isinstance(fragments, list) else False

        if not elem.get("is_occluded"):
            state = OCCLUSION_STATE_NONE
        elif has_visible:
            state = OCCLUSION_STATE_PARTIAL
        else:
            state = OCCLUSION_STATE_HIDDEN
        elem["occlusion_state"] = state
        counts[state] += 1
    return counts


def drop_fully_hidden_elements(elements) -> int:
    """Remove elements with no visible area, in place. Returns how many went.

    An element that is occluded with no visible fragments is not on the screen,
    so serializing its box teaches a model to predict controls a human cannot
    see. Containers are kept even when their own box is fully covered, because
    a visible child still needs its parent in the nesting.
    """
    keep = []
    removed = 0
    child_bearing = set()
    for elem in elements:
        for key in ("children_indices", "_children_dom_indices"):
            for cidx in elem.get(key) or []:
                if isinstance(cidx, int):
                    child_bearing.add(cidx)

    surviving_indices = set()
    for elem in elements:
        fragments = elem.get("visible_fragments")
        if not isinstance(fragments, list):
            fragments = elem.get("_visible_fragments")
        has_visible = bool(fragments) if isinstance(fragments, list) else True
        if elem.get("is_occluded") and not has_visible:
            has_children = bool(
                elem.get("children_indices") or elem.get("_children_dom_indices")
            )
            if not has_children:
                removed += 1
                continue
        keep.append(elem)
        idx = elem.get("_dom_index")
        if isinstance(idx, int):
            surviving_indices.add(idx)

    if removed:
        elements[:] = keep
        # Drop dangling child references so serialization does not look for
        # elements that are no longer present.
        for elem in elements:
            for key in ("children_indices", "_children_dom_indices"):
                refs = elem.get(key)
                if isinstance(refs, list):
                    elem[key] = [c for c in refs if not isinstance(c, int) or c in surviving_indices]
    return removed


def build_amodal_elements(elements, hidden_elements) -> list:
    """Combine visible elements with the fully covered ones.

    The modal view (visible only) is the right training target for perception:
    it prevents a model hallucinating controls a human cannot see. But an agent
    that must *plan* needs the other view - knowing a Save button exists behind
    the dialog is what turns "click Save" into "move the dialog, then click
    Save". Both views come from the same capture, so emitting the amodal one
    costs nothing and enables reveal-planning tasks.

    Fully covered elements keep their original (unclipped) extent, since that is
    where they would be if revealed.
    """
    amodal = []
    for elem in elements:
        entry = dict(elem)
        entry["amodal_visibility"] = entry.get("occlusion_state", "none")
        amodal.append(entry)
    for elem in hidden_elements:
        entry = dict(elem)
        entry["amodal_visibility"] = OCCLUSION_STATE_HIDDEN
        entry["occlusion_state"] = OCCLUSION_STATE_HIDDEN
        # Restore the pre-clip extent when the occlusion pass recorded one.
        original = entry.get("_occlusion_original_rect") or entry.get("_visibility_source_rect")
        if isinstance(original, dict) and original:
            entry["rect"] = clone_rect(original)
        entry["visible_fragments"] = []
        amodal.append(entry)
    return amodal


def partition_hidden_elements(elements) -> tuple:
    """Split into (visible, fully-hidden) without mutating the input."""
    visible, hidden = [], []
    for elem in elements:
        fragments = elem.get("visible_fragments")
        if not isinstance(fragments, list):
            fragments = elem.get("_visible_fragments")
        has_visible = bool(fragments) if isinstance(fragments, list) else True
        (hidden if (elem.get("is_occluded") and not has_visible) else visible).append(elem)
    return visible, hidden
