"""Unit tests for local extraction post-processing cleanup."""

from PIL import Image, ImageDraw

from deskshot.extraction.run_extraction import (
    _cleanup_elements,
    _build_leaf_elements,
    _clip_elements_to_visible_ancestor_chain,
    _repair_detached_menu_popup_coordinates,
    _suppress_blank_stacked_text_alternatives,
    _suppress_redundant_nested_same_area,
    _suppress_semantic_atomic_duplicates,
    _suppress_redundant_same_area_clusters,
    _text_norm,
)


def _elem(idx, role, rect, parent=None, text="", source="app", app_name="firefox"):
    return {
        "_dom_index": idx,
        "_parent_dom_index": parent,
        "parent_index": parent,
        "_children_dom_indices": [],
        "children_indices": [],
        "role": role,
        "type": role,
        "inner_text": text,
        "source": source,
        "app_name": app_name,
        "rect": dict(rect),
    }


def _write_text_suppression_fixture(tmp_path, elements, *, visible_rects=()):
    image_path = tmp_path / "screenshot.png"
    img = Image.new("RGB", (1300, 900), (247, 247, 247))
    draw = ImageDraw.Draw(img)
    for rect in visible_rects:
        draw.text((rect["x"] + 3, rect["y"] + 3), "Visible text pixels", fill=(30, 30, 30))
    img.save(image_path)
    return image_path


def test_blank_stacked_text_alternatives_are_suppressed(tmp_path) -> None:
    elements = [
        _elem(0, "frame", {"x": 250, "y": 80, "w": 1000, "h": 760}, None, text="Thunderbird", app_name="thunderbird"),
        {**_elem(1, "paragraph", {"x": 321, "y": 366, "w": 887, "h": 28}, 0, text="Thunderbird lets you connect to your existing email account.", app_name="thunderbird"), "type": "Text"},
        {**_elem(2, "paragraph", {"x": 321, "y": 366, "w": 887, "h": 28}, 0, text="Thunderbird lets you organize all your contacts.", app_name="thunderbird"), "type": "Text"},
        {**_elem(3, "paragraph", {"x": 321, "y": 366, "w": 430, "h": 25}, 0, text="Thunderbird lets you connect to all the newsgroups you want.", app_name="thunderbird"), "type": "Text"},
        {**_elem(4, "paragraph", {"x": 317, "y": 455, "w": 550, "h": 51}, 0, text="Thunderbird lets you import mail messages.", app_name="thunderbird"), "type": "Text"},
    ]
    elements[0]["_children_dom_indices"] = [1, 2, 3, 4]
    elements[0]["children_indices"] = [1, 2, 3, 4]
    image_path = _write_text_suppression_fixture(tmp_path, elements, visible_rects=[elements[4]["rect"]])

    out, meta = _suppress_blank_stacked_text_alternatives(elements, image_path)

    texts = {e.get("inner_text") for e in out}
    assert "Thunderbird lets you connect to all the newsgroups you want." not in texts
    assert "Thunderbird lets you import mail messages." in texts
    assert meta["num_dropped"] == 3
    assert meta["num_blank_groups"] == 1
    assert out[0]["children_indices"] == [1]


def test_stacked_text_with_visible_pixels_is_preserved(tmp_path) -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 500, "h": 300}, None, app_name="demo"),
        {**_elem(1, "paragraph", {"x": 80, "y": 90, "w": 260, "h": 30}, 0, text="First visible alternative", app_name="demo"), "type": "Text"},
        {**_elem(2, "paragraph", {"x": 80, "y": 90, "w": 260, "h": 30}, 0, text="Second visible alternative", app_name="demo"), "type": "Text"},
        {**_elem(3, "paragraph", {"x": 80, "y": 91, "w": 240, "h": 29}, 0, text="Third visible alternative", app_name="demo"), "type": "Text"},
    ]
    elements[0]["_children_dom_indices"] = [1, 2, 3]
    elements[0]["children_indices"] = [1, 2, 3]
    image_path = _write_text_suppression_fixture(tmp_path, elements, visible_rects=[elements[1]["rect"]])

    out, meta = _suppress_blank_stacked_text_alternatives(elements, image_path)

    assert [e.get("inner_text") for e in out if e.get("role") == "paragraph"] == [
        "First visible alternative",
        "Second visible alternative",
        "Third visible alternative",
    ]
    assert meta["num_dropped"] == 0
    assert meta["num_candidate_groups"] == 1


def test_blank_single_text_and_actionable_text_are_preserved(tmp_path) -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 500, "h": 300}, None, app_name="demo"),
        {**_elem(1, "paragraph", {"x": 60, "y": 80, "w": 180, "h": 24}, 0, text="Single hidden-looking text", app_name="demo"), "type": "Text"},
        {**_elem(2, "link", {"x": 60, "y": 130, "w": 140, "h": 24}, 0, text="Action link one", app_name="demo"), "type": "Link"},
        {**_elem(3, "link", {"x": 60, "y": 130, "w": 140, "h": 24}, 0, text="Action link two", app_name="demo"), "type": "Link"},
        {**_elem(4, "link", {"x": 60, "y": 130, "w": 140, "h": 24}, 0, text="Action link three", app_name="demo"), "type": "Link"},
    ]
    elements[0]["_children_dom_indices"] = [1, 2, 3, 4]
    elements[0]["children_indices"] = [1, 2, 3, 4]
    image_path = _write_text_suppression_fixture(tmp_path, elements)

    out, meta = _suppress_blank_stacked_text_alternatives(elements, image_path)

    assert [e.get("inner_text") for e in out] == [e.get("inner_text") for e in elements]
    assert meta["num_dropped"] == 0
    assert meta["num_candidate_groups"] == 0


def test_detached_menu_popup_coordinates_are_reanchored_to_menu_bar_anchor() -> None:
    elements = [
        _elem(0, "frame", {"x": 690, "y": 70, "w": 804, "h": 762}, None, text="Mousepad", app_name="mousepad"),
        _elem(1, "menu bar", {"x": 692, "y": 98, "w": 800, "h": 26}, 0, app_name="mousepad"),
        _elem(2, "menu", {"x": 692, "y": 98, "w": 36, "h": 26}, 1, text="File", app_name="mousepad"),
        _elem(3, "menu item", {"x": 8, "y": 86, "w": 325, "h": 8}, 2, text="New", app_name="mousepad"),
        _elem(4, "menu item", {"x": 8, "y": 110, "w": 102, "h": 24}, 2, text="New Window", app_name="mousepad"),
        _elem(5, "menu item", {"x": 8, "y": 134, "w": 102, "h": 24}, 2, text="Open", app_name="mousepad"),
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[0]["children_indices"] = [1]
    elements[1]["_children_dom_indices"] = [2]
    elements[1]["children_indices"] = [2]
    elements[2]["_children_dom_indices"] = [3, 4, 5]
    elements[2]["children_indices"] = [3, 4, 5]

    meta = _repair_detached_menu_popup_coordinates(elements, viewport_w=1600, viewport_h=900)

    by_text = {e.get("inner_text"): e for e in elements}
    assert meta == {"num_roots": 1, "num_elements": 3}
    assert by_text["New"]["rect"] == {"x": 692, "y": 124, "w": 325, "h": 8}
    assert by_text["New Window"]["rect"] == {"x": 692, "y": 148, "w": 102, "h": 24}
    assert by_text["Open"]["rect"] == {"x": 692, "y": 172, "w": 102, "h": 24}
    assert by_text["New"].get("_coords_repaired_from_detached_menu_popup") is True


def test_menu_popup_coordinates_inside_owner_are_not_repaired() -> None:
    elements = [
        _elem(0, "frame", {"x": 690, "y": 70, "w": 804, "h": 762}, None, text="Mousepad", app_name="mousepad"),
        _elem(1, "menu bar", {"x": 692, "y": 98, "w": 800, "h": 26}, 0, app_name="mousepad"),
        _elem(2, "menu", {"x": 692, "y": 98, "w": 36, "h": 26}, 1, text="File", app_name="mousepad"),
        _elem(3, "menu item", {"x": 692, "y": 124, "w": 325, "h": 24}, 2, text="New", app_name="mousepad"),
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[1]["_children_dom_indices"] = [2]
    elements[2]["_children_dom_indices"] = [3]

    meta = _repair_detached_menu_popup_coordinates(elements, viewport_w=1600, viewport_h=900)

    assert meta == {"num_roots": 0, "num_elements": 0}
    assert elements[3]["rect"] == {"x": 692, "y": 124, "w": 325, "h": 24}


def test_nested_same_area_keeps_semantic_child() -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 400, "h": 300}, None),
        _elem(1, "scroll pane", {"x": 20, "y": 20, "w": 300, "h": 200}, 0),
        _elem(2, "document web", {"x": 20, "y": 20, "w": 300, "h": 200}, 1),
    ]

    out = _suppress_redundant_nested_same_area(elements)
    roles = [e["role"] for e in out]
    assert "document web" in roles
    assert "scroll pane" not in roles


