from deskshot.generation.predicates import ElementMatch
from deskshot.generation.task_builders import (
    build_units,
    compose_task,
    dialog_round_trip,
    is_safe_item,
    opens_dialog,
    plan_long_task,
    plan_unit_tasks,
    probe_menus,
    discover_effects,
    TOGGLE_LIKE,
    settings_task,
    plan_settings_task,
    plan_cross_app_task,
    focus_app,
    Affordance,
    discover_affordances,
    affordance_goal,
    affordance_skill,
    affordance_task,
    default_target,
)
from deskshot.generation.skills import OpenMenu, discover_header_menu_buttons

MENU_MAP = {
    "File": ["New", "Open...", "Save", "Save As...", "Quit"],
    "Edit": ["Cut", "Copy", "Preferences…"],
    "Help": ["Contents", "About"],
}


def test_destructive_and_blocking_items_are_refused() -> None:
    """These end the session or hang on something outside the scene."""
    for label in ("Quit", "Log Out", "Delete", "Empty Trash", "Print...", "Contents"):
        assert not is_safe_item(label), label
    for label in ("Open...", "Preferences…", "About", "Copy"):
        assert is_safe_item(label), label


def test_close_is_refused_but_not_words_containing_it() -> None:
    """A word-boundary match, so "Close" goes and "Closed Captions" stays."""
    assert not is_safe_item("Close")
    assert is_safe_item("Closed Captions")


def test_dialog_items_are_recognised_by_either_ellipsis() -> None:
    """GTK writes three dots in some apps and the single character in others."""
    assert opens_dialog("Open...")
    assert opens_dialog("Preferences…")
    assert not opens_dialog("Save")


def test_units_cover_the_safe_dialog_items_only() -> None:
    units = build_units(MENU_MAP, app_name="mousepad")

    assert [(u["menu"], u["item"]) for u in units] == [
        ("Edit", "Preferences…"),
        ("File", "Open..."),
        ("File", "Save As..."),
    ]
    assert all(u["kind"] == "dialog" for u in units)


def test_non_dialog_units_are_available_when_asked_for() -> None:
    units = build_units(MENU_MAP, app_name="mousepad", dialogs_only=False)
    labels = [u["item"] for u in units]

    assert "Save" in labels and "About" in labels
    assert "Quit" not in labels and "Contents" not in labels


def test_a_round_trip_ends_where_it_started() -> None:
    """What makes units composable: the dialog is dismissed, so the next unit
    starts from a closed state instead of behind a modal."""
    skills = dialog_round_trip("File", "Open...", app_name="mousepad")

    assert [s.name for s in skills] == [
        "open_menu(File)",
        "open_dialog(File/Open...)",
        "press(Escape)",
    ]
    assert skills[-1].goal.parts[0].kind == "absent"
    assert skills[-1].goal.parts[0].match.role_any is not None


def test_instruction_lists_the_units_in_order() -> None:
    task = compose_task(
        build_units(MENU_MAP, app_name="mousepad")[:2],
        task_id="t",
        app_name="mousepad",
    )

    assert task.instruction == (
        'In mousepad: open "Preferences…" from the "Edit" menu and close it again; '
        'then open "Open..." from the "File" menu and close it again.'
    )
    assert task.metadata["num_units"] == 2


def test_long_task_reaches_the_requested_horizon() -> None:
    """Three dialog items still make a hundred-step task, by revisiting them."""
    task = plan_long_task(MENU_MAP, app_name="mousepad", target_steps=100, seed=7)

    assert task is not None
    assert len(task.skills) >= 100          # one action each when the app cooperates
    assert task.max_steps >= 100            # worst case, if every skill needs a retry
    assert all(s.intent for s in task.skills)


def test_long_task_is_deterministic_in_its_seed() -> None:
    a = plan_long_task(MENU_MAP, app_name="mousepad", target_steps=40, seed=3)
    b = plan_long_task(MENU_MAP, app_name="mousepad", target_steps=40, seed=3)
    c = plan_long_task(MENU_MAP, app_name="mousepad", target_steps=40, seed=4)

    assert [s.name for s in a.skills] == [s.name for s in b.skills]
    assert [s.name for s in a.skills] != [s.name for s in c.skills]


