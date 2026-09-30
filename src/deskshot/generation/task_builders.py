"""Build tasks from what an app actually offers, by asking it.

Hand-written task lists do not scale and do not survive: they cover the apps
someone had time for, and they rot the moment a menu changes. Here the app is
probed - open each menu, read what is in it, close it again - and the tasks are
generated from that. Adding an app to the pool adds its tasks with it, and the
tasks cannot reference an item the app does not have.

Probing reads the accessibility tree directly rather than taking observations,
because probe frames are thrown away and an observation costs a screenshot plus
the whole annotation pipeline.

The unit this produces is a **dialog round trip**: open a menu, choose an item
that opens a dialog, dismiss it. Three actions, each with its own postcondition,
and the app ends where it started - which is what makes the units composable
into a long task without the state drifting somewhere the next unit cannot run.
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from deskshot.generation.predicates import (
    Conjunction,
    Element,
    ElementMatch,
    Predicate,
    element_text,
)
from deskshot.generation.skills import (
    ClickTarget,
    MENU_ITEM_ROLES,
    OpenMenu,
    PressKey,
    SelectItem,
    SelectTab,
    SetCheckbox,
    SetComboValue,
    TypeInto,
    Skill,
    _pick,
    discover_header_menu_buttons,
    discover_menu_item_roles,
    discover_menus,
    open_header_menu,
)
from deskshot.generation.tasks import Task

logger = logging.getLogger(__name__)

#: Roles that mean "a dialog is on screen".
DIALOG_ROLES = ("dialog", "alert", "file chooser", "color chooser", "font chooser")

#: Menu items that must never be activated during collection. Two kinds: things
#: that end the session, and things that destroy the state later units depend
#: on. Printing is here because it blocks on printer discovery, and help
#: contents because it opens a browser outside the scene.
UNSAFE_ITEM = re.compile(
    r"\b(quit|exit|log ?out|shut ?down|restart|close|delete|remove|erase|clear"
    r"|empty|purge|trash|discard|revert|reset|uninstall|format|print|help"
    r"|contents|report a (bug|problem)|donate|translate"
    # Measured on mousepad: probing these detached the tab into a new window and
    # the app disappeared from the scene mid-episode.
    r"|detach|new window|new tab"
    # And these remove the app's own affordances: hiding the menu bar leaves
    # nothing to open the menu bar *with*, so the toggle cannot be undone by the
    # same route that set it.
    r"|menu ?bar|full ?screen)\b",
    re.IGNORECASE,
)

#: Roles AT-SPI gives a menu item that toggles state. A toggle is safe to probe:
#: it is reversible by construction, and its effect is a property of the app
#: rather than an edit to the user's document.
TOGGLE_ITEM_ROLES = ("check menu item", "radio menu item")

#: Labels that name a view toggle, used when the role does not distinguish one.
#: Measured on this stack - GTK3 3.24.31 with AT-SPI2 2.40.3 - mousepad reports
#: all 46 of its menu items as plain "menu item" and exposes no checkable or
#: checked state on any of them, so there is nothing in the tree that separates
#: "Word Wrap" from "Detach Tab". The label is the only signal left. This is a
#: filter on what to *try*, never a guarantee: the window invariant below is
#: what actually protects the scene.
TOGGLE_LIKE = re.compile(
    r"\b(word ?wrap|wrap|auto ?indent|line numbers?|status ?bar|tool ?bar"
    r"|side ?bar|viewer mode|highlight|bom|ruler|grid"
    r"|whitespace|read[- ]?only|preview|show|hide|display)\b",
    re.IGNORECASE,
)

#: Items that reliably open a dialog. GTK marks them with a trailing ellipsis,
#: which is a convention this pipeline can rely on: it is what the toolkit's own
#: guidelines require, and it is far more reliable than guessing from the label.
OPENS_DIALOG = re.compile(r"(\.\.\.|…)\s*$")


def is_safe_item(label: str) -> bool:
    return bool(label.strip()) and not UNSAFE_ITEM.search(label)


def opens_dialog(label: str) -> bool:
    return bool(OPENS_DIALOG.search(label))


def probe_menus(
    env: Any,
    *,
    app_name: str,
    max_menus: int = 8,
    roles_out: Optional[Dict[str, str]] = None,
) -> Dict[str, List[str]]:
    """Open each menu in turn and record what it contains.

    Returns `{menu_label: [item_label, ...]}`. A menu that will not open is
    reported empty rather than raising: one uncooperative menu should not cost
    the whole app its tasks.
    """
    from deskshot.generation.actions import SceneAction

    found: Dict[str, List[str]] = {}
    state = env.peek()

    # Raise the app before reading it. A background window still advertises its
    # menu bar, but clicking one does not open it, so probing a scene's second
    # app without focusing reports every menu as empty - measured on xarchiver,
    # which came back {'Archive': 0, 'Action': 0, 'Help': 0} while mousepad was
    # in front, and 0 settable widgets as a result.
    frame = _pick(state, ElementMatch(role_any=("frame", "window"), app_name=app_name))
    if frame is not None:
        try:
            env.act(SceneAction(type="click", target_uid=frame.get("uid")), state)
            state = env.peek()
        except Exception:
            logger.warning("probe: could not focus %s", app_name, exc_info=True)

    menus = discover_menus(state, app_name=app_name)[:max_menus]
    if not menus:
        # No menu bar. Measured across the pool, that is 12 of 20 apps - they use
        # a header bar, and their menus sit behind a button. Opening it puts the
        # same menu items in the tree, so everything downstream works unchanged.
        found.update(_probe_header_menus(env, app_name=app_name, roles_out=roles_out))
        if not found:
            logger.info("probe: %s exposes no menu bar and no header menu", app_name)
        return found

    for menu in menus:
        skill = OpenMenu.build(menu, app_name=app_name)
        action = skill.next_action(state)
        if action is None:
            found[menu] = []
            continue
        try:
            env.act(action, state)
        except Exception:
            logger.warning("probe: could not open %s/%s", app_name, menu, exc_info=True)
            found[menu] = []
            state = env.peek()
            continue
        state = env.peek()
        roles = discover_menu_item_roles(state, app_name=app_name)
        found[menu] = list(roles)
        if roles_out is not None:
            roles_out.update(roles)
        # Leave the app as it was found, so the next menu is opened from a
        # closed state rather than from whatever the last one left behind.
        env.act(SceneAction(type="key", value="Escape"), state)
        state = env.peek()

    logger.info(
        "probe: %s -> %s",
        app_name,
        {m: len(items) for m, items in found.items()},
    )
    return found


def _probe_header_menus(
    env: Any,
    *,
    app_name: str,
    roles_out: Optional[Dict[str, str]] = None,
) -> Dict[str, List[str]]:
    """Read the menus behind a header-bar app's menu button."""
    from deskshot.generation.actions import SceneAction

    found: Dict[str, List[str]] = {}
    state = env.peek()
    buttons = discover_header_menu_buttons(state, app_name=app_name)
    for button in buttons[:3]:
        label = element_text(button) or "Menu"
        skill = open_header_menu(button)
        action = skill.next_action(state)
        if action is None:
            continue
        try:
            env.act(action, state)
            state = env.peek()
            roles = discover_menu_item_roles(state, app_name=app_name)
            if roles:
                found[label] = list(roles)
                if roles_out is not None:
                    roles_out.update(roles)
            env.act(SceneAction(type="key", value="Escape"), state)
            state = env.peek()
        except Exception:
            logger.warning("probe: header menu %s/%s failed", app_name, label, exc_info=True)
    if found:
        logger.info(
            "probe: %s header menus -> %s",
            app_name, {m: len(i) for m, i in found.items()},
        )
    return found


