from __future__ import annotations

from deskshot.extraction.type_refinement import assign_element_types


def _elem(
    idx: int,
    role: str,
    rect: dict,
    *,
    elem_type: str,
    parent: int | None = None,
    text: str = "",
    name: str | None = None,
    source: str = "app",
    app_name: str = "test-app",
    attrs: dict | None = None,
    children: list[int] | None = None,
):
    return {
        "_dom_index": idx,
        "_parent_dom_index": parent,
        "parent_index": parent,
        "_children_dom_indices": list(children or []),
        "children_indices": list(children or []),
        "role": role,
        "type": elem_type,
        "inner_text": text,
        "name": name,
        "rect": dict(rect),
        "attrs": attrs or {},
        "source": source,
        "app_name": app_name,
    }


def test_refines_non_editable_text_to_text() -> None:
    elements = [
        _elem(
            0,
            "text",
            {"x": 100, "y": 100, "w": 300, "h": 40},
            elem_type="Text Input",
            text="Status message",
            attrs={"interfaces": {"text": True}},
        ),
    ]

    meta = assign_element_types(elements, viewport_width=1200, viewport_height=800)

    assert meta["num_refined"] == 1
    assert elements[0]["type"] == "Text"


def test_keeps_editable_text_as_text_input() -> None:
    elements = [
        _elem(
            0,
            "text",
            {"x": 100, "y": 100, "w": 600, "h": 300},
            elem_type="Text Input",
            text="Editable body",
            attrs={"interfaces": {"text": True}, "states": {"editable": True, "multi_line": True}},
        ),
    ]

    meta = assign_element_types(elements, viewport_width=1200, viewport_height=800)

    assert meta["num_refined"] == 0
    assert elements[0]["type"] == "Text Input"


def test_refines_editable_text_to_search_field() -> None:
    elements = [
        _elem(
            0,
            "text",
            {"x": 100, "y": 100, "w": 420, "h": 36},
            elem_type="Text Input",
            text="Search",
            attrs={
                "interfaces": {"text": True, "editable_text": True},
                "states": {"editable": True, "single_line": True},
            },
        ),
    ]

    meta = assign_element_types(elements, viewport_width=1200, viewport_height=800)

    assert meta["num_refined"] == 1
    assert elements[0]["type"] == "Search Field"


def test_refines_search_combo_box_to_search_field() -> None:
    elements = [
        _elem(
            0,
            "combo box",
            {"x": 300, "y": 100, "w": 260, "h": 32},
            elem_type="Select",
            text="Search",
        ),
    ]

    meta = assign_element_types(elements, viewport_width=1200, viewport_height=800)

    assert meta["num_refined"] == 1
    assert elements[0]["type"] == "Search Field"


def test_refines_baobab_forward_icon_to_button() -> None:
    elements = [
        _elem(
            0,
            "list box",
            {"x": 100, "y": 100, "w": 700, "h": 500},
            elem_type="Select",
            children=[1],
        ),
        _elem(
            1,
            "list item",
            {"x": 110, "y": 120, "w": 680, "h": 86},
            elem_type="List Item",
            parent=0,
            children=[2],
        ),
        _elem(
            2,
            "icon",
            {"x": 740, "y": 132, "w": 36, "h": 64},
            elem_type="File Icon",
            parent=1,
            text="Forward",
            attrs={"action_names": ["click"]},
        ),
    ]

    meta = assign_element_types(elements, viewport_width=1400, viewport_height=900)

    assert meta["num_refined"] == 1
    assert elements[2]["type"] == "Button"


def test_keeps_desktop_icon_as_file_icon() -> None:
    elements = [
        _elem(
            0,
            "icon",
            {"x": 40, "y": 40, "w": 80, "h": 72},
            elem_type="File Icon",
            text="Computer",
            source="desktop_chrome",
            app_name="ata_",
        ),
    ]

    assign_element_types(elements, viewport_width=1400, viewport_height=900)

    assert elements[0]["type"] == "File Icon"


def test_refines_image_table_cell_in_file_row_to_file_icon() -> None:
    elements = [
        _elem(
            0,
            "table cell",
            {"x": 256, "y": 314, "w": 156, "h": 22},
            elem_type="Text",
            children=[1, 2],
        ),
        _elem(
            1,
            "table cell",
            {"x": 256, "y": 314, "w": 16, "h": 22},
            elem_type="Text",
            parent=0,
            attrs={
                "states": {"focusable": True, "selectable": True},
                "interfaces": {"action": True, "image": True},
            },
        ),
        _elem(
            2,
            "table cell",
            {"x": 274, "y": 316, "w": 30, "h": 18},
            elem_type="Text",
            parent=0,
            text="logs",
            attrs={
                "states": {"focusable": True, "selectable": True, "single_line": True},
                "interfaces": {"action": True, "text": True},
            },
        ),
    ]

    meta = assign_element_types(elements, viewport_width=1920, viewport_height=1080)

    assert meta["num_refined"] == 1
    assert elements[1]["type"] == "File Icon"
    assert elements[2]["type"] == "Text"


def test_refines_landmark_to_navigation_bar() -> None:
    elements = [
        _elem(
            0,
            "landmark",
            {"x": 120, "y": 80, "w": 900, "h": 72},
            elem_type="landmark",
        ),
    ]

    meta = assign_element_types(elements, viewport_width=1440, viewport_height=900)

    assert meta["num_refined"] == 1
    assert elements[0]["type"] == "Navigation Bar"


def test_refines_internal_frame_to_window() -> None:
    elements = [
        _elem(
            0,
            "internal frame",
            {"x": 200, "y": 140, "w": 500, "h": 400},
            elem_type="internal frame",
        ),
    ]

    meta = assign_element_types(elements, viewport_width=1440, viewport_height=900)

    assert meta["num_refined"] == 1
    assert elements[0]["type"] == "Window"


def test_keeps_calculator_display_as_text_input_without_editable_flags() -> None:
    elements = [
        _elem(
            0,
            "frame",
            {"x": 800, "y": 400, "w": 420, "h": 320},
            elem_type="Window",
            text="Calculator",
            children=[1],
            app_name="gnome-calculator",
        ),
        _elem(
            1,
            "text",
            {"x": 830, "y": 430, "w": 360, "h": 20},
            elem_type="Text Input",
            parent=0,
            app_name="gnome-calculator",
            attrs={"interfaces": {"text": True}},
        ),
    ]

    meta = assign_element_types(elements, viewport_width=1440, viewport_height=900)

    assert meta["num_refined"] == 0
    assert elements[1]["type"] == "Text Input"
