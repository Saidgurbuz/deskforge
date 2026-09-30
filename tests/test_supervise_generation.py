"""The supervisor must resubmit exactly what is lost, and nothing else.

Two failures matter and they point opposite ways. Resubmitting a shard that is
still running duplicates work and puts two writers in one directory. Failing to
resubmit a shard that died leaves the corpus quietly short - which is what
happened when four shards pended six hours behind an advance reservation and
the array looked healthy.

These exercise the branches that only fire when something has gone wrong: an
EXIT job, a job that vanished from `bjobs` entirely, a shard that keeps failing,
and a cluster-wide outage that would otherwise become a submission storm.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from typing import List

import pytest

_spec = importlib.util.spec_from_file_location(
    "supervise_generation",
    Path(__file__).resolve().parents[1] / "scripts/supervise_generation.py",
)
sup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sup)


def _args(root: Path, **over) -> argparse.Namespace:
    base = dict(root=root, name="dsv3", shards=4, cores=24, queue="normal",
                group="grp", walltime="", max_attempts=6, max_resubmits=64,
                dry_run=False)
    base.update(over)
    return argparse.Namespace(**base)


def _mark_done(root: Path, *indices: int) -> None:
    d = root / "status" / "done"
    d.mkdir(parents=True, exist_ok=True)
    for i in indices:
        (d / f"shard-{i:04d}").write_text("2026-08-23T00:00:00Z")


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    (tmp_path / "logs").mkdir()
    (tmp_path / "run_shard.sh").write_text("#!/bin/bash\ntrue\n")
    return tmp_path


def _capture_submits(monkeypatch) -> List[List[str]]:
    calls: List[List[str]] = []

    def fake_run(cmd, timeout=120):
        if cmd and cmd[0] == "bsub":
            calls.append(cmd)
            return "Job <1234> is submitted to queue <normal>."
        return ""

    monkeypatch.setattr(sup, "_run", fake_run)
    return calls


def test_a_finished_shard_is_never_resubmitted(root, monkeypatch) -> None:
    _mark_done(root, 0, 1, 2, 3)
    calls = _capture_submits(monkeypatch)
    monkeypatch.setattr(sup, "live_shard_indices", lambda name: set())
    state = sup.one_pass(_args(root))
    assert state["finished"] == 4
    assert state["resubmitted"] == 0
    assert calls == []


def test_a_running_shard_is_never_resubmitted(root, monkeypatch) -> None:
    """Two writers in one shard directory is the thing to avoid."""
    calls = _capture_submits(monkeypatch)
    monkeypatch.setattr(sup, "live_shard_indices", lambda name: {0, 1, 2, 3})
    state = sup.one_pass(_args(root))
    assert state["resubmitted"] == 0 and calls == []


def test_a_shard_that_died_is_resubmitted(root, monkeypatch) -> None:
    """No marker and not in bjobs - the silent loss this exists to catch."""
    _mark_done(root, 0, 1)
    calls = _capture_submits(monkeypatch)
    monkeypatch.setattr(sup, "live_shard_indices", lambda name: {2})
    state = sup.one_pass(_args(root))
    assert state["resubmitted"] == 1
    assert calls[0][:4] == ["bsub", "-J", "dsv3-3", "-n"]
    assert str(root / "run_shard.sh") in calls[0] and "3" == calls[0][-1]


def test_no_host_is_ever_named(root, monkeypatch) -> None:
    """Pinning with -m is what left shards pending behind a reservation."""
    calls = _capture_submits(monkeypatch)
    monkeypatch.setattr(sup, "live_shard_indices", lambda name: set())
    sup.one_pass(_args(root))
    assert calls
    for cmd in calls:
        assert "-m" not in cmd


def test_attempts_accumulate_and_then_it_gives_up(root, monkeypatch) -> None:
    calls = _capture_submits(monkeypatch)
    monkeypatch.setattr(sup, "live_shard_indices", lambda name: set())
    args = _args(root, shards=1, max_attempts=3)
    for expected in (1, 2, 3):
        state = sup.one_pass(args)
        assert state["resubmitted"] == 1
        assert sup.load_attempts(root)["0"] == expected
    state = sup.one_pass(args)
    assert state["resubmitted"] == 0
    assert state["exhausted"] == [0]
    assert len(calls) == 3


def test_a_cluster_wide_outage_does_not_become_a_submission_storm(
    root, monkeypatch
) -> None:
    calls = _capture_submits(monkeypatch)
    monkeypatch.setattr(sup, "live_shard_indices", lambda name: set())
    state = sup.one_pass(_args(root, shards=500, max_resubmits=10))
    assert state["stalled"] == 500
    assert state["resubmitted"] == 10 and len(calls) == 10


def test_a_failed_bsub_does_not_count_as_an_attempt(root, monkeypatch) -> None:
    monkeypatch.setattr(sup, "_run", lambda cmd, timeout=120: "Batch system down")
    monkeypatch.setattr(sup, "live_shard_indices", lambda name: set())
    state = sup.one_pass(_args(root, shards=1))
    assert state["resubmitted"] == 0
    assert sup.load_attempts(root) == {}


def test_dry_run_submits_nothing(root, monkeypatch) -> None:
    calls = _capture_submits(monkeypatch)
    monkeypatch.setattr(sup, "live_shard_indices", lambda name: set())
    state = sup.one_pass(_args(root, dry_run=True))
    assert state["resubmitted"] == 4 and calls == []
    assert sup.load_attempts(root) == {}


# --- reading LSF -----------------------------------------------------------

def test_live_indices_reads_only_our_jobs_and_only_live_states(monkeypatch) -> None:
    monkeypatch.setattr(sup, "_run", lambda cmd, timeout=120: "\n".join([
        "dsv3-0 RUN",
        "dsv3-1 PEND",
        "dsv3-2 EXIT",         # dead: must be resubmitted
        "dsv3-3 DONE",         # finished by LSF, but the marker is the authority
        "dsv3-4 SSUSP",        # suspended, still ours - do not duplicate
        "othername-9 RUN",     # someone else's run
        "malformed",
        "dsv3-notanumber RUN",
    ]))
    assert sup.live_shard_indices("dsv3") == {0, 1, 4}


def test_live_indices_survives_bjobs_failing(monkeypatch) -> None:
    monkeypatch.setattr(sup, "_run", lambda cmd, timeout=120: "")
    assert sup.live_shard_indices("dsv3") == set()


def test_a_done_state_in_lsf_is_not_treated_as_finished(root, monkeypatch) -> None:
    """LSF can report DONE for a job whose work was cut short."""
    calls = _capture_submits(monkeypatch)
    monkeypatch.setattr(sup, "live_shard_indices", lambda name: set())
    state = sup.one_pass(_args(root, shards=1))
    assert state["finished"] == 0
    assert state["resubmitted"] == 1 and len(calls) == 1


def test_markers_are_read_back_and_junk_ignored(root) -> None:
    _mark_done(root, 0, 7)
    (root / "status" / "done" / "not-a-shard").write_text("x")
    (root / "status" / "done" / "shard-xx").write_text("x")
    assert sup.finished_shard_indices(root) == {0, 7}


def test_no_marker_directory_yet(tmp_path: Path) -> None:
    assert sup.finished_shard_indices(tmp_path) == set()


def test_attempts_file_survives_corruption(root) -> None:
    path = root / "status" / "supervisor_attempts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json")
    assert sup.load_attempts(root) == {}
    sup.save_attempts(root, {"3": 2})
    assert sup.load_attempts(root) == {"3": 2}


def test_a_missing_runner_is_reported_not_crashed(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "logs").mkdir()
    monkeypatch.setattr(sup, "live_shard_indices", lambda name: set())
    state = sup.one_pass(_args(tmp_path, shards=1))
    assert state["resubmitted"] == 0


# --- two runs into one corpus must not read each other's completions --------

def test_markers_are_read_from_the_per_job_directory(root, monkeypatch) -> None:
    """A second run into the same corpus starts with zero completions."""
    old = root / "status" / "done"
    old.mkdir(parents=True)
    for i in range(4):                      # the previous run's markers
        (old / f"shard-{i:04d}").write_text("x")
    new = old / "dsv4"
    new.mkdir()
    (new / "shard-0000").write_text("x")    # this run has finished one

    assert sup.finished_shard_indices(root, "dsv4") == {0}
    calls = _capture_submits(monkeypatch)
    monkeypatch.setattr(sup, "live_shard_indices", lambda name: set())
    state = sup.one_pass(_args(root, name="dsv4", shards=4))
    assert state["finished"] == 1
    assert state["resubmitted"] == 3, "it inherited the previous run's markers"
    assert len(calls) == 3


def test_a_run_predating_the_change_still_reads_the_flat_directory(root) -> None:
    old = root / "status" / "done"
    old.mkdir(parents=True)
    for i in (0, 2):
        (old / f"shard-{i:04d}").write_text("x")
    assert sup.finished_shard_indices(root, "dsv3") == {0, 2}


def test_the_runner_writes_into_the_per_job_directory() -> None:
    """Guard the shell template, not just the Python side."""
    from pathlib import Path as _P
    text = (_P(__file__).resolve().parents[1] / "scripts/submit_generation.sh").read_text()
    assert 'DONE_MARKER="$ROOT/status/done/__JOBNAME__/' in text
    assert "s|__JOBNAME__|$JOBNAME|" in text