def _structural_key(elem: Element) -> tuple:
    """Identify an element by what it is, not where it sits in the tree.

    Uids come from the accessibility path, which shifts whenever anything is
    inserted above an element, so a uid diff reports churn that is invisible on
    screen.
    """
    from deskshot.generation.predicates import element_text

    return (str(elem.get("role") or "").strip().lower(), element_text(elem))


def _affordances(state: Sequence[Element], app_name: str) -> tuple:
    """What the app offers to act *through*: its windows and its menu bar.

    Probing may change what an app shows; it may not change what an app can be
    driven by. Measured on mousepad: the probe toggled "Menubar" off, and from
    then on no menu could be opened - including the one that would have turned
    it back on - so the toggle could not be undone by the route that set it and
    every later step failed with a message blaming the menu skill.

    Windows alone do not catch that: hiding the menu bar leaves the window
    count untouched.
    """
    from deskshot.generation.predicates import element_text

    windows = 0
    menus = set()
    for elem in state:
        if elem.get("app_name") != app_name:
            continue
        role = str(elem.get("role") or "").strip().lower()
        if role in {"frame", "window"}:
            windows += 1
        elif role == "menu":
            menus.add(element_text(elem))
    return (windows, frozenset(menus))


def _menu_noise(key: tuple) -> bool:
    role = key[0]
    return role in {r.lower() for r in MENU_ITEM_ROLES} or role in {"menu", "menu bar"}