def test_same_area_cluster_removes_generic_container_duplicate() -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 600, "h": 400}, None),
        _elem(1, "scroll pane", {"x": 30, "y": 30, "w": 300, "h": 200}, 0),
        _elem(2, "document web", {"x": 30, "y": 30, "w": 300, "h": 200}, 0),
    ]

    out = _suppress_redundant_nested_same_area(elements)
    out = _suppress_redundant_same_area_clusters(out)
    roles = [e["role"] for e in out]
    assert "document web" in roles
    assert "scroll pane" not in roles


def test_same_area_cluster_keeps_conflicting_non_generic_texts() -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 500, "h": 300}, None),
        _elem(1, "push button", {"x": 50, "y": 40, "w": 100, "h": 30}, 0, text="OK"),
        _elem(2, "push button", {"x": 50, "y": 40, "w": 100, "h": 30}, 0, text="Cancel"),
    ]

    out = _suppress_redundant_same_area_clusters(elements)
    texts = sorted((e.get("inner_text") or "") for e in out if e["role"] == "push button")
    assert texts == ["Cancel", "OK"]


def test_hierarchy_visibility_clips_descendant_to_visible_ancestors() -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 800, "h": 600}, None),
        _elem(1, "document web", {"x": 0, "y": 100, "w": 800, "h": 400}, 0),
        _elem(2, "section", {"x": 50, "y": 450, "w": 300, "h": 120}, 1),
        _elem(3, "push button", {"x": 60, "y": 470, "w": 220, "h": 90}, 2, text="Submit"),
    ]

    out, meta = _clip_elements_to_visible_ancestor_chain(elements)

    assert meta["num_clipped"] >= 2
    assert meta["num_dropped"] == 0
    by_role = {e["role"]: e for e in out}
    assert by_role["section"]["rect"] == {"x": 50, "y": 450, "w": 300, "h": 50}
    assert by_role["push button"]["rect"] == {"x": 60, "y": 470, "w": 220, "h": 30}
    assert by_role["push button"]["_hierarchy_visibility_clipped"] is True
    assert by_role["push button"]["is_occluded"] is False
    assert by_role["push button"]["visible_fragments"] == [{"x": 60, "y": 470, "w": 220, "h": 30}]


def test_hierarchy_visibility_preserves_multi_fragment_metadata() -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 300, "h": 200}, None),
        _elem(1, "document web", {"x": 0, "y": 0, "w": 300, "h": 200}, 0),
        _elem(2, "push button", {"x": 10, "y": 10, "w": 100, "h": 40}, 1, text="Split"),
    ]
    elements[2]["visible_fragments"] = [
        {"x": 10, "y": 10, "w": 40, "h": 40},
        {"x": 70, "y": 10, "w": 40, "h": 40},
    ]
    elements[2]["_visible_fragments"] = [
        {"x": 10, "y": 10, "w": 40, "h": 40},
        {"x": 70, "y": 10, "w": 40, "h": 40},
    ]
    elements[2]["_visibility_source_rect"] = {"x": 10, "y": 10, "w": 100, "h": 40}
    elements[2]["is_occluded"] = True
    elements[2]["_is_occluded_by_overlap"] = True

    out, meta = _clip_elements_to_visible_ancestor_chain(elements)

    assert meta["num_dropped"] == 0
    button = [e for e in out if e.get("inner_text") == "Split"][0]
    assert button["visible_fragments"] == [
        {"x": 10, "y": 10, "w": 40, "h": 40},
        {"x": 70, "y": 10, "w": 40, "h": 40},
    ]
    assert button["rect"] == {"x": 10, "y": 10, "w": 100, "h": 40}
    assert button["is_occluded"] is True


def test_hierarchy_visibility_drops_fully_hidden_descendant() -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 800, "h": 600}, None),
        _elem(1, "document web", {"x": 0, "y": 100, "w": 800, "h": 400}, 0),
        _elem(2, "section", {"x": 50, "y": 520, "w": 300, "h": 120}, 1),
        _elem(3, "push button", {"x": 60, "y": 540, "w": 220, "h": 40}, 2, text="Archive"),
    ]

    out, meta = _clip_elements_to_visible_ancestor_chain(elements)

    assert meta["num_dropped"] == 2
    roles = [e["role"] for e in out]
    assert roles == ["frame", "document web"]


def test_hierarchy_visibility_ignores_disjoint_generic_section_ancestors() -> None:
    elements = [
        _elem(0, "frame", {"x": 10, "y": 27, "w": 945, "h": 1053}, None),
        _elem(1, "document web", {"x": 11, "y": 115, "w": 943, "h": 965}, 0),
        _elem(2, "section", {"x": 11, "y": 115, "w": 943, "h": 2}, 1, text="\ufffc"),
        _elem(3, "section", {"x": 11, "y": 115, "w": 943, "h": 2}, 2, text="\ufffc"),
        _elem(4, "landmark", {"x": 306, "y": 467, "w": 353, "h": 207}, 3, text="\ufffc"),
        _elem(5, "section", {"x": 306, "y": 467, "w": 353, "h": 207}, 4, text="\ufffc"),
        {
            **_elem(6, "heading", {"x": 306, "y": 467, "w": 353, "h": 29}, 5, text="Sign in"),
            "type": "Heading",
        },
        {
            **_elem(7, "entry", {"x": 308, "y": 507, "w": 349, "h": 37}, 5, text="Enter your email, phone, or Skype."),
            "type": "Text Input",
        },
    ]

    out, meta = _clip_elements_to_visible_ancestor_chain(elements)

    assert meta["num_dropped"] == 0
    by_text = {e.get("inner_text"): e for e in out}
    assert "Sign in" in by_text
    assert "Enter your email, phone, or Skype." in by_text
    assert by_text["Sign in"]["rect"] == {"x": 306, "y": 467, "w": 353, "h": 29}
    assert by_text["Enter your email, phone, or Skype."]["rect"] == {"x": 308, "y": 507, "w": 349, "h": 37}


