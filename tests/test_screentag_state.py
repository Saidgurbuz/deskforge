"""ScreenTag must be able to express state the screen is showing.

A ticked check box and an unticked one occupy the same rectangle and carry the
same label. Before these tokens the two serialized to the identical string, so
the representation could not describe a settings panel at all - and a whole class
of episode frames were byte-identical training targets.
"""

from deskshot.extraction.run_extraction import _basic_screentag, _state_tokens


def _elem(role, text, interaction=None, **extra):
    elem = {
        "role": role,
        "type": role,
        "tag": role,
        "inner_text": text,
        "rect": {"x": 0, "y": 0, "w": 100, "h": 20},
        "_dom_index": extra.pop("dom", 0),
        "reading_order_index": 0,
    }
    if interaction is not None:
        elem["interaction"] = interaction
    elem.update(extra)
    return elem


def test_checked_and_unchecked_are_both_emitted() -> None:
    """Unchecked is a fact about the screen, not missing data, so it gets a
    token too - otherwise its absence is ambiguous with "not a check box"."""
    on = _elem("check box", "Wrap", {"checked": True})
    off = _elem("check box", "Wrap", {"checked": False})

    assert _state_tokens(on) == "<checked/>"
    assert _state_tokens(off) == "<unchecked/>"


def test_the_two_states_no_longer_serialize_identically() -> None:
    on = _basic_screentag([_elem("check box", "Wrap", {"checked": True})], 1000, 1000)
    off = _basic_screentag([_elem("check box", "Wrap", {"checked": False})], 1000, 1000)

    assert on != off
    assert "<checked/>Wrap" in on
    assert "<unchecked/>Wrap" in off


def test_checked_state_is_read_for_roles_that_lie_about_being_checkable() -> None:
    """Measured on this stack: GTK dialog check boxes report checkable=False
    while carrying a real checked value. Gating on the flag drops them all."""
    elem = _elem("check box", "Show line numbers", {"checkable": False, "checked": True})

    assert _state_tokens(elem) == "<checked/>"


def test_a_plain_label_gets_no_state_token() -> None:
    """Absence has to stay cheap: most elements have no state worth naming."""
    assert _state_tokens(_elem("label", "Hello", {"checked": False})) == ""
    assert _state_tokens(_elem("push button", "OK", {"enabled": True})) == ""


def test_only_the_informative_side_is_emitted_for_the_common_cases() -> None:
    """Almost everything is enabled, unexpanded and unselected. Emitting the
    negative for each would triple a typical tag for no information."""
    assert _state_tokens(_elem("push button", "OK", {"enabled": False})) == "<disabled/>"
    assert _state_tokens(_elem("page tab", "View", {"selected": True})) == "<selected/>"
    assert _state_tokens(_elem("page tab", "File", {"selected": False})) == ""
    assert _state_tokens(
        _elem("tree item", "src", {"expandable": True, "expanded": True})
    ) == "<expanded/>"
    assert _state_tokens(
        _elem("tree item", "src", {"expandable": True, "expanded": False})
    ) == "<collapsed/>"


def test_tokens_are_ordered_and_combine() -> None:
    elem = _elem("check menu item", "Word Wrap", {
        "checked": True, "expandable": True, "expanded": False,
        "selected": True, "enabled": False,
    })

    assert _state_tokens(elem) == "<checked/><collapsed/><selected/><disabled/>"


def test_state_sits_before_the_text_and_after_the_geometry() -> None:
    """So a reader hits position, then what the widget is, then what it says."""
    tag = _basic_screentag([_elem("check box", "Wrap", {"checked": True})], 1000, 1000)

    assert tag == "<screentag><check_box><loc_0><loc_0><loc_50><loc_10><checked/>Wrap</check_box></screentag>"


def test_an_element_without_an_interaction_block_is_unchanged() -> None:
    """Peeked and legacy elements carry no interaction block; they must still
    serialize rather than raise."""
    assert _state_tokens(_elem("check box", "Wrap")) == ""
