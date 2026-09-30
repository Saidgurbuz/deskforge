from deskshot.extraction.run_extraction import _basic_screentag


def _elem(idx, role, elem_type, rect, text="", parent=None, children=None):
    return {
        "_dom_index": idx,
        "_parent_dom_index": parent,
        "parent_index": parent,
        "_children_dom_indices": list(children or []),
        "children_indices": list(children or []),
        "role": role,
        "type": elem_type,
        "inner_text": text,
        "rect": dict(rect),
        "reading_order_index": idx,
    }


def test_basic_screentag_nests_under_window() -> None:
    elements = [
        _elem(0, "frame", "Window", {"x": 0, "y": 0, "w": 500, "h": 400}, "App", None, [1, 2]),
        _elem(1, "push button", "Button", {"x": 10, "y": 10, "w": 40, "h": 20}, "Back", 0),
        _elem(2, "text", "Text Input", {"x": 60, "y": 10, "w": 200, "h": 20}, "/tmp/path", 0),
        _elem(3, "icon", "File Icon", {"x": 5, "y": 450, "w": 30, "h": 30}, "Trash", None),
    ]

    out = _basic_screentag(elements, viewport_w=1000, viewport_h=1000)

    assert "<Window><loc_0><loc_0><loc_250><loc_200>App" in out
    assert "<Button><loc_5><loc_5><loc_25><loc_15>Back</Button>" in out
    assert "<Text_Input><loc_30><loc_5><loc_130><loc_15>/tmp/path</Text_Input>" in out
    assert "<File_Icon><loc_2><loc_225><loc_17><loc_240>Trash</File_Icon>" in out
    assert "<fragment>" not in out
    assert out.index("<Window>") < out.index("<File_Icon>")
    assert "\n" not in out


def test_basic_screentag_keeps_full_text_without_truncation() -> None:
    long_text = (
        "Visible paragraph text should stay complete in ScreenTag serialization "
        "even when it exceeds one hundred characters so downstream training keeps "
        "the full visible content."
    )
    window_text = (
        "Long window titles should also stay complete without being truncated by "
        "the serializer."
    )
    elements = [
        _elem(0, "frame", "Window", {"x": 0, "y": 0, "w": 600, "h": 400}, window_text, None, [1]),
        _elem(1, "text", "Text", {"x": 20, "y": 40, "w": 560, "h": 120}, long_text, 0),
    ]

    out = _basic_screentag(elements, viewport_w=1000, viewport_h=1000)

    assert window_text in out
    assert long_text in out


def test_basic_screentag_includes_visible_fragments() -> None:
    elements = [
        {
            **_elem(0, "frame", "Window", {"x": 0, "y": 0, "w": 500, "h": 400}, "App", None, [1]),
            "is_occluded": True,
            "visible_fragments": [{"x": 0, "y": 0, "w": 500, "h": 400}],
        },
        {
            **_elem(1, "push button", "Button", {"x": 10, "y": 10, "w": 40, "h": 20}, "Split", 0),
            "is_occluded": True,
            "visible_fragments": [
                {"x": 10, "y": 10, "w": 15, "h": 20},
                {"x": 35, "y": 10, "w": 15, "h": 20},
            ],
        },
    ]

    out = _basic_screentag(elements, viewport_w=1000, viewport_h=1000)

    assert "<Window><loc_0><loc_0><loc_250><loc_200>App" in out
    assert "<Button><loc_5><loc_5><loc_25><loc_15><fragment><loc_5><loc_5><loc_12><loc_15></fragment><fragment><loc_17><loc_5><loc_25><loc_15></fragment>Split</Button>" in out
    assert "<Window><loc_0><loc_0><loc_250><loc_200>App" in out and "<fragment><loc_0><loc_0><loc_250><loc_200></fragment>" not in out


def test_basic_screentag_places_fragments_before_text() -> None:
    elements = [
        {
            **_elem(0, "push button", "Button", {"x": 10, "y": 10, "w": 40, "h": 20}, "Split"),
            "is_occluded": True,
            "visible_fragments": [
                {"x": 10, "y": 10, "w": 15, "h": 20},
                {"x": 35, "y": 10, "w": 15, "h": 20},
            ],
        },
    ]

    out = _basic_screentag(elements, viewport_w=1000, viewport_h=1000)

    assert out == "<screentag><Button><loc_5><loc_5><loc_25><loc_15><fragment><loc_5><loc_5><loc_12><loc_15></fragment><fragment><loc_17><loc_5><loc_25><loc_15></fragment>Split</Button></screentag>"


def test_basic_screentag_prefers_visible_text_when_present() -> None:
    elements = [
        {
            **_elem(0, "text", "Text", {"x": 10, "y": 10, "w": 200, "h": 40}, "Full hidden text"),
            "visible_text": "Visible tail",
        },
    ]

    out = _basic_screentag(elements, viewport_w=1000, viewport_h=1000)

    assert "Visible tail" in out
    assert "Full hidden text" not in out


def test_basic_screentag_omits_text_for_unsupported_partial_visibility() -> None:
    elements = [
        {
            **_elem(0, "text", "Text", {"x": 10, "y": 10, "w": 200, "h": 40}, "Possibly hidden text"),
            "visible_text": None,
            "visible_text_status": "unsupported_partial",
            "is_occluded": True,
            "visible_fragments": [{"x": 100, "y": 10, "w": 110, "h": 40}],
        },
    ]

    out = _basic_screentag(elements, viewport_w=1000, viewport_h=1000)

    assert "Possibly hidden text" not in out
    assert "<fragment>" in out


def test_screentag_marks_occluded_text_truncation() -> None:
    """A clipped string must not serialize as if it were complete content."""
    from deskshot.extraction.run_extraction import _elements_to_screentag
    from deskshot.extraction.text_visibility import TEXT_CLIP_MARKER

    elements = [
        {
            "_dom_index": 0,
            "type": "text",
            "role": "text",
            "rect": {"x": 0, "y": 0, "w": 100, "h": 20},
            "inner_text": "the document is here",
            "visible_text": "the docu",
            "visible_text_marked": f"the docu{TEXT_CLIP_MARKER}",
            "visible_text_status": "clipped",
            "is_occluded": True,
            "visible_fragments": [{"x": 0, "y": 0, "w": 40, "h": 20}],
            "reading_order_index": 0,
        }
    ]

    tag = _elements_to_screentag(elements, 1000, 1000)

    assert f"the docu{TEXT_CLIP_MARKER}" in tag
    assert "the document is here" not in tag


def test_screentag_leaves_unoccluded_text_unmarked() -> None:
    from deskshot.extraction.run_extraction import _elements_to_screentag
    from deskshot.extraction.text_visibility import TEXT_CLIP_MARKER

    elements = [
        {
            "_dom_index": 0,
            "type": "text",
            "role": "text",
            "rect": {"x": 0, "y": 0, "w": 100, "h": 20},
            "inner_text": "Save As...",
            "visible_text": "Save As...",
            "visible_text_status": "full_visible",
            "is_occluded": False,
            "reading_order_index": 0,
        }
    ]

    tag = _elements_to_screentag(elements, 1000, 1000)

    # A literal ellipsis in UI text must survive untouched and unmarked.
    assert "Save As..." in tag
    assert TEXT_CLIP_MARKER not in tag
