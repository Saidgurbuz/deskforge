"""Executor behaviour, against a fake GUI rather than a real session.

The transitions below are the ones that actually bite in GTK: a menu only lists
its items once it is open, and clicking an open menu closes it again.
"""

from typing import Any, Dict, List

import pytest

from deskshot.generation.actions import diff_states
from deskshot.generation.predicates import Conjunction, ElementMatch, Predicate
from deskshot.generation.skills import ClickTarget, OpenMenu, TypeInto, activate_menu_item
from deskshot.generation.tasks import Task, run_task


def _elem(role, text, uid=None, **extra):
    elem = {
        "role": role,
        "inner_text": text,
        "uid": uid or f"{role}:{text}",
        "rect": {"x": 0, "y": 0, "w": 40, "h": 20},
        "interaction": {"actionable": True, "click_point": [20, 10]},
    }
    elem.update(extra)
    return elem


MENUS = {
    "File": ["New", "Open", "Save"],
    "Edit": ["Cut", "Copy"],
}


class FakeApp:
    """A menu bar, one open menu at a time, and a text field."""

    def __init__(self) -> None:
        self.open_menu: str | None = None
        self.field_text = ""
        self.saved = False

    def elements(self) -> List[Dict[str, Any]]:
        state = [_elem("menu", name) for name in MENUS]
        state.append(_elem("text", self.field_text, uid="field"))
        if self.open_menu:
            state += [
                _elem("menu item", item, uid=f"item:{self.open_menu}:{item}")
                for item in MENUS[self.open_menu]
            ]
        if self.saved:
            state.append(_elem("label", "Saved", uid="saved"))
        return state

    def click(self, uid: str) -> None:
        if uid.startswith("menu:"):
            name = uid.split(":", 1)[1]
            self.open_menu = None if self.open_menu == name else name
        elif uid.startswith("item:"):
            _, _menu, item = uid.split(":", 2)
            self.open_menu = None
            if item == "Save":
                self.saved = True
        elif uid == "field":
            self.open_menu = None


class FakeEnv:
    """Just the two members `run_task` uses."""

    def __init__(self, app: FakeApp) -> None:
        self.app = app
        self._elements = app.elements()
        self.n = 0

    @property
    def last_elements(self) -> List[Dict[str, Any]]:
        return list(self._elements)

    def step(self, action: Any) -> Dict[str, Any]:
        before = self._elements
        if action.type == "click" and action.target_uid:
            self.app.click(action.target_uid)
        elif action.type == "type":
            self.app.field_text = action.text
        elif action.type == "key" and action.value == "ctrl+a":
            pass
        self._elements = self.app.elements()
        self.n += 1
        return {
            "action": action.to_dict(),
            "observation": {"stem": f"step{self.n}"},
            "diff": diff_states(before, self._elements),
            "step_index": self.n,
        }


def _save_task(skills, goal=None) -> Task:
    goal = goal or Conjunction((Predicate("exists", ElementMatch(text="Saved")),))
    return Task(task_id="t", instruction="save the file", skills=skills, goal=goal)


def test_menu_item_task_succeeds_in_exactly_two_steps() -> None:
    """Open the menu, choose the item. No third click, no filler."""
    env = FakeEnv(FakeApp())
    saved = Conjunction((Predicate("exists", ElementMatch(text="Saved")),))
    task = _save_task(activate_menu_item("File", "Save", result=saved))

    result = run_task(task, env)

    assert result.status == "success"
    assert [s.action["type"] for s in result.steps] == ["click", "click"]
    assert result.steps[0].intent == 'open the "File" menu'
    assert result.steps[1].intent == 'choose "Save" from the "File" menu'


def test_a_satisfied_skill_contributes_no_step() -> None:
    """The mechanism behind "every step was needed": the File menu is already
    open, so the skill that opens it emits nothing at all."""
    app = FakeApp()
    app.open_menu = "File"
    env = FakeEnv(app)
    saved = Conjunction((Predicate("exists", ElementMatch(text="Saved")),))
    task = _save_task(activate_menu_item("File", "Save", result=saved))

    result = run_task(task, env)

    assert result.status == "success"
    assert len(result.steps) == 1
    assert result.steps[0].skill == "activate(File/Save)"