def discover_effects(
    env: Any,
    menu_map: Dict[str, List[str]],
    *,
    app_name: str,
    max_probes: int = 10,
) -> List[Dict[str, Any]]:
    """Find menu items whose effect is visible, by trying them and watching.

    A composed task needs an end state that was not already true when it
    started, otherwise "did the task succeed" is a question with no content -
    which is exactly the weakness of chaining dialog round trips, since each one
    deliberately undoes itself and "no dialog is open" holds at step zero.

    Rather than hard-coding which items do something observable, each candidate
    is activated and the tree is compared before and after. An item is kept only
    if it changed the structure **and** activating it again put the structure
    back: an effect that cannot be undone would strand every later unit in a
    state its preconditions do not describe, and one that cannot be reproduced
    is not an effect, it is a coincidence.

    Menu churn is excluded from the comparison, since opening a menu populates
    the tree with its items and that says nothing about what the item did.
    """
    from deskshot.generation.actions import SceneAction

    effects: List[Dict[str, Any]] = []
    baseline = _affordances(env.peek(), app_name)
    for menu, item in _toggle_candidates(env, menu_map, app_name=app_name):
        if len(effects) >= max_probes:
            break
        before = {_structural_key(e) for e in env.peek() if not _menu_noise(_structural_key(e))}
        if not _activate(env, menu, item, app_name=app_name):
            continue
        # The invariant that matters: a probe may open dialogs and flip view
        # state, but it may never change what the app can be driven by. One
        # attempt to undo it, and if the app is still altered, discovery stops -
        # continuing would test an app that is no longer the one being described.
        if _affordances(env.peek(), app_name) != baseline:
            logger.warning(
                "effect probe: %s/%s changed the app's affordances; stopping discovery",
                app_name, item,
            )
            _activate(env, menu, item, app_name=app_name)
            break
        mid_state = env.peek()
        mid = {_structural_key(e) for e in mid_state if not _menu_noise(_structural_key(e))}

        appeared = sorted(mid - before)
        disappeared = sorted(before - mid)
        if not appeared and not disappeared:
            continue

        # Put it back, and require that it actually went back.
        reverted = _activate(env, menu, item, app_name=app_name)
        after = {_structural_key(e) for e in env.peek() if not _menu_noise(_structural_key(e))}
        if not reverted or after != before:
            logger.info(
                "effect probe: %s/%s changed the tree but did not revert; discarding",
                app_name, item,
            )
            env.act(SceneAction(type="key", value="Escape"), env.peek())
            continue

        role, label = (appeared or disappeared)[0]
        match = ElementMatch(role=role, text=label or None, app_name=app_name)
        effects.append(
            {
                "menu": menu,
                "item": item,
                "app_name": app_name,
                "kind": "effect",
                "predicate": Predicate(
                    "exists" if appeared else "absent", match
                ).to_dict(),
                "appeared": [list(k) for k in appeared[:5]],
                "disappeared": [list(k) for k in disappeared[:5]],
            }
        )
        logger.info(
            "effect probe: %s/%s -> +%d -%d", app_name, item, len(appeared), len(disappeared)
        )
    return effects


def _toggle_candidates(
    env: Any,
    menu_map: Dict[str, List[str]],
    *,
    app_name: str,
) -> List[tuple]:
    """(menu, item) pairs whose item is a toggle, read from the live tree.

    Restricting probes to toggles is what keeps the probe from damaging the
    scene. The first version tried every non-dialog item, which in mousepad
    meant running "Detach Tab" and "New Window": the app left the scene, and
    every step after that failed with an error that pointed at the menu skill
    rather than at the probe that had removed the window.

    A toggle is safe by construction - it is reversible, and its effect is a
    property of the app rather than an edit to the document.
    """
    from deskshot.generation.actions import SceneAction
    from deskshot.generation.predicates import element_text

    toggle_roles = {r.lower() for r in TOGGLE_ITEM_ROLES}
    candidates: List[tuple] = []
    for menu, items in sorted(menu_map.items()):
        if not any(is_safe_item(i) and not opens_dialog(i) for i in items):
            continue
        state = env.peek()
        action = OpenMenu.build(menu, app_name=app_name).next_action(state)
        if action is None:
            continue
        try:
            env.act(action, state)
            state = env.peek()
            roles = discover_menu_item_roles(state, app_name=app_name)
            # Prefer the role when the toolkit gives one; fall back to the label
            # only for the menus where it does not.
            typed = [lbl for lbl, role in roles.items() if role in toggle_roles]
            named = [lbl for lbl in roles if TOGGLE_LIKE.search(lbl)]
            for label in typed or named:
                if is_safe_item(label) and not opens_dialog(label):
                    candidates.append((menu, label))
            env.act(SceneAction(type="key", value="Escape"), state)
        except Exception:
            logger.warning("effect probe: listing %s/%s failed", app_name, menu, exc_info=True)
    return candidates


def _activate(env: Any, menu: str, item: str, *, app_name: str) -> bool:
    """Open a menu and click one item, using peeks rather than observations."""
    from deskshot.generation.actions import SceneAction

    state = env.peek()
    open_skill = OpenMenu.build(menu, app_name=app_name, expect_item=item)
    action = open_skill.next_action(state)
    if action is None:
        return False
    try:
        env.act(action, state)
        state = env.peek()
        if not open_skill.goal.holds(state):
            env.act(SceneAction(type="key", value="Escape"), state)
            return False
        target = ClickTarget(
            name="probe",
            goal=Conjunction(()),
            match=ElementMatch(role_any=MENU_ITEM_ROLES, text=item, app_name=app_name),
        ).next_action(state)
        if target is None:
            env.act(SceneAction(type="key", value="Escape"), state)
            return False
        env.act(target, state)
    except Exception:
        logger.warning("effect probe: activating %s/%s failed", menu, item, exc_info=True)
        return False
    return True


def effect_unit(effect: Dict[str, Any]) -> List[Skill]:
    """A menu activation whose postcondition is the effect that was measured."""
    from deskshot.generation.skills import activate_menu_item

    return activate_menu_item(
        effect["menu"],
        effect["item"],
        app_name=effect["app_name"],
        result=Conjunction((Predicate.from_dict(effect["predicate"]),)),
    )