def test_hierarchy_visibility_does_not_clip_popup_menu_items_to_menu_label() -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 900, "h": 700}, None, text="Mousepad"),
        _elem(1, "menu bar", {"x": 10, "y": 10, "w": 880, "h": 24}, 0),
        _elem(2, "menu", {"x": 10, "y": 10, "w": 35, "h": 24}, 1, text="File"),
        _elem(3, "menu item", {"x": 16, "y": 42, "w": 220, "h": 22}, 2, text="Open..."),
        _elem(4, "menu item", {"x": 16, "y": 64, "w": 220, "h": 22}, 2, text="Save"),
    ]

    out, meta = _clip_elements_to_visible_ancestor_chain(elements)

    assert meta["num_dropped"] == 0
    by_text = {e.get("inner_text"): e for e in out}
    assert by_text["Open..."]["rect"] == {"x": 16, "y": 42, "w": 220, "h": 22}
    assert by_text["Save"]["rect"] == {"x": 16, "y": 64, "w": 220, "h": 22}


def test_hierarchy_visibility_skips_disjoint_hard_ancestors_for_visible_sidebar_items() -> None:
    elements = [
        _elem(0, "frame", {"x": 130, "y": 80, "w": 800, "h": 706}, None),
        _elem(1, "document web", {"x": 131, "y": 168, "w": 798, "h": 617}, 0),
        _elem(2, "section", {"x": 131, "y": 168, "w": 783, "h": 732}, 1, text="\ufffc"),
        _elem(3, "section", {"x": 131, "y": 168, "w": 783, "h": 617}, 2, text="\ufffc"),
        _elem(4, "section", {"x": 131, "y": 224, "w": 783, "h": 172}, 3, text="\ufffc"),
        _elem(5, "landmark", {"x": 131, "y": 168, "w": 368, "h": 56}, 4, text="\ufffc"),
        _elem(6, "link", {"x": 135, "y": 376, "w": 64, "h": 20}, 5, text="Subscriptions"),
        _elem(7, "static", {"x": 136, "y": 423, "w": 62, "h": 11}, 6, text="Subscriptions"),
    ]

    out, meta = _clip_elements_to_visible_ancestor_chain(elements)

    assert meta["num_dropped"] == 0
    by_text = {e.get("inner_text"): e for e in out if e.get("inner_text")}
    assert "Subscriptions" in by_text
    assert by_text["Subscriptions"]["rect"] == {"x": 136, "y": 423, "w": 62, "h": 11}
    assert by_text["Subscriptions"]["visible_fragments"] == [{"x": 136, "y": 423, "w": 62, "h": 11}]


def test_hierarchy_visibility_keeps_desktop_icons_with_disjoint_desktop_parent() -> None:
    elements = [
        {
            **_elem(0, "frame", {"x": 0, "y": 671, "w": 1366, "h": 97}, None, text="Desktop", source="desktop_chrome", app_name="caja"),
            "_window_stack_index": 1,
        },
        {
            **_elem(1, "scroll pane", {"x": 0, "y": 671, "w": 1366, "h": 97}, 0, text="Content View", source="desktop_chrome", app_name="caja"),
            "_window_stack_index": 1,
        },
        {
            **_elem(2, "icon", {"x": 52, "y": 70, "w": 58, "h": 46}, 1, text="Computer", source="desktop_chrome", app_name="caja"),
            "_window_stack_index": 1,
            "_visibility_source_rect": {"x": 52, "y": 47, "w": 73, "h": 69},
            "_visible_fragments": [{"x": 52, "y": 47, "w": 58, "h": 69}],
            "visible_fragments": [{"x": 52, "y": 47, "w": 58, "h": 69}],
            "_is_occluded_by_overlap": True,
            "is_occluded": True,
        },
    ]

    out, meta = _clip_elements_to_visible_ancestor_chain(elements)

    assert meta["num_dropped"] == 0
    icon = [e for e in out if e.get("inner_text") == "Computer"][0]
    assert icon["visible_fragments"]


def test_hierarchy_visibility_uses_parent_visible_fragments_not_best_fragment_rect() -> None:
    elements = [
        {
            **_elem(0, "frame", {"x": 0, "y": 25, "w": 1366, "h": 191}, None, text="Desktop", source="desktop_chrome", app_name="caja"),
            "_window_stack_index": 1,
            "_visibility_source_rect": {"x": 0, "y": 0, "w": 1366, "h": 768},
            "_visible_fragments": [
                {"x": 0, "y": 25, "w": 1366, "h": 191},
                {"x": 0, "y": 216, "w": 507, "h": 372},
                {"x": 859, "y": 216, "w": 507, "h": 372},
                {"x": 0, "y": 588, "w": 1366, "h": 180},
            ],
            "visible_fragments": [
                {"x": 0, "y": 25, "w": 1366, "h": 191},
                {"x": 0, "y": 216, "w": 507, "h": 372},
                {"x": 859, "y": 216, "w": 507, "h": 372},
                {"x": 0, "y": 588, "w": 1366, "h": 180},
            ],
            "_is_occluded_by_overlap": True,
            "is_occluded": True,
        },
        {
            **_elem(1, "scroll pane", {"x": 0, "y": 25, "w": 1366, "h": 191}, 0, text="Content View", source="desktop_chrome", app_name="caja"),
            "_window_stack_index": 1,
            "_visibility_source_rect": {"x": 0, "y": 0, "w": 1366, "h": 768},
            "_visible_fragments": [{"x": 0, "y": 0, "w": 1366, "h": 768}],
            "visible_fragments": [{"x": 0, "y": 0, "w": 1366, "h": 768}],
        },
        {
            **_elem(2, "icon", {"x": 34, "y": 182, "w": 110, "h": 34}, 1, text="Filesystem root", source="desktop_chrome", app_name="caja"),
            "_window_stack_index": 1,
            "_visibility_source_rect": {"x": 34, "y": 182, "w": 110, "h": 69},
            "_visible_fragments": [{"x": 34, "y": 182, "w": 110, "h": 69}],
            "visible_fragments": [{"x": 34, "y": 182, "w": 110, "h": 69}],
        },
        {
            **_elem(3, "icon", {"x": 532, "y": 182, "w": 49, "h": 34}, 1, text="Trash", source="desktop_chrome", app_name="caja"),
            "_window_stack_index": 1,
            "_visibility_source_rect": {"x": 532, "y": 182, "w": 49, "h": 69},
            "_visible_fragments": [{"x": 532, "y": 182, "w": 49, "h": 34}],
            "visible_fragments": [{"x": 532, "y": 182, "w": 49, "h": 34}],
            "_is_occluded_by_overlap": True,
            "is_occluded": True,
        },
    ]

    out, meta = _clip_elements_to_visible_ancestor_chain(elements)

    assert meta["num_dropped"] == 0
    by_text = {e.get("inner_text"): e for e in out}
    assert by_text["Filesystem root"]["visible_fragments"] == [{"x": 34, "y": 182, "w": 110, "h": 69}]
    assert by_text["Filesystem root"]["rect"] == {"x": 34, "y": 182, "w": 110, "h": 69}
    assert by_text["Trash"]["visible_fragments"] == [{"x": 532, "y": 182, "w": 49, "h": 34}]
    assert by_text["Trash"]["is_occluded"] is True


