#!/usr/bin/env python3
"""Generate a deterministic PDF fixture for reader-app qualification."""

from __future__ import annotations

import json
from pathlib import Path

import fitz


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "assets" / "audit" / "sample_report.pdf"
STRUCTURE_OUTPUT = ROOT / "assets" / "audit" / "sample_report.structure.json"


def _rect(rect: fitz.Rect) -> dict[str, float]:
    return {
        "x0": round(rect.x0, 1),
        "y0": round(rect.y0, 1),
        "x1": round(rect.x1, 1),
        "y1": round(rect.y1, 1),
        "w": round(rect.width, 1),
        "h": round(rect.height, 1),
    }


def _draw_table(page: fitz.Page, x: float, y: float, blocks: list[dict[str, object]], page_no: int) -> None:
    headers = ["Metric", "Observed", "Target"]
    rows = [
        ("Availability", "99.97%", "99.90%"),
        ("P95 latency", "184 ms", "< 220 ms"),
        ("Failed jobs", "3", "< 8"),
        ("Hotfixes", "1", "< 2"),
    ]
    col_widths = [160, 120, 120]
    row_h = 24
    total_w = sum(col_widths)
    total_h = row_h * (1 + len(rows))

    caption_rect = fitz.Rect(x, y - 28, x + total_w, y - 4)
    page.insert_text((x, y - 14), "Table 1. Release Validation Metrics", fontsize=12, fontname="helv")
    page.draw_rect(fitz.Rect(x, y, x + total_w, y + total_h), color=(0, 0, 0), width=1)
    blocks.append(
        {
            "page": page_no,
            "label": "Table",
            "bbox": _rect(fitz.Rect(x, y, x + total_w, y + total_h)),
            "caption_bbox": _rect(caption_rect),
            "caption": "Table 1. Release Validation Metrics",
            "rows": len(rows) + 1,
            "cols": len(headers),
            "text": "Table 1. Release Validation Metrics Metric Observed Target Availability 99.97% 99.90% P95 latency 184 ms < 220 ms Failed jobs 3 < 8 Hotfixes 1 < 2",
        }
    )

    cur_x = x
    for width in col_widths[:-1]:
        cur_x += width
        page.draw_line((cur_x, y), (cur_x, y + total_h), color=(0, 0, 0), width=0.8)

    for row_idx in range(1, 1 + len(rows)):
        yy = y + row_idx * row_h
        page.draw_line((x, yy), (x + total_w, yy), color=(0, 0, 0), width=0.8)

    cur_x = x
    for header, width in zip(headers, col_widths):
        page.insert_text((cur_x + 8, y + 16), header, fontsize=11, fontname="helv")
        cur_x += width

    for row_idx, row in enumerate(rows, start=1):
        cur_x = x
        yy = y + row_idx * row_h + 16
        for cell, width in zip(row, col_widths):
            page.insert_text((cur_x + 8, yy), cell, fontsize=11, fontname="helv")
            cur_x += width


def _draw_figure(page: fitz.Page, x: float, y: float, blocks: list[dict[str, object]], page_no: int) -> None:
    caption_rect = fitz.Rect(x, y - 28, x + 280, y - 4)
    page.insert_text((x, y - 14), "Figure 1. Service topology sketch", fontsize=12, fontname="helv")
    boxes = [
        ("Load Balancer", fitz.Rect(x, y, x + 120, y + 48)),
        ("API", fitz.Rect(x + 170, y, x + 270, y + 48)),
        ("Workers", fitz.Rect(x + 170, y + 92, x + 270, y + 140)),
        ("Postgres", fitz.Rect(x + 340, y + 46, x + 460, y + 94)),
    ]
    figure_rect = fitz.Rect(x, y, x + 460, y + 140)
    blocks.append(
        {
            "page": page_no,
            "label": "Image",
            "subtype": "Figure",
            "bbox": _rect(figure_rect),
            "caption_bbox": _rect(caption_rect),
            "caption": "Figure 1. Service topology sketch",
            "text": "Figure 1. Service topology sketch Load Balancer API Workers Postgres",
        }
    )
    for label, rect in boxes:
        page.draw_rect(rect, color=(0.1, 0.2, 0.5), fill=(0.92, 0.95, 1.0), width=1.2)
        page.insert_text((rect.x0 + 12, rect.y0 + 26), label, fontsize=11, fontname="helv")

    connections = [
        ((x + 120, y + 24), (x + 170, y + 24)),
        ((x + 220, y + 48), (x + 220, y + 92)),
        ((x + 270, y + 24), (x + 340, y + 70)),
        ((x + 270, y + 116), (x + 340, y + 70)),
    ]
    for start, end in connections:
        page.draw_line(start, end, color=(0.2, 0.2, 0.2), width=1.0)


