"""Units of goal-directed behaviour: an action generator plus its success test.

The point of a skill is that it carries its own postcondition. A trajectory
assembled from skills therefore has a checkable claim attached to every step -
"this click was for opening the File menu, and the File menu did open" - which
is what separates a goal-directed episode from a sequence of clicks that
happened to be recorded.

A skill emits **one action at a time, from the current state**, rather than a
plan computed up front. That is not a detail: the item a menu skill must click
does not exist in the tree until the menu is open, so any design that plans the
whole sequence from the initial state can only guess where it will be. Asking
the live state each time also makes the skill self-correcting - if a click
missed, the target is still there and the skill simply emits it again.

Targets are located by role and label, never by uid or coordinates, so a skill
keeps working across the redraws it is itself causing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from deskshot.generation.actions import SceneAction
from deskshot.generation.predicates import (
    Conjunction,
    Element,
    ElementMatch,
    Predicate,
    element_text,
)

# Roles a menu exposes once it is open. GTK is inconsistent about which of these
# it uses, so a menu-open test that names only one of them fails on some apps.
MENU_ITEM_ROLES = ("menu item", "check menu item", "radio menu item")


def _visible(elem: Element) -> bool:
    return elem.get("occlusion_state", "none") != "hidden"


def _pick(state: Sequence[Element], match: ElementMatch) -> Optional[Element]:
    """The best element for a match: actionable and unobscured first.

    Several elements can carry the same label - a toolbar button and its tooltip,
    a menu and the item that reopens it. Preferring one that is actionable and
    visible picks the one a person would click.
    """
    found = [e for e in match.find(state) if _visible(e)]
    if not found:
        return None
    found.sort(
        key=lambda e: (
            0 if (e.get("interaction") or {}).get("actionable") else 1,
            0 if e.get("occlusion_state", "none") == "none" else 1,
            -int((e.get("rect") or {}).get("w", 0)) * int((e.get("rect") or {}).get("h", 0)),
        )
    )
    return found[0]


@dataclass
class Skill:
    """One goal-directed unit of work.

    `goal` is the postcondition the executor checks after every action; a skill
    is finished the moment it holds, so a skill that needs fewer actions than its
    worst case simply stops early.
    """

    name: str
    goal: Conjunction
    max_actions: int = 4
    #: Human-readable purpose, used as the per-step instruction in the dataset.
    intent: str = ""

    def next_action(self, state: Sequence[Element]) -> Optional[SceneAction]:
        raise NotImplementedError  # pragma: no cover

    def reset(self) -> None:
        """Clear any progress this skill was holding.

        A plan is a list of skill objects, so running the same Task twice would
        otherwise start a multi-action skill halfway through - it would skip
        straight to typing without focusing the field, and the failure would
        look like the app ignoring a click.
        """

    def describe(self) -> str:
        return self.intent or f"{self.name}: {self.goal.describe()}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill": self.name,
            "intent": self.describe(),
            "goal": self.goal.to_dict(),
            "max_actions": self.max_actions,
        }


@dataclass
class ClickTarget(Skill):
    """Click one element identified by label. The generic building block."""

    match: ElementMatch = field(default_factory=ElementMatch)

    def next_action(self, state: Sequence[Element]) -> Optional[SceneAction]:
        target = _pick(state, self.match)
        if target is None:
            return None
        return SceneAction(
            type="click",
            target_uid=target.get("uid"),
            metadata={
                "skill": self.name,
                "role": target.get("role"),
                "label": element_text(target),
            },
        )


@dataclass
class OpenMenu(Skill):
    """Open a named menu and leave it open.

    Success is "menu items are on screen", not "the menu was clicked". Clicking a
    menu that is already open closes it, so a click-counting version of this
    skill toggles the menu shut and reports success.
    """

    menu: str = ""
    app_name: Optional[str] = None

    @staticmethod
    def build(
        menu: str,
        *,
        app_name: Optional[str] = None,
        min_items: int = 1,
        expect_item: Optional[str] = None,
    ) -> "OpenMenu":
        # Naming the item that should appear makes the skill self-correcting:
        # if some *other* menu is already open, "a menu item exists" is already
        # true and the menu never opens, so the next skill clicks into the wrong
        # menu. Testing for the item we are about to choose cannot be satisfied
        # by the wrong menu.
        if expect_item is not None:
            goal = Conjunction((
                Predicate(
                    "exists",
                    ElementMatch(role_any=MENU_ITEM_ROLES, text=expect_item, app_name=app_name),
                ),
            ))
        else:
            goal = Conjunction((
                Predicate(
                    "count_at_least",
                    ElementMatch(role_any=MENU_ITEM_ROLES, app_name=app_name),
                    count=min_items,
                ),
            ))
        return OpenMenu(
            name=f"open_menu({menu})",
            goal=goal,
            menu=menu,
            app_name=app_name,
            intent=f'open the "{menu}" menu',
            max_actions=2,
        )

    def next_action(self, state: Sequence[Element]) -> Optional[SceneAction]:
        target = _pick(state, ElementMatch(role="menu", text=self.menu, app_name=self.app_name))
        if target is None:
            target = _pick(
                state, ElementMatch(role="menu bar item", text=self.menu, app_name=self.app_name)
            )
        if target is None:
            return None
        return SceneAction(
            type="click",
            target_uid=target.get("uid"),
            metadata={"skill": self.name, "menu": self.menu},
        )


@dataclass
class TypeInto(Skill):
    """Focus a field, clear it, and type a value.

    Emitted as three actions rather than one so the trajectory shows the same
    sequence a person performs, and so a failure is attributable to the click,
    the clear, or the typing.
    """

    match: ElementMatch = field(default_factory=ElementMatch)
    text: str = ""
    _phase: int = 0

    @staticmethod
    def build(match: ElementMatch, text: str, *, exact: bool = True) -> "TypeInto":
        kind = "text_equals" if exact else "text_contains"
        return TypeInto(
            name=f"type_into({text!r})",
            goal=Conjunction((Predicate(kind, match, value=text),)),
            match=match,
            text=text,
            intent=f'type "{text}" into the {match.describe()}',
            max_actions=4,
        )

    def reset(self) -> None:
        self._phase = 0

    def next_action(self, state: Sequence[Element]) -> Optional[SceneAction]:
        if self._phase == 0:
            target = _pick(state, self.match)
            if target is None:
                return None
            self._phase = 1
            return SceneAction(
                type="click", target_uid=target.get("uid"), metadata={"skill": self.name}
            )
        if self._phase == 1:
            self._phase = 2
            return SceneAction(type="key", value="ctrl+a", metadata={"skill": self.name})
        self._phase = 3
        return SceneAction(type="type", text=self.text, metadata={"skill": self.name})


@dataclass
class SetCheckbox(Skill):
    """Put a check box into a wanted state.

    The reason this is a skill rather than a click: its goal is the *state*, not
    the click. A box already in the wanted state emits no action at all, so a
    task that asks for ten settings and finds three already correct costs seven
    steps, not ten. That is what makes a conjunctive settings task minimal rather
    than merely short.
    """

    match: ElementMatch = field(default_factory=ElementMatch)
    want: bool = True

    @staticmethod
    def build(label: str, want: bool, *, app_name: Optional[str] = None) -> "SetCheckbox":
        match = ElementMatch(role="check box", text=label, app_name=app_name)
        return SetCheckbox(
            name=f"set({label}={'on' if want else 'off'})",
            goal=Conjunction((Predicate("checked" if want else "unchecked", match),)),
            match=match,
            want=want,
            intent=f'turn {"on" if want else "off"} "{label}"',
            max_actions=2,
        )

    def next_action(self, state: Sequence[Element]) -> Optional[SceneAction]:
        target = _pick(state, self.match)
        if target is None:
            return None
        return SceneAction(
            type="click",
            target_uid=target.get("uid"),
            metadata={"skill": self.name, "want": self.want},
        )


@dataclass
class SelectTab(Skill):
    """Switch to a named page tab inside a dialog.

    Measured: GTK reports page tabs as `actionable=False` while still giving them
    a click point, and the click lands - `actionable` describes the AT-SPI Action
    interface, not whether a widget can be clicked. So this deliberately does not
    filter on it.

    Success is that the tab's own contents are present, named by the caller,
    since "the tab was clicked" is true even when the panel did not change.
    """

    tab: str = ""
    app_name: Optional[str] = None

    @staticmethod
    def build(
        tab: str, expect_widget: str, *, app_name: Optional[str] = None
    ) -> "SelectTab":
        return SelectTab(
            name=f"select_tab({tab})",
            goal=Conjunction((
                Predicate(
                    "exists",
                    ElementMatch(role="check box", text=expect_widget, app_name=app_name),
                ),
            )),
            tab=tab,
            app_name=app_name,
            intent=f'switch to the "{tab}" tab',
            max_actions=2,
        )

    def next_action(self, state: Sequence[Element]) -> Optional[SceneAction]:
        target = _pick(
            state, ElementMatch(role="page tab", text=self.tab, app_name=self.app_name)
        )
        if target is None:
            return None
        return SceneAction(
            type="click",
            target_uid=target.get("uid"),
            metadata={"skill": self.name, "tab": self.tab},
        )


@dataclass
class PressKey(Skill):
    """Send a key combination whose effect the caller states as the goal."""

    combo: str = ""

    @staticmethod
    def build(combo: str, goal: Conjunction, *, intent: str = "") -> "PressKey":
        return PressKey(
            name=f"press({combo})",
            goal=goal,
            combo=combo,
            intent=intent or f"press {combo}",
            max_actions=2,
        )

    def next_action(self, state: Sequence[Element]) -> Optional[SceneAction]:
        return SceneAction(type="key", value=self.combo, metadata={"skill": self.name})


def activate_menu_item(
    menu: str,
    item: str,
    *,
    app_name: Optional[str] = None,
    result: Optional[Conjunction] = None,
) -> List[Skill]:
    """Open a menu and activate one of its items: the canonical two-step unit.

    Returned as two skills rather than one so each half is verified separately.
    Collapsing them hides the common failure - the menu never opened, so the
    second click landed on whatever was underneath.

    `result` is what the item is supposed to *do*. Without it the only checkable
    claim is that the menu closed again, which is true even when the item did
    nothing, so a caller that knows the effect should say so.
    """
    closed = Conjunction((
        Predicate("absent", ElementMatch(role_any=MENU_ITEM_ROLES, text=item, app_name=app_name)),
    ))
    return [
        OpenMenu.build(menu, app_name=app_name, expect_item=item),
        ClickTarget(
            name=f"activate({menu}/{item})",
            goal=result or closed,
            match=ElementMatch(role_any=MENU_ITEM_ROLES, text=item, app_name=app_name),
            intent=f'choose "{item}" from the "{menu}" menu',
            max_actions=2,
        ),
    ]


#: Labels and descriptions GTK gives the hamburger button that replaces a menu
#: bar in a header-bar app. Measured across the pool: 12 of 20 apps expose no
#: role `menu` at all, so menu-bar discovery finds nothing in them - their menus
#: live behind a button instead, and the button is the only way in.
HEADER_MENU_HINTS = (
    "main menu", "menu", "hamburger", "application menu", "app menu",
    "primary menu", "options", "more", "view options",
)


def discover_header_menu_buttons(
    state: Sequence[Element], *, app_name: Optional[str] = None
) -> List[Element]:
    """Buttons that look like they open a menu, for apps with no menu bar.

    Identified by the button advertising a menu-ish name or description, and by
    being expandable - GTK marks a menu button `expandable`, which is the one
    structural signal that separates it from an ordinary toolbar button.
    """
    out: List[Element] = []
    for elem in state:
        role = str(elem.get("role") or "").strip().lower()
        if role not in ("push button", "toggle button"):
            continue
        if app_name is not None and elem.get("app_name") != app_name:
            continue
        interaction = elem.get("interaction") or {}
        label = element_text(elem).lower()
        description = str(((elem.get("attrs") or {}).get("description") or "")).lower()
        hinted = any(h in label or h in description for h in HEADER_MENU_HINTS)
        if hinted or interaction.get("expandable"):
            out.append(elem)
    return out


def open_header_menu(elem: Element) -> Skill:
    """Click a header-bar menu button and require items to appear."""
    app_name = elem.get("app_name")
    label = element_text(elem) or "menu"
    return ClickTarget(
        name=f"open_header_menu({label})",
        goal=Conjunction((
            Predicate(
                "count_at_least",
                ElementMatch(role_any=MENU_ITEM_ROLES, app_name=app_name),
                count=1,
            ),
        )),
        match=ElementMatch(
            role=str(elem.get("role")), text=element_text(elem) or None, app_name=app_name
        ),
        intent=f'open the "{label}" menu',
        max_actions=2,
    )


def discover_menus(state: Sequence[Element], *, app_name: Optional[str] = None) -> List[str]:
    """Menu labels the app is currently offering.

    Tasks are generated from what an app actually exposes rather than from a
    hand-written list, so adding an app to the pool adds its tasks too.
    """
    seen: List[str] = []
    for elem in state:
        if str(elem.get("role") or "").strip().lower() != "menu":
            continue
        if app_name is not None and elem.get("app_name") != app_name:
            continue
        label = element_text(elem)
        if label and label not in seen:
            seen.append(label)
    return seen


def discover_menu_items(state: Sequence[Element], *, app_name: Optional[str] = None) -> List[str]:
    """Item labels visible in whatever menu is currently open."""
    return list(discover_menu_item_roles(state, app_name=app_name))


def discover_menu_item_roles(
    state: Sequence[Element], *, app_name: Optional[str] = None
) -> Dict[str, str]:
    """`{label: role}` for the open menu, in order.

    The role is what distinguishes a toggle from a command, and it decides
    whether an item is safe to probe, so it is worth carrying rather than
    rediscovering.
    """
    seen: Dict[str, str] = {}
    for elem in state:
        role = str(elem.get("role") or "").strip().lower()
        if role not in MENU_ITEM_ROLES:
            continue
        if app_name is not None and elem.get("app_name") != app_name:
            continue
        label = element_text(elem)
        if label and label not in seen:
            seen[label] = role
    return seen


@dataclass
class SetComboValue(Skill):
    """Open a combo box and choose the option with a given label.

    Two actions, and the second target does not exist until the first has run -
    the option list is not in the tree while the combo is closed. Same shape as a
    menu, which is why it is a skill rather than a click.
    """

    match: ElementMatch = field(default_factory=ElementMatch)
    option: str = ""

    @staticmethod
    def build(
        label: str, option: str, *, app_name: Optional[str] = None
    ) -> "SetComboValue":
        match = ElementMatch(role="combo box", text=label, app_name=app_name)
        return SetComboValue(
            name=f"choose({label}={option})",
            goal=Conjunction((
                Predicate(
                    "text_equals",
                    ElementMatch(role="combo box", app_name=app_name),
                    value=option,
                ),
            )),
            match=match,
            option=option,
            intent=f'set "{label}" to "{option}"',
            max_actions=3,
        )

    def next_action(self, state: Sequence[Element]) -> Optional[SceneAction]:
        # If the option is already listed the combo is open, so pick it;
        # otherwise open the combo. Reading the state rather than counting
        # actions means a combo that was already open is not toggled shut.
        option = _pick(
            state,
            ElementMatch(role_any=("menu item", "list item"), text=self.option,
                         app_name=self.match.app_name),
        )
        if option is not None:
            return SceneAction(
                type="click", target_uid=option.get("uid"),
                metadata={"skill": self.name, "option": self.option},
            )
        combo = _pick(state, self.match)
        if combo is None:
            return None
        return SceneAction(
            type="click", target_uid=combo.get("uid"), metadata={"skill": self.name}
        )


@dataclass
class SelectItem(Skill):
    """Select a row in a list or tree by its label."""

    match: ElementMatch = field(default_factory=ElementMatch)

    @staticmethod
    def build(label: str, *, app_name: Optional[str] = None) -> "SelectItem":
        match = ElementMatch(
            role_any=("list item", "table row", "tree item", "table cell"),
            text=label, app_name=app_name,
        )
        return SelectItem(
            name=f"select({label})",
            goal=Conjunction((Predicate("selected", match),)),
            match=match,
            intent=f'select "{label}"',
            max_actions=2,
        )

    def next_action(self, state: Sequence[Element]) -> Optional[SceneAction]:
        target = _pick(state, self.match)
        if target is None:
            return None
        return SceneAction(
            type="click", target_uid=target.get("uid"), metadata={"skill": self.name}
        )
