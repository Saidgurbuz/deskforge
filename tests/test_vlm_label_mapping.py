"""Tests for conservative VLM label refinement."""

from deskshot.extraction.vlm_label_mapping import assign_vlm_labels


def _elem(idx, role, rect, parent=None, *, elem_type=None, text="", source="app"):
    return {
        "_dom_index": idx,
        "_parent_dom_index": parent,
        "parent_index": parent,
        "_children_dom_indices": [],
        "children_indices": [],
        "role": role,
        "type": elem_type or role,
        "inner_text": text,
        "name": None,
        "attrs": {},
        "source": source,
        "rect": dict(rect),
    }


def test_vlm_label_defaults_to_existing_type() -> None:
    elements = [
        _elem(0, "push button", {"x": 10, "y": 10, "w": 80, "h": 30}, elem_type="Button", text="Save"),
    ]

    meta = assign_vlm_labels(elements, viewport_width=800, viewport_height=600)

    assert meta["num_refined"] == 0
    assert elements[0]["vlm_label"] == "Button"


def test_vlm_label_promotes_search_field() -> None:
    elements = [
        {
            **_elem(0, "entry", {"x": 100, "y": 20, "w": 280, "h": 32}, elem_type="Text Input", text="Search docs"),
            "attrs": {"description": "Search"},
        },
    ]

    assign_vlm_labels(elements, viewport_width=1200, viewport_height=800)

    assert elements[0]["vlm_label"] == "Search Field"


def test_vlm_label_promotes_search_bar_container() -> None:
    elements = [
        _elem(0, "tool bar", {"x": 0, "y": 0, "w": 600, "h": 40}, elem_type="Toolbar"),
        {
            **_elem(1, "entry", {"x": 180, "y": 4, "w": 220, "h": 32}, 0, elem_type="Text Input", text="Search"),
            "attrs": {"description": "Search"},
        },
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[0]["children_indices"] = [1]

    assign_vlm_labels(elements, viewport_width=1200, viewport_height=800)

    assert elements[0]["vlm_label"] == "Search Bar"
    assert elements[1]["vlm_label"] == "Search Field"


def test_vlm_label_promotes_utility_button() -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 800, "h": 600}, elem_type="Window", text="Browser"),
        _elem(1, "push button", {"x": 760, "y": 0, "w": 24, "h": 24}, 0, elem_type="Button", text="Close"),
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[0]["children_indices"] = [1]

    assign_vlm_labels(elements, viewport_width=1200, viewport_height=800)

    assert elements[1]["vlm_label"] == "Utility Button"


def test_vlm_label_keeps_regular_small_button_as_button() -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 800, "h": 600}, elem_type="Window", text="Calculator"),
        _elem(1, "push button", {"x": 220, "y": 260, "w": 36, "h": 36}, 0, elem_type="Button", text="="),
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[0]["children_indices"] = [1]

    assign_vlm_labels(elements, viewport_width=1200, viewport_height=800)

    assert elements[1]["vlm_label"] == "Button"


def test_vlm_label_keeps_named_browser_button_as_button() -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 1200, "h": 900}, elem_type="Window", text="Chromium"),
        _elem(1, "push button", {"x": 1020, "y": 120, "w": 34, "h": 34}, 0, elem_type="Button", text="You"),
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[0]["children_indices"] = [1]

    assign_vlm_labels(elements, viewport_width=1200, viewport_height=900)

    assert elements[1]["vlm_label"] == "Button"


def test_vlm_label_promotes_edit_menu() -> None:
    elements = [
        _elem(0, "menu item", {"x": 20, "y": 5, "w": 60, "h": 24}, elem_type="Menu", text="Edit"),
    ]

    assign_vlm_labels(elements, viewport_width=800, viewport_height=600)

    assert elements[0]["vlm_label"] == "EditMenu"


def test_vlm_label_maps_desktop_frame_to_screen() -> None:
    elements = [
        _elem(0, "desktop frame", {"x": 0, "y": 0, "w": 1920, "h": 1080}, elem_type="Window", text="Desktop", source="desktop_chrome"),
    ]

    assign_vlm_labels(elements, viewport_width=1920, viewport_height=1080)

    assert elements[0]["vlm_label"] == "Screen"
