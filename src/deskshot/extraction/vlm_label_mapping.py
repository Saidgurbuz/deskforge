"""Conservative refinement layer for VLM-facing labels.

The current `type` field already uses the canonical 55-label taxonomy, but it
is mostly assigned from raw AT-SPI role names. `vlm_label` is a second-pass
field that preserves the existing `type` while allowing a few high-confidence
semantic refinements when role + text + hierarchy provide stronger evidence.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from deskshot.extraction.role_mapping import CANONICAL_CLASSES_SET, get_screentag_class

SEARCH_TERMS = {
    "search",
    "find",
    "filter",
    "lookup",
    "query",
    "quick search",
    "quick open",
}
UTILITY_BUTTON_TEXTS = {
    "close",
    "maximize",
    "minimize",
    "back",
    "forward",
    "reload",
    "menu",
    "more",
    "more options",
    "new tab",
    "search tabs",
}
ICONIC_CHARS = {"…", "⋯", "⋮", "×", "✕", "✖", "←", "→", "↺", "↻", "+", "-"}
SEARCH_BAR_CONTAINER_LABELS = {"Navigation Bar", "Toolbar"}


def _text_norm(value: str) -> str:
    return " ".join((value or "").replace("\ufffc", "").strip().split())


def _base_label(elem: Dict[str, Any]) -> str:
    elem_type = str(elem.get("type") or "").strip()
    if elem_type in CANONICAL_CLASSES_SET:
        return elem_type
    role = str(elem.get("role") or "").strip()
    mapped = get_screentag_class(role)
    if mapped is not None:
        return mapped
    return "Text"


def _description_text(elem: Dict[str, Any]) -> str:
    attrs = elem.get("attrs") or {}
    if not isinstance(attrs, dict):
        return ""
    return _text_norm(str(attrs.get("description") or ""))


def _contains_search_signal(elem: Dict[str, Any]) -> bool:
    haystacks = [
        _text_norm(str(elem.get("inner_text") or "")).lower(),
        _description_text(elem).lower(),
        _text_norm(str(elem.get("name") or "")).lower(),
    ]
    return any(any(term in haystack for term in SEARCH_TERMS) for haystack in haystacks)


def _is_iconic_text(text: str) -> bool:
    normalized = _text_norm(text)
    if not normalized:
        return True
    if normalized in ICONIC_CHARS:
        return True
    return not any(ch.isalnum() for ch in normalized)


def _parent(elem: Dict[str, Any], by_idx: Dict[int, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    pidx = elem.get("parent_index")
    if isinstance(pidx, int) and pidx in by_idx:
        return by_idx[pidx]
    pidx = elem.get("_parent_dom_index")
    if isinstance(pidx, int) and pidx in by_idx:
        return by_idx[pidx]
    return None


def _has_descendant_with_label(
    elem: Dict[str, Any],
    target_label: str,
    by_idx: Dict[int, Dict[str, Any]],
    labels: Dict[int, str],
) -> bool:
    stack = list(elem.get("children_indices") or elem.get("_children_dom_indices") or [])
    seen: set[int] = set()
    while stack:
        idx = int(stack.pop())
        if idx in seen or idx not in by_idx:
            continue
        seen.add(idx)
        if labels.get(idx) == target_label:
            return True
        child = by_idx[idx]
        stack.extend(child.get("children_indices") or child.get("_children_dom_indices") or [])
    return False


def _attrs(elem: Dict[str, Any]) -> Dict[str, Any]:
    attrs = elem.get("attrs") or {}
    return attrs if isinstance(attrs, dict) else {}


def _states(elem: Dict[str, Any]) -> Dict[str, bool]:
    states = _attrs(elem).get("states") or {}
    return states if isinstance(states, dict) else {}


def _is_small_rect(elem: Dict[str, Any], *, max_size: int = 48) -> bool:
    rect = elem.get("rect") or {}
    w = int(rect.get("w", 0) or 0)
    h = int(rect.get("h", 0) or 0)
    return 0 < w <= max_size and 0 < h <= max_size


def _is_top_band_child(elem: Dict[str, Any], parent: Optional[Dict[str, Any]]) -> bool:
    if parent is None:
        return False
    rect = elem.get("rect") or {}
    parent_rect = parent.get("rect") or {}
    parent_h = int(parent_rect.get("h", 0) or 0)
    if parent_h <= 0:
        return False
    band_h = min(max(56, parent_h // 8), 96)
    return int(rect.get("y", 0) or 0) <= int(parent_rect.get("y", 0) or 0) + band_h


def assign_vlm_labels(
    elements: List[Dict[str, Any]],
    *,
    viewport_width: int,
    viewport_height: int,
) -> Dict[str, int]:
    """Populate `vlm_label` conservatively in-place and return summary stats."""
    if not elements:
        return {"num_elements": 0, "num_refined": 0}

    by_idx = {
        int(e["_dom_index"]): e
        for e in elements
        if "_dom_index" in e
    }

    labels: Dict[int, str] = {}
    for elem in elements:
        dom_index = int(elem["_dom_index"])
        label = _base_label(elem)
        role = str(elem.get("role") or "").strip().lower()
        text = _text_norm(str(elem.get("inner_text") or ""))
        text_lower = text.lower()
        source = str(elem.get("source") or "").strip().lower()

        if label == "Window" and role == "desktop frame":
            label = "Screen"
        elif label == "Text Input" and _contains_search_signal(elem):
            label = "Search Field"
        elif label == "Button":
            parent = _parent(elem, by_idx)
            parent_label = labels.get(int(parent["_dom_index"])) if parent is not None else None
            states = _states(elem)
            if text_lower in UTILITY_BUTTON_TEXTS:
                label = "Utility Button"
            elif (
                _is_iconic_text(text)
                and _is_small_rect(elem)
                and parent_label in {"Toolbar", "Navigation Bar", "Tab Bar"}
            ):
                label = "Utility Button"
            elif (
                _is_iconic_text(text)
                and _is_small_rect(elem)
                and parent_label == "Window"
                and _is_top_band_child(elem, parent)
            ):
                label = "Utility Button"
            elif (
                states.get("has_popup")
                and _is_small_rect(elem)
                and parent_label in {"Toolbar", "Navigation Bar"}
            ):
                label = "Utility Button"
        elif label == "Menu" and text_lower == "edit":
            label = "EditMenu"

        labels[dom_index] = label

    for elem in elements:
        dom_index = int(elem["_dom_index"])
        label = labels[dom_index]
        base = _base_label(elem)
        role = str(elem.get("role") or "").strip().lower()
        if (
            label == base
            and label in SEARCH_BAR_CONTAINER_LABELS
            and _has_descendant_with_label(elem, "Search Field", by_idx, labels)
        ):
            label = "Search Bar"
        elif label == base and role == "panel" and _has_descendant_with_label(elem, "Search Field", by_idx, labels):
            label = "Search Bar"
        elem["vlm_label"] = label
        labels[dom_index] = label

    num_refined = sum(
        1
        for elem in elements
        if str(elem.get("vlm_label") or "") != _base_label(elem)
    )
    return {
        "num_elements": len(elements),
        "num_refined": num_refined,
    }
