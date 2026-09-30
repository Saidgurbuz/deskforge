"""Map AT-SPI2 roles to the 55-class ScreenTag taxonomy.

Reference: gi.repository.Atspi.Role enum (130 values in AT-SPI 2.40).
Target: webshot canonical classes from vlm_refine.CANON_CLASSES.
"""

from __future__ import annotations

from typing import Optional

# ── AT-SPI Role name → ScreenTag class ──────────────────────────────────
#
# AT-SPI role names use the value_nick format (lowercase, hyphens)
# as returned by Atspi.Role.get_name() or role.value_nick.
#
# Roles not in this mapping are classified as None (skipped or fallback).

ROLE_TO_SCREENTAG: dict[str, str] = {
    # ── Interactive controls ──
    "button":               "Button",
    "push-button":          "Button",
    "toggle-button":        "Toggles",
    "check-box":            "Checkbox",
    "check-menu-item":      "Checkbox",
    "radio-button":         "Radiobox",
    "radio-menu-item":      "Radiobox",
    "entry":                "Text Input",
    "password-text":        "Text Input",
    "text":                 "Text Input",
    "spin-button":          "Steppers",
    "combo-box":            "Select",
    "list-box":             "Select",
    "slider":               "Slider",
    "link":                 "Link",

    # ── Menus ──
    "menu-bar":             "Navigation Bar",
    "menu":                 "Menu",
    "menu-item":            "Menu",
    "popup-menu":           "PopUp Menu",
    "tearoff-menu-item":    "Menu",

    # ── Tabs ──
    "page-tab":             "Tab",
    "page-tab-list":        "Tab Bar",

    # ── Bars ──
    "tool-bar":             "Toolbar",
    "status-bar":           "Status Bar",
    "scroll-bar":           "Scroll",
    "progress-bar":         "Progress bar",
    "info-bar":             "Alert",
    "level-bar":            "Progress bar",

    # ── Text & content ──
    "label":                "Text",
    "paragraph":            "Text",
    "heading":              "Heading",
    "static":               "Text",
    "caption":              "Text",
    "block-quote":          "Text",
    "comment":              "Text",
    "content-deletion":     "Text",
    "content-insertion":    "Text",
    "description-list":     "List",
    "description-term":     "Heading",
    "description-value":    "Text",
    "document-text":        "Text",
    "document-web":         "Text",
    "document-email":       "Text",
    "document-spreadsheet": "Table",
    "document-presentation":"Text",

    # ── Lists & tables ──
    "list":                 "List",
    "list-item":            "List Item",
    "table":                "Table",
    "table-cell":           "Text",
    "table-column-header":  "Heading",
    "table-row":            "List Item",
    "table-row-header":     "Heading",
    "tree":                 "List",
    "tree-item":            "List Item",
    "tree-table":           "Table",

    # ── Media ──
    "image":                "Image",
    "icon":                 "File Icon",
    "animation":            "Image",
    "canvas":               "Image",
    "chart":                "Chart",
    "video":                "Video",
    "audio":                "Video",  # no separate Audio class in ScreenTag

    # ── Containers & windows ──
    "frame":                "Window",
    "dialog":               "Window",
    "file-chooser":         "Window",
    "color-chooser":        "Window",
    "font-chooser":         "Window",
    "alert":                "Alert",
    "notification":         "Notification",
    "tooltip":              "Tooltip",
    "calendar":             "Calendar",

    # ── Navigation ──
    "scroll-pane":          "Scroll",
    "split-pane":           "Side Bar",
    "separator":            "Text",  # visual separator, treat as decorative text

    # ── Forms & misc widgets ──
    "form":                 "Text",  # no separate Form class; children carry semantics
    "editbar":              "Text Input",
    "edit-bar":             "Text Input",

    # ── Misc mapped ──
    # A window's title bar is a visible element with text, and until it was
    # mapped nothing covered it: measured across a 12-scene batch, 42% of all
    # flagged uncovered ink sat in the title-bar band.
    "title bar":            "Heading",
    "title-bar":            "Heading",
    "header":               "Heading",
    "footer":               "Status Bar",
    "section":              "Text",
    "redundant-object":     None,  # skip
    "application":          None,  # skip (top-level container)
    "desktop-frame":        None,  # skip
    "filler":               None,  # skip (layout spacer)
    "panel":                None,  # skip (generic container — children carry info)
    "viewport":             None,  # skip
    "glass-pane":           None,  # skip
    "layered-pane":         None,  # skip
    "embedded":             None,  # skip
    "unknown":              None,  # skip
    "invalid":              None,  # skip
    "extended":             None,  # skip
    "window":               "Window",
}