def dialog_round_trip(
    menu: str,
    item: str,
    *,
    app_name: Optional[str] = None,
) -> List[Skill]:
    """Open a menu, choose a dialog item, dismiss the dialog.

    The dismissal is not tidying-up: it is what lets units compose. Without it
    the second unit starts behind a modal dialog, its menu click goes nowhere,
    and a long task fails at step four every time.
    """
    dialog = ElementMatch(role_any=DIALOG_ROLES, app_name=app_name)
    return [
        OpenMenu.build(menu, app_name=app_name, expect_item=item),
        ClickTarget(
            name=f"open_dialog({menu}/{item})",
            goal=Conjunction((Predicate("exists", dialog),)),
            match=ElementMatch(role_any=MENU_ITEM_ROLES, text=item, app_name=app_name),
            intent=f'choose "{item}" from the "{menu}" menu to open its dialog',
            max_actions=2,
        ),
        PressKey.build(
            "Escape",
            Conjunction((Predicate("absent", dialog),)),
            intent="dismiss the dialog",
        ),
    ]


def menu_toggle_unit(
    menu: str,
    item: str,
    *,
    app_name: Optional[str] = None,
) -> List[Skill]:
    """Open a menu and activate a non-dialog item.

    Two actions. The only claim made is that the menu closed again, because what
    the item did is app-specific and unknown to a generic builder - so that is
    all the postcondition asserts.
    """
    from deskshot.generation.skills import activate_menu_item

    return activate_menu_item(menu, item, app_name=app_name)


def build_units(
    menu_map: Dict[str, List[str]],
    *,
    app_name: str,
    dialogs_only: bool = True,
) -> List[Dict[str, Any]]:
    """Every safe (menu, item) pair, as a reusable unit description."""
    units: List[Dict[str, Any]] = []
    for menu, items in sorted(menu_map.items()):
        for item in items:
            if not is_safe_item(item):
                continue
            is_dialog = opens_dialog(item)
            if dialogs_only and not is_dialog:
                continue
            units.append(
                {
                    "menu": menu,
                    "item": item,
                    "app_name": app_name,
                    "kind": "dialog" if is_dialog else "activate",
                }
            )
    return units


def _skills_for(unit: Dict[str, Any]) -> List[Skill]:
    if unit["kind"] == "dialog":
        return dialog_round_trip(unit["menu"], unit["item"], app_name=unit["app_name"])
    if unit["kind"] == "effect":
        return effect_unit(unit)
    return menu_toggle_unit(unit["menu"], unit["item"], app_name=unit["app_name"])


def compose_task(
    units: Sequence[Dict[str, Any]],
    *,
    task_id: str,
    app_name: str,
) -> Task:
    """Chain units into one task whose instruction lists them in order.

    The goal is the final unit's end state, which is the only thing still true
    at the end - each round trip deliberately undoes itself. The per-step
    subgoals carry the rest, and those are verified as the episode runs, so the
    trajectory is checked throughout rather than only at the last frame.
    """
    skills: List[Skill] = []
    for unit in units:
        skills.extend(_skills_for(unit))

    sentences = [
        f'open "{u["item"]}" from the "{u["menu"]}" menu and close it again'
        if u["kind"] == "dialog"
        else f'turn on "{u["item"]}" from the "{u["menu"]}" menu'
        if u["kind"] == "effect"
        else f'choose "{u["item"]}" from the "{u["menu"]}" menu'
        for u in units
    ]
    instruction = f"In {app_name}: " + "; then ".join(sentences) + "."

    goal = skills[-1].goal if skills else Conjunction(())
    return Task(
        task_id=task_id,
        instruction=instruction,
        skills=skills,
        goal=goal,
        app_name=app_name,
        metadata={"units": list(units), "num_units": len(units)},
    )


def plan_long_task(
    menu_map: Dict[str, List[str]],
    *,
    app_name: str,
    target_steps: int,
    seed: int,
    allow_repeats: bool = True,
    effects: Optional[Sequence[Dict[str, Any]]] = None,
) -> Optional[Task]:
    """Compose units until the plan is at least `target_steps` long.

    Horizon is requested in steps rather than units because that is the property
    that matters and units differ in length. An app with four dialog items can
    still produce a hundred-step task by revisiting them in a different order;
    that is honest repetition, not filler, since every visit is a real
    open-inspect-dismiss cycle with its own checks.

    When measured effects are available the plan ends on one, so the task's goal
    is a state that was false when the episode began. Without that the goal of a
    chain of round trips is "no dialog is open", which is already true at step
    zero and therefore tests nothing at the end - the per-step subgoals still
    carry the verification, but the task has no end-state claim of its own.
    """
    units = build_units(menu_map, app_name=app_name)
    if not units:
        units = build_units(menu_map, app_name=app_name, dialogs_only=False)
    if not units:
        return None

    rng = random.Random(seed)
    terminal = rng.choice(list(effects)) if effects else None
    # Budgeted in skills rather than in each skill's worst case. A skill that
    # reaches its goal on the first action contributes one step, which is what
    # happens whenever the app cooperates, so counting worst cases asks for 60
    # steps and delivers 32.
    budget = target_steps
    if terminal is not None:
        budget -= len(_skills_for(terminal))

    chosen: List[Dict[str, Any]] = []
    steps = 0
    pool = list(units)
    rng.shuffle(pool)
    cursor = 0

    while steps < budget:
        if cursor >= len(pool):
            if not allow_repeats:
                break
            rng.shuffle(pool)
            cursor = 0
        unit = pool[cursor]
        cursor += 1
        chosen.append(unit)
        steps += len(_skills_for(unit))

    if terminal is not None:
        chosen.append(terminal)
    if not chosen:
        return None
    return compose_task(chosen, task_id=f"{app_name}-long-{seed}", app_name=app_name)


