"""Element text must not be readable as ScreenTag markup.

Measured on a Bluefish window showing an HTML file: the editor's contents were
serialized raw, so the document's own `<html>`, `<head>` and `<title>` appeared
in the ScreenTag as though they were elements - eleven phantom tags in one
capture. Any app that displays markup or code does this.
"""

import re

from deskshot.extraction.run_extraction import (
    _basic_screentag,
    _serialized_text,
    escape_screentag_text,
)
from deskshot.extraction.text_visibility import TEXT_CLIP_MARKER


def test_angle_brackets_in_content_are_escaped() -> None:
    assert escape_screentag_text("<title>x</title>") == "&lt;title&gt;x&lt;/title&gt;"


def test_ampersand_is_escaped_first() -> None:
    """Otherwise escaping `<` would corrupt an entity that was already there."""
    assert escape_screentag_text("a & b < c") == "a &amp; b &lt; c"


def test_a_document_full_of_markup_adds_no_tags() -> None:
    """The measured case: an editor showing HTML source."""
    elem = {
        "role": "text", "type": "text", "tag": "text",
        "inner_text": "<!DOCTYPE html>\n<html>\n<head><title>Page</title></head>\n",
        "rect": {"x": 0, "y": 0, "w": 100, "h": 40},
        "_dom_index": 0, "reading_order_index": 0,
    }

    tag = _basic_screentag([elem], 1000, 1000)

    opened = [t for t in re.findall(r"<([a-z_]+)>", tag) if t != "screentag"]
    assert opened == ["text"]
    assert "<title>" not in tag and "<html>" not in tag
    assert "&lt;title&gt;" in tag


def test_the_occlusion_marker_survives_escaping() -> None:
    """The marker is markup we emit deliberately; the text around it is not."""
    out = _serialized_text({"visible_text_marked": f"ab{TEXT_CLIP_MARKER}<b>c"})

    assert out == f"ab{TEXT_CLIP_MARKER}&lt;b&gt;c"


def test_plain_text_is_unchanged() -> None:
    assert _serialized_text({"inner_text": "Save As..."}) == "Save As..."


def test_unsupported_partial_still_yields_nothing() -> None:
    assert _serialized_text(
        {"visible_text_status": "unsupported_partial", "inner_text": "<b>x"}
    ) == ""
