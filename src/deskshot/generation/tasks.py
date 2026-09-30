"""Long-horizon tasks: a goal, a plan of skills, and a verified trajectory.

What makes an episode worth collecting is that every step can be justified after
the fact. The executor here enforces that in two ways, and they are the whole
design:

**A skill that is already satisfied emits no action.** Its postcondition is
checked before each action, not after a fixed number of them, so the trajectory
contains no step that was not needed at the moment it was taken. This is the
mechanical version of "no filler": a step exists only because some subgoal did
not hold, and the record says which one.

**A skill that never reaches its postcondition ends the episode.** It is not
skipped and the run does not continue past it. Continuing would produce steps
whose stated purpose is false, and a trajectory that is wrong about its own
intent is worse than no trajectory - a policy trained on it learns that the
action for a goal is whatever happened to be recorded.

Horizon comes from composing verified units, not from letting a single search
run longer. A task that enters eight rows is forty-odd steps, and each one is
still attached to a subgoal that was checked.

Each step records the local instruction, the action, the resulting diff, and how
much of the final goal now holds - so the trajectory carries a dense progress
signal rather than one reward at the end.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence

from deskshot.generation.predicates import Conjunction, Element
from deskshot.generation.skills import Skill


class SteppableEnv(Protocol):
    """The part of `DesktopEnv` a task needs. Narrow enough to fake in tests."""

    @property
    def last_elements(self) -> List[Element]: ...

    def step(self, action: Any) -> Dict[str, Any]: ...


@dataclass
class Task:
    """A goal, the plan believed to reach it, and the instruction that says so."""

    task_id: str
    instruction: str
    skills: List[Skill]
    goal: Conjunction
    app_name: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def max_steps(self) -> int:
        """Worst-case length. The realised episode is usually shorter."""
        return sum(s.max_actions for s in self.skills)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "instruction": self.instruction,
            "app_name": self.app_name,
            "goal": self.goal.to_dict(),
            "plan": [s.to_dict() for s in self.skills],
            "max_steps": self.max_steps,
            "metadata": dict(self.metadata),
        }


@dataclass
class StepRecord:
    """One action, with everything needed to supervise it."""

    step_index: int
    skill: str
    intent: str
    action: Dict[str, Any]
    observation_stem: Optional[str]
    diff: Dict[str, Any]
    subgoal_met: bool
    goal_progress: float
    remaining: List[str]
    changed: bool

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class TaskResult:
    """The outcome, and enough detail to tell why a failure failed."""

    task: Task
    status: str                       # success | goal_unmet | skill_failed | error
    steps: List[StepRecord]
    reason: str = ""
    failed_skill: Optional[str] = None
    elapsed_sec: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.status == "success"

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.task.to_dict(),
            "status": self.status,
            "reason": self.reason,
            "failed_skill": self.failed_skill,
            "num_steps": len(self.steps),
            "elapsed_sec": round(self.elapsed_sec, 2),
            "steps": [s.to_dict() for s in self.steps],
        }


def run_task(
    task: Task,
    env: SteppableEnv,
    *,
    on_step: Optional[Callable[[StepRecord], None]] = None,
    now: Callable[[], float] = time.monotonic,
) -> TaskResult:
    """Execute a task, verifying every skill, and return the trajectory."""
    started = now()
    records: List[StepRecord] = []

    # An open menu holds a pointer grab, and under it every mouse move blocks
    # until it times out. Whatever ran before this - a probe, a previous task -
    # may have left one, so the grab is released before the first action rather
    # than discovered by a ten-second stall.
    release = getattr(env, "release_grabs", None)
    if callable(release):
        release()
    state: Sequence[Element] = env.last_elements

    def _finish(status: str, reason: str = "", skill: Optional[str] = None) -> TaskResult:
        return TaskResult(
            task=task,
            status=status,
            steps=records,
            reason=reason,
            failed_skill=skill,
            elapsed_sec=now() - started,
        )

    for skill in task.skills:
        skill.reset()
        used = 0
        while not skill.goal.holds(state):
            if used >= skill.max_actions:
                return _finish(
                    "skill_failed",
                    f"{skill.name} did not reach its goal in {skill.max_actions} actions "
                    f"({skill.goal.describe()})",
                    skill.name,
                )
            action = skill.next_action(state)
            if action is None:
                return _finish(
                    "skill_failed",
                    f"{skill.name} found no target for its next action",
                    skill.name,
                )
            try:
                result = env.step(action)
            except Exception as exc:  # a dead app must not look like a bad plan
                return _finish("error", f"{skill.name} raised: {exc}", skill.name)

            used += 1
            state = env.last_elements
            observation = result.get("observation") or {}
            diff = result.get("diff") or {}
            record = StepRecord(
                step_index=len(records),
                skill=skill.name,
                intent=skill.describe(),
                action=result.get("action") or action.to_dict(),
                observation_stem=observation.get("stem"),
                diff=diff,
                subgoal_met=skill.goal.holds(state),
                goal_progress=round(task.goal.progress(state), 4),
                remaining=[p.describe() for p in task.goal.unmet(state)],
                changed=bool(diff.get("changed")),
            )
            records.append(record)
            if on_step is not None:
                on_step(record)

    if not task.goal.holds(state):
        # Every subgoal was verified, so the plan was carried out; the goal is
        # still unmet. That is a fact about the task definition, not the run, and
        # it is worth keeping distinct from a step that failed.
        return _finish(
            "goal_unmet",
            f"plan completed but goal does not hold: {task.goal.describe()}",
        )
    return _finish("success")
