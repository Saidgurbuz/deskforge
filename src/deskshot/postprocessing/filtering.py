"""Thin wrapper around webshot.filtering for desktop UI elements."""

from __future__ import annotations

from typing import Any, Dict, List

from deskshot.config import ensure_webshot_importable


def filter_elements(
    elements: List[Dict[str, Any]],
    viewport_w: int = 1920,
    viewport_h: int = 1080,
    iou_threshold: float = 0.95,
    containment_threshold: float = 0.98,
    min_box_size: int = 4,
    max_box_size: int = 1000,
) -> List[Dict[str, Any]]:
    """Filter desktop UI elements using webshot's filter pipeline.

    Falls back to basic filtering if webshot is not available.
    """
    try:
        ensure_webshot_importable()
        from webshot.filtering import filter_elements as wf_filter

        return wf_filter(
            elements,
            viewport_w=viewport_w,
            viewport_h=viewport_h,
            iou_threshold=iou_threshold,
            containment_threshold=containment_threshold,
            min_box_size=min_box_size,
            max_box_size=max_box_size,
        )
    except ImportError:
        return _basic_filter(elements, viewport_w, viewport_h, min_box_size)


def _basic_filter(
    elements: List[Dict[str, Any]],
    viewport_w: int,
    viewport_h: int,
    min_box_size: int,
) -> List[Dict[str, Any]]:
    """Basic filter: remove tiny, zero-size, and offscreen elements."""
    filtered = []
    for elem in elements:
        rect = elem.get("rect", {})
        w, h = rect.get("w", 0), rect.get("h", 0)
        x, y = rect.get("x", 0), rect.get("y", 0)

        # Skip tiny
        if w < min_box_size or h < min_box_size:
            continue
        # Skip offscreen
        if x + w <= 0 or y + h <= 0 or x >= viewport_w or y >= viewport_h:
            continue

        filtered.append(elem)

    return filtered