def test_hierarchy_visibility_does_not_clip_popup_menu_items_to_lower_owner_frame() -> None:
    elements = [
        {
            **_elem(0, "frame", {"x": 451, "y": 190, "w": 685, "h": 517}, None, text="xarchiver"),
            "_window_stack_index": 4,
        },
        {
            **_elem(1, "menu bar", {"x": 457, "y": 233, "w": 673, "h": 25}, 0),
            "_window_stack_index": 4,
        },
        {
            **_elem(2, "menu", {"x": 457, "y": 233, "w": 61, "h": 25}, 1, text="Archive"),
            "_window_stack_index": 4,
        },
        {
            **_elem(3, "menu item", {"x": 395, "y": 258, "w": 170, "h": 23}, 2, text="New"),
            "_window_stack_index": 5,
        },
        {
            **_elem(4, "menu item", {"x": 395, "y": 281, "w": 170, "h": 23}, 2, text="Open"),
            "_window_stack_index": 5,
        },
    ]

    out, meta = _clip_elements_to_visible_ancestor_chain(elements)

    assert meta["num_dropped"] == 0
    by_text = {e.get("inner_text"): e for e in out}
    assert by_text["New"]["rect"] == {"x": 395, "y": 258, "w": 170, "h": 23}
    assert by_text["Open"]["rect"] == {"x": 395, "y": 281, "w": 170, "h": 23}


def test_leaf_export_does_not_parent_promoted_popup_items_to_lower_window() -> None:
    elements = [
        {
            **_elem(0, "frame", {"x": 600, "y": 260, "w": 640, "h": 500}, None, text="Mousepad", app_name="mousepad"),
            "_window_stack_index": 4,
        },
        {
            **_elem(1, "menu bar", {"x": 640, "y": 293, "w": 320, "h": 26}, 0, app_name="mousepad"),
            "_window_stack_index": 4,
        },
        {
            **_elem(2, "menu", {"x": 640, "y": 293, "w": 36, "h": 26}, 1, text="File", app_name="mousepad"),
            "_window_stack_index": 4,
        },
        {
            **_elem(3, "menu item", {"x": 646, "y": 325, "w": 325, "h": 24}, 2, text="New", app_name="mousepad"),
            "_window_stack_index": 5,
        },
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[0]["children_indices"] = [1]
    elements[1]["_children_dom_indices"] = [2]
    elements[1]["children_indices"] = [2]
    elements[2]["_children_dom_indices"] = [3]
    elements[2]["children_indices"] = [3]

    leaves = _build_leaf_elements(elements)

    window = next(e for e in leaves if e.get("role") == "frame")
    file_menu = next(e for e in leaves if e.get("inner_text") == "File")
    popup_item = next(e for e in leaves if e.get("inner_text") == "New")
    assert file_menu["parent_index"] == window["_dom_index"]
    assert popup_item["parent_index"] is None


def test_hierarchy_visibility_ignores_disjoint_same_window_split_pane() -> None:
    elements = [
        {
            **_elem(0, "frame", {"x": 451, "y": 190, "w": 685, "h": 517}, None, text="xarchiver"),
            "_window_stack_index": 4,
        },
        {
            **_elem(1, "split pane", {"x": 457, "y": 462, "w": 673, "h": 218}, 0),
            "_window_stack_index": 4,
        },
        {
            **_elem(2, "page tab list", {"x": 658, "y": 346, "w": 472, "h": 334}, 1),
            "_window_stack_index": 4,
        },
        {
            **_elem(3, "scroll pane", {"x": 659, "y": 347, "w": 470, "h": 332}, 2),
            "_window_stack_index": 4,
        },
        {
            **_elem(4, "table", {"x": 659, "y": 347, "w": 470, "h": 332}, 3),
            "_window_stack_index": 4,
        },
        {
            **_elem(5, "table column header", {"x": 659, "y": 347, "w": 111, "h": 24}, 4, text="Filename"),
            "_window_stack_index": 4,
        },
        {
            **_elem(6, "table cell", {"x": 683, "y": 420, "w": 89, "h": 22}, 4, text="README.md"),
            "_window_stack_index": 4,
        },
    ]

    out, meta = _clip_elements_to_visible_ancestor_chain(elements)

    assert meta["num_dropped"] == 0
    by_text = {e.get("inner_text"): e for e in out}
    assert by_text["Filename"]["rect"] == {"x": 659, "y": 347, "w": 111, "h": 24}
    assert by_text["README.md"]["rect"] == {"x": 683, "y": 420, "w": 89, "h": 22}


def test_same_area_cluster_drops_placeholder_text_container_duplicate() -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 600, "h": 400}, None, text="VS Code"),
        _elem(1, "section", {"x": 50, "y": 50, "w": 200, "h": 40}, 0, text="\ufffc"),
        _elem(2, "push button", {"x": 50, "y": 50, "w": 200, "h": 40}, 0, text="Open"),
    ]

    out = _suppress_redundant_same_area_clusters(elements)
    roles = [e["role"] for e in out]
    assert "push button" in roles
    assert "section" not in roles


def test_semantic_duplicate_drops_generic_list_item_wrapper_around_button() -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 900, "h": 700}, None, text="Microsoft 365 Copilot"),
        {
            **_elem(1, "list item", {"x": 385, "y": 115, "w": 86, "h": 52}, 0, text="\ufffc"),
            "type": "List Item",
        },
        {
            **_elem(2, "push button", {"x": 385, "y": 116, "w": 86, "h": 51}, 0, text="Products\ufffc"),
            "type": "Button",
        },
    ]

    out = _suppress_semantic_atomic_duplicates(elements)

    roles = [e["role"] for e in out]
    texts = [e.get("inner_text") for e in out]
    assert "push button" in roles
    assert "list item" not in roles
    assert any((t or "").startswith("Products") for t in texts)


def test_text_norm_strips_placeholder_glyphs() -> None:
    assert _text_norm("\ufffc  Open \u200b") == "Open"


def test_nested_same_area_keeps_text_input_leaf_over_page_tab_list() -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 650, "h": 500}, None),
        _elem(1, "page tab list", {"x": 20, "y": 60, "w": 600, "h": 380}, 0),
        _elem(2, "scroll pane", {"x": 20, "y": 60, "w": 600, "h": 380}, 1),
        {
            **_elem(3, "text", {"x": 20, "y": 60, "w": 600, "h": 380}, 2),
            "type": "Text Input",
        },
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[1]["_children_dom_indices"] = [2]
    elements[2]["_children_dom_indices"] = [3]
    elements[0]["children_indices"] = [1]
    elements[1]["children_indices"] = [2]
    elements[2]["children_indices"] = [3]

    out = _suppress_redundant_nested_same_area(elements)
    roles = [e["role"] for e in out]
    types = [e["type"] for e in out]

    assert "page tab list" not in roles
    assert "Text Input" in types


def test_semantic_duplicate_prefers_heading_over_child_text() -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 500, "h": 300}, None),
        {
            **_elem(1, "heading", {"x": 30, "y": 40, "w": 120, "h": 28}, 0, text="SEARCH"),
            "type": "Heading",
        },
        {
            **_elem(2, "static", {"x": 32, "y": 46, "w": 120, "h": 13}, 1, text="SEARCH"),
            "type": "Text",
        },
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[1]["_children_dom_indices"] = [2]
    elements[0]["children_indices"] = [1]
    elements[1]["children_indices"] = [2]

    out = _suppress_semantic_atomic_duplicates(elements)
    roles = [e["role"] for e in out]
    texts = [e.get("inner_text") for e in out]

    assert "heading" in roles
    assert "static" not in roles
    assert texts.count("SEARCH") == 1


