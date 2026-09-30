"""No undefined name may reach the repository.

This exists because of a specific, expensive failure. `_write_sample_verdict`
called `_capture_dir_for`, which lives in another module and was never imported.
The function only runs in a shard's end-of-run audit pass, so the entire test
suite passed, the code shipped, and all 64 shards of a live 52,000-scene run
died with a `NameError` **after** finishing their captures. Fixing that one then
uncovered a second (`shard_tag`, used in a different function from the one that
defines it) which would have crashed at the very end of every shard - so the
first fix would only have moved the crash later.

Tests cannot cover this: a name that is only wrong on a branch nobody takes is
invisible to them. A static check reads every branch whether or not it runs,
which is the right instrument, and it costs a second.

`pyflakes` is a hard dependency of the test rather than an optional nicety,
because a check that silently skips is not a check.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

#: Everything that runs. Tests are included: a broken test is a silent gap.
SCANNED = ("src", "scripts", "tests")


def _pyflakes(paths: list[str]) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "pyflakes", *paths],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode > 1 and "No module named" in result.stderr:
        pytest.fail(
            "pyflakes is not installed, so the undefined-name check cannot run. "
            "Install it with `pip install --user pyflakes`. This check is not "
            "optional: it is the only thing that reads branches the tests do not."
        )
    return result.stdout


def test_no_undefined_names_anywhere() -> None:
    findings = [
        line
        for line in _pyflakes([str(REPO / part) for part in SCANNED]).splitlines()
        if "undefined name" in line
    ]
    assert not findings, "undefined names found:\n  " + "\n  ".join(findings)


def test_the_check_would_actually_catch_one(tmp_path) -> None:
    """A guard that cannot fail is not a guard - prove this one can."""
    broken = tmp_path / "broken.py"
    broken.write_text(
        "def f():\n    return some_name_that_does_not_exist\n", encoding="utf-8"
    )
    assert "undefined name" in _pyflakes([str(broken)])
