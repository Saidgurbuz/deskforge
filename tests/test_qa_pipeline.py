"""The gate has to fail, or it is decoration.

A quality gate nobody has seen reject anything is worse than no gate: it gets
trusted. These tests pin both directions - a regressed run must fail, an
improved one must pass - and the rule that catches the sneaky case, where every
rate improves because annotations were deleted rather than fixed.
"""

import importlib.util
import json
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("qa", PROJECT_ROOT / "scripts" / "qa.py")
qa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qa)


def _metrics(**over):
    base = {
        "root": "run",
        "captures": 30,
        # A comparable run by default, so these tests exercise the enforced
        # path; the comparability rule has its own tests below.
        "seeds": list(range(930000, 930030)),
        "elements_per_capture": 150.0,
        "fp_blank_rate": 0.002,
        "fp_blank_count": 4,
        "fp_blank_checked": 2000,
        "fp_blank_by_role": {},
        "fn_uncovered_ink": 0.05,
        "phantom_rate": 0.007,
        "drift_rate": 0.008,
        "text_unsupported_rate": 0.09,
        "text_status": {},
        "by_app": {},
    }
    base.update(over)
    return base


def test_identical_runs_pass(capsys):
    assert qa.compare(_metrics(), _metrics()) is True
    assert qa.gate(_metrics(), _metrics()) is True


def test_false_positives_rising_fails():
    """A box over blank pixels is the error this project most wants gone."""
    assert qa.gate(_metrics(fp_blank_rate=0.02), _metrics()) is False


def test_false_negatives_rising_fails():
    assert qa.gate(_metrics(fn_uncovered_ink=0.09), _metrics()) is False


def test_withheld_text_rising_fails():
    """Withheld text is content the screen shows and ground truth does not."""
    assert qa.gate(_metrics(text_unsupported_rate=0.20), _metrics()) is False


def test_improvement_passes():
    better = _metrics(fp_blank_rate=0.0001, fn_uncovered_ink=0.03,
                      text_unsupported_rate=0.02)
    assert qa.gate(better, _metrics()) is True


def test_deleting_annotations_is_not_an_improvement():
    """Every rate improves, because a third of the annotations are gone.

    Rates are ratios, so dropping elements flatters all of them at once. The
    density guard is the only thing that notices.
    """
    hollow = _metrics(
        fp_blank_rate=0.0, fn_uncovered_ink=0.01, phantom_rate=0.0,
        drift_rate=0.0, text_unsupported_rate=0.0,
        elements_per_capture=100.0,
    )
    assert qa.gate(hollow, _metrics()) is False
    assert qa.compare(_metrics(), hollow) is False


def test_small_moves_stay_within_tolerance():
    """Which windows a seed draws varies run to run; the gate must not fire on that."""
    noisy = _metrics(fn_uncovered_ink=0.05 + qa.TOLERANCE["fn_uncovered_ink"] / 2)
    assert qa.gate(noisy, _metrics()) is True


def test_measure_writes_a_comparable_record(tmp_path):
    """Whatever `measure` returns must be what `gate` can read back."""
    record = _metrics()
    path = tmp_path / "quality.json"
    path.write_text(json.dumps(record))
    assert qa._read(tmp_path) == record
    assert qa._read(path) == record


def test_missing_quality_json_is_a_clear_error(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        qa._read(tmp_path)
    assert "qa.py audit" in str(excinfo.value)


# --------------------------------------------------------------------------
# a verdict is only given on a comparable run
# --------------------------------------------------------------------------

def _seeded(seeds, captures=30, **over):
    return _metrics(seeds=list(seeds), captures=captures, **over)


def test_different_seeds_get_no_verdict():
    """Found by running the documented loop: a 5-capture run on a different seed
    range failed on density and withheld text with no code change at all. Every
    metric is composition-sensitive, so judging across scene sets reports the
    scene mix as a regression."""
    base = _seeded(range(930000, 930030))
    other = _seeded(range(940000, 940030), elements_per_capture=100.0)
    ok, why = qa.comparable(other, base)
    assert ok is False and "seeds" in why
    assert qa.gate(other, base) is True  # advisory, not enforced


def test_too_few_captures_get_no_verdict():
    base = _seeded(range(930000, 930030))
    tiny = _seeded(range(930000, 930005), captures=5)
    ok, why = qa.comparable(tiny, base)
    assert ok is False and "captures" in why
    assert qa.gate(tiny, base) is True


def test_same_seeds_are_judged():
    base = _seeded(range(930000, 930030))
    same = _seeded(range(930000, 930030), fp_blank_rate=0.05)
    ok, _ = qa.comparable(same, base)
    assert ok is True
    assert qa.gate(same, base) is False  # a real regression still fails


def test_mostly_overlapping_seeds_are_judged():
    """Runs never reproduce a seed set exactly - retries drop some scenes."""
    base = _seeded(range(930000, 930030))
    most = _seeded(range(930000, 930025))
    assert qa.comparable(most, base)[0] is True


# --------------------------------------------------------------------------
# gating on what a seed was resolved against
# --------------------------------------------------------------------------

def _prov(pool_hash="p1", config_hash="c1", commit="abc"):
    return {"code": {"commit": commit, "branch": "main", "dirty": False},
            "app_pool": {"hash": pool_hash, "size": 20},
            "config_hash": config_hash, "schema": 1}


def test_a_different_commit_still_gets_a_verdict():
    """The commit is the thing under test.

    Requiring it to match would make the gate unpassable the moment anything is
    fixed, which is the opposite of what a regression gate is for.
    """
    base = _seeded(range(930000, 930030), provenance=_prov(commit="old"))
    now = _seeded(range(930000, 930030), provenance=_prov(commit="new"))
    ok, why = qa.comparable(now, base)
    assert ok is True and "same pool and config" in why


def test_a_changed_pool_refuses_a_verdict():
    """The FileZilla case: the same seed stopped meaning the same scene."""
    base = _seeded(range(930000, 930030), provenance=_prov(pool_hash="with-filezilla"))
    now = _seeded(range(930000, 930030), provenance=_prov(pool_hash="without"))
    ok, why = qa.comparable(now, base)
    assert ok is False and "pool" in why


def test_a_changed_config_refuses_a_verdict():
    base = _seeded(range(930000, 930030), provenance=_prov(config_hash="light"))
    now = _seeded(range(930000, 930030), provenance=_prov(config_hash="dark"))
    assert qa.comparable(now, base)[0] is False


def test_a_run_mixing_builds_refuses_a_verdict():
    base = _seeded(range(930000, 930030), provenance=_prov())
    now = _seeded(range(930000, 930030), provenance=_prov(), provenance_mixed=True)
    ok, why = qa.comparable(now, base)
    assert ok is False and "mixes" in why


def test_unstamped_runs_fall_back_to_seed_overlap():
    """Captures predating the stamp still get compared, with the caveat said."""
    base = _seeded(range(930000, 930030))
    now = _seeded(range(930000, 930030))
    ok, why = qa.comparable(now, base)
    assert ok is True and "no provenance stamp" in why
