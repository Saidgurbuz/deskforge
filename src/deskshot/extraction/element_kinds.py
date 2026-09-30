"""A second, smaller vocabulary for what an element *is*.

The legacy 55 ScreenTag classes stay exactly as they are; this is emitted beside
them as `kind` so the two can be compared on real captures before anything
downstream changes.

## Why a second vocabulary

Measured over 4,763 leaf elements (`v238_final`):

- **25 of the 55 classes ever appear.** 30 are dead vocabulary.
- `Text` 33.9% + `Button` 24.9% + `File Icon` 12.3% = **71% in three classes**,
  while `List` got 3, `Tab Bar` 1 and `Table` 1.

So it is too fine and too coarse at once, in three specific ways:

**Splits with no visual difference.** A two-state control was three classes -
`Toggles` 266, `Checkbox` 10, `Radiobox` 12 - split by which role the *toolkit*
chose rather than by anything on the screen. A GTK toggle button and a check box
are frequently the same picture. Here they are one `toggle`, and which state it
is in is already carried by the state tokens, which is where a visible fact
belongs.

**A catch-all that erased structure.** `Text` absorbed 1,122 table cells along
with statics, labels, sections and paragraphs, so "this is a grid cell" - which
is visible, and is exactly what a screen parser should report - was thrown away.
`cell` is separate here.

**A class that guessed content instead of naming a widget.** `File Icon` came
from `icon` (322) *and* `table cell` (264): a file manager's semantics leaking
into a general vocabulary, and mistyping cells on the way.

## The rule each entry has to pass

A kind earns its place only if it is **visually distinguishable** from the
others and **functionally distinct**. Anything that fails the first test is a
state token or an attribute; anything that fails the second is a merge. That is
why there is no `search field` apart from `textinput` (same picture, and the
placeholder text already says which it is), no `tooltip` apart from `window`,
and no `separator` at all (thin rules are decoration this pipeline
deliberately does not annotate).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

#: The vocabulary. Twenty-six, grouped only for reading - the field is flat.
KINDS = (
    # Structure a person sees as a region of the screen.
    "window", "titlebar", "menubar", "toolbar", "statusbar", "sidebar",
    # Navigation.
    "menu", "menuitem", "tab", "link",
    # Controls.
    "button", "toggle", "textinput", "select", "slider", "scrollbar", "progress",
    # Content.
    "text", "heading", "image", "icon", "canvas",
    # Collections, where the structure itself is visible.
    "list", "listitem", "table", "cell",
)

#: AT-SPI role -> kind. Roles absent here fall through to `_by_shape`, and any
#: role that reaches neither is reported by `unmapped_roles` rather than being
#: silently typed - a vocabulary that quietly swallows the unknown is how the
#: legacy one ended up with a 34% catch-all.
ROLE_TO_KIND: Dict[str, str] = {
    "frame": "window", "window": "window", "dialog": "window",
    "alert": "window", "file chooser": "window", "tool tip": "window",
    "title bar": "titlebar",
    "menu bar": "menubar",
    "tool bar": "toolbar", "toolbar": "toolbar",
    "status bar": "statusbar",

    "menu": "menu", "popup menu": "menu",
    "menu item": "menuitem",
    "check menu item": "menuitem", "radio menu item": "menuitem",
    "page tab": "tab",
    "link": "link",

    "push button": "button",
    "toggle button": "toggle", "check box": "toggle", "radio button": "toggle",
    "switch": "toggle",

    "entry": "textinput", "text": "textinput", "password text": "textinput",
    "spin button": "textinput", "search field": "textinput",
    "combo box": "select", "list box": "select",
    "slider": "slider", "scroll bar": "scrollbar",
    "progress bar": "progress",

    "label": "text", "static": "text", "paragraph": "text", "caption": "text",
    "heading": "heading",
    "image": "image", "icon": "icon",
    "drawing area": "canvas", "canvas": "canvas", "video": "canvas",
    "terminal": "canvas", "document web": "canvas",

    "list": "list", "tree": "list", "tree table": "table", "table": "table",
    "list item": "listitem", "tree item": "listitem",
    "table cell": "cell", "table column header": "cell", "table row": "cell",
}

#: Roles that are containers this pipeline does not annotate on their own. They
#: are recorded so `unmapped_roles` does not report them as gaps.
STRUCTURAL_ROLES = frozenset({
    "panel", "filler", "section", "split pane", "scroll pane", "viewport",
    "page tab list", "application", "desktop frame", "layered pane",
    "separator", "group", "landmark", "form", "redundant object", "unknown",
})


def _by_shape(elem: Dict[str, Any]) -> Optional[str]:
    """Last resort: decide from what the element carries, not its role name.

    Toolkits invent roles. Rather than grow the table forever, an unknown role
    that behaves like a labelled control is read as one, and everything else is
    left unmapped so it shows up in the audit.
    """
    attrs = elem.get("attrs") or {}
    interfaces = attrs.get("interfaces") or {}
    states = attrs.get("states") or {}
    if interfaces.get("editable_text") or states.get("editable"):
        return "textinput"
    if str(elem.get("inner_text") or "").strip():
        return "text"
    return None


def kind_for(elem: Dict[str, Any]) -> Optional[str]:
    """The kind for one element, or None when nothing can be said honestly."""
    role = str(elem.get("role") or "").strip().lower()
    kind = ROLE_TO_KIND.get(role)
    if kind is not None:
        return kind
    if role in STRUCTURAL_ROLES:
        return None
    return _by_shape(elem)


def annotate_kinds(elements) -> Dict[str, Any]:
    """Set `kind` on each element. Returns counts, including what was missed."""
    counts: Dict[str, int] = {}
    unmapped: Dict[str, int] = {}
    for elem in elements:
        kind = kind_for(elem)
        if kind is None:
            role = str(elem.get("role") or "").strip().lower()
            if role not in STRUCTURAL_ROLES:
                unmapped[role] = unmapped.get(role, 0) + 1
            continue
        elem["kind"] = kind
        counts[kind] = counts.get(kind, 0) + 1
    return {"counts": counts, "unmapped_roles": unmapped,
            "num_kinds_used": len(counts)}