def plan_unit_tasks(
    menu_map: Dict[str, List[str]],
    *,
    app_name: str,
    limit: int = 0,
) -> List[Task]:
    """One short task per unit - the atoms, useful on their own as evaluation."""
    units = build_units(menu_map, app_name=app_name)
    if limit:
        units = units[:limit]
    return [
        compose_task([u], task_id=f"{app_name}-{u['menu']}-{u['item']}", app_name=app_name)
        for u in units
    ]


# --- settings inside dialogs -------------------------------------------------
#
# A setting is the best goal this pipeline can generate: its state is readable
# (`checked`), the route to it is unique, and reaching it is provably minimal.
# The menu path to a dialog is the only path, so `menu -> item -> [tab] -> click`
# is not a good plan, it is the shortest one.


def probe_dialog_settings(
    env: Any,
    *,
    menu: str,
    item: str,
    app_name: str,
    max_tabs: int = 6,
) -> List[Dict[str, Any]]:
    """Open a dialog, walk its tabs, and record every check box it offers.

    Returns one entry per setting with the tab it lives on and its current state.
    The dialog is closed again, so this composes with the rest of probing.
    """
    from deskshot.generation.actions import SceneAction
    from deskshot.generation.predicates import element_text

    settings: List[Dict[str, Any]] = []
    if not _activate(env, menu, item, app_name=app_name):
        return settings

    state = env.peek()
    if not Predicate(
        "exists", ElementMatch(role_any=DIALOG_ROLES, app_name=app_name)
    ).holds(state):
        logger.info("settings probe: %s/%s opened no dialog", app_name, item)
        env.act(SceneAction(type="key", value="Escape"), state)
        return settings

    def _boxes(cur: Sequence[Element]) -> List[Dict[str, Any]]:
        out = []
        for elem in cur:
            if elem.get("app_name") != app_name:
                continue
            if str(elem.get("role") or "").strip().lower() != "check box":
                continue
            label = element_text(elem)
            checked = (elem.get("interaction") or {}).get("checked")
            if label and checked is not None:
                out.append({"label": label, "checked": bool(checked)})
        return out

    tabs = [
        element_text(e)
        for e in state
        if e.get("app_name") == app_name
        and str(e.get("role") or "").strip().lower() == "page tab"
        and element_text(e)
    ][:max_tabs]

    def _record(tab: Optional[str], cur: Sequence[Element]) -> None:
        for box in _boxes(cur):
            if any(s["label"] == box["label"] for s in settings):
                continue
            settings.append({
                "menu": menu, "dialog": item, "app_name": app_name,
                "tab": tab, "label": box["label"], "initial": box["checked"],
            })

    _record(tabs[0] if tabs else None, state)
    for tab in tabs[1:]:
        target = _pick(state, ElementMatch(role="page tab", text=tab, app_name=app_name))
        if target is None:
            continue
        env.act(SceneAction(type="click", target_uid=target.get("uid")), state)
        state = env.peek()
        _record(tab, state)

    env.act(SceneAction(type="key", value="Escape"), env.peek())
    logger.info(
        "settings probe: %s/%s -> %d setting(s) across %d tab(s)",
        app_name, item, len(settings), max(1, len(tabs)),
    )
    return settings


def settings_task(
    settings: Sequence[Dict[str, Any]],
    targets: Dict[str, bool],
    *,
    task_id: str,
    app_name: str,
) -> Optional[Task]:
    """A conjunctive task over dialog settings, planned with the shared prefix.

    The prefix is what makes this minimal rather than merely correct. Ten
    settings planned independently would open the dialog ten times; they all sit
    behind the same `menu -> dialog`, so it is opened once and each tab visited
    once. Settings are grouped by tab for the same reason.

    The dialog is deliberately left open at the end. Every check box exists only
    while it is showing, so closing it first would put the goal beyond checking -
    and a task whose success cannot be evaluated at its final state is not one
    worth collecting.
    """
    wanted = [s for s in settings if s["label"] in targets]
    if not wanted:
        return None

    menu = wanted[0]["menu"]
    dialog = wanted[0]["dialog"]
    by_tab: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for s in wanted:
        by_tab.setdefault(s["tab"], []).append(s)

    skills: List[Skill] = [
        OpenMenu.build(menu, app_name=app_name, expect_item=dialog),
        ClickTarget(
            name=f"open_dialog({menu}/{dialog})",
            goal=Conjunction((
                # App-scoped, or another app's open dialog satisfies it. Measured
                # in a cross-app run: mousepad's Preferences was still up, so
                # thunar's "a dialog exists" was already true, the skill was
                # skipped, and the next step looked for a check box that had
                # never been opened.
                Predicate(
                    "exists", ElementMatch(role_any=DIALOG_ROLES, app_name=app_name)
                ),
            )),
            match=ElementMatch(role_any=MENU_ITEM_ROLES, text=dialog, app_name=app_name),
            intent=f'open "{dialog}"',
            max_actions=2,
        ),
    ]
    # The tab holding the first setting is already showing, so it needs no click.
    first_tab = wanted[0]["tab"]
    ordered = [first_tab] + [t for t in by_tab if t != first_tab]
    for tab in ordered:
        group = by_tab.get(tab) or []
        if not group:
            continue
        if tab is not None and tab != first_tab:
            skills.append(
                SelectTab.build(tab, group[0]["label"], app_name=app_name)
            )
        for s in group:
            skills.append(SetCheckbox.build(s["label"], targets[s["label"]], app_name=app_name))

    goal = Conjunction(tuple(
        Predicate(
            "checked" if targets[s["label"]] else "unchecked",
            ElementMatch(role="check box", text=s["label"], app_name=app_name),
        )
        for s in wanted
    ))
    phrases = [
        f'turn {"on" if targets[s["label"]] else "off"} "{s["label"]}"' for s in wanted
    ]
    instruction = (
        f'In {app_name}, open {dialog} from the "{menu}" menu and '
        + ", ".join(phrases[:-1] + [f"and {phrases[-1]}"] if len(phrases) > 1 else phrases)
        + "."
    )
    return Task(
        task_id=task_id,
        instruction=instruction,
        skills=skills,
        goal=goal,
        app_name=app_name,
        metadata={
            "kind": "settings",
            "dialog": dialog,
            "num_settings": len(wanted),
            "tabs": [t for t in ordered if by_tab.get(t)],
            "targets": {s["label"]: targets[s["label"]] for s in wanted},
        },
    )