def test_without_repeats_the_plan_stops_at_the_units_available() -> None:
    task = plan_long_task(
        MENU_MAP, app_name="mousepad", target_steps=100, seed=1, allow_repeats=False
    )

    assert task.metadata["num_units"] == 3


def test_an_app_with_no_usable_menus_yields_no_task() -> None:
    assert plan_long_task({}, app_name="xterm", target_steps=10, seed=1) is None
    assert plan_long_task({"File": ["Quit"]}, app_name="x", target_steps=10, seed=1) is None


def test_unit_tasks_are_one_per_unit() -> None:
    tasks = plan_unit_tasks(MENU_MAP, app_name="mousepad")

    assert len(tasks) == 3
    assert all(len(t.skills) == 3 for t in tasks)
    assert tasks[0].task_id == "mousepad-Edit-Preferences…"


# --- probing -----------------------------------------------------------------


class ProbeEnv:
    """An app whose menus only list their items while open."""

    def __init__(self, menu_map, refuse=()):
        self.menu_map = menu_map
        self.refuse = set(refuse)
        self.open_menu = None
        self.actions = []

    def peek(self):
        state = [
            {"role": "menu", "name": m, "app_name": "mousepad", "uid": f"m:{m}"}
            for m in self.menu_map
        ]
        if self.open_menu:
            state += [
                {"role": "menu item", "name": i, "app_name": "mousepad", "uid": f"i:{i}"}
                for i in self.menu_map[self.open_menu]
            ]
        return state

    def act(self, action, state=None):
        self.actions.append(action)
        if action.type == "key" and action.value == "Escape":
            self.open_menu = None
            return
        label = (action.target_uid or "").split(":", 1)[-1]
        if label in self.refuse:
            raise RuntimeError("menu jammed")
        self.open_menu = label if label in self.menu_map else self.open_menu


def test_probe_reads_every_menu_and_leaves_them_closed() -> None:
    env = ProbeEnv(MENU_MAP)

    found = probe_menus(env, app_name="mousepad")

    assert found == MENU_MAP
    assert env.open_menu is None
    assert [a.value for a in env.actions if a.type == "key"] == ["Escape"] * 3


def test_one_uncooperative_menu_does_not_cost_the_others() -> None:
    env = ProbeEnv(MENU_MAP, refuse={"Edit"})

    found = probe_menus(env, app_name="mousepad")

    assert found["Edit"] == []
    assert found["File"] == MENU_MAP["File"]
    assert found["Help"] == MENU_MAP["Help"]


def test_probing_an_app_without_a_menu_bar_is_not_an_error() -> None:
    assert probe_menus(ProbeEnv({}), app_name="mousepad") == {}


# --- effect discovery --------------------------------------------------------


class EffectEnv(ProbeEnv):
    """Menu items with real consequences: a toggle, a one-way switch, an inert item."""

    def __init__(self, menu_map):
        super().__init__(menu_map)
        self.statusbar = False
        self.burned = False

    TOGGLES = {"Statusbar", "Burn", "Nothing"}

    def peek(self):
        state = super().peek()
        for elem in state:
            if elem["role"] == "menu item" and elem["name"] in self.TOGGLES:
                elem["role"] = "check menu item"
        if self.statusbar:
            state.append({"role": "status bar", "name": "Ln 1", "app_name": "mousepad",
                          "uid": "sb"})
        if self.burned:
            state.append({"role": "label", "name": "Burned", "app_name": "mousepad",
                          "uid": "burned"})
        return state

    def act(self, action, state=None):
        label = (action.target_uid or "").split(":", 1)[-1]
        if action.type == "click" and label == "Statusbar":
            self.statusbar = not self.statusbar
        if action.type == "click" and label in ("Burn", "Show Burn"):
            self.burned = True          # cannot be undone
        super().act(action, state)


EFFECT_MENUS = {"View": ["Statusbar", "Burn", "Nothing"]}


