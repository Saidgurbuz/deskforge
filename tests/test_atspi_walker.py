"""Integration tests for AT-SPI tree walker.

These tests require a running Xvfb + D-Bus + AT-SPI session with
an app (gnome-calculator) launched. Run via:

    dsd validate  # first ensure session works
    pytest tests/test_atspi_walker.py -v
"""

import json
import os
import pytest

# Skip all tests if no DISPLAY is set (no Xvfb session)
pytestmark = pytest.mark.skipif(
    not os.environ.get("DISPLAY"),
    reason="No DISPLAY set — requires Xvfb session",
)


class TestAtspiWalker:
    """Integration tests for walk_application()."""

    def test_walk_returns_list(self):
        """walk_application should return a list (even if empty)."""
        from deskshot.extraction.atspi_walker import walk_application
        elements = walk_application("nonexistent-app-xyz")
        assert isinstance(elements, list)

    def test_element_schema(self):
        """If elements are found, they should have the correct schema."""
        from deskshot.extraction.atspi_walker import walk_application
        # Try to find any app on the desktop
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi

        desktop = Atspi.get_desktop(0)
        if desktop is None or desktop.get_child_count() == 0:
            pytest.skip("No apps in AT-SPI tree")

        first_app = desktop.get_child_at_index(0)
        if first_app is None:
            pytest.skip("No accessible apps")

        app_name = first_app.get_name()
        elements = walk_application(app_name)

        if not elements:
            pytest.skip(f"No elements extracted from {app_name}")

        # Check schema of first element
        elem = elements[0]
        required_keys = [
            "tag", "role", "rect", "inner_text", "type",
            "_dom_index", "_parent_dom_index", "_children_dom_indices",
            "_depth", "parent_index", "children_indices",
        ]
        for key in required_keys:
            assert key in elem, f"Missing key: {key}"

        # Check rect structure
        rect = elem["rect"]
        assert "x" in rect and "y" in rect
        assert "w" in rect and "h" in rect

    def test_elements_have_valid_bounds(self):
        """All elements should have positive width/height within viewport."""
        from deskshot.extraction.atspi_walker import walk_application
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi

        desktop = Atspi.get_desktop(0)
        if desktop is None or desktop.get_child_count() == 0:
            pytest.skip("No apps in AT-SPI tree")

        first_app = desktop.get_child_at_index(0)
        app_name = first_app.get_name()
        elements = walk_application(app_name, viewport_w=1920, viewport_h=1080)

        for elem in elements:
            rect = elem["rect"]
            assert rect["w"] >= 2, f"Element too narrow: {rect}"
            assert rect["h"] >= 2, f"Element too short: {rect}"
            assert rect["x"] >= 0, f"Element x negative: {rect}"
            assert rect["y"] >= 0, f"Element y negative: {rect}"

    def test_hierarchy_consistency(self):
        """Parent-child references should be consistent."""
        from deskshot.extraction.atspi_walker import walk_application
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi

        desktop = Atspi.get_desktop(0)
        if desktop is None or desktop.get_child_count() == 0:
            pytest.skip("No apps in AT-SPI tree")

        first_app = desktop.get_child_at_index(0)
        app_name = first_app.get_name()
        elements = walk_application(app_name)

        if len(elements) < 2:
            pytest.skip("Not enough elements for hierarchy test")

        for i, elem in enumerate(elements):
            parent_idx = elem["_parent_dom_index"]
            if parent_idx is not None:
                assert 0 <= parent_idx < len(elements), (
                    f"Element {i} has invalid parent index {parent_idx}"
                )
                parent = elements[parent_idx]
                assert i in parent["_children_dom_indices"], (
                    f"Element {i} claims parent {parent_idx} but parent "
                    f"doesn't list it as child"
                )
