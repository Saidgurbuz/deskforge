"""ScreenTag tags must be spelled the way the tokenizer holds them.

The patched tokenizers carry 167 element tags as single tokens and every one is
capitalised. The serializer lowercased, so the corpus emitted `<button>` where
the vocabulary held `<Button>` - three tokens instead of one, twice per element,
about 135 elements per capture. Measured over 150 captures that cost 21% of the
sequence, which on a dense screen is the difference between fitting the context
and not.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from deskshot.extraction.run_extraction import _elements_to_screentag

#: The patched tokenizer's spelling for every type the corpus produces.
CORPUS_TYPES = [
    "Button", "Text", "File Icon", "Toggles", "Menu", "Heading", "Window",
    "Image", "Tab", "Text Input", "Link", "Select", "List Item", "Scroll",
    "Status Bar", "Search Field", "Side Bar", "Radiobox", "Alert", "List",
    "Checkbox", "Table", "Toolbar", "Tab Bar", "Calendar", "Steppers",
    "Navigation Bar",
]


def _elem(kind: str, i: int = 0):
    return {
        "type": kind, "role": "push button", "source": "app",
        "rect": {"x": 10 * i, "y": 10, "w": 40, "h": 20},
        "_dom_index": i, "reading_order_index": i,
        "visible_text": kind, "visible_fragments": [{"x": 10 * i, "y": 10, "w": 40, "h": 20}],
    }


@pytest.mark.parametrize("kind", CORPUS_TYPES)
def test_each_type_serialises_with_its_vocabulary_spelling(kind: str) -> None:
    out = _elements_to_screentag([_elem(kind)], 1920, 1080)
    expected = "<" + kind.replace(" ", "_") + ">"
    assert expected in out, f"{kind!r} did not emit {expected}: {out[:120]}"


def test_no_tag_is_emitted_in_lowercase() -> None:
    out = _elements_to_screentag([_elem(k, i) for i, k in enumerate(CORPUS_TYPES)],
                                 1920, 1080)
    tags = set(re.findall(r"</?([A-Za-z_]+)>", out))
    wrong = {
        t for t in tags
        if t.lower() == t
        and t.replace("_", " ").title().replace(" ", "_") in
        {k.replace(" ", "_") for k in CORPUS_TYPES}
    }
    assert not wrong, f"emitted lowercase where the vocabulary is capitalised: {wrong}"


def test_multiword_types_use_underscores_not_spaces() -> None:
    out = _elements_to_screentag([_elem("File Icon")], 1920, 1080)
    assert "<File_Icon>" in out and "<File Icon>" not in out


def test_the_structure_is_otherwise_unchanged() -> None:
    """Only the casing moved; geometry, text and closing tags still work."""
    out = _elements_to_screentag([_elem("Button")], 1920, 1080)
    assert out.startswith("<screentag>") and out.endswith("</screentag>")
    assert "</Button>" in out
    assert re.search(r"<loc_\d+><loc_\d+><loc_\d+><loc_\d+>", out)
    assert "Button</Button>" in out


def test_the_saving_is_real_on_the_projects_own_tokenizer() -> None:
    """Skips unless the patched tokenizer is present; asserts the win when it is."""
    path = Path(os.environ.get("DESKSHOT_PATCHED_TOKENIZER", "/nonexistent/granite-patched"))
    cfg = path / "tokenizer_config.json"
    if not cfg.is_file():
        pytest.skip("patched tokenizer not available here")
    vocab = {
        v["content"]
        for v in (json.loads(cfg.read_text()).get("added_tokens_decoder") or {}).values()
    }
    for kind in CORPUS_TYPES:
        tag = "<" + kind.replace(" ", "_") + ">"
        assert tag in vocab, f"{tag} is not a single token in the patched vocabulary"