def test_a_reversible_effect_is_discovered_and_described() -> None:
    env = EffectEnv(EFFECT_MENUS)

    effects = discover_effects(env, EFFECT_MENUS, app_name="mousepad")

    assert [e["item"] for e in effects] == ["Statusbar"]
    assert effects[0]["predicate"] == {
        "kind": "exists",
        "match": {"role": "status bar", "text": "Ln 1", "app_name": "mousepad"},
    }
    assert env.statusbar is False       # probing left the app as it found it


def test_an_effect_that_cannot_be_undone_is_discarded() -> None:
    """Keeping it would strand every later unit in a state its plan does not
    describe, and the probe itself would have damaged the scene."""
    env = EffectEnv({"View": ["Burn"]})

    effects = discover_effects(env, {"View": ["Burn"]}, app_name="mousepad")

    assert effects == []


def test_an_item_with_no_visible_consequence_is_not_an_effect() -> None:
    env = EffectEnv({"View": ["Nothing"]})

    assert discover_effects(env, {"View": ["Nothing"]}, app_name="mousepad") == []


def test_menu_churn_is_not_mistaken_for_an_effect() -> None:
    """Opening a menu fills the tree with its items; that is not a consequence."""
    env = EffectEnv({"View": ["Nothing"], "File": ["Save"]})

    assert discover_effects(env, {"View": ["Nothing"], "File": ["Save"]},
                            app_name="mousepad") == []


def test_a_measured_effect_becomes_the_tasks_end_state() -> None:
    """The point of the whole probe: a goal that was false at step zero."""
    env = EffectEnv(EFFECT_MENUS)
    effects = discover_effects(env, EFFECT_MENUS, app_name="mousepad")

    task = plan_long_task(
        MENU_MAP, app_name="mousepad", target_steps=12, seed=5, effects=effects
    )

    assert task.goal.parts[0].kind == "exists"
    assert task.goal.parts[0].match.role == "status bar"
    assert not task.goal.holds(env.peek())          # false before the task runs
    assert task.instruction.endswith('turn on "Statusbar" from the "View" menu.')


def test_labels_pick_out_toggles_when_the_toolkit_will_not() -> None:
    """Measured: mousepad reports all 46 items as plain "menu item" with no
    checkable state, so the label is the only signal left."""
    assert TOGGLE_LIKE.search("Word Wrap")
    assert TOGGLE_LIKE.search("Line Numbers")
    assert TOGGLE_LIKE.search("Viewer Mode")
    assert not TOGGLE_LIKE.search("Detach Tab")
    assert not TOGGLE_LIKE.search("Save All")


class UntypedEffectEnv(EffectEnv):
    """A toolkit that reports every item as a plain menu item, like mousepad."""

    TOGGLES: set = set()


def test_effects_are_still_found_when_no_item_is_typed_as_a_toggle() -> None:
    env = UntypedEffectEnv({"View": ["Statusbar", "Save All"]})

    effects = discover_effects(env, {"View": ["Statusbar", "Save All"]}, app_name="mousepad")

    assert [e["item"] for e in effects] == ["Statusbar"]


class WindowSplittingEnv(UntypedEffectEnv):
    """An item that moves the document into a second window."""

    def peek(self):
        state = super().peek()
        state.append({"role": "frame", "name": "Main", "app_name": "mousepad", "uid": "f1"})
        if self.burned:
            state.append({"role": "frame", "name": "Second", "app_name": "mousepad",
                          "uid": "f2"})
        return state


def test_a_probe_that_changes_the_window_set_stops_discovery() -> None:
    """One of the two failures seen live: "Detach Tab" moved the document into a
    second window and the app left the scene."""
    menus = {"View": ["Show Burn", "Statusbar"]}
    env = WindowSplittingEnv(menus)
    env.TOGGLES = set()

    effects = discover_effects(env, menus, app_name="mousepad")

    assert effects == []


def test_hiding_the_menu_bar_is_refused_outright() -> None:
    """It cannot be undone by the route that set it: with the menu bar hidden
    there is no menu left to reopen it from."""
    assert not is_safe_item("Menubar")
    assert not is_safe_item("Fullscreen")
    assert not TOGGLE_LIKE.search("Menubar") or not is_safe_item("Menubar")


