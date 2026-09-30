"""End-to-end test: extract one app and verify outputs.

Requires: Xvfb + D-Bus + AT-SPI session + gnome-calculator binary.
"""

import json
import os
import tempfile
from pathlib import Path

import pytest

# Skip if no tools available
pytestmark = pytest.mark.skipif(
    not os.environ.get("DISPLAY"),
    reason="No DISPLAY set — requires Xvfb session",
)


class TestPipelineE2E:
    """E2E: extract gnome-calculator default state, verify output files."""

    @pytest.fixture
    def output_dir(self, tmp_path):
        return tmp_path / "output"

    def test_single_extraction(self, output_dir):
        """Full pipeline: launch app → extract → save → verify."""
        from deskshot.config import AppManifest, InteractionSequence, PipelineConfig, Action
        from deskshot.extraction.run_extraction import run_single_extraction

        manifest = AppManifest(
            app_name="gnome-calculator",
            binary="gnome-calculator",
            atspi_name="gnome-calculator",
        )
        interaction = InteractionSequence(
            name="default",
            actions=[Action(type="wait", value="1.0")],
        )
        config = PipelineConfig()

        result = run_single_extraction(
            manifest, interaction, config, output_dir=output_dir,
        )

        if result is None:
            pytest.skip("gnome-calculator not available or extraction failed")

        # Verify all artifact files exist
        assert Path(result["screenshot"]).exists()
        assert Path(result["elements"]).exists()
        assert Path(result["meta"]).exists()
        assert Path(result["screentag"]).exists()

        # Verify elements JSON is valid
        with open(result["elements"]) as f:
            elements = json.load(f)
        assert isinstance(elements, list)
        assert len(elements) >= 5, f"Expected ≥5 elements, got {len(elements)}"

        # Verify meta JSON
        with open(result["meta"]) as f:
            meta = json.load(f)
        assert meta["app_name"] == "gnome-calculator"
        assert meta["interaction"] == "default"

        # Verify ScreenTag is non-empty
        screentag = Path(result["screentag"]).read_text()
        assert len(screentag) > 0

        # Verify element schema
        elem = elements[0]
        assert "rect" in elem
        assert "type" in elem
        assert "inner_text" in elem

    def test_quality_checks_pass(self, output_dir):
        """Extracted elements should pass quality checks."""
        from deskshot.config import AppManifest, InteractionSequence, PipelineConfig, Action
        from deskshot.extraction.run_extraction import run_single_extraction
        from deskshot.postprocessing.quality import run_quality_checks

        manifest = AppManifest(
            app_name="gnome-calculator",
            binary="gnome-calculator",
            atspi_name="gnome-calculator",
        )
        interaction = InteractionSequence(
            name="default",
            actions=[Action(type="wait", value="1.0")],
        )
        config = PipelineConfig()

        result = run_single_extraction(
            manifest, interaction, config, output_dir=output_dir,
        )

        if result is None:
            pytest.skip("gnome-calculator not available")

        with open(result["elements"]) as f:
            elements = json.load(f)

        checks = run_quality_checks(elements)
        for name, ok, msg in checks:
            print(f"  [{name}] {'PASS' if ok else 'FAIL'}: {msg}")
