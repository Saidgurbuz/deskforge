"""Checkable statements about a desktop state.

A long-horizon task is only worth collecting if you can say whether it was
achieved, and say it mechanically. Trajectories from random clicking have no
such statement: whatever happened is what happened, so there is nothing to
verify, nothing to score a policy against, and no way to tell a step that
advanced the task from one that merely fired.

So the goal is a predicate, not a description. Everything here evaluates against
the elements of one observation - the same annotated elements the parser already
emits - which keeps three properties that matter:

*Verifiable* - the answer is computed, never asserted.
*Serializable* - a predicate round-trips through JSON, so a task can be stored,
 replayed, and shipped as part of the dataset.
*Legible* - `describe()` renders the same object as the natural-language
 instruction, so the instruction and the success test cannot drift apart. This is
 the usual failure of hand-written benchmarks: the prompt says one thing, the
 checker tests another.

Matching is by role and text rather than by uid. Uids are derived from the
accessibility path and change as the tree changes, so a task pinned to a uid
stops meaning anything the moment the app redraws. "the button labelled Save"
survives that; `f8cbe8cb141d67e8` does not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

Element = Dict[str, Any]


def element_text(elem: Element) -> str:
    """The text this element shows, preferring what is actually on screen.

    `visible_text` is the occlusion-aware string, so a partially covered element
    matches on what a person can read rather than on what the widget holds.
    """
    for key in ("visible_text", "inner_text", "name"):
        val = elem.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _norm(text: str) -> str:
    """Collapse whitespace and case, and drop GTK's mnemonic underscores.

    A menu labelled `_File` in the tree is drawn as `File`, and a task written
    against the screen should not have to know that.
    """
    return re.sub(r"\s+", " ", text.replace("_", "")).strip().lower()


@dataclass(frozen=True)
class ElementMatch:
    """How to find an element in a state, by what it is rather than where."""

    role: Optional[str] = None
    #: Any one of these roles. GTK spells the same concept several ways - a
    #: dialog is "dialog", "alert", or "file chooser" depending on the widget -
    #: so a match naming a single role silently misses two thirds of them.
    role_any: Optional[tuple] = None
    text: Optional[str] = None              # exact, after normalisation
    text_contains: Optional[str] = None
    app_name: Optional[str] = None
    actionable: Optional[bool] = None

    def matches(self, elem: Element) -> bool:
        role = _norm(str(elem.get("role") or ""))
        if self.role is not None and role != _norm(self.role):
            return False
        if self.role_any is not None and role not in {_norm(r) for r in self.role_any}:
            return False
        if self.app_name is not None:
            if str(elem.get("app_name") or "") != self.app_name:
                return False
        if self.actionable is not None:
            got = bool((elem.get("interaction") or {}).get("actionable"))
            if got != self.actionable:
                return False
        if self.text is not None:
            if _norm(element_text(elem)) != _norm(self.text):
                return False
        if self.text_contains is not None:
            if _norm(self.text_contains) not in _norm(element_text(elem)):
                return False
        return True

    def find(self, state: Sequence[Element]) -> List[Element]:
        return [e for e in state if self.matches(e)]

    def describe(self) -> str:
        parts: List[str] = []
        if self.text is not None:
            parts.append(f'labelled "{self.text}"')
        elif self.text_contains is not None:
            parts.append(f'whose label contains "{self.text_contains}"')
        noun = self.role or (" or ".join(self.role_any) if self.role_any else "element")
        if self.app_name:
            parts.append(f"in {self.app_name}")
        return " ".join([noun] + parts)

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "ElementMatch":
        fields = {k: data[k] for k in data if k in ElementMatch.__annotations__}
        if isinstance(fields.get("role_any"), list):
            # JSON has no tuples, and the dataclass is frozen/hashable.
            fields["role_any"] = tuple(fields["role_any"])
        return ElementMatch(**fields)


# Every predicate kind, and how to phrase it. Keeping the phrasing beside the
# evaluation is what stops the instruction and the check from drifting apart.
_PHRASING = {
    "exists": "there is a {match}",
    "absent": "there is no {match}",
    "visible": "the {match} is fully visible",
    "text_equals": 'the {match} reads "{value}"',
    "text_contains": 'the {match} contains "{value}"',
    "checked": "the {match} is checked",
    "unchecked": "the {match} is not checked",
    "expanded": "the {match} is expanded",
    "selected": "the {match} is selected",
    "count_at_least": "there are at least {count} of {match}",
    "count_equals": "there are exactly {count} of {match}",
}


@dataclass(frozen=True)
class Predicate:
    """One checkable statement about a state."""

    kind: str
    match: ElementMatch = field(default_factory=ElementMatch)
    value: str = ""
    count: int = 1

    def __post_init__(self) -> None:
        if self.kind not in _PHRASING:
            raise ValueError(f"unknown predicate kind: {self.kind!r}")

    def holds(self, state: Sequence[Element]) -> bool:
        found = self.match.find(state)
        kind = self.kind

        if kind == "exists":
            return bool(found)
        if kind == "absent":
            return not found
        if kind == "count_at_least":
            return len(found) >= self.count
        if kind == "count_equals":
            return len(found) == self.count
        if kind == "visible":
            return any(e.get("occlusion_state", "none") == "none" for e in found)
        if kind == "text_equals":
            return any(_norm(element_text(e)) == _norm(self.value) for e in found)
        if kind == "text_contains":
            return any(_norm(self.value) in _norm(element_text(e)) for e in found)
        if kind in ("checked", "unchecked"):
            want = kind == "checked"
            return any(
                bool((e.get("interaction") or {}).get("checked")) == want for e in found
            )
        if kind == "expanded":
            return any(bool((e.get("interaction") or {}).get("expanded")) for e in found)
        if kind == "selected":
            return any(bool((e.get("interaction") or {}).get("selected")) for e in found)
        raise AssertionError(f"unhandled predicate kind {kind!r}")  # pragma: no cover

    def describe(self) -> str:
        return _PHRASING[self.kind].format(
            match=self.match.describe(), value=self.value, count=self.count
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"kind": self.kind, "match": self.match.to_dict()}
        if self.value:
            out["value"] = self.value
        if self.count != 1:
            out["count"] = self.count
        return out

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Predicate":
        return Predicate(
            kind=data["kind"],
            match=ElementMatch.from_dict(data.get("match") or {}),
            value=data.get("value", ""),
            count=int(data.get("count", 1)),
        )


@dataclass(frozen=True)
class Conjunction:
    """All of these must hold. A task's goal is one of these."""

    parts: tuple = ()

    def holds(self, state: Sequence[Element]) -> bool:
        return all(p.holds(state) for p in self.parts)

    def unmet(self, state: Sequence[Element]) -> List[Predicate]:
        """Which parts do not hold - the progress signal, not just pass/fail."""
        return [p for p in self.parts if not p.holds(state)]

    def progress(self, state: Sequence[Element]) -> float:
        if not self.parts:
            return 1.0
        met = len(self.parts) - len(self.unmet(state))
        return met / len(self.parts)

    def describe(self) -> str:
        described = [p.describe() for p in self.parts]
        if not described:
            return "nothing"
        if len(described) == 1:
            return described[0]
        return ", and ".join([", ".join(described[:-1]), described[-1]])

    def to_dict(self) -> Dict[str, Any]:
        return {"all": [p.to_dict() for p in self.parts]}

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Conjunction":
        return Conjunction(tuple(Predicate.from_dict(p) for p in data.get("all") or []))