class MenuHidingEnv(UntypedEffectEnv):
    """An item that removes the app's menu bar - the failure seen live."""

    def peek(self):
        state = super().peek()
        state.append({"role": "frame", "name": "Main", "app_name": "mousepad", "uid": "f1"})
        if self.burned:
            state = [e for e in state if e["role"] != "menu"]
        return state


def test_a_probe_that_removes_the_menu_bar_stops_discovery() -> None:
    """Windows alone do not catch this: the window count never changes."""
    menus = {"View": ["Show Burn", "Statusbar"]}
    env = MenuHidingEnv(menus)

    effects = discover_effects(env, menus, app_name="mousepad")

    assert effects == []


# --- dialog settings ---------------------------------------------------------


SETTINGS = [
    {"menu": "Edit", "dialog": "Preferences...", "app_name": "mousepad",
     "tab": "View", "label": "Show line numbers", "initial": False},
    {"menu": "Edit", "dialog": "Preferences...", "app_name": "mousepad",
     "tab": "View", "label": "Display whitespace", "initial": False},
    {"menu": "Edit", "dialog": "Preferences...", "app_name": "mousepad",
     "tab": "Editor", "label": "Insert Tabs", "initial": True},
]


def test_the_dialog_is_opened_once_for_all_the_settings_behind_it() -> None:
    """The prefix merge: planned independently these would open Preferences
    three times."""
    task = settings_task(
        SETTINGS,
        {"Show line numbers": True, "Display whitespace": True, "Insert Tabs": False},
        task_id="t", app_name="mousepad",
    )

    names = [s.name for s in task.skills]

    assert names == [
        "open_menu(Edit)",
        "open_dialog(Edit/Preferences...)",
        "set(Show line numbers=on)",
        "set(Display whitespace=on)",
        "select_tab(Editor)",
        "set(Insert Tabs=off)",
    ]
    assert names.count("open_dialog(Edit/Preferences...)") == 1


def test_the_tab_already_showing_costs_no_click() -> None:
    """Both settings are on the tab the dialog opens on."""
    task = settings_task(
        SETTINGS[:2], {"Show line numbers": True, "Display whitespace": True},
        task_id="t", app_name="mousepad",
    )

    assert not any(s.name.startswith("select_tab") for s in task.skills)
    assert len(task.skills) == 4          # menu, dialog, two settings


def test_the_goal_is_the_conjunction_of_every_setting() -> None:
    task = settings_task(
        SETTINGS, {"Show line numbers": True, "Display whitespace": False,
                   "Insert Tabs": True},
        task_id="t", app_name="mousepad",
    )

    kinds = [p.kind for p in task.goal.parts]

    assert kinds == ["checked", "unchecked", "checked"]
    assert task.instruction == (
        'In mousepad, open Preferences... from the "Edit" menu and '
        'turn on "Show line numbers", turn off "Display whitespace", '
        'and turn on "Insert Tabs".'
    )


def test_a_box_already_in_the_wanted_state_costs_no_step() -> None:
    """Where minimality comes from: the plan lists the setting, the executor
    finds it already correct, and no action is emitted."""
    from deskshot.generation.tasks import run_task

    class DialogEnv:
        def __init__(self):
            self.boxes = {"Show line numbers": True, "Display whitespace": False}
            self.n = 0

        def _state(self):
            return [
                {"role": "check box", "name": k, "uid": f"c:{k}", "app_name": "mousepad",
                 "interaction": {"actionable": True, "click_point": [1, 1], "checked": v}}
                for k, v in self.boxes.items()
            ]

        @property
        def last_elements(self):
            return self._state()

        def step(self, action):
            label = (action.target_uid or "").split(":", 1)[-1]
            if label in self.boxes:
                self.boxes[label] = not self.boxes[label]
            self.n += 1
            return {"action": action.to_dict(), "observation": {"stem": f"s{self.n}"},
                    "diff": {"changed": True}}

    task = settings_task(
        SETTINGS[:2], {"Show line numbers": True, "Display whitespace": True},
        task_id="t", app_name="mousepad",
    )
    # Drop the navigation skills: this test is about the settings themselves.
    task.skills = [s for s in task.skills if s.name.startswith("set(")]

    result = run_task(task, DialogEnv())

    assert result.status == "success"
    assert len(result.steps) == 1                      # not 2
    assert result.steps[0].skill == "set(Display whitespace=on)"


