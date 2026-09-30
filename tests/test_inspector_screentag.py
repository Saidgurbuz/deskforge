"""The link between the serialized ScreenTag and the elements it came from.

This is the part of the inspector most likely to be subtly wrong and least
likely to look wrong: a span index that is off by one element still highlights
*a* box for every block, and reading a capture with it would quietly attribute
one widget's markup to its neighbour. So the tag under test is not a hand-typed
string - it is produced by the pipeline's own serializer, and the mapping is
then checked through channels the matcher never looks at:

- the element's own text, which the index does not use to decide anything, must
  come out byte-identical to what `_serialized_text` would emit for the element
  the block was mapped to;
- the block's `<loc_>` tokens must decode back onto that element's rect within
  the one grid cell the 0-500 truncation can lose;
- the spans must tile the file exactly, because a single dropped character puts
  every offset after it out by one.
"""

from __future__ import annotations

import json

import pytest
from PIL import Image

from deskshot.extraction.run_extraction import _basic_screentag, _serialized_text
from deskshot.inspector import overlay, screentag
from deskshot.inspector.app import InspectorApp

VIEWPORT_W, VIEWPORT_H = 1000, 800


def _elements():
    """A capture holding every shape that can move a span boundary.

    Nesting (a window with children), text that has to be escaped, the
    `<occluded/>` clip marker inside text, visible fragments in the head, state
    tokens both ways, an empty text, and an element with no uid so the key falls
    back to the dom form.
    """
    return [
        {
            "uid": "win", "role": "frame", "type": "Window", "name": "Notes",
            "app_name": "zim", "source": "app", "_dom_index": 0,
            "children_indices": [1, 2, 3, 4, 5],
            "rect": {"x": 100, "y": 40, "w": 600, "h": 500},
            "reading_order_index": 0,
            "visible_text": "Notes & <untitled>", "visible_text_status": "name_only",
        },
        {
            "uid": "btn", "role": "push button", "type": "Button", "name": "Back",
            "app_name": "zim", "source": "app", "_dom_index": 1,
            "rect": {"x": 110, "y": 60, "w": 40, "h": 24},
            "reading_order_index": 1,
            "visible_text": "Back", "visible_text_status": "full_visible",
            "interaction": {"enabled": False},
        },
        {
            "uid": "chk", "role": "check box", "type": "Toggles", "name": "Bold",
            "app_name": "zim", "source": "app", "_dom_index": 2,
            "rect": {"x": 160, "y": 60, "w": 60, "h": 24},
            "reading_order_index": 2,
            "visible_text": "Bold", "visible_text_status": "full_visible",
            "interaction": {"checked": True, "expandable": True, "expanded": False},
        },
        {
            "uid": "clip", "role": "static", "type": "Text", "name": "path",
            "app_name": "zim", "source": "app", "_dom_index": 3,
            "rect": {"x": 110, "y": 100, "w": 300, "h": 20},
            "reading_order_index": 3,
            "is_occluded": True,
            "visible_fragments": [
                {"x": 110, "y": 100, "w": 120, "h": 20},
                {"x": 260, "y": 100, "w": 150, "h": 20},
            ],
            "visible_text": "a/b & c",
            "visible_text_marked": "a & b<occluded/><c>",
            "visible_text_status": "clipped",
        },
        {
            "uid": "empty", "role": "push button", "type": "Button", "name": "",
            "app_name": "zim", "source": "app", "_dom_index": 4,
            "rect": {"x": 420, "y": 60, "w": 24, "h": 24},
            "reading_order_index": 4,
            "visible_text": "", "visible_text_status": "no_text",
        },
        {
            # No uid: the key falls back to `dom:<app>:<index>:<source>`.
            "role": "label", "type": "Text", "name": "Ready",
            "app_name": "zim", "source": "app", "_dom_index": 5,
            "rect": {"x": 110, "y": 500, "w": 200, "h": 18},
            "reading_order_index": 5,
            "visible_text": "Ready", "visible_text_status": "full_visible",
        },
        {
            # Outside the window: serialized after it, at the top level.
            "uid": "dock", "role": "icon", "type": "File_icon", "name": "Trash",
            "app_name": "plank", "source": "chrome", "_dom_index": 6,
            "rect": {"x": 20, "y": 700, "w": 48, "h": 48},
            "reading_order_index": 6,
            "visible_text": "Trash", "visible_text_status": "full_visible",
        },
    ]


