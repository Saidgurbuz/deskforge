"""Tests for depth-aware occlusion clipping."""

from deskshot.extraction.occlusion import (
    WindowLayer,
    _find_xdotool,
    _find_containing_stack_hint,
    _expand_window_stack_with_owner_frames,
    _window_name_is_ignored_decorative,
    apply_occlusion_clipping,
)


def _base_elem(dom_index, role, rect, parent=None, text=""):
    return {
        "_dom_index": dom_index,
        "_parent_dom_index": parent,
        "parent_index": parent,
        "_children_dom_indices": [],
        "children_indices": [],
        "role": role,
        "rect": dict(rect),
        "inner_text": text,
        "type": role,
        "z": 0,
    }


def test_dock_backdrop_window_is_ignored_as_decorative_occluder() -> None:
    assert _window_name_is_ignored_decorative("deskshot-dock-backdrop")
    assert not _window_name_is_ignored_decorative("plank")


def test_find_xdotool_prefers_configured_fix_binary(monkeypatch, tmp_path) -> None:
    fixed_dir = tmp_path / "deskshot_bin_fix"
    fixed_dir.mkdir()
    fixed = fixed_dir / "xdotool"
    fixed.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fixed.chmod(0o755)

    monkeypatch.setenv("DESKSHOT_BIN_FIX_DIR", str(fixed_dir))

    assert _find_xdotool() == str(fixed)


def test_full_occlusion_drops_hidden_elements() -> None:
    elements = [
        _base_elem(0, "frame", {"x": 0, "y": 0, "w": 100, "h": 100}, None, "Back"),
        _base_elem(1, "push button", {"x": 10, "y": 10, "w": 20, "h": 20}, 0, "B"),
        _base_elem(2, "frame", {"x": 0, "y": 0, "w": 100, "h": 100}, None, "Front"),
    ]
    stack = [
        WindowLayer(window_id="1", name="Back", rect={"x": 0, "y": 0, "w": 100, "h": 100}, stack_index=0),
        WindowLayer(window_id="2", name="Front", rect={"x": 0, "y": 0, "w": 100, "h": 100}, stack_index=1),
    ]

    out, meta = apply_occlusion_clipping(
        elements,
        window_stack=stack,
        allow_partial_clip=True,
    )
    assert meta["enabled"] is True
    assert meta["num_dropped"] >= 2
    assert len(out) == 1
    assert out[0]["inner_text"] == "Front"
    assert elements[1]["is_occluded"] is True
    assert elements[1]["visible_fragments"] == []


def test_partial_occlusion_clips_rect() -> None:
    elements = [
        _base_elem(0, "frame", {"x": 0, "y": 0, "w": 160, "h": 120}, None, "Back"),
        _base_elem(1, "push button", {"x": 10, "y": 10, "w": 100, "h": 40}, 0, "Button"),
        _base_elem(2, "frame", {"x": 50, "y": 0, "w": 140, "h": 120}, None, "Front"),
    ]
    stack = [
        WindowLayer(window_id="1", name="Back", rect={"x": 0, "y": 0, "w": 160, "h": 120}, stack_index=0),
        WindowLayer(window_id="2", name="Front", rect={"x": 50, "y": 0, "w": 140, "h": 120}, stack_index=1),
    ]

    out, meta = apply_occlusion_clipping(
        elements,
        window_stack=stack,
        allow_partial_clip=True,
    )
    clipped = [e for e in out if e.get("inner_text") == "Button"][0]
    assert meta["num_clipped"] >= 1
    assert clipped["rect"]["x"] == 10
    assert clipped["rect"]["w"] == 40
    assert clipped.get("_occlusion_clipped") is True
    assert clipped["is_occluded"] is True
    assert clipped["visible_fragments"] == [{"x": 10, "y": 10, "w": 40, "h": 40}]


def test_partial_occlusion_keeps_multiple_visible_fragments() -> None:
    elements = [
        _base_elem(0, "frame", {"x": 0, "y": 0, "w": 160, "h": 120}, None, "Back"),
        _base_elem(1, "push button", {"x": 10, "y": 10, "w": 100, "h": 40}, 0, "Button"),
        _base_elem(2, "frame", {"x": 50, "y": 10, "w": 20, "h": 40}, None, "Front"),
    ]
    stack = [
        WindowLayer(window_id="1", name="Back", rect={"x": 0, "y": 0, "w": 160, "h": 120}, stack_index=0),
        WindowLayer(window_id="2", name="Front", rect={"x": 50, "y": 10, "w": 20, "h": 40}, stack_index=1),
    ]

    out, _meta = apply_occlusion_clipping(
        elements,
        window_stack=stack,
        allow_partial_clip=True,
    )

    clipped = [e for e in out if e.get("inner_text") == "Button"][0]
    assert clipped["rect"] == {"x": 10, "y": 10, "w": 40, "h": 40}
    assert clipped["is_occluded"] is True
    assert clipped["visible_fragments"] == [
        {"x": 10, "y": 10, "w": 40, "h": 40},
        {"x": 70, "y": 10, "w": 40, "h": 40},
    ]