def build_sample_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    structure: list[dict[str, object]] = []
    doc.set_metadata(
        {
            "title": "DeskShot Sample Report",
            "author": "DeskShot",
            "subject": "Deterministic PDF fixture",
        }
    )

    page1 = doc.new_page(width=595, height=842)
    page1.insert_text((56, 64), "Quarterly Systems Reliability Report", fontsize=24, fontname="helv")
    structure.append(
        {
            "page": 1,
            "label": "Heading",
            "bbox": _rect(fitz.Rect(56, 38, 450, 72)),
            "text": "Quarterly Systems Reliability Report",
        }
    )
    intro = (
        "This synthetic report is used to validate dense annotation on PDF reader "
        "surfaces. It contains headings, paragraphs, a simple table, and a labeled "
        "figure so the extraction pipeline can be checked against a realistic mix "
        "of document content."
    )
    intro_rect = fitz.Rect(56, 92, 539, 160)
    page1.insert_textbox(intro_rect, intro, fontsize=12, fontname="helv", lineheight=1.3)
    structure.append({"page": 1, "label": "Text", "bbox": _rect(intro_rect), "text": intro})
    _draw_table(page1, 56, 196, structure, 1)
    _draw_figure(page1, 56, 370, structure, 1)
    summary_rect = fitz.Rect(56, 600, 539, 720)
    page1.insert_textbox(
        summary_rect,
        "Summary\n"
        "The release met all service-level objectives. Operator-visible incidents dropped, "
        "rollback time improved, and batch backfill jobs finished within the planned window.",
        fontsize=12,
        fontname="helv",
        lineheight=1.3,
    )
    structure.append(
        {
            "page": 1,
            "label": "Text",
            "bbox": _rect(summary_rect),
            "text": "Summary The release met all service-level objectives. Operator-visible incidents dropped, rollback time improved, and batch backfill jobs finished within the planned window.",
        }
    )

    page2 = doc.new_page(width=595, height=842)
    page2.insert_text((56, 64), "Appendix A. Deployment Notes", fontsize=22, fontname="helv")
    structure.append(
        {
            "page": 2,
            "label": "Heading",
            "bbox": _rect(fitz.Rect(56, 40, 370, 72)),
            "text": "Appendix A. Deployment Notes",
        }
    )
    bullets = [
        "Staging soak time was extended from 6 to 10 hours.",
        "The Europe-West replica was promoted during the March maintenance window.",
        "The backup restore drill recovered 1.4 TB in 37 minutes.",
        "Runbook update R-219 added a branch for queue-depth mitigation.",
    ]
    y = 112
    bullet_rects = []
    for bullet in bullets:
        page2.insert_text((72, y), f"- {bullet}", fontsize=12, fontname="helv")
        bullet_rects.append(_rect(fitz.Rect(72, y - 14, 539, y + 6)))
        y += 26
    page2.insert_text((56, 262), "Operator checklist", fontsize=16, fontname="helv")
    structure.append(
        {
            "page": 2,
            "label": "List",
            "bbox": _rect(fitz.Rect(72, 98, 539, 194)),
            "items": bullets,
            "item_bboxes": bullet_rects,
        }
    )
    structure.append(
        {
            "page": 2,
            "label": "Heading",
            "bbox": _rect(fitz.Rect(56, 244, 190, 268)),
            "text": "Operator checklist",
        }
    )
    checklist = (
        "1. Verify dashboards are green.\n"
        "2. Confirm alert routing and on-call ownership.\n"
        "3. Check replication lag, worker queue depth, and canary response time.\n"
        "4. Archive screenshots and incident notes in the release folder."
    )
    checklist_rect = fitz.Rect(56, 286, 539, 396)
    page2.insert_textbox(checklist_rect, checklist, fontsize=12, fontname="helv", lineheight=1.35)
    structure.append({"page": 2, "label": "Text", "bbox": _rect(checklist_rect), "text": checklist.replace("\n", " ")})
    page2.insert_text((56, 438), "Example command", fontsize=16, fontname="helv")
    structure.append(
        {
            "page": 2,
            "label": "Heading",
            "bbox": _rect(fitz.Rect(56, 420, 198, 444)),
            "text": "Example command",
        }
    )
    code_rect = fitz.Rect(56, 458, 539, 560)
    page2.draw_rect(code_rect, color=(0.4, 0.4, 0.4), fill=(0.96, 0.96, 0.96), width=0.8)
    page2.insert_textbox(
        fitz.Rect(72, 476, 523, 548),
        "kubectl rollout status deploy/api --timeout=180s\n"
        "python tools/check_release.py --env prod --report metrics.csv\n"
        "tar -czf release_bundle.tgz notes.md screenshots/ metrics.csv",
        fontsize=11,
        fontname="cour",
        lineheight=1.3,
    )
    structure.append(
        {
            "page": 2,
            "label": "Code snippet",
            "bbox": _rect(code_rect),
            "text": "kubectl rollout status deploy/api --timeout=180s\npython tools/check_release.py --env prod --report metrics.csv\ntar -czf release_bundle.tgz notes.md screenshots/ metrics.csv",
        }
    )

    doc.save(path)
    doc.close()
    STRUCTURE_OUTPUT.write_text(json.dumps(structure, indent=2), encoding="utf-8")


def main() -> None:
    build_sample_pdf(OUTPUT)
    print(OUTPUT)
    print(STRUCTURE_OUTPUT)


if __name__ == "__main__":
    main()