@pytest.fixture()
def capture():
    elements = _elements()
    text = _basic_screentag(elements, VIEWPORT_W, VIEWPORT_H)
    index = screentag.build_index(
        text, elements, VIEWPORT_W, VIEWPORT_H, overlay.element_keys(elements)
    )
    return text, elements, index


# ------------------------------------------------------------------ parsing


def test_the_tokeniser_finds_every_block_and_nests_them(capture):
    text, elements, index = capture
    spans = index["spans"]
    assert len(spans) == len(elements)
    window = spans[0]
    assert window["tag"] == "window" and window["depth"] == 0 and window["parent"] == -1
    # The five children are inside the window's range, the dock icon is not.
    inside = [span for span in spans[1:] if span["start"] > window["start"]
              and span["end"] <= window["end"]]
    assert [span["tag"] for span in inside] == ["button", "toggles", "text", "button", "text"]
    assert all(span["depth"] == 1 and span["parent"] == 0 for span in inside)
    assert spans[-1]["tag"] == "file_icon" and spans[-1]["depth"] == 0


def test_state_tokens_are_head_not_text(capture):
    text, elements, index = capture
    by_tag = {span["tag"]: span for span in index["spans"]}
    assert by_tag["toggles"]["states"] == ["checked", "collapsed"]
    assert index["spans"][1]["states"] == ["disabled"]
    # And they are not left in the element's own text.
    assert "<checked/>" not in text[by_tag["toggles"]["head_end"]:by_tag["toggles"]["text_end"]]


def _by_key(index, key):
    return [span for span in index["spans"] if span["key"] == key][0]


def test_the_occlusion_marker_and_entities_stay_inside_the_text(capture):
    text, elements, index = capture
    span = _by_key(index, "uid:clip")
    own = text[span["head_end"]:span["text_end"]]
    assert own == "a &amp; b<occluded/>&lt;c&gt;"
    # The two visible fragments are head, and decode back to where they were.
    assert [rect["x"] for rect in span["fragments"]] == [110, 260]


def test_a_labelled_token_belongs_to_its_window_rather_than_becoming_a_block(capture):
    """`<title>` describes the window; it is not an element and has no box.

    Told apart structurally - no `<loc_>` follows it - so a token added upstream
    later lands in the same place instead of appearing as a block that matches
    nothing.
    """
    text, elements, index = capture
    window = index["spans"][0]
    assert window["labels"] == [["title", "Notes"]]
    assert "<title>" not in text[window["head_end"]:window["text_end"]]
    assert "title" not in [span["tag"] for span in index["spans"]]
    # A caret in the title is a caret in the window.
    assert screentag.innermost(index["spans"], text.index("<title>") + 3) == 0


def test_an_unknown_labelled_token_does_not_shift_the_blocks_after_it():
    plain = "<screentag><button><loc_1><loc_2><loc_3><loc_4>Go</button></screentag>"
    labelled = plain.replace("<loc_4>", "<loc_4><tooltip>press me</tooltip>")
    blocks = screentag.parse_blocks(labelled)
    assert [block["tag"] for block in blocks] == ["button"]
    assert blocks[0]["labels"] == [["tooltip", "press me"]]
    assert labelled[blocks[0]["head_end"]:blocks[0]["text_end"]] == "Go"


def test_a_stray_closing_tag_does_not_unwind_the_document():
    blocks = screentag.parse_blocks("<screentag></button><text><loc_1></text></screentag>")
    assert [block["tag"] for block in blocks] == ["text"]
    assert blocks[0]["end"] is not None


def test_an_unclosed_block_is_closed_at_the_end_of_the_file():
    text = "<screentag><window><loc_1><loc_2><loc_3><loc_4>cut off"
    blocks = screentag.parse_blocks(text)
    assert len(blocks) == 1 and blocks[0]["end"] == len(text)


# ----------------------------------------------------------------- mapping