def test_planning_only_asks_for_values_that_differ_from_the_current_one() -> None:
    """Both View settings start False, so both are asked for as True. The Editor
    setting is on another tab and is left out - see the one-tab test below."""
    task = plan_settings_task(SETTINGS, app_name="mousepad", seed=1, num_settings=3)

    assert task.metadata["targets"] == {
        "Show line numbers": True, "Display whitespace": True,
    }


def test_no_settings_means_no_task() -> None:
    assert plan_settings_task([], app_name="mousepad", seed=1) is None
    assert settings_task(SETTINGS, {}, task_id="t", app_name="mousepad") is None


def test_a_settings_task_stays_on_one_tab_so_its_goal_is_checkable() -> None:
    """Measured: switching tabs removes the previous tab's boxes from the tree,
    so a conjunction spanning two tabs is true at no single moment."""
    task = plan_settings_task(SETTINGS, app_name="mousepad", seed=1, num_settings=3)

    tabs = {s["tab"] for s in SETTINGS if s["label"] in task.metadata["targets"]}

    assert tabs == {"View"}
    assert not any(s.name.startswith("select_tab") for s in task.skills)


# --- cross-app composition ---------------------------------------------------


CROSS = {
    "mousepad": [
        {"menu": "Edit", "dialog": "Preferences...", "app_name": "mousepad",
         "tab": "View", "label": "Show line numbers", "initial": False},
        {"menu": "Edit", "dialog": "Preferences...", "app_name": "mousepad",
         "tab": "View", "label": "Display whitespace", "initial": False},
    ],
    "xarchiver": [
        {"menu": "Edit", "dialog": "Preferences...", "app_name": "xarchiver",
         "tab": "Archive", "label": "Store full paths", "initial": True},
    ],
}


def test_each_app_segment_is_prefixed_with_a_focus_step() -> None:
    """A cross-app plan leaves the last app on top, so a menu click aimed at a
    background window either misses or hits the wrong app."""
    task = plan_cross_app_task(CROSS, seed=1, settings_per_app=2)

    names = [s.name for s in task.skills]

    assert names[0] == "focus(mousepad)"
    assert "focus(xarchiver)" in names
    assert names.index("focus(xarchiver)") > names.index("open_menu(Edit)")


def test_focus_succeeds_on_the_window_coming_forward_not_on_the_click() -> None:
    """A click that failed to raise the window is exactly what this must catch."""
    skill = focus_app("mousepad")

    assert skill.goal.parts[0].kind == "visible"
    assert skill.goal.parts[0].match.role_any == ("frame", "window")
    assert skill.goal.parts[0].match.app_name == "mousepad"


def test_the_goal_spans_every_app_not_just_the_last() -> None:
    """So the final check catches an app that reverted while another was in
    front - the thing a cross-app task is uniquely able to detect."""
    task = plan_cross_app_task(CROSS, seed=1, settings_per_app=2)

    apps = {p.match.app_name for p in task.goal.parts}

    assert apps == {"mousepad", "xarchiver"}
    assert task.metadata["apps"] == ["mousepad", "xarchiver"]


def test_the_instruction_names_the_apps_in_order() -> None:
    task = plan_cross_app_task(CROSS, seed=1, settings_per_app=1)

    assert task.instruction.startswith("Across the desktop: mousepad, open Preferences...")
    assert "; then xarchiver, open Preferences..." in task.instruction


def test_one_app_is_not_a_cross_app_task() -> None:
    assert plan_cross_app_task({"mousepad": CROSS["mousepad"]}, seed=1) is None
    assert plan_cross_app_task({}, seed=1) is None
    assert plan_cross_app_task({"a": [], "b": []}, seed=1) is None