def test_open_menu_corrects_the_wrong_menu_being_open() -> None:
    """A menu skill that only asked "is any menu open?" would be satisfied by
    the Edit menu and let the next click land on the wrong list."""
    app = FakeApp()
    app.open_menu = "Edit"
    env = FakeEnv(app)
    saved = Conjunction((Predicate("exists", ElementMatch(text="Saved")),))
    task = _save_task(activate_menu_item("File", "Save", result=saved))

    result = run_task(task, env)

    assert result.status == "success"
    assert app.open_menu is None and app.saved
    assert len(result.steps) == 2


def test_missing_target_fails_the_task_and_names_the_skill() -> None:
    env = FakeEnv(FakeApp())
    task = _save_task([
        ClickTarget(
            name="click_ghost",
            goal=Conjunction((Predicate("exists", ElementMatch(text="Nothing")),)),
            match=ElementMatch(role="push button", text="Does Not Exist"),
        )
    ])

    result = run_task(task, env)

    assert result.status == "skill_failed"
    assert result.failed_skill == "click_ghost"
    assert "no target" in result.reason
    assert result.steps == []


def test_a_skill_that_never_converges_stops_the_episode() -> None:
    """The plan is not continued past an unmet subgoal: later steps would carry
    an intent that is false."""
    env = FakeEnv(FakeApp())
    unreachable = Conjunction((Predicate("exists", ElementMatch(text="Never")),))
    task = _save_task([
        ClickTarget(
            name="click_file_forever",
            goal=unreachable,
            match=ElementMatch(role="menu", text="File"),
            max_actions=3,
        ),
        ClickTarget(
            name="should_not_run",
            goal=unreachable,
            match=ElementMatch(role="menu", text="Edit"),
        ),
    ])

    result = run_task(task, env)

    assert result.status == "skill_failed"
    assert result.failed_skill == "click_file_forever"
    assert len(result.steps) == 3
    assert {s.skill for s in result.steps} == {"click_file_forever"}


def test_plan_completed_but_goal_unmet_is_its_own_status() -> None:
    """Every subgoal held, so the run was fine; the task definition was not."""
    env = FakeEnv(FakeApp())
    task = _save_task(
        activate_menu_item("File", "Open"),
        goal=Conjunction((Predicate("exists", ElementMatch(text="Saved")),)),
    )

    result = run_task(task, env)

    assert result.status == "goal_unmet"
    assert result.failed_skill is None
    assert len(result.steps) == 2


def test_steps_carry_a_dense_progress_signal() -> None:
    """Not one reward at the end: each step says how much of the goal holds and
    what is still missing."""
    env = FakeEnv(FakeApp())
    goal = Conjunction((
        Predicate("exists", ElementMatch(text="Saved")),
        Predicate("text_equals", ElementMatch(role="text"), value="hello"),
    ))
    task = Task(
        task_id="t",
        instruction="save, then type hello",
        skills=[
            *activate_menu_item("File", "Save"),
            TypeInto.build(ElementMatch(role="text"), "hello"),
        ],
        goal=goal,
    )

    result = run_task(task, env)

    assert result.status == "success"
    assert result.steps[0].goal_progress == 0.0
    assert result.steps[1].goal_progress == 0.5
    assert result.steps[-1].goal_progress == 1.0
    assert result.steps[1].remaining == ['the text reads "hello"']
    assert result.steps[-1].remaining == []


def test_typing_is_recorded_as_the_three_actions_a_person_performs() -> None:
    env = FakeEnv(FakeApp())
    task = Task(
        task_id="t",
        instruction="type hello",
        skills=[TypeInto.build(ElementMatch(role="text"), "hello")],
        goal=Conjunction((Predicate("text_equals", ElementMatch(role="text"), value="hello"),)),
    )

    result = run_task(task, env)

    assert result.status == "success"
    assert [s.action["type"] for s in result.steps] == ["click", "key", "type"]