def test_every_block_maps_to_the_element_that_produced_it(capture):
    text, elements, index = capture
    assert index["stats"] == {
        "blocks": len(elements), "elements": len(elements), "matched": len(elements),
        "unmatched_blocks": 0, "unmatched_elements": 0, "repeats": 0, "ambiguous": 0,
    }
    keys = overlay.element_keys(elements)
    for span in index["spans"]:
        element = elements[span["element"]]
        assert span["key"] == keys[span["element"]]
        # The independent channel: text is not part of the signature the
        # matcher uses, so agreeing here means the pairing is right.
        assert text[span["head_end"]:span["text_end"]] == _serialized_text(element)


def test_loc_tokens_decode_back_onto_the_element_they_belong_to(capture):
    text, elements, index = capture
    cell_w, cell_h = VIEWPORT_W / screentag.LOC_GRID, VIEWPORT_H / screentag.LOC_GRID
    for span in index["spans"]:
        rect = elements[span["element"]]["rect"]
        decoded = span["rect"]
        # Truncation can only lose part of a cell per edge, and never gains.
        assert 0 <= rect["x"] - decoded["x"] <= cell_w
        assert 0 <= rect["y"] - decoded["y"] <= cell_h
        assert abs((decoded["x"] + decoded["w"]) - (rect["x"] + rect["w"])) <= cell_w
        assert abs((decoded["y"] + decoded["h"]) - (rect["y"] + rect["h"])) <= cell_h


def test_the_key_is_the_one_the_boxes_are_drawn_with(capture):
    text, elements, index = capture
    keys = [span["key"] for span in index["spans"]]
    assert "uid:btn" in keys
    # The element with no uid falls back to the dom form the overlay uses.
    assert "dom:zim:5:app" in keys


def test_the_spans_tile_the_file_with_no_gap_and_no_overlap(capture):
    """Rebuild the text from the ranges, the way the browser renders it."""
    text, elements, index = capture
    spans = index["spans"]
    children = {}
    for position, span in enumerate(spans):
        children.setdefault(span["parent"], []).append(position)

    def emit(position):
        span = spans[position]
        out = [text[span["start"]:span["text_end"]]]
        cursor = span["text_end"]
        for child in children.get(position, []):
            out.append(text[cursor:spans[child]["start"]])
            out.append(emit(child))
            cursor = spans[child]["end"]
        out.append(text[cursor:span["end"]])
        return "".join(out)

    rebuilt = []
    cursor = 0
    for position in children.get(-1, []):
        rebuilt.append(text[cursor:spans[position]["start"]])
        rebuilt.append(emit(position))
        cursor = spans[position]["end"]
    rebuilt.append(text[cursor:])
    assert "".join(rebuilt) == text


def test_a_caret_resolves_to_the_innermost_block(capture):
    text, elements, index = capture
    spans = index["spans"]
    button = spans[1]
    # Anywhere inside the button's range is the button, not the window holding it.
    for offset in (button["start"], button["head_end"], button["end"] - 1):
        assert screentag.innermost(spans, offset) == 1
    # The window's own head belongs to the window.
    assert screentag.innermost(spans, spans[0]["head_end"]) == 0
    # And its closing tag, after the last child, still does.
    assert screentag.innermost(spans, spans[0]["end"] - 1) == 0


def test_a_block_no_element_accounts_for_is_reported_not_guessed():
    elements = _elements()[:1]
    text = _basic_screentag(_elements(), VIEWPORT_W, VIEWPORT_H)
    index = screentag.build_index(text, elements, VIEWPORT_W, VIEWPORT_H)
    assert index["stats"]["matched"] == 1
    assert index["stats"]["unmatched_blocks"] == 6
    assert [span["element"] for span in index["spans"]] == [0, None, None, None, None, None, None]