class TwoAppEnv(ProbeEnv):
    """Only the focused app's menus list their items, as X does."""

    def __init__(self, menu_map):
        super().__init__(menu_map)
        self.focused = "other"

    def peek(self):
        state = [
            {"role": "frame", "name": "Main", "app_name": "mousepad", "uid": "f:mousepad"},
        ]
        if self.focused == "mousepad":
            state += super().peek()
        else:
            state += [
                {"role": "menu", "name": m, "app_name": "mousepad", "uid": f"m:{m}"}
                for m in self.menu_map
            ]
        return state

    def act(self, action, state=None):
        label = (action.target_uid or "").split(":", 1)[-1]
        if action.type == "click" and (action.target_uid or "").startswith("f:"):
            self.focused = label
            return
        if self.focused != "mousepad":
            return                      # a click on a background window does nothing
        super().act(action, state)


def test_probing_focuses_the_app_before_reading_its_menus() -> None:
    """Measured: probing a scene's second app without focusing reported every
    menu as empty, and so zero settable widgets."""
    env = TwoAppEnv(MENU_MAP)

    found = probe_menus(env, app_name="mousepad")

    assert env.focused == "mousepad"
    assert found == MENU_MAP


def test_another_apps_open_dialog_does_not_satisfy_this_apps_goal() -> None:
    """Measured in a cross-app run: mousepad's Preferences was still up, so
    thunar's "a dialog exists" was already true and the skill was skipped."""
    task = settings_task(
        CROSS["xarchiver"], {"Store full paths": False},
        task_id="t", app_name="xarchiver",
    )
    open_dialog = next(s for s in task.skills if s.name.startswith("open_dialog"))

    other_apps_dialog = [
        {"role": "dialog", "name": "Preferences", "app_name": "mousepad", "uid": "d1"}
    ]

    assert not open_dialog.goal.holds(other_apps_dialog)
    assert open_dialog.goal.holds(
        [{"role": "dialog", "name": "Preferences", "app_name": "xarchiver", "uid": "d2"}]
    )


def test_focus_is_not_satisfied_by_a_background_windows_menu_bar() -> None:
    """A background window still advertises its menus, so a menu-based goal
    holds without focusing and the next skill drives the wrong window. No frame
    reports `active` on this stack, so occlusion is the observable evidence."""
    skill = focus_app("thunar")
    background = [
        {"role": "menu", "name": "Edit", "app_name": "thunar", "uid": "m"},
        {"role": "frame", "name": "Thunar", "app_name": "thunar", "uid": "f",
         "occlusion_state": "partial"},
    ]
    raised = [dict(background[0]),
              {"role": "frame", "name": "Thunar", "app_name": "thunar", "uid": "f",
               "occlusion_state": "none"}]

    assert not skill.goal.holds(background)
    assert skill.goal.holds(raised)


# --- affordances: the general form -------------------------------------------


def _widget(role, label, app="mousepad", **interaction):
    return {"role": role, "name": label, "app_name": app, "uid": f"{role}:{label}",
            "interaction": {"actionable": True, "click_point": [1, 1], **interaction}}


MIXED = [
    _widget("check box", "Word wrap", checked=False),
    _widget("page tab", "Editor", selected=False),
    _widget("combo box", "Monospace 10"),
    _widget("entry", "search terms", editable=True),
    _widget("list item", "report.pdf", selected=False),
    _widget("label", "not a widget"),
]


def test_every_state_bearing_widget_kind_is_discovered() -> None:
    """Not just check boxes: an app is full of things whose state is readable."""
    found = discover_affordances(MIXED, app_name="mousepad")

    assert [(a.kind, a.label) for a in found] == [
        ("toggle", "Word wrap"),
        ("tab", "Editor"),
        ("choice", "Monospace 10"),
        ("text", "search terms"),
        ("item", "report.pdf"),
    ]


def test_a_widget_from_another_app_is_not_discovered() -> None:
    other = [_widget("check box", "Word wrap", app="thunar", checked=False)]

    assert discover_affordances(other, app_name="mousepad") == []