def plan_settings_task(
    settings: Sequence[Dict[str, Any]],
    *,
    app_name: str,
    seed: int,
    num_settings: int = 6,
) -> Optional[Task]:
    """Pick settings to change and build the task that changes them.

    Only settings whose wanted value differs from the current one are asked for.
    Asking for a value a box already holds is not wrong - the skill would emit
    nothing - but it makes the instruction claim work that was never needed, and
    the point of the exercise is that every step is necessary.

    All settings come from **one tab**, because a goal has to be checkable at the
    state it ends in. Measured: switching from the View tab to the Window tab
    removes the View boxes from the accessibility tree entirely, so a conjunction
    spanning both is never true at any single moment - the run climbed to 0.50,
    switched tab, and fell back to 0.25 with every step still verified. The
    largest tab is used, which is also the one that yields the longest task.
    """
    if not settings:
        return None
    by_tab: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for s in settings:
        by_tab.setdefault(s["tab"], []).append(s)
    pool = max(by_tab.values(), key=len)

    rng = random.Random(seed)
    pool = list(pool)
    rng.shuffle(pool)
    chosen = pool[:num_settings]
    targets = {s["label"]: (not s["initial"]) for s in chosen}
    return settings_task(
        chosen, targets, task_id=f"{app_name}-settings-{seed}", app_name=app_name
    )


# --- cross-application tasks -------------------------------------------------
#
# A task confined to one app is not what desktop work looks like, and it caps
# both horizon and screen variety: mousepad has 6 dialogs and about 15 distinct
# screens, so no plan inside it can be long *and* varied. Spanning apps lifts
# both at once, and it is the realistic case - people configure an editor, then
# look something up, then file the result.
#
# The composition is still verified end to end. What changes is that reaching a
# subgoal now needs the right window in front, so focus becomes a skill with a
# postcondition like any other.


@dataclass
class _AppPlan:
    """One app's contribution to a cross-app task."""

    app_name: str
    skills: List[Skill]
    goal_parts: tuple
    phrase: str


def focus_app(app_name: str, *, expect_menu: Optional[str] = None) -> Skill:
    """Bring an app's window forward by clicking its frame.

    Needed because a cross-app plan leaves whichever app it used last on top,
    and a menu click aimed at a background window either misses or lands on the
    wrong app. Success is that the app's own menu bar is showing, not that a
    click happened - a click that failed to raise the window is exactly the case
    this has to catch.
    """
    # "A menu exists for this app" was the first attempt and it is useless: a
    # background window still advertises its menu bar, so the goal held, the
    # focus click never happened, and the next skill drove the wrong window.
    # No frame on this stack reports an `active` state either - measured, they
    # carry only {enabled, sensitive}. What *is* observable is occlusion: raising
    # a window uncovers it, so an unoccluded frame is the evidence that the click
    # landed and the window came forward.
    goal = Conjunction((
        Predicate("visible", ElementMatch(role_any=("frame", "window"), app_name=app_name)),
    ))
    return ClickTarget(
        name=f"focus({app_name})",
        goal=goal,
        match=ElementMatch(role_any=("frame", "window"), app_name=app_name),
        intent=f"bring {app_name} to the front",
        max_actions=2,
    )


