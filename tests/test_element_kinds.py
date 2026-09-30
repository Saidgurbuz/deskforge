"""The second vocabulary has to fix what the first one got wrong.

Measured over 4,763 leaf elements, the legacy 55 classes used 25, put 71% of
everything into three, and made three specific mistakes. Each is pinned here, so
the new vocabulary cannot drift back into them.
"""

from deskshot.extraction.element_kinds import (
    KINDS,
    ROLE_TO_KIND,
    STRUCTURAL_ROLES,
    annotate_kinds,
    kind_for,
)


def _elem(role, **kw):
    e = {"role": role, "rect": {"x": 0, "y": 0, "w": 10, "h": 10}}
    e.update(kw)
    return e


# --------------------------------------------------------------------------
# the three defects
# --------------------------------------------------------------------------

def test_a_two_state_control_is_one_kind():
    """`Toggles` 266 / `Checkbox` 10 / `Radiobox` 12 split by which role the
    toolkit chose, not by anything on screen. Which state it is in is carried by
    the state tokens, where a visible fact belongs."""
    for role in ("toggle button", "check box", "radio button", "switch"):
        assert kind_for(_elem(role)) == "toggle"


def test_a_grid_cell_is_not_generic_text():
    """`Text` absorbed 1,122 table cells, so "this is tabular" - which is
    visible - was thrown away."""
    assert kind_for(_elem("table cell")) == "cell"
    assert kind_for(_elem("label")) == "text"


def test_a_cell_is_never_typed_as_an_icon():
    """`File Icon` came from `icon` (322) *and* `table cell` (264): a file
    manager's semantics mistyping generic cells."""
    assert kind_for(_elem("icon")) == "icon"
    assert kind_for(_elem("table cell")) != "icon"


# --------------------------------------------------------------------------
# coverage and honesty
# --------------------------------------------------------------------------

def test_every_mapped_kind_is_in_the_vocabulary():
    unknown = sorted(set(ROLE_TO_KIND.values()) - set(KINDS))
    assert unknown == []


def test_containers_are_not_given_a_kind():
    """They are not annotated on their own, and inventing a kind for them is how
    a catch-all starts."""
    for role in ("panel", "filler", "split pane", "scroll pane", "section"):
        assert role in STRUCTURAL_ROLES
        assert kind_for(_elem(role)) is None


def test_an_unknown_role_is_reported_not_guessed():
    """A vocabulary that quietly swallows the unknown is how the legacy one
    ended up with a 34% catch-all."""
    elements = [_elem("some-future-toolkit-thing")]
    meta = annotate_kinds(elements)
    assert meta["unmapped_roles"] == {"some-future-toolkit-thing": 1}
    assert "kind" not in elements[0]


def test_an_unknown_role_that_is_editable_reads_as_a_text_input():
    elem = _elem("weird-entry", attrs={"interfaces": {"editable_text": True}})
    assert kind_for(elem) == "textinput"


def test_an_unknown_role_carrying_text_reads_as_text():
    assert kind_for(_elem("weird-label", inner_text="Hello")) == "text"


def test_annotate_sets_the_field_without_touching_the_legacy_one():
    elements = [_elem("push button", type="Button")]
    annotate_kinds(elements)
    assert elements[0]["kind"] == "button"
    assert elements[0]["type"] == "Button", "the legacy class must survive"


def test_search_fields_and_entries_are_one_kind():
    """Same picture; the placeholder already says which it is."""
    assert kind_for(_elem("search field")) == kind_for(_elem("entry")) == "textinput"
