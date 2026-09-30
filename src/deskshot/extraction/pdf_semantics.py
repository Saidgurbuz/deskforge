"""Hybrid PDF semantic extraction and viewer-surface mapping."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

import fitz

from deskshot.config import AppManifest


_MIN_TEXT_LEN = 3


def _norm_text(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9%./:_+\-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _block_text(block: Dict[str, Any]) -> str:
    text = block.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    caption = block.get("caption")
    if isinstance(caption, str) and caption.strip():
        return caption.strip()
    items = block.get("items")
    if isinstance(items, list):
        parts = [str(item).strip() for item in items if str(item).strip()]
        if parts:
            return " ".join(parts)
    return ""


def extract_generic_pdf_semantics(pdf_path: Path) -> List[Dict[str, Any]]:
    """Extract coarse semantic blocks from a PDF using PyMuPDF only."""
    doc = fitz.open(pdf_path)
    rows: List[Dict[str, Any]] = []
    for page_no, page in enumerate(doc, start=1):
        text_blocks = []
        for block in page.get_text("blocks"):
            x0, y0, x1, y1, text = block[:5]
            norm = " ".join((text or "").split())
            if not norm:
                continue
            text_blocks.append((fitz.Rect(x0, y0, x1, y1), norm))
            label = "Text"
            if any(norm.startswith(prefix) for prefix in ("Quarterly", "Appendix", "Operator checklist", "Example command", "Summary")):
                label = "Heading"
            if "kubectl rollout status" in norm or "tar -czf" in norm:
                label = "Code snippet"
            rows.append(
                {
                    "page": page_no,
                    "label": label,
                    "bbox": {
                        "x0": round(x0, 1),
                        "y0": round(y0, 1),
                        "x1": round(x1, 1),
                        "y1": round(y1, 1),
                        "w": round(x1 - x0, 1),
                        "h": round(y1 - y0, 1),
                    },
                    "text": norm,
                }
            )

        try:
            tables = page.find_tables()
        except Exception:
            tables = None
        if tables is not None:
            for table in tables.tables:
                trect = fitz.Rect(*table.bbox)
                snippets = [txt for rect, txt in text_blocks if rect.intersects(trect)]
                rows.append(
                    {
                        "page": page_no,
                        "label": "Table",
                        "bbox": {
                            "x0": round(trect.x0, 1),
                            "y0": round(trect.y0, 1),
                            "x1": round(trect.x1, 1),
                            "y1": round(trect.y1, 1),
                            "w": round(trect.width, 1),
                            "h": round(trect.height, 1),
                        },
                        "rows": table.row_count,
                        "cols": table.col_count,
                        "text": " ".join(snippets).strip(),
                    }
                )

        for image in page.get_image_info():
            bbox = image.get("bbox")
            if not bbox:
                continue
            x0, y0, x1, y1 = bbox
            rows.append(
                {
                    "page": page_no,
                    "label": "Image",
                    "bbox": {
                        "x0": round(x0, 1),
                        "y0": round(y0, 1),
                        "x1": round(x1, 1),
                        "y1": round(y1, 1),
                        "w": round(x1 - x0, 1),
                        "h": round(y1 - y0, 1),
                    },
                }
            )

    doc.close()
    return rows


def load_pdf_semantic_blocks(manifest: AppManifest) -> tuple[List[Dict[str, Any]], str]:
    """Load sidecar PDF semantics or fall back to generic extraction."""
    semantics_path = Path(manifest.document_semantics) if manifest.document_semantics else None
    if semantics_path and semantics_path.is_file():
        return json.loads(semantics_path.read_text(encoding="utf-8")), "sidecar"
    document_path = Path(manifest.document_path) if manifest.document_path else None
    if document_path and document_path.is_file() and document_path.suffix.lower() == ".pdf":
        return extract_generic_pdf_semantics(document_path), "fitz"
    return [], "none"


def _text_matches(block_norm: str, elem_norm: str) -> bool:
    if not block_norm or not elem_norm or len(elem_norm) < _MIN_TEXT_LEN:
        return False
    if elem_norm == block_norm:
        return True
    if elem_norm in block_norm and (len(elem_norm) >= 8 or " " in elem_norm):
        return True
    block_tokens = set(block_norm.split())
    elem_tokens = set(elem_norm.split())
    if len(elem_tokens) >= 2 and elem_tokens.issubset(block_tokens):
        return True
    return False


def build_pdf_semantic_elements(
    manifest: AppManifest,
    leaf_elements: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Map source-side PDF semantics onto visible viewer text boxes."""
    blocks, backend = load_pdf_semantic_blocks(manifest)
    if not blocks:
        return [], {"backend": backend, "num_blocks_source": 0, "num_blocks_mapped": 0}

    candidates = []
    windows_by_dom: Dict[int, Dict[str, Any]] = {}
    for elem in leaf_elements:
        if elem.get("source") != "app":
            continue
        if manifest.app_name and elem.get("app_name") != manifest.app_name:
            continue
        source_dom = elem.get("_source_dom_index")
        if (elem.get("role") or "").strip().lower() in {"frame", "window"}:
            if isinstance(source_dom, int):
                windows_by_dom[source_dom] = elem
            owner_dom = elem.get("_window_owner_dom_index")
            if isinstance(owner_dom, int):
                windows_by_dom[owner_dom] = elem

        text = (elem.get("inner_text") or "").strip()
        norm = _norm_text(text)
        if len(norm) < _MIN_TEXT_LEN:
            continue
        candidates.append((elem, norm))

    mapped: List[Dict[str, Any]] = []
    for idx, block in enumerate(blocks):
        block_text = _block_text(block)
        block_norm = _norm_text(block_text)
        if not block_norm:
            continue
        matched = [elem for elem, norm in candidates if _text_matches(block_norm, norm)]
        if not matched:
            continue

        x0 = min(float(e["rect"]["x"]) for e in matched)
        y0 = min(float(e["rect"]["y"]) for e in matched)
        x1 = max(float(e["rect"]["x"]) + float(e["rect"]["w"]) for e in matched)
        y1 = max(float(e["rect"]["y"]) + float(e["rect"]["h"]) for e in matched)

        owner_counter = Counter(
            int(e["_window_owner_dom_index"])
            for e in matched
            if isinstance(e.get("_window_owner_dom_index"), int)
        )
        owner_dom = owner_counter.most_common(1)[0][0] if owner_counter else None
        window_elem = windows_by_dom.get(owner_dom) if isinstance(owner_dom, int) else None

        semantic_elem: Dict[str, Any] = {
            "_dom_index": idx,
            "_parent_dom_index": None,
            "_children_dom_indices": [],
            "parent_index": None,
            "children_indices": [],
            "source": "pdf_semantic",
            "app_name": manifest.app_name,
            "role": "pdf semantic block",
            "type": str(block.get("label") or "Text"),
            "vlm_label": str(block.get("label") or "Text"),
            "inner_text": block_text,
            "rect": {
                "x": int(round(x0)),
                "y": int(round(y0)),
                "w": int(round(max(0.0, x1 - x0))),
                "h": int(round(max(0.0, y1 - y0))),
            },
            "document_page": block.get("page"),
            "document_path": manifest.document_path,
            "semantic_backend": backend,
            "matched_leaf_reading_order_indices": [
                int(e["reading_order_index"])
                for e in matched
                if isinstance(e.get("reading_order_index"), int)
            ],
            "matched_leaf_count": len(matched),
        }
        if window_elem is not None:
            semantic_elem["_window_owner_dom_index"] = owner_dom
            semantic_elem["_window_stack_index"] = window_elem.get("_window_stack_index")
            semantic_elem["window_reading_order_index"] = window_elem.get("reading_order_index")

        mapped.append(semantic_elem)

    return mapped, {
        "backend": backend,
        "num_blocks_source": len(blocks),
        "num_blocks_mapped": len(mapped),
    }