def cross_app_task(
    plans: Sequence[_AppPlan],
    *,
    task_id: str,
) -> Optional[Task]:
    """Chain per-app plans into one task with one instruction and one goal.

    Each app's segment is prefixed with a focus skill, and the conjunction spans
    every app - so the final check asks whether *all* of the work survived, not
    just the part done last. That is the property that makes a cross-app task
    worth more than the sum of its single-app parts: it catches an app that
    silently reverted while another was in front.
    """
    plans = [p for p in plans if p.skills]
    if not plans:
        return None

    skills: List[Skill] = []
    goal_parts: List[Predicate] = []
    for plan in plans:
        skills.append(focus_app(plan.app_name))
        skills.extend(plan.skills)
        goal_parts.extend(plan.goal_parts)

    phrases = [p.phrase for p in plans]
    instruction = (
        "Across the desktop: "
        + "; then ".join(phrases)
        + "."
    )
    return Task(
        task_id=task_id,
        instruction=instruction,
        skills=skills,
        goal=Conjunction(tuple(goal_parts)),
        app_name=None,
        metadata={
            "kind": "cross_app",
            "apps": [p.app_name for p in plans],
            "num_apps": len(plans),
        },
    )


def plan_cross_app_task(
    per_app_settings: Dict[str, List[Dict[str, Any]]],
    *,
    seed: int,
    settings_per_app: int = 3,
) -> Optional[Task]:
    """Build a cross-app task from settings discovered in each app.

    Settings are used as the unit because they are the only goal this pipeline
    can state, reach minimally, and check at the end - see `settings_task`. One
    tab per app, for the same reason it is one tab per single-app task: a
    conjunction has to be true at a single moment, and switching tabs takes the
    previous tab's widgets out of the tree.
    """
    rng = random.Random(seed)
    plans: List[_AppPlan] = []

    for app_name in sorted(per_app_settings):
        settings = per_app_settings[app_name]
        if not settings:
            continue
        by_tab: Dict[Optional[str], List[Dict[str, Any]]] = {}
        for s in settings:
            by_tab.setdefault(s["tab"], []).append(s)
        pool = list(max(by_tab.values(), key=len))
        rng.shuffle(pool)
        chosen = pool[:settings_per_app]
        if not chosen:
            continue
        targets = {s["label"]: (not s["initial"]) for s in chosen}
        single = settings_task(
            chosen, targets, task_id=f"{app_name}-part", app_name=app_name
        )
        if single is None:
            continue
        phrase = single.instruction.rstrip(".")
        if phrase.startswith("In "):
            phrase = phrase[3:]
        plans.append(
            _AppPlan(
                app_name=app_name,
                skills=single.skills,
                goal_parts=single.goal.parts,
                phrase=phrase,
            )
        )

    if len(plans) < 2:
        return None
    return cross_app_task(plans, task_id=f"cross-app-{seed}")


# --- affordances: the general form, of which a setting is one case ------------
#
# A check box was the first thing given a goal because two properties happen to
# hold for it: its state is readable from the tree, and one click changes it. But
# neither property is about *settings* - they are about state-bearing widgets,
# and an app is full of them. A combo box's value is its text; a page tab knows
# whether it is selected; an entry holds the string it displays; a list row knows
# whether it is selected.
#
# So the pipeline does not need a task type per interaction. It needs, per widget
# kind, one answer to "what state can this be in" and one skill that puts it
# there. Everything else - discovery, planning, verification, the instruction -
# is already generic. Adding "set the font size" or "select this file" is a table
# entry, not a code path, and it works in every app at once.


@dataclass(frozen=True)
class Affordance:
    """A widget that can be asked to be in a particular state."""

    kind: str                 # toggle | tab | choice | text | item
    label: str
    role: str
    app_name: str
    current: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


#: Widget roles grouped by the kind of goal they support. The grouping is the
#: whole extension point: a new row here gives every app in the pool a new class
#: of task, with no per-app code anywhere.
AFFORDANCE_ROLES: Dict[str, tuple] = {
    "toggle": ("check box", "radio button", "check menu item", "radio menu item"),
    "tab": ("page tab",),
    "choice": ("combo box",),
    "text": ("entry", "password text"),
    # Table cells are included because GTK tree views expose cells rather than
    # rows - measured, they are the single most common role in these apps - and
    # clicking one selects its row, so the goal is both reachable and observable.
    "item": ("list item", "table row", "tree item", "table cell"),
}

_ROLE_TO_KIND = {
    role: kind for kind, roles in AFFORDANCE_ROLES.items() for role in roles
}