def test_each_kind_gets_a_goal_and_a_skill_with_no_per_app_code() -> None:
    """The extension point: a kind is a table row, not a code path."""
    found = discover_affordances(MIXED, app_name="mousepad")

    for aff in found:
        target = default_target(aff)
        assert affordance_goal(aff, target) is not None
        assert affordance_skill(aff, target) is not None


def test_the_default_target_is_a_state_the_widget_is_not_already_in() -> None:
    """A goal that already holds asks for work that was never needed."""
    on = Affordance("toggle", "Wrap", "check box", "mousepad", current=True)
    off = Affordance("toggle", "Wrap", "check box", "mousepad", current=False)

    assert default_target(on) is False
    assert default_target(off) is True


def test_goals_read_the_right_state_for_each_kind() -> None:
    toggle = Affordance("toggle", "Wrap", "check box", "mousepad", current=False)
    tab = Affordance("tab", "Editor", "page tab", "mousepad", current=False)
    choice = Affordance("choice", "Mono 10", "combo box", "mousepad", current="Mono 10")

    assert affordance_goal(toggle, True).kind == "checked"
    assert affordance_goal(toggle, False).kind == "unchecked"
    assert affordance_goal(tab, True).kind == "selected"
    assert affordance_goal(choice, "Mono 12").kind == "text_equals"
    assert affordance_goal(choice, "Mono 12").value == "Mono 12"


def test_a_task_can_mix_kinds_in_one_conjunction() -> None:
    """A realistic instruction is not five check boxes - it is a tab, a value,
    and some text, checked together."""
    found = discover_affordances(MIXED, app_name="mousepad", kinds=("toggle", "tab", "text"))

    task = affordance_task(found, task_id="t", app_name="mousepad")

    assert task.metadata["kinds"] == ["tab", "text", "toggle"]
    assert [p.kind for p in task.goal.parts] == ["checked", "selected", "text_equals"]
    assert task.instruction.startswith("In mousepad: turn on \"Word wrap\"")


def test_navigation_is_supplied_by_the_caller_not_baked_in() -> None:
    """So the same builder serves a dialog, a tab, or a file list."""
    found = discover_affordances(MIXED, app_name="mousepad", kinds=("toggle",))
    prefix = [OpenMenu.build("Edit", app_name="mousepad", expect_item="Preferences...")]

    task = affordance_task(found, task_id="t", app_name="mousepad", prefix=prefix)

    assert task.skills[0].name == "open_menu(Edit)"
    assert task.skills[1].name == "set(Word wrap=on)"


def test_nothing_stateful_means_no_task() -> None:
    assert affordance_task([], task_id="t", app_name="mousepad") is None
    assert discover_affordances([_widget("label", "hi")], app_name="mousepad") == []


# --- header-bar apps ---------------------------------------------------------


class HeaderBarEnv:
    """An app with no menu bar: its menus sit behind a button."""

    ITEMS = ["Preferences...", "Keyboard Shortcuts", "About"]

    def __init__(self, expandable=True, label="Main Menu"):
        self.open = False
        self.label = label
        self.expandable = expandable
        self.actions = []

    def peek(self):
        state = [{
            "role": "push button", "name": self.label, "app_name": "eog",
            "uid": "b:menu",
            "interaction": {"actionable": True, "click_point": [1, 1],
                            "expandable": self.expandable},
        }]
        if self.open:
            state += [
                {"role": "menu item", "name": i, "app_name": "eog", "uid": f"i:{i}"}
                for i in self.ITEMS
            ]
        return state

    def act(self, action, state=None):
        self.actions.append(action)
        if action.type == "key" and action.value == "Escape":
            self.open = False
        elif (action.target_uid or "") == "b:menu":
            self.open = not self.open


def test_a_hamburger_button_is_recognised_by_its_label() -> None:
    env = HeaderBarEnv(expandable=False, label="Main Menu")

    buttons = discover_header_menu_buttons(env.peek(), app_name="eog")

    assert [b["name"] for b in buttons] == ["Main Menu"]


def test_a_menu_button_is_recognised_by_being_expandable() -> None:
    """GTK marks a menu button expandable, which separates it from an ordinary
    toolbar button even when its label says nothing."""
    env = HeaderBarEnv(expandable=True, label="")

    assert len(discover_header_menu_buttons(env.peek(), app_name="eog")) == 1


