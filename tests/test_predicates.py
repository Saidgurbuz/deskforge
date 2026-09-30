from deskshot.generation.predicates import (
    Conjunction,
    ElementMatch,
    Predicate,
    element_text,
)


def _elem(role, text, **extra):
    elem = {"role": role, "inner_text": text, "uid": text, "rect": {"x": 0, "y": 0, "w": 10, "h": 10}}
    elem.update(extra)
    return elem


def test_match_ignores_mnemonic_underscores_and_case() -> None:
    """GTK exposes `_File` for a menu the screen draws as `File`."""
    state = [_elem("menu", "_File")]

    assert ElementMatch(role="menu", text="File").find(state)
    assert ElementMatch(role="MENU", text="file").find(state)


def test_match_prefers_the_text_that_is_actually_on_screen() -> None:
    """A partly covered element matches on what a person can read."""
    elem = _elem("table cell", "1234,56", visible_text="12")

    assert element_text(elem) == "12"
    assert ElementMatch(text="12").matches(elem)
    assert not ElementMatch(text="1234,56").matches(elem)


def test_exists_and_absent_are_opposites() -> None:
    state = [_elem("push button", "Save")]
    here = Predicate("exists", ElementMatch(role="push button", text="Save"))
    gone = Predicate("absent", ElementMatch(role="push button", text="Save"))

    assert here.holds(state)
    assert not gone.holds(state)
    assert not here.holds([])
    assert gone.holds([])


def test_visible_requires_an_unoccluded_instance() -> None:
    covered = [_elem("push button", "Save", occlusion_state="partial")]
    clear = [_elem("push button", "Save", occlusion_state="none")]
    pred = Predicate("visible", ElementMatch(text="Save"))

    assert not pred.holds(covered)
    assert pred.holds(clear)


def test_checked_reads_the_interaction_block() -> None:
    on = [_elem("check box", "Wrap", interaction={"checked": True})]
    off = [_elem("check box", "Wrap", interaction={"checked": False})]

    assert Predicate("checked", ElementMatch(text="Wrap")).holds(on)
    assert not Predicate("checked", ElementMatch(text="Wrap")).holds(off)
    assert Predicate("unchecked", ElementMatch(text="Wrap")).holds(off)


def test_count_predicates_measure_repetition() -> None:
    """The check behind a repeated-entry task: N rows, not merely some rows."""
    state = [_elem("table row", f"row {i}") for i in range(4)]
    match = ElementMatch(role="table row")

    assert Predicate("count_at_least", match, count=4).holds(state)
    assert not Predicate("count_at_least", match, count=5).holds(state)
    assert Predicate("count_equals", match, count=4).holds(state)


def test_conjunction_reports_which_parts_are_unmet() -> None:
    """Partial credit is the progress signal a long task needs."""
    goal = Conjunction((
        Predicate("exists", ElementMatch(text="A")),
        Predicate("exists", ElementMatch(text="B")),
        Predicate("exists", ElementMatch(text="C")),
    ))
    state = [_elem("x", "A"), _elem("x", "C")]

    unmet = goal.unmet(state)

    assert not goal.holds(state)
    assert [p.match.text for p in unmet] == ["B"]
    assert goal.progress(state) == 2 / 3
    assert goal.progress([_elem("x", n) for n in "ABC"]) == 1.0


def test_predicates_round_trip_through_json() -> None:
    """A task is stored and replayed, so its success test must survive JSON."""
    import json

    goal = Conjunction((
        Predicate("text_equals", ElementMatch(role="text", app_name="mousepad"), value="hi"),
        Predicate("count_at_least", ElementMatch(role="table row"), count=3),
    ))

    restored = Conjunction.from_dict(json.loads(json.dumps(goal.to_dict())))

    assert restored == goal


def test_describe_reads_as_an_instruction() -> None:
    """The instruction is rendered from the checker, so the two cannot disagree."""
    goal = Conjunction((
        Predicate("text_equals", ElementMatch(role="text", app_name="mousepad"), value="hello"),
        Predicate("exists", ElementMatch(role="push button", text="Save")),
    ))

    assert goal.describe() == (
        'the text in mousepad reads "hello", and there is a push button labelled "Save"'
    )


def test_unknown_predicate_kind_is_rejected_at_construction() -> None:
    """A typo in a task definition must fail loudly, not evaluate to False."""
    import pytest

    with pytest.raises(ValueError):
        Predicate("is_probably_fine", ElementMatch(text="x"))


def test_role_any_matches_any_of_several_spellings() -> None:
    """GTK calls the same concept dialog, alert, or file chooser."""
    match = ElementMatch(role_any=("dialog", "alert", "file chooser"))

    assert match.matches(_elem("alert", "Really?"))
    assert match.matches(_elem("file chooser", "Open"))
    assert not match.matches(_elem("frame", "Main"))
    assert "dialog or alert or file chooser" in match.describe()


def test_role_any_survives_json_as_a_tuple() -> None:
    """JSON has no tuples and the match must stay hashable after a round trip."""
    import json

    pred = Predicate("exists", ElementMatch(role_any=("dialog", "alert")))

    restored = Predicate.from_dict(json.loads(json.dumps(pred.to_dict())))

    assert restored == pred
    assert isinstance(restored.match.role_any, tuple)
