"""Tests for hybrid PDF semantic mapping."""

from pathlib import Path

from deskshot.config import AppManifest
from deskshot.extraction.pdf_semantics import build_pdf_semantic_elements, extract_generic_pdf_semantics


def test_build_pdf_semantic_elements_maps_blocks_to_visible_leaf_text(tmp_path: Path) -> None:
    semantics_path = tmp_path / "sample.structure.json"
    semantics_path.write_text(
        """
[
  {
    "page": 1,
    "label": "Heading",
    "text": "Quarterly Systems Reliability Report"
  },
  {
    "page": 1,
    "label": "Table",
    "text": "Table 1. Release Validation Metrics Metric Observed Target Availability 99.97% 99.90%"
  }
]
""".strip(),
        encoding="utf-8",
    )

    manifest = AppManifest(
        app_name="firefox-pdf",
        binary="firefox-bin",
        atspi_name="firefox",
        document_path=str(tmp_path / "sample.pdf"),
        document_semantics=str(semantics_path),
    )
    leaf_elements = [
        {
            "_dom_index": 0,
            "_source_dom_index": 100,
            "_window_owner_dom_index": 900,
            "_window_stack_index": 0,
            "role": "frame",
            "source": "app",
            "app_name": "firefox-pdf",
            "type": "Window",
            "inner_text": "sample_report.pdf",
            "rect": {"x": 10, "y": 10, "w": 900, "h": 700},
            "reading_order_index": 0,
        },
        {
            "_dom_index": 1,
            "_source_dom_index": 101,
            "_window_owner_dom_index": 900,
            "_window_stack_index": 0,
            "source": "app",
            "app_name": "firefox-pdf",
            "type": "Heading",
            "inner_text": "Quarterly Systems Reliability Report",
            "rect": {"x": 80, "y": 120, "w": 380, "h": 28},
            "reading_order_index": 4,
        },
        {
            "_dom_index": 2,
            "_source_dom_index": 102,
            "_window_owner_dom_index": 900,
            "_window_stack_index": 0,
            "source": "app",
            "app_name": "firefox-pdf",
            "type": "Text",
            "inner_text": "Table 1. Release Validation Metrics",
            "rect": {"x": 80, "y": 220, "w": 220, "h": 20},
            "reading_order_index": 9,
        },
        {
            "_dom_index": 3,
            "_source_dom_index": 103,
            "_window_owner_dom_index": 900,
            "_window_stack_index": 0,
            "source": "app",
            "app_name": "firefox-pdf",
            "type": "Text",
            "inner_text": "Metric Observed Target",
            "rect": {"x": 90, "y": 246, "w": 200, "h": 18},
            "reading_order_index": 10,
        },
        {
            "_dom_index": 4,
            "_source_dom_index": 104,
            "_window_owner_dom_index": 900,
            "_window_stack_index": 0,
            "source": "app",
            "app_name": "firefox-pdf",
            "type": "Text",
            "inner_text": "Availability 99.97% 99.90%",
            "rect": {"x": 90, "y": 270, "w": 220, "h": 18},
            "reading_order_index": 11,
        },
    ]

    mapped, meta = build_pdf_semantic_elements(manifest, leaf_elements)

    assert meta["backend"] == "sidecar"
    assert meta["num_blocks_source"] == 2
    assert meta["num_blocks_mapped"] == 2
    assert [row["type"] for row in mapped] == ["Heading", "Table"]
    assert mapped[0]["rect"] == {"x": 80, "y": 120, "w": 380, "h": 28}
    assert mapped[1]["rect"] == {"x": 80, "y": 220, "w": 230, "h": 68}
    assert mapped[1]["matched_leaf_reading_order_indices"] == [9, 10, 11]
    assert mapped[0]["window_reading_order_index"] == 0


def test_extract_generic_pdf_semantics_finds_table_and_code_blocks() -> None:
    rows = extract_generic_pdf_semantics(
        Path(__file__).resolve().parents[1] / "assets/audit/sample_report.pdf"
    )

    labels = [row["label"] for row in rows]
    assert "Table" in labels
    assert "Code snippet" in labels
    assert any("Quarterly Systems Reliability Report" in row.get("text", "") for row in rows)