def test_a_failing_env_is_an_error_not_a_bad_plan() -> None:
    class Broken(FakeEnv):
        def step(self, action):
            raise RuntimeError("app died")

    task = _save_task(activate_menu_item("File", "Save"))

    result = run_task(task, Broken(FakeApp()))

    assert result.status == "error"
    assert "app died" in result.reason


def test_result_round_trips_to_json() -> None:
    import json

    env = FakeEnv(FakeApp())
    saved = Conjunction((Predicate("exists", ElementMatch(text="Saved")),))
    result = run_task(_save_task(activate_menu_item("File", "Save", result=saved)), env)

    payload = json.loads(json.dumps(result.to_dict(), ensure_ascii=False))

    assert payload["status"] == "success"
    assert payload["instruction"] == "save the file"
    assert len(payload["steps"]) == 2
    assert payload["plan"][0]["intent"] == 'open the "File" menu'


@pytest.mark.parametrize("rows", [3, 8])
def test_horizon_scales_by_composing_verified_units(rows: int) -> None:
    """Where 10-100 steps come from: repetition with a checkable end state, not
    a longer random walk."""
    env = FakeEnv(FakeApp())
    skills = []
    for i in range(rows):
        skills += activate_menu_item("Edit", "Copy")
        skills.append(TypeInto.build(ElementMatch(role="text"), f"row {i}"))
    task = Task(
        task_id="bulk",
        instruction=f"enter {rows} rows",
        skills=skills,
        goal=Conjunction((
            Predicate("text_equals", ElementMatch(role="text"), value=f"row {rows - 1}"),
        )),
    )

    result = run_task(task, env)

    assert result.status == "success"
    assert len(result.steps) == rows * 5          # open, choose, click, ctrl+a, type
    assert all(s.intent for s in result.steps)    # every step states its purpose


def test_a_task_can_be_run_twice() -> None:
    """A plan is a list of skill objects; re-running must not start a
    multi-action skill halfway through."""
    goal = Conjunction((Predicate("text_equals", ElementMatch(role="text"), value="hi"),))
    task = Task(
        task_id="t",
        instruction="type hi",
        skills=[TypeInto.build(ElementMatch(role="text"), "hi")],
        goal=goal,
    )

    first = run_task(task, FakeEnv(FakeApp()))
    second = run_task(task, FakeEnv(FakeApp()))

    assert first.status == second.status == "success"
    assert [s.action["type"] for s in second.steps] == ["click", "key", "type"]


def test_a_toggle_is_not_recorded_as_an_action_that_did_nothing() -> None:
    """Ticking a check box moves nothing and renames nothing. Without state in
    the diff the trajectory claims the click had no effect."""
    def box(checked):
        return [{"uid": "c1", "role": "check box", "inner_text": "Wrap",
                 "rect": {"x": 0, "y": 0, "w": 10, "h": 10},
                 "interaction": {"checked": checked}}]

    diff = diff_states(box(False), box(True))

    assert diff["state_changed"] == ["c1"]
    assert diff["changed"] is True
    assert diff["moved"] == [] and diff["text_changed"] == []


def test_the_diff_says_what_changed_not_only_that_something_did() -> None:
    """An episode should be readable on its own, without joining it back
    against the two element files it was computed from."""
    def box(checked, label="Wrap"):
        return [{"uid": "c1", "role": "check box", "inner_text": label,
                 "rect": {"x": 0, "y": 0, "w": 10, "h": 10},
                 "interaction": {"checked": checked}}]

    diff = diff_states(box(False), box(True))

    change = diff["changes"][0]
    assert change["kind"] == "state"
    assert change["label"] == "Wrap"
    assert change["before"]["checked"] is False
    assert change["after"]["checked"] is True


def test_magnitude_summarises_how_far_the_screen_moved() -> None:
    """One number, so episodes can be filtered or ordered without reading diffs."""
    before = [{"uid": f"u{i}", "role": "label", "inner_text": str(i),
               "rect": {"x": 0, "y": 0, "w": 5, "h": 5}} for i in range(10)]
    after = before[:5]

    assert diff_states(before, before)["magnitude"] == 0.0
    assert diff_states(before, after)["magnitude"] == 0.5