def test_leaf_export_promotes_atomic_button_over_text_child() -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 500, "h": 300}, None),
        {
            **_elem(1, "push button", {"x": 40, "y": 60, "w": 120, "h": 36}, 0, text="Save"),
            "type": "Button",
        },
        {
            **_elem(2, "static", {"x": 60, "y": 70, "w": 60, "h": 13}, 1, text="Save"),
            "type": "Text",
        },
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[1]["_children_dom_indices"] = [2]
    elements[0]["children_indices"] = [1]
    elements[1]["children_indices"] = [2]

    out = _build_leaf_elements(elements)

    assert [e["role"] for e in out] == ["push button"]
    assert out[0]["inner_text"] == "Save"
    assert out[0]["parent_index"] is None
    assert out[0]["children_indices"] == []
    assert out[0]["_source_dom_index"] == 1


def test_leaf_export_keeps_top_level_app_window_for_context() -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 500, "h": 300}, None, text="Calculator", app_name="gnome-calculator"),
        {
            **_elem(1, "push button", {"x": 40, "y": 60, "w": 120, "h": 36}, 0, text="Save", app_name="gnome-calculator"),
            "type": "Button",
        },
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[0]["children_indices"] = [1]

    out = _build_leaf_elements(elements)

    assert [e["role"] for e in out] == ["frame", "push button"]
    assert out[0]["inner_text"] == "Calculator"
    assert out[1]["inner_text"] == "Save"
    assert out[0]["children_indices"] == [1]
    assert out[1]["parent_index"] == 0


def test_top_level_app_window_with_controls_survives_same_area_cleanup() -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 900, "h": 700}, None, text="\ufffc", app_name="vscode"),
        _elem(1, "push button", {"x": 804, "y": 0, "w": 32, "h": 32}, 0, text="Minimize", app_name="vscode"),
        _elem(2, "push button", {"x": 836, "y": 0, "w": 32, "h": 32}, 0, text="Maximize", app_name="vscode"),
        _elem(3, "push button", {"x": 868, "y": 0, "w": 32, "h": 32}, 0, text="Close", app_name="vscode"),
        _elem(4, "section", {"x": 0, "y": 0, "w": 900, "h": 700}, 0, text="\ufffc", app_name="vscode"),
        _elem(5, "section", {"x": 0, "y": 0, "w": 900, "h": 700}, 4, text="\ufffc", app_name="vscode"),
    ]
    elements[0]["_children_dom_indices"] = [1, 2, 3, 4]
    elements[0]["children_indices"] = [1, 2, 3, 4]
    elements[4]["_children_dom_indices"] = [5]
    elements[4]["children_indices"] = [5]

    out = _suppress_redundant_nested_same_area(elements)
    out = _suppress_redundant_same_area_clusters(out)

    roles = [e["role"] for e in out]
    assert "frame" in roles
    assert roles.count("section") < 2

    leaves = _build_leaf_elements(out)
    leaf_roles = [e["role"] for e in leaves]
    assert "frame" in leaf_roles


def test_leaf_export_assigns_same_app_contained_leaf_to_window_when_parent_chain_is_broken() -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 900, "h": 700}, None, text="\ufffc", app_name="vscode"),
        _elem(1, "push button", {"x": 804, "y": 0, "w": 32, "h": 32}, 0, text="Minimize", app_name="vscode"),
        _elem(2, "push button", {"x": 836, "y": 0, "w": 32, "h": 32}, 0, text="Maximize", app_name="vscode"),
        _elem(3, "push button", {"x": 868, "y": 0, "w": 32, "h": 32}, 0, text="Close", app_name="vscode"),
        {
            **_elem(4, "entry", {"x": 120, "y": 120, "w": 240, "h": 32}, None, text="Search", app_name="vscode"),
            "type": "Text Input",
        },
    ]
    elements[0]["_children_dom_indices"] = [1, 2, 3]
    elements[0]["children_indices"] = [1, 2, 3]

    out = _build_leaf_elements(elements)

    assert [e["role"] for e in out[:2]] == ["frame", "push button"]
    frame = out[0]
    entry = next(e for e in out if e["role"] == "entry")
    assert entry["parent_index"] == frame["_dom_index"]
    assert entry["_dom_index"] in frame["children_indices"]


def test_leaf_export_prefers_window_stack_hint_over_same_app_containment() -> None:
    elements = [
        {
            **_elem(0, "frame", {"x": 500, "y": 250, "w": 604, "h": 436}, None, text="archive", app_name="xarchiver"),
            "_window_stack_index": 4,
            "_window_owner_dom_index": 77,
        },
        {
            **_elem(1, "frame", {"x": 814, "y": 106, "w": 680, "h": 710}, None, text="create archive", app_name="xarchiver"),
            "_window_stack_index": 5,
            "_window_owner_dom_index": 183,
        },
        {
            **_elem(2, "label", {"x": 854, "y": 274, "w": 104, "h": 36}, None, text="Documents", app_name="xarchiver"),
            "type": "Text",
            "_window_stack_index": 5,
            "_window_owner_dom_index": 183,
        },
    ]

    out = _build_leaf_elements(elements)

    windows = [e for e in out if e["role"] == "frame"]
    assert len(windows) == 2
    dialog = next(e for e in windows if e.get("_window_stack_index") == 5)
    documents = next(e for e in out if e.get("inner_text") == "Documents")
    assert documents["parent_index"] == dialog["_dom_index"]
    assert documents["_dom_index"] in dialog["children_indices"]


def test_leaf_export_keeps_text_child_when_parent_button_has_no_label() -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 500, "h": 300}, None),
        {
            **_elem(1, "push button", {"x": 40, "y": 60, "w": 120, "h": 36}, 0, text=""),
            "type": "Button",
        },
        {
            **_elem(2, "static", {"x": 60, "y": 70, "w": 60, "h": 13}, 1, text="Save"),
            "type": "Text",
        },
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[1]["_children_dom_indices"] = [2]
    elements[0]["children_indices"] = [1]
    elements[1]["children_indices"] = [2]

    out = _build_leaf_elements(elements)

    assert [e["role"] for e in out] == ["static"]
    assert out[0]["inner_text"] == "Save"


def test_semantic_duplicate_drops_icon_glyph_child_under_atomic_parent() -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 500, "h": 300}, None),
        {
            **_elem(1, "page tab", {"x": 30, "y": 40, "w": 40, "h": 40}, 0, text=""),
            "type": "Tab",
        },
        {
            **_elem(2, "text", {"x": 38, "y": 48, "w": 16, "h": 16}, 1, text=""),
            "type": "Text",
        },
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[1]["_children_dom_indices"] = [2]
    elements[0]["children_indices"] = [1]
    elements[1]["children_indices"] = [2]

    out = _suppress_semantic_atomic_duplicates(elements)
    assert [e["role"] for e in out] == ["frame", "page tab"]

    leaves = _build_leaf_elements(out)
    assert [e["role"] for e in leaves] == ["page tab"]


def test_semantic_duplicate_keeps_table_cell_icon_subcell_but_drops_tiny_spacer() -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 500, "h": 300}, None, app_name="thunar"),
        {
            **_elem(1, "table cell", {"x": 114, "y": 279, "w": 166, "h": 24}, 0, text="", app_name="thunar"),
            "type": "Text",
        },
        {
            **_elem(2, "table cell", {"x": 114, "y": 279, "w": 12, "h": 24}, 1, text="", app_name="thunar"),
            "type": "Text",
        },
        {
            **_elem(3, "table cell", {"x": 128, "y": 279, "w": 24, "h": 24}, 1, text="", app_name="thunar"),
            "type": "Text",
        },
        {
            **_elem(4, "table cell", {"x": 154, "y": 279, "w": 130, "h": 24}, 1, text="File System", app_name="thunar"),
            "type": "Text",
        },
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[1]["_children_dom_indices"] = [2, 3, 4]
    elements[0]["children_indices"] = [1]
    elements[1]["children_indices"] = [2, 3, 4]

    out = _suppress_semantic_atomic_duplicates(elements)
    rects = {(e["rect"]["x"], e["rect"]["w"], e.get("inner_text", "")) for e in out}

    assert (114, 12, "") not in rects
    assert (128, 24, "") in rects
    assert (154, 130, "File System") in rects

    leaves = _build_leaf_elements(out)
    leaf_rects = {(e["rect"]["x"], e["rect"]["w"], e.get("inner_text", "")) for e in leaves}
    assert (128, 24, "") in leaf_rects
    assert (154, 130, "File System") in leaf_rects