def test_the_wrong_viewport_fails_loudly_rather_than_shifting_everything():
    """Half the viewport doubles every token, so nothing may match by accident."""
    elements = _elements()
    text = _basic_screentag(elements, VIEWPORT_W, VIEWPORT_H)
    index = screentag.build_index(text, elements, VIEWPORT_W // 2, VIEWPORT_H // 2)
    assert index["stats"]["matched"] == 0


def test_an_element_serialized_twice_reuses_its_own_span():
    """A child listed by two windows is emitted twice; both blocks are its own."""
    elements = _elements()
    elements[0]["children_indices"] = [1]
    elements.insert(1, {
        "uid": "win2", "role": "dialog", "type": "Window", "name": "Second",
        "app_name": "zim", "source": "app", "_dom_index": 7,
        "children_indices": [1], "reading_order_index": 7,
        "rect": {"x": 300, "y": 200, "w": 200, "h": 150},
        "visible_text": "", "visible_text_status": "no_text",
    })
    text = _basic_screentag(elements, VIEWPORT_W, VIEWPORT_H)
    index = screentag.build_index(
        text, elements, VIEWPORT_W, VIEWPORT_H, overlay.element_keys(elements))
    buttons = [span for span in index["spans"] if span["key"] == "uid:btn"]
    assert len(buttons) == 2
    assert [span["repeat"] for span in buttons] == [False, True]
    assert index["stats"]["unmatched_blocks"] == 0


# --------------------------------------------------------------------- api


RUN = "v900_tag/batch"
STEM = "scene-tagged"


@pytest.fixture()
def tag_app(tmp_path):
    run = tmp_path / "runs" / RUN
    run.mkdir(parents=True)
    elements = _elements()
    Image.new("RGB", (VIEWPORT_W, VIEWPORT_H), (20, 20, 20)).save(str(run / (STEM + ".png")))
    (run / (STEM + ".elements.leaf.json")).write_text(json.dumps(elements), encoding="utf-8")
    (run / (STEM + ".meta.json")).write_text(json.dumps({
        "num_elements_leaf": len(elements), "launched_apps": ["zim"],
        "screentag_source": "leaf",
        "viewport": {"width": VIEWPORT_W, "height": VIEWPORT_H},
        "scene": {"seed": 1},
    }), encoding="utf-8")
    (run / (STEM + ".screentag.txt")).write_text(
        _basic_screentag(elements, VIEWPORT_W, VIEWPORT_H), encoding="utf-8")
    return InspectorApp(root=tmp_path / "runs", golden_root=tmp_path / "golden",
                        author="tester", cache_dir=tmp_path / "cache", project_root=tmp_path)


def _get(app, path, **params):
    return app.handle("GET", path, {key: [str(value)] for key, value in params.items()})


def test_the_tag_endpoint_still_serves_plain_text(tag_app):
    response = _get(tag_app, "/api/screentag", run=RUN, stem=STEM)
    assert response.headers["Content-Type"].startswith("text/plain")
    assert response.read().decode("utf-8").startswith("<screentag>")


def test_the_tag_endpoint_serves_the_span_index(tag_app):
    body = _get(tag_app, "/api/screentag", run=RUN, stem=STEM, spans=1).json()
    assert body["view"] == "leaf" and body["grid"] == screentag.LOC_GRID
    assert body["viewport"] == {"width": VIEWPORT_W, "height": VIEWPORT_H}
    assert body["stats"]["unmatched_blocks"] == 0
    assert body["spans"][0]["key"] == "uid:win"
    # The keys are the ones `/api/sample` draws its boxes with, which is the
    # whole reason the browser can link the two panes.
    sample = _get(tag_app, "/api/sample", run=RUN, stem=STEM, view="leaf").json()
    drawn = set(element["_key"] for element in sample["elements"])
    assert set(span["key"] for span in body["spans"]) <= drawn


def test_the_index_says_so_when_it_cannot_link(tag_app, tmp_path):
    (tmp_path / "runs" / RUN / (STEM + ".elements.leaf.json")).unlink()
    body = _get(tag_app, "/api/screentag", run=RUN, stem=STEM, spans=1).json()
    assert body["spans"] == [] and "unlinked" in body


def test_the_viewport_falls_back_to_the_png_when_meta_has_none(tag_app, tmp_path):
    meta_path = tmp_path / "runs" / RUN / (STEM + ".meta.json")
    meta = json.loads(meta_path.read_text())
    del meta["viewport"]
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    body = _get(tag_app, "/api/screentag", run=RUN, stem=STEM, spans=1).json()
    assert body["viewport"] == {"width": VIEWPORT_W, "height": VIEWPORT_H}
    assert body["stats"]["unmatched_blocks"] == 0
