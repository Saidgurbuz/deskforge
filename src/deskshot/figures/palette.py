"""One colour per element family, so every figure in the project agrees.

`render_annotations.py` used to pick a colour with `hash(kind) % 997`. Python
salts `hash()` for strings, so the same capture came out a different colour on
every invocation - two figures in one paper would disagree about what blue
means. Colours here are a fixed table.

Twenty-seven element types appear in a typical capture and fifty-five exist in
the schema. Fifty-five hues are not distinguishable, so types are grouped into
seven **families** that carry the distinction a reader actually needs - is this
a container, a control, a piece of text, an icon - and the family owns the hue.
The palette is Okabe-Ito, which stays separable under the common forms of
colour blindness and survives greyscale printing.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Dict, Optional, Tuple

#: family -> (hex colour, one-line meaning shown in a legend)
FAMILIES: "OrderedDict[str, Tuple[str, str]]" = OrderedDict(
    (
        ("container", ("#7a8aa0", "windows, panels, bars")),
        ("action", ("#0072b2", "buttons and links")),
        ("menu", ("#009e73", "menus and their items")),
        ("input", ("#56b4e9", "fields, choices, toggles")),
        ("text", ("#e69f00", "text and headings")),
        ("media", ("#cc79a7", "icons, images, video")),
        ("structure", ("#d55e00", "lists, tables, tabs")),
    )
)

NEUTRAL = "#8c93a0"

#: Exact type names, as `elements.leaf.json` writes them (title case, spaces).
TYPE_FAMILY: Dict[str, str] = {}


def _assign(family: str, *type_names: str) -> None:
    for name in type_names:
        TYPE_FAMILY[name.lower()] = family


_assign(
    "container",
    "Window", "Dialog", "Alert", "Panel", "Side Bar", "Navigation Bar",
    "Toolbar", "Status Bar", "Tab Bar", "Menu Bar", "Title Bar", "Header",
    "Footer", "Form", "Card", "Modal", "Popup", "Tooltip", "Desktop",
)
_assign(
    "action",
    "Button", "Link", "Toggles", "Steppers", "Icon Button", "Close Button",
    "Minimize Button", "Maximize Button", "Breadcrumb",
)
_assign("menu", "Menu", "Menu Item", "Context Menu", "Dropdown", "Submenu")
_assign(
    "input",
    "Text Input", "Search Field", "Select", "Checkbox", "Radiobox", "Slider",
    "Switch", "Spinner", "Date Picker", "Color Picker", "File Input",
    "Text Area", "Combo Box", "Progress Bar",
)
_assign("text", "Text", "Heading", "Paragraph", "Caption", "Label", "Code")
_assign(
    "media",
    "Image", "Video", "Icon", "File Icon", "Folder Icon", "Avatar", "Logo",
    "Chart", "Canvas", "Map",
)
_assign(
    "structure",
    "List", "List Item", "Table", "Table Cell", "Table Row", "Table Header",
    "Tree", "Tree Item", "Tab", "Scroll", "Scroll Bar", "Divider", "Grid",
)

#: Substring rules for a type the table has not met - a new label in a later
#: schema still lands somewhere sensible instead of all going grey. Order
#: matters: "file icon" must reach `media` before "file" reaches `input`.
_KEYWORDS = (
    ("icon", "media"), ("image", "media"), ("video", "media"),
    ("avatar", "media"), ("logo", "media"), ("chart", "media"),
    ("menu", "menu"), ("dropdown", "menu"),
    ("button", "action"), ("link", "action"), ("toggle", "action"),
    ("input", "input"), ("field", "input"), ("box", "input"),
    ("select", "input"), ("slider", "input"), ("picker", "input"),
    ("heading", "text"), ("text", "text"), ("label", "text"),
    ("list", "structure"), ("table", "structure"), ("tree", "structure"),
    ("tab", "structure"), ("scroll", "structure"), ("row", "structure"),
    ("bar", "container"), ("window", "container"), ("panel", "container"),
    ("dialog", "container"), ("alert", "container"),
)


def family_of(type_name: Optional[str]) -> str:
    """The family a type belongs to, or "other" if nothing claims it."""
    if not type_name:
        return "other"
    key = str(type_name).strip().lower()
    if key in TYPE_FAMILY:
        return TYPE_FAMILY[key]
    for needle, family in _KEYWORDS:
        if needle in key:
            return family
    return "other"


def colour_of(type_name: Optional[str]) -> str:
    """`#rrggbb` for an element type. Stable across processes and releases."""
    return FAMILIES.get(family_of(type_name), (NEUTRAL, ""))[0]


def rgb(colour: str) -> Tuple[int, int, int]:
    colour = colour.lstrip("#")
    return int(colour[0:2], 16), int(colour[2:4], 16), int(colour[4:6], 16)


def readable_ink(colour: str) -> Tuple[int, int, int]:
    """Black or white text on `colour`, whichever a reader can actually read.

    Relative luminance per WCAG, thresholded where the two contrast ratios
    cross rather than at a guessed midpoint.
    """
    def channel(value: int) -> float:
        c = value / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = rgb(colour)
    luminance = 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
    return (17, 17, 20) if luminance > 0.179 else (255, 255, 255)


def legend_entries(counts: Dict[str, int]) -> list:
    """Families present in a capture, biggest first, with their type counts.

    Takes a type->count mapping and returns
    [{family, colour, meaning, count, types: [(type, count), ...]}, ...].
    """
    grouped: Dict[str, Dict[str, int]] = {}
    for type_name, count in counts.items():
        grouped.setdefault(family_of(type_name), {})[str(type_name)] = count
    rows = []
    for family, types in grouped.items():
        colour, meaning = FAMILIES.get(family, (NEUTRAL, "unclassified"))
        rows.append({
            "family": family,
            "colour": colour,
            "meaning": meaning,
            "count": sum(types.values()),
            "types": sorted(types.items(), key=lambda kv: (-kv[1], kv[0])),
        })
    rows.sort(key=lambda row: (-row["count"], row["family"]))
    return rows