def test_editable_text_input_keeps_widget_rect_under_partial_occlusion() -> None:
    elements = [
        _base_elem(0, "frame", {"x": 0, "y": 0, "w": 320, "h": 220}, None, "Mousepad"),
        _base_elem(1, "text", {"x": 10, "y": 40, "w": 300, "h": 160}, 0, "Notes"),
        _base_elem(2, "frame", {"x": 120, "y": 90, "w": 80, "h": 60}, None, "Popup"),
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[0]["children_indices"] = [1]
    elements[1]["type"] = "Text Input"
    elements[1]["attrs"] = {
        "states": {"editable": True, "multi_line": True},
        "interfaces": {"editable_text": True, "text": True},
    }
    stack = [
        WindowLayer(window_id="1", name="Mousepad", rect={"x": 0, "y": 0, "w": 320, "h": 220}, stack_index=0),
        WindowLayer(window_id="2", name="Popup", rect={"x": 120, "y": 90, "w": 80, "h": 60}, stack_index=1),
    ]

    out, _meta = apply_occlusion_clipping(
        elements,
        window_stack=stack,
        allow_partial_clip=True,
    )

    text = [e for e in out if e.get("inner_text") == "Notes"][0]
    assert text["rect"] == {"x": 10, "y": 40, "w": 300, "h": 160}
    assert text.get("_occlusion_preserved_full_rect") is True
    assert text["is_occluded"] is True
    assert len(text["visible_fragments"]) > 1


def test_enclosing_root_container_keeps_full_rect_on_partial_occlusion() -> None:
    elements = [
        _base_elem(0, "frame", {"x": 0, "y": 0, "w": 200, "h": 160}, None, "Main"),
        _base_elem(1, "scroll pane", {"x": 0, "y": 0, "w": 200, "h": 160}, 0, ""),
        _base_elem(2, "push button", {"x": 10, "y": 10, "w": 100, "h": 40}, 1, "Button"),
        _base_elem(3, "frame", {"x": 80, "y": 0, "w": 90, "h": 90}, None, "Popup"),
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[1]["children_indices"] = [2]
    elements[1]["_children_dom_indices"] = [2]

    stack = [
        WindowLayer(window_id="1", name="Main", rect={"x": 0, "y": 0, "w": 200, "h": 160}, stack_index=0),
        WindowLayer(window_id="2", name="Popup", rect={"x": 80, "y": 0, "w": 90, "h": 90}, stack_index=1),
    ]

    out, meta = apply_occlusion_clipping(
        elements,
        window_stack=stack,
        allow_partial_clip=True,
    )

    main = [e for e in out if e.get("inner_text") == "Main"][0]
    button = [e for e in out if e.get("inner_text") == "Button"][0]
    assert meta["num_clipped"] >= 1
    assert main["rect"] == {"x": 0, "y": 0, "w": 200, "h": 160}
    assert main.get("_occlusion_preserved_full_rect") is True
    assert button.get("_occlusion_clipped") is True


def test_document_web_container_keeps_full_rect_for_visible_top_nav() -> None:
    elements = [
        _base_elem(0, "frame", {"x": 220, "y": 90, "w": 800, "h": 760}, None, "Back"),
        _base_elem(1, "document web", {"x": 225, "y": 182, "w": 790, "h": 663}, 0, ""),
        _base_elem(2, "section", {"x": 225, "y": 182, "w": 775, "h": 898}, 1, ""),
        _base_elem(3, "landmark", {"x": 225, "y": 182, "w": 775, "h": 46}, 2, ""),
        _base_elem(4, "tree", {"x": 226, "y": 185, "w": 773, "h": 43}, 3, ""),
        _base_elem(5, "link", {"x": 226, "y": 185, "w": 128, "h": 43}, 4, "Python"),
        _base_elem(6, "frame", {"x": 700, "y": 250, "w": 860, "h": 720}, None, "Front"),
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[1]["_children_dom_indices"] = [2]
    elements[2]["_children_dom_indices"] = [3]
    elements[3]["_children_dom_indices"] = [4]
    elements[4]["_children_dom_indices"] = [5]
    elements[1]["children_indices"] = [2]
    elements[2]["children_indices"] = [3]
    elements[3]["children_indices"] = [4]
    elements[4]["children_indices"] = [5]

    stack = [
        WindowLayer(window_id="1", name="Back", rect={"x": 220, "y": 90, "w": 800, "h": 760}, stack_index=0),
        WindowLayer(window_id="2", name="Front", rect={"x": 700, "y": 250, "w": 860, "h": 720}, stack_index=1),
    ]

    out, _meta = apply_occlusion_clipping(
        elements,
        window_stack=stack,
        allow_partial_clip=True,
    )

    doc = [e for e in out if e.get("role") == "document web"][0]
    python_link = [e for e in out if e.get("inner_text") == "Python"][0]
    assert doc["rect"] == {"x": 225, "y": 182, "w": 790, "h": 663}
    assert doc.get("_occlusion_preserved_full_rect") is True
    assert python_link["rect"] == {"x": 226, "y": 185, "w": 128, "h": 43}


def test_document_web_container_keeps_full_rect_for_lower_visible_children() -> None:
    elements = [
        _base_elem(0, "frame", {"x": 110, "y": 70, "w": 559, "h": 290}, None, "Browser"),
        _base_elem(1, "document web", {"x": 111, "y": 158, "w": 557, "h": 201}, 0, ""),
        _base_elem(2, "landmark", {"x": 135, "y": 0, "w": 494, "h": 768}, 1, ""),
        _base_elem(3, "static", {"x": 165, "y": 272, "w": 85, "h": 18}, 2, "First released"),
        _base_elem(4, "frame", {"x": 389, "y": 252, "w": 182, "h": 210}, None, "Popup"),
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[1]["_children_dom_indices"] = [2]
    elements[2]["_children_dom_indices"] = [3]
    elements[0]["children_indices"] = [1]
    elements[1]["children_indices"] = [2]
    elements[2]["children_indices"] = [3]

    stack = [
        WindowLayer(window_id="1", name="Browser", rect={"x": 110, "y": 70, "w": 559, "h": 290}, stack_index=0),
        WindowLayer(window_id="2", name="Popup", rect={"x": 389, "y": 252, "w": 182, "h": 210}, stack_index=1),
    ]

    out, _meta = apply_occlusion_clipping(
        elements,
        window_stack=stack,
        allow_partial_clip=True,
    )

    doc = [e for e in out if e.get("role") == "document web"][0]
    lower_text = [e for e in out if e.get("inner_text") == "First released"][0]
    assert doc["rect"] == {"x": 111, "y": 158, "w": 557, "h": 201}
    assert doc.get("_occlusion_preserved_full_rect") is True
    assert lower_text["rect"] == {"x": 165, "y": 272, "w": 85, "h": 18}


def test_duplicate_window_layers_do_not_drop_partially_visible_alert() -> None:
    elements = [
        _base_elem(0, "alert", {"x": 40, "y": 40, "w": 120, "h": 80}, None, "Error"),
        _base_elem(1, "push button", {"x": 60, "y": 90, "w": 80, "h": 20}, 0, "Delete"),
        _base_elem(2, "frame", {"x": 0, "y": 0, "w": 110, "h": 160}, None, "Front"),
    ]
    stack = [
        WindowLayer(window_id="1", name="Error", rect={"x": 40, "y": 40, "w": 120, "h": 80}, stack_index=0),
        WindowLayer(window_id="2", name="Error", rect={"x": 40, "y": 40, "w": 120, "h": 80}, stack_index=1),
        WindowLayer(window_id="3", name="Front", rect={"x": 0, "y": 0, "w": 110, "h": 160}, stack_index=2),
    ]

    out, meta = apply_occlusion_clipping(
        elements,
        window_stack=stack,
        allow_partial_clip=True,
    )

    assert meta["num_clipped"] >= 1
    assert any(e.get("inner_text") == "Error" for e in out)


def test_plank_uses_accessible_dock_union_instead_of_full_width_window() -> None:
    elements = [
        _base_elem(0, "desktop frame", {"x": 0, "y": 0, "w": 1920, "h": 1080}, None, "Desktop"),
        _base_elem(1, "icon", {"x": 46, "y": 928, "w": 85, "h": 69}, 0, "03_Hobbies"),
        _base_elem(2, "frame", {"x": 760, "y": 980, "w": 400, "h": 80}, None, "plank"),
        _base_elem(3, "push button", {"x": 820, "y": 996, "w": 48, "h": 48}, 2, ""),
        _base_elem(4, "push button", {"x": 892, "y": 996, "w": 48, "h": 48}, 2, ""),
    ]
    for elem in elements[:2]:
        elem["source"] = "desktop_chrome"
        elem["app_name"] = "ata_"
    for elem in elements[2:]:
        elem["source"] = "desktop_chrome"
        elem["app_name"] = "plank"

    stack = [
        WindowLayer(window_id="1", name="Desktop", rect={"x": 0, "y": 0, "w": 1920, "h": 1080}, stack_index=0),
        WindowLayer(window_id="2", name="plank", rect={"x": 0, "y": 923, "w": 1920, "h": 157}, stack_index=1),
    ]

    out, _meta = apply_occlusion_clipping(elements, window_stack=stack, allow_partial_clip=True)

    hobbies = [e for e in out if e.get("inner_text") == "03_Hobbies"][0]
    assert hobbies["rect"] == {"x": 46, "y": 928, "w": 85, "h": 69}
    assert hobbies["is_occluded"] is False


def test_shadow_padded_xdotool_window_keeps_tighter_owner_frame_rect() -> None:
    stack = [
        WindowLayer(
            window_id="desktop",
            name="Desktop",
            rect={"x": 0, "y": 0, "w": 1920, "h": 1080},
            stack_index=0,
        ),
        WindowLayer(
            window_id="firefox",
            name="Mozilla Firefox",
            rect={"x": -45, "y": -3, "w": 1370, "h": 978},
            stack_index=1,
        ),
    ]
    by_dom = {
        10: {
            "_dom_index": 10,
            "role": "frame",
            "rect": {"x": 0, "y": 26, "w": 1280, "h": 888},
            "name": "Mozilla Firefox",
        }
    }
    owner_to_stack = {10: 1}

    expanded = _expand_window_stack_with_owner_frames(
        stack,
        owner_to_stack=owner_to_stack,
        by_dom=by_dom,
    )

    assert expanded[1].rect == {"x": 0, "y": 26, "w": 1280, "h": 888}


def test_shadow_padded_owner_frame_does_not_expand_stack_rect() -> None:
    stack = [
        WindowLayer(
            window_id="file-roller",
            name="workspace_probe_archive.zip - Archive Manager",
            rect={"x": 520, "y": 160, "w": 760, "h": 560},
            stack_index=0,
        ),
    ]
    by_dom = {
        10: {
            "_dom_index": 10,
            "role": "frame",
            "rect": {"x": 504, "y": 144, "w": 792, "h": 592},
            "name": "workspace_probe_archive.zip - Archive Manager",
        }
    }
    owner_to_stack = {10: 0}

    expanded = _expand_window_stack_with_owner_frames(
        stack,
        owner_to_stack=owner_to_stack,
        by_dom=by_dom,
    )

    assert expanded[0].rect == {"x": 520, "y": 160, "w": 760, "h": 560}


def test_containing_alert_can_hint_front_stack_for_orphan_label() -> None:
    alert = {
        **_base_elem(0, "alert", {"x": 496, "y": 128, "w": 547, "h": 158}, None, "Error"),
        "app_name": "mousepad",
        "source": "app",
        "_window_stack_index": 3,
    }
    label = {
        **_base_elem(
            1,
            "label",
            {"x": 534, "y": 191, "w": 470, "h": 34},
            106,
            "Error opening file sample_notes.md: Permission denied.",
        ),
        "app_name": "mousepad",
        "source": "app",
        "_window_stack_index": 1,
    }

    stack_idx, owner_hint = _find_containing_stack_hint(label, [alert, label])

    assert stack_idx == 3
    assert owner_hint is None


def test_top_level_alert_rect_occludes_underlay_beyond_bare_window_geometry() -> None:
    elements = [
        _base_elem(0, "frame", {"x": 130, "y": 80, "w": 800, "h": 706}, None, "Browser"),
        _base_elem(1, "push button", {"x": 719, "y": 132, "w": 82, "h": 24}, 0, ""),
        _base_elem(2, "push button", {"x": 809, "y": 132, "w": 24, "h": 24}, 0, ""),
        _base_elem(3, "alert", {"x": 496, "y": 128, "w": 547, "h": 158}, None, "Error"),
        _base_elem(4, "label", {"x": 636, "y": 162, "w": 265, "h": 19}, 3, "Failed to open the document."),
    ]
    elements[0]["app_name"] = "chromium-browser"
    elements[1]["app_name"] = "chromium-browser"
    elements[2]["app_name"] = "chromium-browser"
    elements[3]["app_name"] = "mousepad"
    elements[4]["app_name"] = "mousepad"
    for elem in elements:
        elem["source"] = "app"

    stack = [
        WindowLayer(window_id="browser", name="YouTube - Chromium", rect={"x": 130, "y": 80, "w": 800, "h": 706}, stack_index=0),
        WindowLayer(window_id="alert", name="Mousepad", rect={"x": 499, "y": 157, "w": 540, "h": 121}, stack_index=1),
    ]

    out, meta = apply_occlusion_clipping(
        elements,
        window_stack=stack,
        allow_partial_clip=True,
    )

    texts = [e.get("inner_text") for e in out]
    assert "Failed to open the document." in texts
    assert [e for e in out if e.get("role") == "push button" and e.get("_window_stack_index") == 0] == []
    assert meta["num_dropped"] >= 2


def test_desktop_icon_under_foreground_window_is_dropped() -> None:
    elements = [
        {
            **_base_elem(0, "frame", {"x": 0, "y": 0, "w": 300, "h": 200}, None, "Desktop"),
            "source": "desktop_chrome",
            "app_name": "caja",
        },
        {
            **_base_elem(1, "icon", {"x": 20, "y": 20, "w": 40, "h": 40}, 0, "Trash"),
            "source": "desktop_chrome",
            "app_name": "caja",
            "type": "File Icon",
        },
        _base_elem(2, "frame", {"x": 0, "y": 0, "w": 100, "h": 100}, None, "Front"),
    ]
    stack = [
        WindowLayer(window_id="1", name="Desktop", rect={"x": 0, "y": 0, "w": 300, "h": 200}, stack_index=0),
        WindowLayer(window_id="2", name="Front", rect={"x": 0, "y": 0, "w": 100, "h": 100}, stack_index=1),
    ]

    out, meta = apply_occlusion_clipping(
        elements,
        window_stack=stack,
        allow_partial_clip=True,
    )

    assert meta["num_dropped"] >= 1
    assert not any(e.get("inner_text") == "Trash" for e in out)


def test_same_window_dialog_drops_underlying_elements_but_keeps_dialog_children() -> None:
    elements = [
        _base_elem(0, "frame", {"x": 0, "y": 0, "w": 500, "h": 400}, None, "Editor"),
        _base_elem(1, "push button", {"x": 320, "y": 320, "w": 80, "h": 24}, 0, "Hidden"),
        _base_elem(2, "push button", {"x": 20, "y": 20, "w": 80, "h": 24}, 0, "Visible"),
        _base_elem(3, "dialog", {"x": 300, "y": 300, "w": 150, "h": 80}, 0, ""),
        _base_elem(4, "push button", {"x": 330, "y": 330, "w": 70, "h": 24}, 3, "OK"),
    ]
    elements[0]["_children_dom_indices"] = [1, 2, 3]
    elements[0]["children_indices"] = [1, 2, 3]
    elements[3]["_children_dom_indices"] = [4]
    elements[3]["children_indices"] = [4]
    stack = [
        WindowLayer(window_id="1", name="Editor", rect={"x": 0, "y": 0, "w": 500, "h": 400}, stack_index=0),
    ]

    out, meta = apply_occlusion_clipping(
        elements,
        window_stack=stack,
        allow_partial_clip=True,
    )

    texts = [e.get("inner_text") for e in out]
    assert "Hidden" not in texts
    assert "Visible" in texts
    assert "OK" in texts
    assert meta["num_overlay_dropped"] >= 1


def test_same_window_dialog_preserves_document_web_full_rect() -> None:
    elements = [
        _base_elem(0, "frame", {"x": 0, "y": 0, "w": 600, "h": 400}, None, "Browser"),
        _base_elem(1, "document web", {"x": 0, "y": 0, "w": 600, "h": 400}, 0, ""),
        _base_elem(2, "link", {"x": 20, "y": 20, "w": 100, "h": 24}, 1, "Docs"),
        _base_elem(3, "dialog", {"x": 400, "y": 200, "w": 160, "h": 120}, 0, ""),
    ]
    elements[0]["_children_dom_indices"] = [1, 3]
    elements[0]["children_indices"] = [1, 3]
    elements[1]["_children_dom_indices"] = [2]
    elements[1]["children_indices"] = [2]
    stack = [
        WindowLayer(window_id="1", name="Browser", rect={"x": 0, "y": 0, "w": 600, "h": 400}, stack_index=0),
    ]

    out, _meta = apply_occlusion_clipping(
        elements,
        window_stack=stack,
        allow_partial_clip=True,
    )

    doc = [e for e in out if e.get("role") == "document web"][0]
    link = [e for e in out if e.get("inner_text") == "Docs"][0]
    assert doc["rect"] == {"x": 0, "y": 0, "w": 600, "h": 400}
    assert doc.get("_occlusion_preserved_full_rect") is True
    assert link["rect"] == {"x": 20, "y": 20, "w": 100, "h": 24}


def test_same_window_child_menus_do_not_clip_menu_bar_ancestor() -> None:
    elements = [
        _base_elem(0, "frame", {"x": 0, "y": 0, "w": 400, "h": 300}, None, "Mousepad"),
        _base_elem(1, "menu bar", {"x": 0, "y": 20, "w": 300, "h": 24}, 0, ""),
        _base_elem(2, "menu", {"x": 0, "y": 20, "w": 40, "h": 24}, 1, "File"),
        _base_elem(3, "menu", {"x": 40, "y": 20, "w": 40, "h": 24}, 1, "Edit"),
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[0]["children_indices"] = [1]
    elements[1]["_children_dom_indices"] = [2, 3]
    elements[1]["children_indices"] = [2, 3]
    stack = [
        WindowLayer(window_id="1", name="Mousepad", rect={"x": 0, "y": 0, "w": 400, "h": 300}, stack_index=0),
    ]

    out, meta = apply_occlusion_clipping(elements, window_stack=stack, allow_partial_clip=True)
    by_role = {e["role"]: e for e in out if e["role"] in {"menu bar", "menu"}}

    assert meta["num_overlay_clipped"] == 0
    assert by_role["menu bar"]["rect"] == {"x": 0, "y": 20, "w": 300, "h": 24}
    assert sorted(e["inner_text"] for e in out if e["role"] == "menu") == ["Edit", "File"]


def test_promoted_popup_window_does_not_raise_menu_bar_sibling_menus() -> None:
    elements = [
        _base_elem(0, "frame", {"x": 938, "y": 96, "w": 556, "h": 722}, None, "workspace_probe - Thunar"),
        _base_elem(1, "menu bar", {"x": 940, "y": 130, "w": 298, "h": 25}, 0, ""),
        _base_elem(2, "menu", {"x": 940, "y": 130, "w": 39, "h": 25}, 1, "File"),
        _base_elem(3, "menu", {"x": 979, "y": 130, "w": 41, "h": 25}, 1, "Edit"),
        _base_elem(4, "menu", {"x": 1020, "y": 130, "w": 48, "h": 25}, 1, "View"),
        _base_elem(5, "menu item", {"x": 907, "y": 59, "w": 284, "h": 25}, 2, "New Tab"),
        _base_elem(6, "menu item", {"x": 907, "y": 84, "w": 284, "h": 25}, 2, "New Window"),
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[0]["children_indices"] = [1]
    elements[1]["_children_dom_indices"] = [2, 3, 4]
    elements[1]["children_indices"] = [2, 3, 4]
    elements[2]["_children_dom_indices"] = [5, 6]
    elements[2]["children_indices"] = [5, 6]
    stack = [
        WindowLayer(window_id="main", name="workspace_probe - Thunar", rect={"x": 938, "y": 96, "w": 556, "h": 722}, stack_index=0),
        WindowLayer(window_id="popup", name="Thunar", rect={"x": 907, "y": 59, "w": 284, "h": 228}, stack_index=1),
    ]

    out, _meta = apply_occlusion_clipping(elements, window_stack=stack, allow_partial_clip=True)
    texts = {(e.get("role"), e.get("inner_text")) for e in out}
    assert ("menu item", "New Tab") in texts
    assert ("menu item", "New Window") in texts
    assert ("menu", "File") in texts
    assert ("menu", "Edit") in texts
    assert ("menu", "View") in texts


def test_promoted_popup_window_uses_popup_union_not_raw_window_rect_for_menubar() -> None:
    elements = [
        _base_elem(0, "frame", {"x": 600, "y": 260, "w": 640, "h": 500}, None, "Mousepad"),
        _base_elem(1, "menu bar", {"x": 640, "y": 293, "w": 320, "h": 26}, 0, ""),
        _base_elem(2, "menu", {"x": 640, "y": 293, "w": 36, "h": 26}, 1, "File"),
        _base_elem(3, "menu", {"x": 676, "y": 293, "w": 40, "h": 26}, 1, "Edit"),
        _base_elem(4, "menu item", {"x": 646, "y": 325, "w": 325, "h": 24}, None, "New"),
        _base_elem(5, "menu item", {"x": 646, "y": 349, "w": 325, "h": 24}, None, "Open..."),
        _base_elem(6, "menu item", {"x": 646, "y": 373, "w": 325, "h": 24}, None, "Save"),
        _base_elem(7, "menu item", {"x": 646, "y": 397, "w": 325, "h": 24}, None, "Save As..."),
        _base_elem(8, "menu item", {"x": 646, "y": 421, "w": 325, "h": 24}, None, "Reload"),
        _base_elem(9, "menu item", {"x": 646, "y": 445, "w": 325, "h": 24}, None, "Quit"),
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[0]["children_indices"] = [1]
    elements[1]["_children_dom_indices"] = [2, 3]
    elements[1]["children_indices"] = [2, 3]
    stack = [
        WindowLayer(window_id="main", name="Mousepad", rect={"x": 600, "y": 260, "w": 640, "h": 500}, stack_index=0),
        WindowLayer(window_id="popup", name="Mousepad", rect={"x": 598, "y": 285, "w": 360, "h": 240}, stack_index=1),
    ]

    out, meta = apply_occlusion_clipping(elements, window_stack=stack, allow_partial_clip=True)

    texts = {(e.get("role"), e.get("inner_text")) for e in out}
    assert meta["num_promoted_popup_windows"] == 1
    assert ("menu", "File") in texts
    assert ("menu", "Edit") in texts
    assert ("menu item", "New") in texts
    assert ("menu item", "Open...") in texts


def test_inferred_same_window_popup_cluster_drops_hidden_underlay() -> None:
    elements = [
        _base_elem(0, "frame", {"x": 0, "y": 0, "w": 360, "h": 402}, None, "Calculator"),
        _base_elem(1, "push button", {"x": 140, "y": 150, "w": 70, "h": 24}, 0, "Hidden"),
        _base_elem(2, "push button", {"x": 20, "y": 20, "w": 70, "h": 24}, 0, "Visible"),
        _base_elem(3, "push button", {"x": 135, "y": 130, "w": 136, "h": 26}, 0, "New Window"),
        _base_elem(4, "separator", {"x": 135, "y": 156, "w": 136, "h": 7}, 0, ""),
        _base_elem(5, "push button", {"x": 135, "y": 163, "w": 136, "h": 26}, 0, "Number format"),
        _base_elem(6, "radio button", {"x": 135, "y": 189, "w": 136, "h": 26}, 0, "Automatic"),
        _base_elem(7, "radio button", {"x": 135, "y": 215, "w": 136, "h": 26}, 0, "Fixed"),
        _base_elem(8, "radio button", {"x": 135, "y": 241, "w": 136, "h": 26}, 0, "Scientific"),
    ]
    elements[0]["_children_dom_indices"] = [1, 2, 3, 4, 5, 6, 7, 8]
    elements[0]["children_indices"] = [1, 2, 3, 4, 5, 6, 7, 8]
    stack = [
        WindowLayer(window_id="1", name="Calculator", rect={"x": 0, "y": 0, "w": 360, "h": 402}, stack_index=0),
    ]

    out, meta = apply_occlusion_clipping(elements, window_stack=stack, allow_partial_clip=True)
    texts = [e.get("inner_text") for e in out]

    assert "Hidden" not in texts
    assert "Visible" in texts
    assert "New Window" in texts
    assert meta["num_overlay_dropped"] >= 1


def test_inferred_menu_popup_cluster_clips_later_editor_text() -> None:
    elements = [
        _base_elem(0, "frame", {"x": 210, "y": 20, "w": 460, "h": 381}, None, "Mousepad"),
        _base_elem(1, "menu bar", {"x": 210, "y": 41, "w": 460, "h": 25}, 0, ""),
        _base_elem(2, "menu", {"x": 210, "y": 41, "w": 39, "h": 25}, 1, "File"),
        _base_elem(3, "menu item", {"x": 210, "y": 66, "w": 304, "h": 25}, 2, "New"),
        _base_elem(4, "menu item", {"x": 210, "y": 91, "w": 304, "h": 25}, 2, "New Window"),
        _base_elem(5, "menu item", {"x": 210, "y": 116, "w": 304, "h": 25}, 2, "Open..."),
        _base_elem(6, "separator", {"x": 210, "y": 141, "w": 304, "h": 6}, 2, ""),
        _base_elem(7, "menu item", {"x": 210, "y": 147, "w": 304, "h": 25}, 2, "Save"),
        _base_elem(8, "menu item", {"x": 210, "y": 172, "w": 304, "h": 25}, 2, "Save As..."),
        _base_elem(9, "menu item", {"x": 210, "y": 197, "w": 304, "h": 25}, 2, "Quit"),
        _base_elem(10, "page tab list", {"x": 210, "y": 68, "w": 460, "h": 331}, 0, ""),
        _base_elem(11, "scroll pane", {"x": 210, "y": 68, "w": 460, "h": 331}, 10, ""),
        _base_elem(
            12,
            "text",
            {"x": 211, "y": 69, "w": 458, "h": 329},
            11,
            "# DeskShot Notes\n\n- Verify dense AT-SPI extraction on editor layouts.",
        ),
    ]
    elements[0]["_children_dom_indices"] = [1, 10]
    elements[0]["children_indices"] = [1, 10]
    elements[1]["_children_dom_indices"] = [2]
    elements[1]["children_indices"] = [2]
    elements[2]["_children_dom_indices"] = [3, 4, 5, 6, 7, 8, 9]
    elements[2]["children_indices"] = [3, 4, 5, 6, 7, 8, 9]
    elements[10]["_children_dom_indices"] = [11]
    elements[10]["children_indices"] = [11]
    elements[11]["_children_dom_indices"] = [12]
    elements[11]["children_indices"] = [12]

    stack = [
        WindowLayer(window_id="1", name="Mousepad", rect={"x": 210, "y": 20, "w": 460, "h": 381}, stack_index=0),
    ]

    out, meta = apply_occlusion_clipping(elements, window_stack=stack, allow_partial_clip=True)
    text = [e for e in out if e.get("role") == "text"][0]

    assert meta["num_overlay_clipped"] >= 1
    assert text["is_occluded"] is True
    assert len(text.get("visible_fragments", [])) >= 2
    assert text["rect"] != {"x": 211, "y": 69, "w": 458, "h": 329}


def test_large_content_list_does_not_clip_browser_toolbar() -> None:
    elements = [
        _base_elem(0, "frame", {"x": 450, "y": 190, "w": 673, "h": 468}, None, "Chromium"),
        _base_elem(1, "tool bar", {"x": 456, "y": 235, "w": 663, "h": 46}, 0, ""),
        _base_elem(2, "push button", {"x": 461, "y": 241, "w": 34, "h": 34}, 1, "Back"),
        _base_elem(3, "push button", {"x": 497, "y": 241, "w": 34, "h": 34}, 1, "Forward"),
        _base_elem(4, "entry", {"x": 613, "y": 246, "w": 378, "h": 24}, 1, "python.org/downloads/"),
        _base_elem(5, "document web", {"x": 456, "y": 282, "w": 663, "h": 371}, 0, ""),
        _base_elem(6, "landmark", {"x": 480, "y": 0, "w": 600, "h": 768}, 5, ""),
        _base_elem(7, "list", {"x": 500, "y": 187, "w": 560, "h": 332}, 6, ""),
    ]
    elements[0]["_children_dom_indices"] = [1, 5]
    elements[0]["children_indices"] = [1, 5]
    elements[1]["_children_dom_indices"] = [2, 3, 4]
    elements[1]["children_indices"] = [2, 3, 4]
    elements[5]["_children_dom_indices"] = [6]
    elements[5]["children_indices"] = [6]
    elements[6]["_children_dom_indices"] = [7]
    elements[6]["children_indices"] = [7]
    stack = [
        WindowLayer(window_id="1", name="Download Python | Python.org - Chromium", rect={"x": 451, "y": 190, "w": 673, "h": 468}, stack_index=0),
    ]

    out, meta = apply_occlusion_clipping(elements, window_stack=stack, allow_partial_clip=True)
    by_text = {e.get("inner_text"): e for e in out}

    assert by_text["Back"]["rect"] == {"x": 461, "y": 241, "w": 34, "h": 34}
    assert by_text["Forward"]["rect"] == {"x": 497, "y": 241, "w": 34, "h": 34}
    assert by_text["python.org/downloads/"]["rect"] == {"x": 613, "y": 246, "w": 378, "h": 24}
    assert meta["num_overlay_clipped"] == 0


def test_small_floating_list_still_blocks_underlay() -> None:
    elements = [
        _base_elem(0, "frame", {"x": 0, "y": 0, "w": 700, "h": 500}, None, "Editor"),
        _base_elem(1, "push button", {"x": 520, "y": 400, "w": 120, "h": 30}, 0, "Hidden"),
        _base_elem(2, "list", {"x": 500, "y": 360, "w": 160, "h": 140}, 0, ""),
        _base_elem(3, "push button", {"x": 520, "y": 375, "w": 120, "h": 24}, 2, "Popup item"),
    ]
    elements[0]["_children_dom_indices"] = [1, 2]
    elements[0]["children_indices"] = [1, 2]
    elements[2]["_children_dom_indices"] = [3]
    elements[2]["children_indices"] = [3]
    stack = [
        WindowLayer(window_id="1", name="Editor", rect={"x": 0, "y": 0, "w": 700, "h": 500}, stack_index=0),
    ]

    out, meta = apply_occlusion_clipping(elements, window_stack=stack, allow_partial_clip=True)
    texts = [e.get("inner_text") for e in out]

    assert "Hidden" not in texts
    assert "Popup item" in texts
    assert meta["num_overlay_dropped"] >= 1


def test_tiny_adjacent_list_overlap_does_not_clip_footer_link() -> None:
    elements = [
        _base_elem(0, "frame", {"x": 0, "y": 0, "w": 1800, "h": 2200}, None, "Chromium"),
        _base_elem(1, "document web", {"x": 0, "y": 100, "w": 1800, "h": 2100}, 0, ""),
        _base_elem(2, "link", {"x": 656, "y": 1906, "w": 197, "h": 36}, 1, "Alternative Implementations"),
        _base_elem(3, "list", {"x": 852, "y": 1830, "w": 220, "h": 180}, 1, ""),
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[0]["children_indices"] = [1]
    elements[1]["_children_dom_indices"] = [2, 3]
    elements[1]["children_indices"] = [2, 3]
    stack = [
        WindowLayer(window_id="1", name="Chromium", rect={"x": 0, "y": 0, "w": 1800, "h": 2200}, stack_index=0),
    ]

    out, meta = apply_occlusion_clipping(elements, window_stack=stack, allow_partial_clip=True)
    link = [e for e in out if e.get("inner_text") == "Alternative Implementations"][0]

    assert link["rect"] == {"x": 656, "y": 1906, "w": 197, "h": 36}
    assert link.get("_overlay_occlusion_clipped") is not True
    assert meta["num_overlay_clipped"] == 0


def test_small_transient_popup_window_is_promoted_above_overlapping_apps() -> None:
    elements = [
        _base_elem(0, "frame", {"x": 457, "y": 233, "w": 673, "h": 468}, None, "file_roller_sample.zip - xarchiver 0.5.4.23"),
        _base_elem(1, "push button", {"x": 463, "y": 266, "w": 32, "h": 28}, 0, "Toolbar New"),
        _base_elem(2, "push button", {"x": 495, "y": 266, "w": 32, "h": 28}, 0, "Toolbar Open"),
        _base_elem(3, "menu bar", {"x": 457, "y": 233, "w": 156, "h": 25}, 0, ""),
        _base_elem(4, "menu", {"x": 457, "y": 233, "w": 61, "h": 25}, 3, "Archive"),
        _base_elem(5, "menu item", {"x": 395, "y": 258, "w": 170, "h": 23}, 4, "New"),
        _base_elem(6, "menu item", {"x": 395, "y": 281, "w": 170, "h": 23}, 4, "Open"),
        _base_elem(9, "menu", {"x": 395, "y": 304, "w": 170, "h": 23}, 4, "List as"),
        _base_elem(10, "menu item", {"x": 395, "y": 327, "w": 170, "h": 23}, 4, "Save As"),
        _base_elem(11, "separator", {"x": 395, "y": 350, "w": 170, "h": 7}, 4, ""),
        _base_elem(12, "menu item", {"x": 395, "y": 357, "w": 170, "h": 23}, 4, "Test"),
        _base_elem(13, "menu item", {"x": 395, "y": 380, "w": 170, "h": 23}, 4, "Properties"),
        _base_elem(14, "menu item", {"x": 395, "y": 403, "w": 170, "h": 23}, 4, "Close"),
        _base_elem(15, "separator", {"x": 395, "y": 426, "w": 170, "h": 7}, 4, ""),
        _base_elem(16, "menu item", {"x": 395, "y": 433, "w": 170, "h": 23}, 4, "Quit"),
        _base_elem(7, "frame", {"x": 116, "y": 113, "w": 613, "h": 548}, None, "desktop_ui - Thunar"),
        _base_elem(8, "table cell", {"x": 158, "y": 312, "w": 130, "h": 24}, 7, "Filesystem root"),
    ]
    elements[0]["_children_dom_indices"] = [1, 2, 3]
    elements[0]["children_indices"] = [1, 2, 3]
    elements[3]["_children_dom_indices"] = [4]
    elements[3]["children_indices"] = [4]
    elements[4]["_children_dom_indices"] = [5, 6, 9, 10, 11, 12, 13, 14, 15, 16]
    elements[4]["children_indices"] = [5, 6, 9, 10, 11, 12, 13, 14, 15, 16]
    elements[7]["_children_dom_indices"] = [8]
    elements[7]["children_indices"] = [8]

    stack = [
        WindowLayer(window_id="popup", name="xarchiver", rect={"x": 389, "y": 252, "w": 182, "h": 210}, stack_index=0),
        WindowLayer(window_id="thunar", name="desktop_ui - Thunar", rect={"x": 116, "y": 113, "w": 613, "h": 548}, stack_index=1),
        WindowLayer(window_id="main", name="file_roller_sample.zip - xarchiver 0.5.4.23", rect={"x": 457, "y": 233, "w": 673, "h": 468}, stack_index=2),
    ]

    out, meta = apply_occlusion_clipping(elements, window_stack=stack, allow_partial_clip=True)
    texts = {e.get("inner_text"): e for e in out}

    assert meta["num_promoted_popup_windows"] == 1
    assert meta["promoted_popup_window_ids"] == ["popup"]
    assert texts["New"]["rect"] == {"x": 395, "y": 258, "w": 170, "h": 23}
    assert texts["Open"]["rect"] == {"x": 395, "y": 281, "w": 170, "h": 23}
    assert "Toolbar New" not in texts
    assert "Toolbar Open" not in texts
    assert texts["Filesystem root"]["rect"] == {"x": 158, "y": 312, "w": 130, "h": 24}


def test_same_window_menu_popup_blocks_underlying_owner_content_after_repair() -> None:
    elements = [
        _base_elem(0, "frame", {"x": 690, "y": 70, "w": 804, "h": 762}, None, "Mousepad"),
        _base_elem(1, "menu bar", {"x": 692, "y": 98, "w": 800, "h": 26}, 0, ""),
        _base_elem(2, "menu", {"x": 692, "y": 98, "w": 36, "h": 26}, 1, "File"),
        _base_elem(3, "menu item", {"x": 692, "y": 124, "w": 325, "h": 24}, 2, "New"),
        _base_elem(4, "menu item", {"x": 692, "y": 148, "w": 325, "h": 24}, 2, "New Window"),
        _base_elem(5, "text", {"x": 700, "y": 130, "w": 240, "h": 40}, 0, "Hidden body text"),
    ]
    elements[0]["_children_dom_indices"] = [1, 5]
    elements[0]["children_indices"] = [1, 5]
    elements[1]["_children_dom_indices"] = [2]
    elements[1]["children_indices"] = [2]
    elements[2]["_children_dom_indices"] = [3, 4]
    elements[2]["children_indices"] = [3, 4]

    stack = [
        WindowLayer(window_id="main", name="sample_notes.md - Mousepad", rect={"x": 690, "y": 70, "w": 804, "h": 762}, stack_index=0),
    ]

    out, meta = apply_occlusion_clipping(elements, window_stack=stack, allow_partial_clip=True)
    by_text = {e.get("inner_text"): e for e in out}

    assert meta["num_overlay_dropped"] >= 1
    assert "Hidden body text" not in by_text
    assert by_text["New"]["rect"] == {"x": 692, "y": 124, "w": 325, "h": 24}


def test_large_menu_popup_window_is_promoted_above_owner_frame() -> None:
    elements = [
        _base_elem(0, "frame", {"x": 38, "y": 105, "w": 460, "h": 381}, None, "sample_notes.md - Mousepad"),
        _base_elem(1, "menu bar", {"x": 38, "y": 126, "w": 460, "h": 25}, 0, ""),
        _base_elem(2, "menu", {"x": 38, "y": 126, "w": 39, "h": 25}, 1, "File"),
        _base_elem(3, "menu item", {"x": 38, "y": 151, "w": 304, "h": 25}, 2, "New"),
        _base_elem(4, "menu item", {"x": 38, "y": 176, "w": 304, "h": 25}, 2, "New Window"),
        _base_elem(5, "menu item", {"x": 38, "y": 201, "w": 304, "h": 25}, 2, "New From Template"),
        _base_elem(6, "menu item", {"x": 38, "y": 227, "w": 304, "h": 25}, 2, "Open..."),
        _base_elem(7, "menu item", {"x": 38, "y": 252, "w": 304, "h": 25}, 2, "Open Recent"),
        _base_elem(8, "menu item", {"x": 38, "y": 278, "w": 304, "h": 25}, 2, "Save"),
        _base_elem(9, "menu item", {"x": 38, "y": 303, "w": 304, "h": 25}, 2, "Save As..."),
        _base_elem(10, "menu item", {"x": 38, "y": 328, "w": 304, "h": 25}, 2, "Save All"),
        _base_elem(11, "menu item", {"x": 38, "y": 353, "w": 304, "h": 25}, 2, "Reload"),
        _base_elem(12, "menu item", {"x": 38, "y": 379, "w": 304, "h": 25}, 2, "Print..."),
        _base_elem(13, "menu item", {"x": 38, "y": 405, "w": 304, "h": 25}, 2, "Detach Tab"),
        _base_elem(14, "menu item", {"x": 38, "y": 431, "w": 304, "h": 25}, 2, "Close Tab"),
        _base_elem(15, "menu item", {"x": 38, "y": 456, "w": 304, "h": 25}, 2, "Close Window"),
        _base_elem(16, "menu item", {"x": 38, "y": 481, "w": 304, "h": 5}, 2, "Quit"),
        _base_elem(17, "page tab list", {"x": 38, "y": 153, "w": 460, "h": 331}, 0, ""),
        _base_elem(18, "scroll pane", {"x": 38, "y": 153, "w": 460, "h": 331}, 17, ""),
        _base_elem(19, "text", {"x": 39, "y": 154, "w": 458, "h": 329}, 18, "Hidden body"),
    ]
    elements[0]["_children_dom_indices"] = [1, 17]
    elements[0]["children_indices"] = [1, 17]
    elements[1]["_children_dom_indices"] = [2]
    elements[1]["children_indices"] = [2]
    elements[2]["_children_dom_indices"] = [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
    elements[2]["children_indices"] = [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
    elements[17]["_children_dom_indices"] = [18]
    elements[17]["children_indices"] = [18]
    elements[18]["_children_dom_indices"] = [19]
    elements[18]["children_indices"] = [19]

    stack = [
        WindowLayer(window_id="popup", name="Mousepad", rect={"x": 32, "y": 146, "w": 316, "h": 367}, stack_index=0),
        WindowLayer(window_id="main", name="sample_notes.md - Mousepad", rect={"x": 38, "y": 105, "w": 460, "h": 381}, stack_index=1),
    ]

    out, meta = apply_occlusion_clipping(elements, window_stack=stack, allow_partial_clip=True)
    by_text = {e.get("inner_text"): e for e in out}

    assert meta["num_promoted_popup_windows"] == 1
    assert meta["promoted_popup_window_ids"] == ["popup"]
    assert by_text["New"]["rect"] == {"x": 38, "y": 151, "w": 304, "h": 25}
    promoted_stack = {w["window_id"]: w["stack_index"] for w in meta["window_stack"]}
    assert promoted_stack["popup"] > promoted_stack["main"]


def test_menu_anchor_does_not_expand_popup_blocker_across_owner_content() -> None:
    elements = [
        _base_elem(0, "frame", {"x": 110, "y": 94, "w": 689, "h": 901}, None, "xarchiver"),
        _base_elem(1, "menu bar", {"x": 111, "y": 122, "w": 170, "h": 26}, 0, ""),
        _base_elem(2, "menu", {"x": 111, "y": 122, "w": 67, "h": 26}, 1, "Archive"),
        _base_elem(3, "menu item", {"x": 667, "y": 400, "w": 185, "h": 24}, 2, "New"),
        _base_elem(4, "menu item", {"x": 667, "y": 424, "w": 185, "h": 24}, 2, "Open"),
        _base_elem(5, "menu", {"x": 667, "y": 448, "w": 185, "h": 24}, 2, "List as"),
        _base_elem(6, "menu item", {"x": 667, "y": 472, "w": 185, "h": 24}, 2, "Save As"),
        _base_elem(7, "separator", {"x": 667, "y": 496, "w": 185, "h": 7}, 2, ""),
        _base_elem(8, "menu item", {"x": 667, "y": 503, "w": 185, "h": 24}, 2, "Test"),
        _base_elem(9, "menu item", {"x": 667, "y": 527, "w": 185, "h": 24}, 2, "Properties"),
        _base_elem(10, "menu item", {"x": 667, "y": 551, "w": 185, "h": 24}, 2, "Close"),
        _base_elem(11, "separator", {"x": 667, "y": 575, "w": 185, "h": 7}, 2, ""),
        _base_elem(12, "menu item", {"x": 667, "y": 582, "w": 185, "h": 24}, 2, "Quit"),
        _base_elem(13, "tree table", {"x": 112, "y": 233, "w": 198, "h": 738}, 0, ""),
        _base_elem(14, "table cell", {"x": 152, "y": 258, "w": 156, "h": 22}, 13, "logs"),
        _base_elem(15, "table", {"x": 313, "y": 233, "w": 484, "h": 738}, 0, ""),
        _base_elem(16, "table column header", {"x": 313, "y": 233, "w": 120, "h": 24}, 15, "Filename"),
    ]
    elements[0]["_children_dom_indices"] = [1, 13, 15]
    elements[0]["children_indices"] = [1, 13, 15]
    elements[1]["_children_dom_indices"] = [2]
    elements[1]["children_indices"] = [2]
    elements[2]["_children_dom_indices"] = [3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
    elements[2]["children_indices"] = [3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
    elements[13]["_children_dom_indices"] = [14]
    elements[13]["children_indices"] = [14]
    elements[15]["_children_dom_indices"] = [16]
    elements[15]["children_indices"] = [16]

    stack = [
        WindowLayer(window_id="popup", name="xarchiver", rect={"x": 661, "y": 394, "w": 191, "h": 212}, stack_index=0),
        WindowLayer(window_id="main", name="workspace_probe_archive.zip - xarchiver 0.5.4.23", rect={"x": 110, "y": 94, "w": 689, "h": 901}, stack_index=1),
    ]

    out, _meta = apply_occlusion_clipping(elements, window_stack=stack, allow_partial_clip=True)
    by_text = {e.get("inner_text"): e for e in out}

    assert "logs" in by_text
    assert by_text["logs"]["visible_fragments"]
    assert by_text["logs"]["rect"] == {"x": 152, "y": 258, "w": 156, "h": 22}
    assert "Filename" in by_text
    assert by_text["Filename"]["visible_fragments"]
    assert by_text["Filename"]["rect"] == {"x": 313, "y": 233, "w": 120, "h": 24}


def test_decorated_window_frame_occludes_underlay_titlebar_region() -> None:
    elements = [
        _base_elem(0, "frame", {"x": 0, "y": 0, "w": 420, "h": 320}, None, "Back"),
        _base_elem(1, "table cell", {"x": 120, "y": 84, "w": 90, "h": 21}, 0, "Hidden By Titlebar"),
        _base_elem(2, "frame", {"x": 94, "y": 60, "w": 312, "h": 246}, None, "Front"),
        _base_elem(3, "push button", {"x": 130, "y": 130, "w": 32, "h": 28}, 2, "Front Button"),
    ]
    elements[0]["_children_dom_indices"] = [1]
    elements[0]["children_indices"] = [1]
    elements[2]["_children_dom_indices"] = [3]
    elements[2]["children_indices"] = [3]

    stack = [
        WindowLayer(window_id="back", name="Back", rect={"x": 0, "y": 0, "w": 420, "h": 320}, stack_index=0),
        WindowLayer(window_id="front", name="Front", rect={"x": 100, "y": 100, "w": 300, "h": 200}, stack_index=1),
    ]

    out, meta = apply_occlusion_clipping(elements, window_stack=stack, allow_partial_clip=True)
    texts = [e.get("inner_text") for e in out]
    front_stack = [w for w in meta["window_stack"] if w["window_id"] == "front"][0]

    assert "Hidden By Titlebar" not in texts
    assert "Front Button" in texts
    assert front_stack["rect"] == {"x": 94, "y": 60, "w": 312, "h": 246}


def test_page_content_list_does_not_blank_the_nav_links_beside_it() -> None:
    """A page `<ul>` is not a popover, and must not hide what it overlaps.

    Intra-window occlusion is inferred from DOM order, which models how a
    toolkit stacks widgets but not how a browser paints a document. Measured on
    a LinkedIn capture: an ordinary list of topic pills reported at
    (49,34,627,300) inside a 1076x933 Chromium window passed every size test for
    a floating overlay. Treating it as one dropped four visible nav links
    (LinkedIn, Top Content, People, Learning) and clipped the toolbar, tab strip,
    Back and Close to slivers at the list's right edge - Jobs survived only
    because its 2px overlap fell under the border-grazing threshold.

    Page content lives under `document web`; a browser's real popups (omnibox
    suggestions, autofill) live in the chrome outside it, so nothing the
    heuristic exists for is lost.
    """
    elements = [
        _base_elem(0, "frame", {"x": 32, "y": 64, "w": 1076, "h": 933}, None, "LinkedIn - Chromium"),
        _base_elem(1, "tool bar", {"x": 33, "y": 105, "w": 1074, "h": 46}, 0, ""),
        _base_elem(2, "push button", {"x": 38, "y": 111, "w": 34, "h": 34}, 1, "Back"),
        _base_elem(3, "document web", {"x": 33, "y": 152, "w": 1074, "h": 844}, 0, ""),
        _base_elem(4, "link", {"x": 49, "y": 164, "w": 102, "h": 52}, 3, "LinkedIn"),
        _base_elem(5, "link", {"x": 408, "y": 164, "w": 73, "h": 52}, 3, "Top Content"),
        _base_elem(6, "link", {"x": 496, "y": 164, "w": 65, "h": 52}, 3, "People"),
        _base_elem(7, "list", {"x": 49, "y": 34, "w": 627, "h": 300}, 3, ""),
        _base_elem(8, "link", {"x": 49, "y": 214, "w": 261, "h": 48}, 7, "Information Technology"),
    ]
    elements[0]["_children_dom_indices"] = [1, 3]
    elements[0]["children_indices"] = [1, 3]
    elements[1]["_children_dom_indices"] = [2]
    elements[1]["children_indices"] = [2]
    elements[3]["_children_dom_indices"] = [4, 5, 6, 7]
    elements[3]["children_indices"] = [4, 5, 6, 7]
    elements[7]["_children_dom_indices"] = [8]
    elements[7]["children_indices"] = [8]
    stack = [
        WindowLayer(window_id="1", name="LinkedIn - Chromium", rect={"x": 32, "y": 64, "w": 1076, "h": 933}, stack_index=0),
    ]

    out, meta = apply_occlusion_clipping(elements, window_stack=stack, allow_partial_clip=True)
    by_text = {e.get("inner_text"): e for e in out}

    assert set(by_text) >= {"LinkedIn", "Top Content", "People"}
    assert by_text["Top Content"]["rect"] == {"x": 408, "y": 164, "w": 73, "h": 52}
    assert by_text["Back"]["rect"] == {"x": 38, "y": 111, "w": 34, "h": 34}
    assert meta["num_overlay_clipped"] == 0
    assert meta["num_overlay_dropped"] == 0


def test_app_list_popover_outside_a_document_still_blocks() -> None:
    """The web-document exemption must not disarm the heuristic elsewhere.

    Native apps really do render popovers as `list` in the same window, and the
    qalculate/editor case that motivated this blocker has no document node
    anywhere in its chain.
    """
    elements = [
        _base_elem(0, "frame", {"x": 0, "y": 0, "w": 700, "h": 500}, None, "Editor"),
        _base_elem(1, "push button", {"x": 520, "y": 400, "w": 120, "h": 30}, 0, "Hidden"),
        _base_elem(2, "list", {"x": 500, "y": 360, "w": 160, "h": 140}, 0, ""),
        _base_elem(3, "push button", {"x": 520, "y": 375, "w": 120, "h": 24}, 2, "Popup item"),
    ]
    elements[0]["_children_dom_indices"] = [1, 2]
    elements[0]["children_indices"] = [1, 2]
    elements[2]["_children_dom_indices"] = [3]
    elements[2]["children_indices"] = [3]
    stack = [
        WindowLayer(window_id="1", name="Editor", rect={"x": 0, "y": 0, "w": 700, "h": 500}, stack_index=0),
    ]

    out, meta = apply_occlusion_clipping(elements, window_stack=stack, allow_partial_clip=True)

    assert "Hidden" not in [e.get("inner_text") for e in out]
    assert meta["num_overlay_dropped"] >= 1


def test_managed_window_is_never_promoted_over_windows_drawn_on_top() -> None:
    """A real app window must not be reordered just because its content looks
    popup-like.

    Taken from a capture that lost 165 of HomeBank's 167 elements. Chromium's
    page content was 35% covered by menu-like roles, so the overlay-coverage
    heuristic promoted the whole browser window above the three windows drawn
    on top of it. Everything under its 834x397 rect was then dropped as
    occluded, while Chromium's own hidden elements were annotated as fully
    visible - a false negative and a false positive from one mistake.

    The browser has an accessible frame matching its X window exactly, so it is
    a managed window and the window manager's stacking order for it is correct.
    """
    elements = [
        # The browser, with a frame that matches its X window to the pixel.
        _base_elem(0, "frame", {"x": 24, "y": 64, "w": 834, "h": 397}, None, "Chromium"),
        # Page content that happens to use popup-like roles over most of it.
        _base_elem(1, "menu", {"x": 24, "y": 64, "w": 834, "h": 200}, 0, "nav"),
        _base_elem(2, "menu", {"x": 24, "y": 264, "w": 834, "h": 197}, 0, "sidebar"),
        # A larger window behind it.
        _base_elem(3, "frame", {"x": 0, "y": 0, "w": 1200, "h": 690}, None, "FileZilla"),
        # A window genuinely drawn on top of the browser.
        _base_elem(4, "frame", {"x": 100, "y": 100, "w": 600, "h": 400}, None, "HomeBank"),
        _base_elem(5, "push button", {"x": 120, "y": 150, "w": 80, "h": 24}, 4, "Add"),
    ]
    for i in (0, 3, 4):
        elements[i]["_children_dom_indices"] = []
        elements[i]["children_indices"] = []
    elements[0]["_children_dom_indices"] = [1, 2]
    elements[0]["children_indices"] = [1, 2]
    elements[4]["_children_dom_indices"] = [5]
    elements[4]["children_indices"] = [5]

    stack = [
        WindowLayer(window_id="desktop", name="Desktop",
                    rect={"x": 0, "y": 0, "w": 1366, "h": 768}, stack_index=0),
        WindowLayer(window_id="filezilla", name="FileZilla",
                    rect={"x": 0, "y": 0, "w": 1200, "h": 690}, stack_index=1),
        WindowLayer(window_id="chromium", name="Chromium",
                    rect={"x": 24, "y": 64, "w": 834, "h": 397}, stack_index=2),
        WindowLayer(window_id="homebank", name="HomeBank",
                    rect={"x": 100, "y": 100, "w": 600, "h": 400}, stack_index=3),
    ]

    out, meta = apply_occlusion_clipping(elements, window_stack=stack, allow_partial_clip=True)

    assert meta["promoted_popup_window_ids"] == []
    # The window on top keeps its contents.
    kept = {e.get("inner_text") for e in out}
    assert "Add" in kept