def discover_affordances(
    state: Sequence[Element],
    *,
    app_name: str,
    kinds: Optional[Sequence[str]] = None,
) -> List[Affordance]:
    """Every widget in the current state that a goal can be stated about.

    Reads only what is on screen now, so it is called wherever the pipeline
    already is - inside a dialog, on a tab, in a file list - rather than needing
    its own navigation.
    """
    from deskshot.generation.predicates import element_text

    wanted = set(kinds) if kinds else set(AFFORDANCE_ROLES)
    out: List[Affordance] = []
    seen: set = set()
    for elem in state:
        if elem.get("app_name") != app_name:
            continue
        role = str(elem.get("role") or "").strip().lower()
        kind = _ROLE_TO_KIND.get(role)
        if kind is None or kind not in wanted:
            continue
        label = element_text(elem)
        if not label or (kind, label) in seen:
            continue
        # A clipped label is not a stable identifier. `element_text` returns the
        # occlusion-aware string, so a cell reading "$ 500," is showing part of
        # "$ 500,00" - and the moment the screen changes, that prefix changes
        # with it and the match finds nothing. Measured: selecting one HomeBank
        # row made the next goal unresolvable for exactly this reason.
        if elem.get("visible_text_status") == "clipped":
            continue
        interaction = elem.get("interaction") or {}
        # A goal that cannot be acted on is not a goal. Thunar's sidebar lists
        # "Places" and "Devices" as tree items alongside the real entries, but
        # they are section headings: not actionable, and selecting them is not
        # something the app permits. Requiring a click point as well filters the
        # widgets that are present in the tree but inert on screen.
        if not interaction.get("actionable") or interaction.get("click_point") is None:
            continue
        if kind == "toggle":
            current: Any = interaction.get("checked")
            if current is None:
                continue
        elif kind == "item" and interaction.get("expandable"):
            # A collapsible group heading, not a row. Thunar's sidebar lists
            # "Places" and "Devices" beside the real entries and reports them
            # actionable, but clicking one collapses the group rather than
            # selecting anything - the goal "this is selected" is unreachable,
            # and the click takes the heading's own children out of the tree.
            continue
        elif kind in ("tab", "item"):
            current = bool(interaction.get("selected"))
        else:
            current = label
        seen.add((kind, label))
        out.append(
            Affordance(kind=kind, label=label, role=role, app_name=app_name,
                       current=current)
        )
    return out


def affordance_goal(aff: Affordance, target: Any) -> Predicate:
    """The checkable statement "this widget is in that state"."""
    match = ElementMatch(role=aff.role, text=aff.label, app_name=aff.app_name)
    if aff.kind == "toggle":
        return Predicate("checked" if target else "unchecked", match)
    if aff.kind in ("tab", "item"):
        return Predicate("selected", match)
    # choice and text are both "the widget now reads this", which is also how
    # their value is exposed - a combo box's text *is* its selection.
    return Predicate(
        "text_equals",
        ElementMatch(role=aff.role, app_name=aff.app_name),
        value=str(target),
    )


def affordance_skill(aff: Affordance, target: Any) -> Optional[Skill]:
    """The skill that puts the widget into the wanted state."""
    if aff.kind == "toggle":
        return SetCheckbox.build(aff.label, bool(target), app_name=aff.app_name)
    if aff.kind == "tab":
        return SelectTab.build(aff.label, aff.label, app_name=aff.app_name)
    if aff.kind == "item":
        return SelectItem.build(aff.label, app_name=aff.app_name)
    if aff.kind == "choice":
        return SetComboValue.build(aff.label, str(target), app_name=aff.app_name)
    if aff.kind == "text":
        return TypeInto.build(
            ElementMatch(role=aff.role, text=aff.label, app_name=aff.app_name),
            str(target),
        )
    return None


def default_target(aff: Affordance, *, text_value: str = "deskshot") -> Any:
    """A state the widget is not already in, so the goal starts out false.

    A task whose goal already holds asks for work that was never needed, and the
    executor would correctly emit no steps for it.
    """
    if aff.kind == "toggle":
        return not bool(aff.current)
    if aff.kind in ("tab", "item"):
        return True
    return text_value


def affordance_task(
    affordances: Sequence[Affordance],
    *,
    task_id: str,
    app_name: str,
    prefix: Sequence[Skill] = (),
    targets: Optional[Dict[str, Any]] = None,
) -> Optional[Task]:
    """A conjunctive task over a mix of widget kinds.

    `prefix` carries whatever navigation was needed to bring the widgets on
    screen - opening a dialog, say - so this stays agnostic about how they were
    reached. `settings_task` is the same shape with the prefix fixed and the
    kinds restricted to toggles.
    """
    # Selection is mutually exclusive within a container: selecting the second
    # row deselects the first, so "select A and select B" can never hold at
    # once. Measured on HomeBank - two selections verified individually while
    # the conjunction stayed at one fifth. At most one selection-kind goal is
    # kept; toggles and text have no such conflict and compose freely.
    exclusive_seen: set = set()
    filtered: List[Affordance] = []
    for aff in affordances:
        if aff.kind in ("item", "tab"):
            if aff.kind in exclusive_seen:
                continue
            exclusive_seen.add(aff.kind)
        filtered.append(aff)
    affordances = filtered

    usable = []
    for aff in affordances:
        target = (targets or {}).get(aff.label, default_target(aff))
        skill = affordance_skill(aff, target)
        if skill is not None:
            usable.append((aff, target, skill))
    if not usable:
        return None

    skills: List[Skill] = list(prefix) + [s for _, _, s in usable]
    goal = Conjunction(tuple(affordance_goal(a, t) for a, t, _ in usable))
    phrases = [s.describe() for _, _, s in usable]
    instruction = (
        f"In {app_name}: "
        + (", ".join(phrases[:-1] + [f"and {phrases[-1]}"]) if len(phrases) > 1
           else phrases[0])
        + "."
    )
    return Task(
        task_id=task_id,
        instruction=instruction,
        skills=skills,
        goal=goal,
        app_name=app_name,
        metadata={
            "kind": "affordance",
            "kinds": sorted({a.kind for a, _, _ in usable}),
            "num_affordances": len(usable),
            "affordances": [a.to_dict() for a, _, _ in usable],
        },
    )