def test_semantic_duplicate_keeps_list_item_icon_partition_with_text_sibling() -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 900, "h": 700}, None, app_name="baobab"),
        {
            **_elem(1, "list item", {"x": 132, "y": 135, "w": 698, "h": 86}, 0, text="", app_name="baobab"),
            "type": "List Item",
        },
        {
            **_elem(2, "icon", {"x": 146, "y": 143, "w": 64, "h": 64}, 1, text="", app_name="baobab"),
            "type": "File Icon",
        },
        {
            **_elem(3, "label", {"x": 222, "y": 158, "w": 97, "h": 17}, 1, text="Home Folder", app_name="baobab"),
            "type": "Text",
        },
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[1]["_children_dom_indices"] = [2, 3]
    elements[0]["children_indices"] = [1]
    elements[1]["children_indices"] = [2, 3]

    out = _suppress_semantic_atomic_duplicates(elements)
    roles = [e["role"] for e in out]
    assert "icon" in roles
    assert "label" in roles

    leaves = _build_leaf_elements(out)
    leaf_roles = [e["role"] for e in leaves]
    assert "icon" in leaf_roles
    assert "label" in leaf_roles


def test_semantic_duplicate_keeps_link_child_under_generic_tree_item() -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 1800, "h": 2200}, None, app_name="chromium-browser"),
        {
            **_elem(1, "tree item", {"x": 1244, "y": 1835, "w": 197, "h": 37}, 0, text="\ufffc", app_name="chromium-browser"),
            "type": "List Item",
        },
        {
            **_elem(2, "link", {"x": 1244, "y": 1835, "w": 197, "h": 37}, 1, text="Scientific", app_name="chromium-browser"),
            "type": "Link",
        },
        {
            **_elem(3, "static", {"x": 1255, "y": 1845, "w": 54, "h": 17}, 2, text="Scientific", app_name="chromium-browser"),
            "type": "Text",
        },
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[1]["_children_dom_indices"] = [2]
    elements[2]["_children_dom_indices"] = [3]
    elements[0]["children_indices"] = [1]
    elements[1]["children_indices"] = [2]
    elements[2]["children_indices"] = [3]

    out = _suppress_semantic_atomic_duplicates(
        _suppress_redundant_same_area_clusters(
            _suppress_redundant_nested_same_area(elements)
        )
    )

    filtered = {(e["role"], e.get("inner_text", "")) for e in out}
    assert ("link", "Scientific") in filtered

    leaves = _build_leaf_elements(out)
    leaf = {(e["role"], e.get("inner_text", "")) for e in leaves}
    assert ("link", "Scientific") in leaf


