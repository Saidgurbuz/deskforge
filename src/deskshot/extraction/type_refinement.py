"""Context-aware refinement for element `type` assignment.

The raw AT-SPI role map is a useful prior, but several roles are too broad to
use as a final semantic label. This pass uses richer metadata, parent context,
and geometry to correct the most common systematic mistakes before filtering
and leaf selection rely on `type`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from deskshot.extraction.role_mapping import CANONICAL_CLASSES_SET, is_desktop_chrome


CONTROL_ICON_TEXTS = {
    "back",
    "forward",
    "next",
    "previous",
    "close",
    "maximize",
    "minimize",
    "reload",
    "refresh",
    "menu",
    "more",
    "more options",
    "open",
    "new",
    "new tab",
    "search tabs",
    "up",
    "down",
    "home",
}
SEARCH_TERMS = {
    "search",
    "find",
    "filter",
    "lookup",
    "query",
    "quick search",
    "quick open",
}
FILE_ICON_TEXT_HINTS = {
    "computer",
    "filesystem root",
    "trash",
    "home",
    "desktop",
    "documents",
    "downloads",
    "hard disk",
}
FILE_EXT_HINTS = (
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".yaml",
    ".yml",
    ".pdf",
    ".zip",
    ".png",
    ".jpg",
    ".jpeg",
    ".svg",
    ".py",
    ".sh",
    ".log",
)
ROW_LIKE_ROLES = {
    "list item",
    "tree item",
    "table row",
    "table cell",
}
TEXT_ENTRY_PARENT_ROLES = {
    "entry",
    "editbar",
    "edit bar",
    "password text",
    "combo box",
    "spin button",
}


def _text_norm(value: str) -> str:
    return " ".join((value or "").replace("\ufffc", "").strip().split())


def _role(elem: Dict[str, Any]) -> str:
    return (elem.get("role") or "").strip().lower()


def _base_type(elem: Dict[str, Any]) -> str:
    return (elem.get("type") or "").strip()


def _attrs(elem: Dict[str, Any]) -> Dict[str, Any]:
    attrs = elem.get("attrs") or {}
    return attrs if isinstance(attrs, dict) else {}


def _states(elem: Dict[str, Any]) -> Dict[str, bool]:
    states = _attrs(elem).get("states") or {}
    return states if isinstance(states, dict) else {}


def _interfaces(elem: Dict[str, Any]) -> Dict[str, bool]:
    interfaces = _attrs(elem).get("interfaces") or {}
    return interfaces if isinstance(interfaces, dict) else {}


def _action_names(elem: Dict[str, Any]) -> List[str]:
    actions = _attrs(elem).get("action_names") or []
    if not isinstance(actions, list):
        return []
    return [str(a).strip().lower() for a in actions if str(a).strip()]


def _description(elem: Dict[str, Any]) -> str:
    return _text_norm(str(_attrs(elem).get("description") or "")).lower()


def _name(elem: Dict[str, Any]) -> str:
    return _text_norm(str(elem.get("name") or "")).lower()


def _text(elem: Dict[str, Any]) -> str:
    return _text_norm(str(elem.get("inner_text") or "")).lower()


def _contains_search_signal(elem: Dict[str, Any]) -> bool:
    haystacks = [_text(elem), _name(elem), _description(elem)]
    return any(any(term in haystack for term in SEARCH_TERMS) for haystack in haystacks)


def _rect(elem: Dict[str, Any]) -> Dict[str, int]:
    rect = elem.get("rect") or {}
    return {
        "x": int(rect.get("x", 0) or 0),
        "y": int(rect.get("y", 0) or 0),
        "w": int(rect.get("w", 0) or 0),
        "h": int(rect.get("h", 0) or 0),
    }


def _parent(elem: Dict[str, Any], by_idx: Dict[int, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    pidx = elem.get("_parent_dom_index")
    if isinstance(pidx, int) and pidx in by_idx:
        return by_idx[pidx]
    pidx = elem.get("parent_index")
    if isinstance(pidx, int) and pidx in by_idx:
        return by_idx[pidx]
    return None


def _children(elem: Dict[str, Any], by_idx: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for idx in elem.get("_children_dom_indices") or elem.get("children_indices") or []:
        if isinstance(idx, int) and idx in by_idx:
            result.append(by_idx[idx])
    return result


def _descendant_roles(elem: Dict[str, Any], by_idx: Dict[int, Dict[str, Any]], *, limit: int = 64) -> Set[str]:
    out: Set[str] = set()
    queue = list(elem.get("_children_dom_indices") or elem.get("children_indices") or [])
    seen: Set[int] = set()
    while queue and len(seen) < limit:
        idx = queue.pop()
        if not isinstance(idx, int) or idx in seen or idx not in by_idx:
            continue
        seen.add(idx)
        child = by_idx[idx]
        out.add(_role(child))
        queue.extend(child.get("_children_dom_indices") or child.get("children_indices") or [])
    return out


def _in_top_band(elem: Dict[str, Any], parent: Optional[Dict[str, Any]]) -> bool:
    if parent is None:
        return False
    rect = _rect(elem)
    parent_rect = _rect(parent)
    if parent_rect["h"] <= 0:
        return False
    band = min(max(56, parent_rect["h"] // 8), 96)
    return rect["y"] <= parent_rect["y"] + band


def _is_small_icon_like(elem: Dict[str, Any], *, max_side: int = 48) -> bool:
    rect = _rect(elem)
    return 0 < rect["w"] <= max_side and 0 < rect["h"] <= max_side


def _looks_fileish(text: str) -> bool:
    if not text:
        return False
    if text in FILE_ICON_TEXT_HINTS:
        return True
    if any(ext in text for ext in FILE_EXT_HINTS):
        return True
    if "/" in text:
        return True
    return False


def _is_desktop_context(elem: Dict[str, Any]) -> bool:
    if (elem.get("source") or "").strip().lower() == "desktop_chrome":
        return True
    return is_desktop_chrome(str(elem.get("app_name") or ""))


def _refine_text_type(elem: Dict[str, Any], parent: Optional[Dict[str, Any]]) -> str:
    states = _states(elem)
    interfaces = _interfaces(elem)
    rect = _rect(elem)
    app_name = str(elem.get("app_name") or "").strip().lower()
    if _role(parent or {}) in TEXT_ENTRY_PARENT_ROLES:
        if _contains_search_signal(elem):
            return "Search Field"
        return "Text"
    if interfaces.get("editable_text"):
        return "Search Field" if _contains_search_signal(elem) else "Text Input"
    if states.get("editable"):
        return "Search Field" if _contains_search_signal(elem) else "Text Input"
    if states.get("multi_line") or states.get("single_line"):
        return "Search Field" if _contains_search_signal(elem) else "Text Input"
    if states.get("focusable") and _interfaces(elem).get("text"):
        return "Search Field" if _contains_search_signal(elem) else "Text Input"
    if app_name == "gnome-calculator" and rect["w"] >= 80 and rect["h"] <= 40:
        return "Text Input"
    return "Text"


def _refine_icon_type(
    elem: Dict[str, Any],
    parent: Optional[Dict[str, Any]],
    by_idx: Dict[int, Dict[str, Any]],
) -> str:
    text = _text(elem) or _name(elem) or _description(elem)
    parent_role = _role(parent or {})
    rect = _rect(elem)
    actions = _action_names(elem)

    if _is_desktop_context(elem):
        return "File Icon"

    if parent_role in ROW_LIKE_ROLES:
        parent_rect = _rect(parent or {})
        if text in CONTROL_ICON_TEXTS or (
            rect["w"] <= max(48, parent_rect["w"] // 6)
            and rect["x"] >= parent_rect["x"] + int(parent_rect["w"] * 0.6)
            and (text in CONTROL_ICON_TEXTS or bool(actions))
        ):
            return "Button"
        if _looks_fileish(text) or rect["w"] >= 32:
            return "File Icon"

    if parent_role in {"push button", "toggle button", "page tab", "menu item", "tool bar", "menu bar"}:
        return "Button"

    if text in CONTROL_ICON_TEXTS:
        return "Button"

    if actions and (_is_small_icon_like(elem) or _in_top_band(elem, parent)):
        return "Button"

    if _looks_fileish(text):
        return "File Icon"

    # If the icon lives inside a file/tree container, prefer File Icon.
    if parent is not None and _descendant_roles(parent, by_idx) & {"label", "static", "list item", "tree item", "table cell"}:
        if parent_role in {"list box", "tree", "table", "directory pane"}:
            return "File Icon"

    return "Image"


def _refine_landmark_type(
    elem: Dict[str, Any],
    *,
    viewport_width: int,
    viewport_height: int,
) -> str:
    rect = _rect(elem)
    if rect["w"] >= int(viewport_width * 0.25) and rect["h"] <= int(max(80, viewport_height * 0.18)) and rect["y"] <= int(viewport_height * 0.25):
        return "Navigation Bar"
    if rect["w"] >= int(viewport_width * 0.25) and rect["h"] <= int(max(90, viewport_height * 0.2)) and rect["y"] + rect["h"] >= int(viewport_height * 0.8):
        return "Bottom navigation"
    if rect["w"] <= int(viewport_width * 0.35) and rect["h"] >= int(viewport_height * 0.25):
        return "Side Bar"
    return "Text"


def _refine_scroll_pane_type(
    elem: Dict[str, Any],
    by_idx: Dict[int, Dict[str, Any]],
    *,
    viewport_width: int,
    viewport_height: int,
) -> str:
    rect = _rect(elem)
    descendants = _descendant_roles(elem, by_idx)
    if rect["w"] <= 64 or rect["h"] <= 64:
        return "Scroll"
    if descendants & {"list item", "tree item", "table row", "table cell"}:
        if rect["w"] <= int(viewport_width * 0.35):
            return "Side Bar"
        return "List"
    if rect["w"] <= int(viewport_width * 0.35) and rect["h"] >= int(viewport_height * 0.25):
        return "Side Bar"
    return "Scroll"


def _refine_section_type(
    elem: Dict[str, Any],
    parent: Optional[Dict[str, Any]],
    *,
    viewport_width: int,
    viewport_height: int,
) -> Optional[str]:
    states = _states(elem)
    rect = _rect(elem)
    parent_role = _role(parent or {})
    if states.get("focusable") and _is_small_icon_like(elem):
        return "Button"
    if parent_role in {"link", "push button", "toggle button"} and rect["w"] <= 48 and rect["h"] <= 48:
        return "Button"
    if rect["w"] <= int(viewport_width * 0.35) and rect["h"] >= int(viewport_height * 0.25):
        return "Side Bar"
    return None


def _refine_table_cell_type(elem: Dict[str, Any], parent: Optional[Dict[str, Any]]) -> Optional[str]:
    interfaces = _interfaces(elem)
    if not interfaces.get("image"):
        return None
    parent_role = _role(parent or {})
    if parent_role not in ROW_LIKE_ROLES:
        return None
    rect = _rect(elem)
    parent_rect = _rect(parent or {})
    if rect["w"] <= 32 and rect["h"] <= 48 and rect["x"] <= parent_rect["x"] + 48:
        return "File Icon"
    return None


def assign_element_types(
    elements: List[Dict[str, Any]],
    *,
    viewport_width: int,
    viewport_height: int,
) -> Dict[str, int]:
    """Refine element `type` assignments in-place and return summary stats."""
    if not elements:
        return {"num_elements": 0, "num_refined": 0}

    by_idx = {
        int(elem["_dom_index"]): elem
        for elem in elements
        if "_dom_index" in elem
    }
    num_refined = 0

    for elem in elements:
        role = _role(elem)
        original = _base_type(elem)
        refined = original
        parent = _parent(elem, by_idx)

        if role == "text":
            refined = _refine_text_type(elem, parent)
        elif role in {"entry", "password text", "editbar", "edit bar"} and _contains_search_signal(elem):
            refined = "Search Field"
        elif role == "combo box" and _contains_search_signal(elem):
            refined = "Search Field"
        elif role == "icon":
            refined = _refine_icon_type(elem, parent, by_idx)
        elif role == "landmark":
            refined = _refine_landmark_type(
                elem,
                viewport_width=viewport_width,
                viewport_height=viewport_height,
            )
        elif role == "article":
            refined = "Text"
        elif role == "internal frame":
            refined = "Window"
        elif role == "directory pane":
            refined = "List"
        elif role == "drawing area":
            refined = "Image"
        elif role == "scroll pane":
            refined = _refine_scroll_pane_type(
                elem,
                by_idx,
                viewport_width=viewport_width,
                viewport_height=viewport_height,
            )
        elif role == "section":
            maybe = _refine_section_type(
                elem,
                parent,
                viewport_width=viewport_width,
                viewport_height=viewport_height,
            )
            if maybe is not None:
                refined = maybe
        elif role == "table cell":
            maybe = _refine_table_cell_type(elem, parent)
            if maybe is not None:
                refined = maybe

        if refined not in CANONICAL_CLASSES_SET:
            refined = "Text"

        if refined != original:
            elem["type"] = refined
            num_refined += 1

    return {
        "num_elements": len(elements),
        "num_refined": num_refined,
    }


def assign_refined_types(elements: List[Dict[str, Any]]) -> Dict[str, int]:
    """Backward-compatible alias for older call sites."""
    return assign_element_types(
        elements,
        viewport_width=1920,
        viewport_height=1080,
    )
