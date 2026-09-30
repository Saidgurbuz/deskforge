"""Unit tests for AT-SPI role → ScreenTag class mapping."""

import pytest

from deskshot.extraction.role_mapping import (
    CANONICAL_CLASSES_SET,
    ROLE_TO_SCREENTAG,
    SKIP_ROLES,
    get_roles_for_class,
    get_screentag_class,
    is_desktop_chrome,
    should_skip_role,
)


class TestRoleMapping:
    """Tests for individual role → class mappings."""

    def test_plain_button(self):
        assert get_screentag_class("button") == "Button"

    def test_push_button(self):
        assert get_screentag_class("push-button") == "Button"

    def test_check_box(self):
        assert get_screentag_class("check-box") == "Checkbox"

    def test_radio_button(self):
        assert get_screentag_class("radio-button") == "Radiobox"

    def test_entry(self):
        assert get_screentag_class("entry") == "Text Input"

    def test_password_text(self):
        assert get_screentag_class("password-text") == "Text Input"

    def test_combo_box(self):
        assert get_screentag_class("combo-box") == "Select"

    def test_slider(self):
        assert get_screentag_class("slider") == "Slider"

    def test_toggle_button(self):
        assert get_screentag_class("toggle-button") == "Toggles"

    def test_menu_bar(self):
        assert get_screentag_class("menu-bar") == "Navigation Bar"

    def test_menu_item(self):
        assert get_screentag_class("menu-item") == "Menu"

    def test_page_tab(self):
        assert get_screentag_class("page-tab") == "Tab"

    def test_page_tab_list(self):
        assert get_screentag_class("page-tab-list") == "Tab Bar"

    def test_tool_bar(self):
        assert get_screentag_class("tool-bar") == "Toolbar"

    def test_status_bar(self):
        assert get_screentag_class("status-bar") == "Status Bar"

    def test_label(self):
        assert get_screentag_class("label") == "Text"

    def test_heading(self):
        assert get_screentag_class("heading") == "Heading"

    def test_list(self):
        assert get_screentag_class("list") == "List"

    def test_list_item(self):
        assert get_screentag_class("list-item") == "List Item"

    def test_table(self):
        assert get_screentag_class("table") == "Table"

    def test_image(self):
        assert get_screentag_class("image") == "Image"

    def test_icon(self):
        assert get_screentag_class("icon") == "File Icon"

    def test_frame(self):
        assert get_screentag_class("frame") == "Window"

    def test_dialog(self):
        assert get_screentag_class("dialog") == "Window"

    def test_alert(self):
        assert get_screentag_class("alert") == "Alert"

    def test_tooltip(self):
        assert get_screentag_class("tooltip") == "Tooltip"

    def test_progress_bar(self):
        assert get_screentag_class("progress-bar") == "Progress bar"

    def test_scroll_bar(self):
        assert get_screentag_class("scroll-bar") == "Scroll"

    def test_link(self):
        assert get_screentag_class("link") == "Link"

    def test_calendar(self):
        assert get_screentag_class("calendar") == "Calendar"

    def test_notification(self):
        assert get_screentag_class("notification") == "Notification"


class TestSkipRoles:
    """Tests for roles that should be skipped."""

    def test_invalid_skipped(self):
        assert should_skip_role("invalid")

    def test_redundant_object_skipped(self):
        assert should_skip_role("redundant-object")

    def test_application_skipped(self):
        assert should_skip_role("application")

    def test_desktop_frame_skipped(self):
        assert should_skip_role("desktop-frame")

    def test_filler_skipped(self):
        assert should_skip_role("filler")

    def test_panel_skipped(self):
        assert should_skip_role("panel")

    def test_button_not_skipped(self):
        assert not should_skip_role("push-button")

    def test_plain_button_not_skipped(self):
        assert not should_skip_role("button")


class TestMappingCompleteness:
    """Verify mapping quality."""

    def test_all_mapped_classes_are_canonical(self):
        """Every non-None mapped class must be in the canonical 55-class set."""
        for role, cls in ROLE_TO_SCREENTAG.items():
            if cls is not None:
                assert cls in CANONICAL_CLASSES_SET, (
                    f"Role '{role}' maps to '{cls}' which is not a canonical class"
                )

    def test_reverse_mapping(self):
        """Check reverse mapping returns correct roles."""
        button_roles = get_roles_for_class("Button")
        assert "push-button" in button_roles

    def test_unknown_role_returns_none(self):
        """Unknown roles should return None."""
        assert get_screentag_class("nonexistent-role") is None

    def test_space_format_push_button(self):
        """Role names with spaces (AT-SPI runtime format) should also work."""
        assert get_screentag_class("push button") == "Button"

    def test_space_format_toggle_button(self):
        assert get_screentag_class("toggle button") == "Toggles"

    def test_space_format_combo_box(self):
        assert get_screentag_class("combo box") == "Select"

    def test_space_format_menu_item(self):
        assert get_screentag_class("menu item") == "Menu"

    def test_space_format_radio_button(self):
        assert get_screentag_class("radio button") == "Radiobox"

    def test_space_format_scroll_bar(self):
        assert get_screentag_class("scroll bar") == "Scroll"

    def test_space_format_skip_roles(self):
        assert should_skip_role("desktop frame")
        assert should_skip_role("redundant object")
        assert not should_skip_role("push button")


class TestDesktopChromeDetection:
    """Desktop chrome app-name detection should cover known aliases."""

    def test_detects_caja(self):
        assert is_desktop_chrome("caja")
        assert is_desktop_chrome("Caja-desktop")

    def test_detects_patched_caja_alias(self):
        assert is_desktop_chrome("ata_")

    def test_detects_plank(self):
        assert is_desktop_chrome("plank")

    def test_non_chrome_app_rejected(self):
        assert not is_desktop_chrome("gnome-calculator")