def test_cleanup_keeps_same_role_row_text_partition() -> None:
    elements = [
        {
            **_elem(0, "table cell", {"x": 2120, "y": 274, "w": 1158, "h": 26}, None, text="", app_name="thunar"),
            "type": "Text",
        },
        {
            **_elem(1, "table cell", {"x": 2146, "y": 274, "w": 1132, "h": 26}, 0, text="assets", app_name="thunar"),
            "type": "Text",
        },
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[0]["children_indices"] = [1]

    out = _cleanup_elements(elements)
    texts = [e.get("inner_text") for e in out]

    assert "" in texts
    assert "assets" in texts


def test_leaf_export_keeps_text_input_with_decorative_icon_child() -> None:
    elements = [
        _elem(0, "frame", {"x": 0, "y": 0, "w": 1800, "h": 400}, None, text="Thunar", app_name="thunar"),
        {
            **_elem(1, "text", {"x": 100, "y": 40, "w": 900, "h": 28}, 0, text="/home/mira/projects/deskforge/", app_name="thunar"),
            "type": "Text Input",
        },
        {
            **_elem(2, "icon", {"x": 110, "y": 46, "w": 22, "h": 16}, 1, text="dialog-error-symbolic", app_name="thunar"),
            "type": "File Icon",
        },
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[0]["children_indices"] = [1]
    elements[1]["_children_dom_indices"] = [2]
    elements[1]["children_indices"] = [2]

    leaves = _build_leaf_elements(elements)
    texts = [e.get("inner_text") for e in leaves]

    assert "/home/mira/projects/deskforge/" in texts
    assert "dialog-error-symbolic" not in texts


def test_leaf_export_keeps_page_tab_alongside_close_button() -> None:
    elements = [
        _elem(0, "frame", {"x": 110, "y": 80, "w": 502, "h": 578}, None, text="Browser", app_name="chromium-browser"),
        _elem(1, "page tab list", {"x": 111, "y": 81, "w": 429, "h": 41}, 0, app_name="chromium-browser"),
        {
            **_elem(2, "page tab", {"x": 139, "y": 81, "w": 256, "h": 41}, 1, text="\ufffc\ufffc", app_name="chromium-browser"),
            "type": "Tab",
        },
        {
            **_elem(3, "push button", {"x": 353, "y": 87, "w": 28, "h": 28}, 2, text="Close", app_name="chromium-browser"),
            "type": "Button",
        },
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[0]["children_indices"] = [1]
    elements[1]["_children_dom_indices"] = [2]
    elements[1]["children_indices"] = [2]
    elements[2]["_children_dom_indices"] = [3]
    elements[2]["children_indices"] = [3]

    leaves = _build_leaf_elements(elements)
    roles = {(e["role"], e.get("inner_text")) for e in leaves}

    assert ("page tab", "\ufffc\ufffc") in roles
    assert ("push button", "Close") in roles


def test_leaf_export_keeps_menu_anchor_alongside_popup_children() -> None:
    elements = [
        _elem(0, "frame", {"x": 600, "y": 260, "w": 640, "h": 500}, None, text="Mousepad", app_name="mousepad"),
        _elem(1, "menu bar", {"x": 640, "y": 293, "w": 320, "h": 26}, 0, app_name="mousepad"),
        _elem(2, "menu", {"x": 640, "y": 293, "w": 36, "h": 26}, 1, text="File", app_name="mousepad"),
        _elem(3, "menu item", {"x": 646, "y": 325, "w": 325, "h": 24}, 2, text="New", app_name="mousepad"),
        _elem(4, "menu item", {"x": 646, "y": 349, "w": 325, "h": 24}, 2, text="Open...", app_name="mousepad"),
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[0]["children_indices"] = [1]
    elements[1]["_children_dom_indices"] = [2]
    elements[1]["children_indices"] = [2]
    elements[2]["_children_dom_indices"] = [3, 4]
    elements[2]["children_indices"] = [3, 4]

    leaves = _build_leaf_elements(elements)
    roles = {(e["role"], e.get("inner_text")) for e in leaves}

    assert ("menu", "File") in roles
    assert ("menu item", "New") in roles
    assert ("menu item", "Open...") in roles


def test_drop_fully_hidden_elements_removes_invisible_leaves() -> None:
    """Fully covered elements must not be serialized as empty boxes."""
    from deskshot.extraction.visibility_fragments import drop_fully_hidden_elements

    elements = [
        {"_dom_index": 1, "is_occluded": False, "visible_fragments": [{"x": 0, "y": 0, "w": 9, "h": 9}]},
        {"_dom_index": 2, "is_occluded": True, "visible_fragments": [{"x": 0, "y": 0, "w": 4, "h": 9}]},
        {"_dom_index": 3, "is_occluded": True, "visible_fragments": []},
    ]

    removed = drop_fully_hidden_elements(elements)

    assert removed == 1
    assert [e["_dom_index"] for e in elements] == [1, 2]


def test_drop_fully_hidden_elements_keeps_containers_with_children() -> None:
    """A covered container may still be needed to nest a visible child."""
    from deskshot.extraction.visibility_fragments import drop_fully_hidden_elements

    elements = [
        {"_dom_index": 1, "is_occluded": True, "visible_fragments": [], "children_indices": [2]},
        {"_dom_index": 2, "is_occluded": False, "visible_fragments": [{"x": 0, "y": 0, "w": 5, "h": 5}]},
    ]

    assert drop_fully_hidden_elements(elements) == 0
    assert len(elements) == 2


def test_drop_fully_hidden_elements_prunes_dangling_child_refs() -> None:
    from deskshot.extraction.visibility_fragments import drop_fully_hidden_elements

    elements = [
        {"_dom_index": 1, "is_occluded": False, "visible_fragments": [{"x": 0, "y": 0, "w": 9, "h": 9}],
         "children_indices": [2, 3]},
        {"_dom_index": 2, "is_occluded": False, "visible_fragments": [{"x": 0, "y": 0, "w": 4, "h": 4}]},
        {"_dom_index": 3, "is_occluded": True, "visible_fragments": []},
    ]

    assert drop_fully_hidden_elements(elements) == 1
    assert elements[0]["children_indices"] == [2]


def test_partition_hidden_elements_splits_without_mutating() -> None:
    from deskshot.extraction.visibility_fragments import partition_hidden_elements

    elements = [
        {"_dom_index": 1, "is_occluded": False, "visible_fragments": [{"x": 0, "y": 0, "w": 9, "h": 9}]},
        {"_dom_index": 2, "is_occluded": True, "visible_fragments": []},
    ]

    visible, hidden = partition_hidden_elements(elements)

    assert [e["_dom_index"] for e in visible] == [1]
    assert [e["_dom_index"] for e in hidden] == [2]
    assert len(elements) == 2          # input untouched


def test_build_amodal_elements_restores_hidden_extent() -> None:
    """Amodal keeps what exists; a hidden element regains its pre-clip extent."""
    from deskshot.extraction.visibility_fragments import build_amodal_elements

    visible = [{"_dom_index": 1, "occlusion_state": "partial",
                "visible_fragments": [{"x": 0, "y": 0, "w": 4, "h": 9}]}]
    hidden = [{"_dom_index": 2, "occlusion_state": "hidden",
               "rect": {"x": 5, "y": 5, "w": 1, "h": 1},
               "_occlusion_original_rect": {"x": 5, "y": 5, "w": 60, "h": 20},
               "visible_fragments": []}]

    amodal = build_amodal_elements(visible, hidden)

    assert len(amodal) == 2
    assert amodal[0]["amodal_visibility"] == "partial"
    restored = amodal[1]
    assert restored["amodal_visibility"] == "hidden"
    assert restored["rect"] == {"x": 5, "y": 5, "w": 60, "h": 20}
    assert restored["visible_fragments"] == []


def test_build_amodal_elements_does_not_mutate_inputs() -> None:
    from deskshot.extraction.visibility_fragments import build_amodal_elements

    visible = [{"_dom_index": 1, "occlusion_state": "none"}]
    hidden = [{"_dom_index": 2, "rect": {"x": 0, "y": 0, "w": 2, "h": 2},
               "_occlusion_original_rect": {"x": 0, "y": 0, "w": 40, "h": 10}}]

    build_amodal_elements(visible, hidden)

    assert "amodal_visibility" not in visible[0]
    assert hidden[0]["rect"] == {"x": 0, "y": 0, "w": 2, "h": 2}


def test_detached_menu_popup_is_placed_on_its_real_popup_window() -> None:
    """A correctly reported popup must not be dragged back into its frame.

    Measured on scene 800002: xarchiver's Archive menu is drawn at (661,373)
    while its frame sits at (1093,536), because the scene moves the window after
    the menu opens and an override-redirect popup does not move with it. AT-SPI
    reported the items correctly, but the anchor-only repair saw "popup outside
    its frame" and moved all ten items 211px down into the frame - which left
    the drawn popup with no annotations at all and dropped four xarchiver
    toolbar buttons as occluded by the relocated menu.

    The popup's own X window is the measurement that settles it: the drawn area
    (661,373,197,218) holds the item union (185x206) with symmetric padding.
    """
    elements = [
        _elem(0, "frame", {"x": 1093, "y": 536, "w": 745, "h": 482}, None, text="archive.zip - xarchiver", app_name="xarchiver"),
        _elem(1, "menu bar", {"x": 1094, "y": 564, "w": 170, "h": 26}, 0, app_name="xarchiver"),
        _elem(2, "menu", {"x": 1094, "y": 564, "w": 67, "h": 26}, 1, text="Archive", app_name="xarchiver"),
        _elem(3, "menu item", {"x": 667, "y": 379, "w": 185, "h": 24}, 2, text="New", app_name="xarchiver"),
        _elem(4, "menu item", {"x": 667, "y": 403, "w": 185, "h": 24}, 2, text="Open", app_name="xarchiver"),
        _elem(5, "menu item", {"x": 667, "y": 561, "w": 185, "h": 24}, 2, text="Quit", app_name="xarchiver"),
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[0]["children_indices"] = [1]
    elements[1]["_children_dom_indices"] = [2]
    elements[1]["children_indices"] = [2]
    elements[2]["_children_dom_indices"] = [3, 4, 5]
    elements[2]["children_indices"] = [3, 4, 5]

    meta = _repair_detached_menu_popup_coordinates(
        elements,
        viewport_w=1920,
        viewport_h=1080,
        popup_content_rects=[{"x": 661, "y": 373, "w": 197, "h": 218}],
    )

    assert meta == {"num_roots": 0, "num_elements": 0}
    assert elements[3]["rect"] == {"x": 667, "y": 379, "w": 185, "h": 24}
    assert elements[5]["rect"] == {"x": 667, "y": 561, "w": 185, "h": 24}


def test_owner_local_menu_popup_snaps_onto_its_popup_window_when_known() -> None:
    """Owner-local coordinates are repaired from the window, not the anchor.

    Mousepad reports its open menu's items relative to the window origin
    (x=8, y=86). The menu bar anchor gives a usable estimate, but the popup's
    own X window is a measurement, so it wins when both are available.
    """
    elements = [
        _elem(0, "frame", {"x": 690, "y": 70, "w": 804, "h": 762}, None, text="Mousepad", app_name="mousepad"),
        _elem(1, "menu bar", {"x": 692, "y": 98, "w": 800, "h": 26}, 0, app_name="mousepad"),
        _elem(2, "menu", {"x": 692, "y": 98, "w": 36, "h": 26}, 1, text="File", app_name="mousepad"),
        _elem(3, "menu item", {"x": 8, "y": 86, "w": 102, "h": 24}, 2, text="New", app_name="mousepad"),
        _elem(4, "menu item", {"x": 8, "y": 110, "w": 102, "h": 24}, 2, text="Open", app_name="mousepad"),
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[0]["children_indices"] = [1]
    elements[1]["_children_dom_indices"] = [2]
    elements[1]["children_indices"] = [2]
    elements[2]["_children_dom_indices"] = [3, 4]
    elements[2]["children_indices"] = [3, 4]

    meta = _repair_detached_menu_popup_coordinates(
        elements,
        viewport_w=1600,
        viewport_h=900,
        # Drawn menu: item union (102x48) plus 6px of border/padding per side.
        popup_content_rects=[{"x": 686, "y": 118, "w": 114, "h": 60}],
    )

    assert meta == {"num_roots": 1, "num_elements": 2}
    assert elements[3]["rect"] == {"x": 692, "y": 124, "w": 102, "h": 24}
    assert elements[4]["rect"] == {"x": 692, "y": 148, "w": 102, "h": 24}
    assert elements[3]["_detached_menu_popup_repair_source"] == "popup_window"


def test_unrelated_popup_window_does_not_capture_a_menu() -> None:
    """A window that cannot hold the menu must not be used to place it.

    Matching is by fit: a window whose drawn area is smaller than the item
    union, or far larger than it, belongs to some other popup. Without that
    check a tooltip or a second app's menu could teleport this one.
    """
    elements = [
        _elem(0, "frame", {"x": 690, "y": 70, "w": 804, "h": 762}, None, text="Mousepad", app_name="mousepad"),
        _elem(1, "menu bar", {"x": 692, "y": 98, "w": 800, "h": 26}, 0, app_name="mousepad"),
        _elem(2, "menu", {"x": 692, "y": 98, "w": 36, "h": 26}, 1, text="File", app_name="mousepad"),
        _elem(3, "menu item", {"x": 8, "y": 86, "w": 102, "h": 24}, 2, text="New", app_name="mousepad"),
        _elem(4, "menu item", {"x": 8, "y": 110, "w": 102, "h": 24}, 2, text="Open", app_name="mousepad"),
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[1]["_children_dom_indices"] = [2]
    elements[2]["_children_dom_indices"] = [3, 4]
    elements[2]["children_indices"] = [3, 4]

    meta = _repair_detached_menu_popup_coordinates(
        elements,
        viewport_w=1600,
        viewport_h=900,
        popup_content_rects=[
            {"x": 100, "y": 100, "w": 60, "h": 20},      # too small to hold it
            {"x": 200, "y": 200, "w": 600, "h": 400},    # far too large
        ],
    )

    assert meta == {"num_roots": 1, "num_elements": 2}
    assert elements[3]["_detached_menu_popup_repair_source"] == "menu_bar_anchor"
    assert elements[3]["rect"] == {"x": 692, "y": 124, "w": 102, "h": 24}


def test_a_search_entry_survives_the_leaf_export_like_any_other_text_field() -> None:
    """Nautilus's header bar carries a 198x46 search entry (AT-SPI role `text`)
    holding a single 16x16 `edit-find-symbolic` icon. `assign_element_types`
    derives Search Field and Text Input from the same branches and splits them
    only on whether a search signal is present, but only Text Input counted as
    an atomic control - so the search entry was dropped from the leaf set and
    the 16x16 icon was all that remained of the widget on screen.
    """
    def _entry(elem_type):
        window = _elem(0, "frame", {"x": 814, "y": 82, "w": 691, "h": 710},
                       None, text="Home", app_name="nautilus")
        entry = {**_elem(1, "text", {"x": 901, "y": 86, "w": 198, "h": 46}, 0,
                         text="Search", app_name="nautilus"), "type": elem_type}
        icon = {**_elem(2, "icon", {"x": 910, "y": 101, "w": 16, "h": 16}, 1,
                        text="edit-find-symbolic", app_name="nautilus"),
                "type": "Image"}
        window["_children_dom_indices"] = [1]
        entry["_children_dom_indices"] = [2]
        return [window, entry, icon]

    search = _build_leaf_elements(_entry("Search Field"))
    plain = _build_leaf_elements(_entry("Text Input"))

    kept = [(e["role"], e["rect"]["w"]) for e in search if e["role"] != "frame"]
    assert kept == [("text", 198)]
    # The two spellings of the same widget must survive identically.
    assert kept == [(e["role"], e["rect"]["w"]) for e in plain if e["role"] != "frame"]


def test_a_nameless_icon_is_kept_when_no_control_around_it_covers_it() -> None:
    """xarchiver draws a 16x20 status graphic at (1225,1338) that AT-SPI reports
    as a nameless `icon` child of the frame. It reached the filtered export and
    was dropped from the leaf export, leaving that part of the screen with no
    annotation at all - the only widget-level gap left in the 5-app scene after
    the tree-cell and border work.

    The same icon inside a button is decoration: the button is annotated and its
    box already covers the graphic, so keeping both would double-annotate it.
    """
    def _under(parent_role):
        parent = _elem(0, parent_role, {"x": 139, "y": 725, "w": 1103, "h": 634},
                       None, text="", app_name="xarchiver")
        icon = _elem(1, "icon", {"x": 1225, "y": 1338, "w": 16, "h": 20}, 0,
                     text="", app_name="xarchiver")
        parent["_children_dom_indices"] = [1]
        return [parent, icon]

    on_frame = _build_leaf_elements(_under("frame"))
    in_button = _build_leaf_elements(_under("push button"))

    assert [e["role"] for e in on_frame] == ["icon"]
    assert [e["role"] for e in in_button] == ["push button"]


def test_a_nameless_dialog_is_kept_as_a_window() -> None:
    """A transmission licence dialog - 592x167, unoccluded, holding a label and
    Cancel/I Agree - was dropped from the export entirely for having no title,
    so a whole visible window went unannotated. The title requirement exists to
    skip generic empty root frames; a dialog is never one of those.
    """
    from deskshot.extraction.run_extraction import _keep_leaf_window_candidate

    dialog = {
        "role": "alert",
        "source": "app",
        "app_name": "transmission-gtk",
        "inner_text": "",
        "parent_index": None,
        "_parent_dom_index": None,
        "_source_parent_dom_index": None,
        "_children_dom_indices": [],
    }
    assert _keep_leaf_window_candidate(dialog, None) is True

    # A nameless plain frame is still skipped - that is the case the rule is for.
    frame = dict(dialog, role="frame")
    assert _keep_leaf_window_candidate(frame, None) is False


def test_a_window_carries_its_title_in_screentag() -> None:
    """Every `<window>` serialized as coordinates only, so a reader could not
    tell which application a block belonged to. The title goes in its own token
    rather than as the tag's text, because the tag's text means "these glyphs
    are drawn inside this box" and the title bar element already carries it.
    """
    from deskshot.extraction.run_extraction import _basic_screentag

    elements = [
        {"_dom_index": 0, "role": "frame", "type": "window", "tag": "window",
         "name": "example.xhb - HomeBank", "inner_text": "",
         "rect": {"x": 0, "y": 0, "w": 500, "h": 400},
         "children_indices": [], "_children_dom_indices": [],
         "reading_order_index": 0},
    ]
    tag = _basic_screentag(elements, 1000, 800)
    assert "<title>example.xhb - HomeBank</title>" in tag
    # It must not become the window's rendered text.
    assert "</window>" in tag


def test_a_nameless_window_gets_no_title_token() -> None:
    from deskshot.extraction.run_extraction import _basic_screentag

    elements = [
        {"_dom_index": 0, "role": "alert", "type": "window", "tag": "window",
         "name": "", "inner_text": "",
         "rect": {"x": 0, "y": 0, "w": 500, "h": 400},
         "children_indices": [], "_children_dom_indices": [],
         "reading_order_index": 0},
    ]
    assert "<title>" not in _basic_screentag(elements, 1000, 800)