# Roles to always skip (not useful for UI annotation)
SKIP_ROLES: set[str] = {
    role for role, cls in ROLE_TO_SCREENTAG.items() if cls is None
} | {
    "invalid", "redundant-object", "extended",
    "desktop-frame", "application",
}


def _normalize(role_name: str) -> str:
    """Normalize role name: AT-SPI may return spaces or hyphens."""
    return role_name.replace(" ", "-").lower()


def get_screentag_class(role_name: str) -> Optional[str]:
    """Map an AT-SPI role name to a ScreenTag class.

    Args:
        role_name: AT-SPI role name (e.g. "push-button" or "push button").

    Returns:
        ScreenTag class name, or None if the role should be skipped.
    """
    return ROLE_TO_SCREENTAG.get(_normalize(role_name))


def should_skip_role(role_name: str) -> bool:
    """Check if this AT-SPI role should be skipped during extraction."""
    return _normalize(role_name) in SKIP_ROLES


# ── Reverse mapping for debugging ────────────────────────────────────────

def get_roles_for_class(screentag_class: str) -> list[str]:
    """Get all AT-SPI roles that map to a given ScreenTag class."""
    return [
        role for role, cls in ROLE_TO_SCREENTAG.items()
        if cls == screentag_class
    ]


# ── Canonical ScreenTag classes (55 classes from webshot) ────────────────

CANONICAL_CLASSES: list[str] = [
    "Table", "Column/Browser", "Button", "Utility Button", "App Icon",
    "Navigation Bar", "Status Bar", "Search Field", "Toolbar", "Tooltip",
    "Video", "Tab Bar", "Side Bar", "Slider", "Picker",
    "ContextMenu", "DockMenu", "EditMenu", "Image", "Scroll",
    "Switch", "File Icon", "Chart", "Window", "Screen",
    "List", "List Item", "PopUp Menu", "Steppers", "Toggles",
    "Text Input", "Rating Indicator", "Checkbox", "Radiobox", "Select",
    "Avatar", "Badge", "Alert", "Progress bar", "Bottom navigation",
    "Breadcrumb", "Page control", "Link", "Menu", "Pagination",
    "Tab", "Search Bar", "Date-Time picker", "Calendar", "Text",
    "Heading", "Code snippet", "Carousel", "Notification", "Logo",
]

CANONICAL_CLASSES_SET: set[str] = set(CANONICAL_CLASSES)


# ── Desktop chrome detection ─────────────────────────────────────────────

DESKTOP_CHROME_APPS: set[str] = {
    # XFCE (xfwm4 still used as WM)
    "xfce4-panel",
    "xfdesktop",
    "wrapper-2.0",
    "xfce4-session",
    "xfsettingsd",
    "xfwm4",
    "xfce4-notifyd",
    "xfce4-power-manager",
    "nm-applet",
    "panel",
    # MATE panel + Caja desktop
    "mate-panel",
    "caja",
    "caja-desktop",
    "plank",
    # In patched/no-sudo deployments, Caja can appear as "ata_" in AT-SPI.
    "ata_",
    "clock-applet",
    "wnck-applet",
    "notification-area",
}


def is_desktop_chrome(app_name: str) -> bool:
    """Check if an AT-SPI app name belongs to desktop chrome."""
    name_lower = app_name.lower()
    for chrome_app in DESKTOP_CHROME_APPS:
        if chrome_app in name_lower:
            return True
    return False
