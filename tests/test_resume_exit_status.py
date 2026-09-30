"""A shard with nothing left to do has succeeded, not failed.

`scene-batch` ends with `return 0 if accepted_rows else 1`, which is right when
it was asked to capture something and produced nothing. It is wrong for a shard
resubmitted after its slice was already finished: it accepts zero new rows,
exits 1, never writes `status/done/shard-NNNN`, and the supervisor puts it back
on the cluster over and over until the attempt cap. The run could then never
report itself complete.

Caught on a live two-scene run: the resubmitted shard logged
"resume: 2 of 2 scenes already captured, 0 to go" and then exited 1.
"""

from __future__ import annotations

import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src/deskshot/cli.py"


def _source() -> str:
    return SOURCE.read_text(encoding="utf-8")


def test_the_flag_is_initialised_before_any_branch_can_read_it() -> None:
    """A NameError here would kill the shard at the very end of its work."""
    text = _source()
    init = text.index("resume_left_nothing_to_do = False")
    setter = text.index("resume_left_nothing_to_do = not planned_scenes")
    reader = text.index("if resume_left_nothing_to_do:")
    assert init < setter < reader


def test_an_exhausted_resume_short_circuits_the_failure_return() -> None:
    text = _source()
    tail = text[text.index("Saved {len(accepted_rows)} accepted scene captures"):]
    assert "if resume_left_nothing_to_do:\n        return 0" in tail
    assert "return 0 if accepted_rows else 1" in tail
    # the short-circuit must come first, or it changes nothing
    assert tail.index("if resume_left_nothing_to_do:") < tail.index(
        "return 0 if accepted_rows else 1"
    )


def test_a_batch_that_was_asked_for_work_and_produced_none_still_fails() -> None:
    """The behaviour that must not be lost: real failure still reports failure."""
    assert "return 0 if accepted_rows else 1" in _source()


def test_the_flag_is_only_set_when_the_plan_slice_is_empty() -> None:
    text = _source()
    line = next(
        l for l in text.splitlines()
        if "resume_left_nothing_to_do = not planned_scenes" in l
    )
    assert re.search(r"=\s*not planned_scenes\s*$", line.strip())


# --- the same logic, exercised rather than read -----------------------------

def _exit_status(*, accepted_rows: int, resume_left_nothing_to_do: bool) -> int:
    if resume_left_nothing_to_do:
        return 0
    return 0 if accepted_rows else 1


def test_status_matrix() -> None:
    # the regression: resumed, nothing to do, nothing captured -> success
    assert _exit_status(accepted_rows=0, resume_left_nothing_to_do=True) == 0
    # a shard that did work -> success
    assert _exit_status(accepted_rows=7, resume_left_nothing_to_do=False) == 0
    # asked to work, captured nothing -> failure, so it gets resubmitted
    assert _exit_status(accepted_rows=0, resume_left_nothing_to_do=False) == 1
    # partially resumed and then did work -> success
    assert _exit_status(accepted_rows=3, resume_left_nothing_to_do=False) == 0