def test_an_ordinary_button_is_not_mistaken_for_a_menu() -> None:
    plain = [{"role": "push button", "name": "Save", "app_name": "eog", "uid": "b",
              "interaction": {"actionable": True}}]

    assert discover_header_menu_buttons(plain, app_name="eog") == []


def test_probing_falls_back_to_the_header_menu_when_there_is_no_menu_bar() -> None:
    """12 of 20 apps in the pool have no role `menu` at all - without this,
    every menu-based mechanism is blind to them."""
    env = HeaderBarEnv()

    found = probe_menus(env, app_name="eog")

    assert found == {"Main Menu": HeaderBarEnv.ITEMS}
    assert env.open is False           # left as it was found


def test_a_header_menu_yields_dialog_units_like_any_other() -> None:
    """The point of reading them at all: everything downstream is unchanged."""
    env = HeaderBarEnv()

    units = build_units(probe_menus(env, app_name="eog"), app_name="eog")

    assert [(u["menu"], u["item"]) for u in units] == [("Main Menu", "Preferences...")]


def test_inert_widgets_are_not_offered_as_goals() -> None:
    """Thunar lists "Places" and "Devices" as tree items beside the real
    entries, but they are section headings the app will not let you select."""
    state = [
        {"role": "tree item", "name": "Places", "app_name": "thunar", "uid": "h",
         "interaction": {"actionable": False, "click_point": None, "selected": False}},
        {"role": "tree item", "name": "Desktop", "app_name": "thunar", "uid": "d",
         "interaction": {"actionable": True, "click_point": [5, 5], "selected": False}},
    ]

    found = discover_affordances(state, app_name="thunar")

    assert [a.label for a in found] == ["Desktop"]


def test_a_collapsible_group_heading_is_not_a_selectable_row() -> None:
    """Thunar reports sidebar headings as actionable tree items, but clicking
    one collapses the group instead of selecting it."""
    state = [
        {"role": "tree item", "name": "Places", "app_name": "thunar", "uid": "g",
         "interaction": {"actionable": True, "click_point": [5, 5],
                         "expandable": True, "selected": False}},
        {"role": "tree item", "name": "Desktop", "app_name": "thunar", "uid": "d",
         "interaction": {"actionable": True, "click_point": [5, 9],
                         "expandable": False, "selected": False}},
    ]

    assert [a.label for a in discover_affordances(state, app_name="thunar")] == ["Desktop"]


def test_a_clipped_label_is_not_used_to_identify_a_widget() -> None:
    """It is a prefix of the real text, and it changes as the screen does -
    measured, selecting one HomeBank row made the next goal unresolvable."""
    state = [
        {"role": "table cell", "name": "$ 500,", "visible_text": "$ 500,",
         "visible_text_status": "clipped", "app_name": "homebank", "uid": "c",
         "interaction": {"actionable": True, "click_point": [5, 5], "selected": False}},
        {"role": "table cell", "name": "Paypal Account", "app_name": "homebank",
         "visible_text_status": "full_visible", "uid": "p",
         "interaction": {"actionable": True, "click_point": [5, 9], "selected": False}},
    ]

    found = discover_affordances(state, app_name="homebank")

    assert [a.label for a in found] == ["Paypal Account"]


def test_a_task_asks_for_at_most_one_selection() -> None:
    """Selecting the second row deselects the first, so a conjunction of two
    selections can never hold - measured on HomeBank, where both steps verified
    individually while the goal stayed at one fifth."""
    rows = [
        Affordance("item", f"row {i}", "table row", "homebank", current=False)
        for i in range(3)
    ]
    toggles = [
        Affordance("toggle", f"opt {i}", "check box", "homebank", current=False)
        for i in range(2)
    ]

    task = affordance_task(rows + toggles, task_id="t", app_name="homebank")

    kinds = [a["kind"] for a in task.metadata["affordances"]]
    assert kinds.count("item") == 1
    assert kinds.count("toggle") == 2
