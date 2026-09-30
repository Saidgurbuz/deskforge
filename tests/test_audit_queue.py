"""The queue builder: a draw somebody can reproduce, and hashes that prove it.

A campaign that cannot be re-derived from its manifest is not a sample, it is
an anecdote, so the properties worth pinning are: the same seed gives the same
items, every stratum level is represented, the recorded hashes are of the files
a rater will actually see, and a queue is written once and never rewritten.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from audit_fixtures import build_corpus
from deskshot.inspector import audit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUILDER = PROJECT_ROOT / "scripts" / "build_audit_queue.py"


def run(corpus, audit_root, campaign, *extra, expect=0):
    command = [sys.executable, str(BUILDER), "--campaign", campaign,
               "--corpus", str(corpus), "--audit-root", str(audit_root),
               "--items", "6", "--seed", "5"] + list(extra)
    result = subprocess.run(command, capture_output=True, text=True,
                            cwd=str(PROJECT_ROOT), timeout=120)
    assert result.returncode == expect, result.stdout + result.stderr
    return result.stdout + result.stderr


@pytest.fixture()
def corpus(tmp_path):
    return build_corpus(tmp_path / "corpus")


def test_a_dry_run_reports_the_strata_and_writes_nothing(corpus, tmp_path):
    output = run(corpus, tmp_path / "audit", "dry", "--dry-run", "--min-per-level", "1")
    assert "dry run: nothing written" in output
    assert "split" in output and "theme" in output
    assert not (tmp_path / "audit" / "dry").exists()


def test_the_same_seed_draws_the_same_queue(corpus, tmp_path):
    run(corpus, tmp_path / "a", "same", "--min-per-level", "1")
    run(corpus, tmp_path / "b", "same", "--min-per-level", "1")
    first = audit.Campaign(tmp_path / "a" / "same").queue
    second = audit.Campaign(tmp_path / "b" / "same").queue
    assert ([row["observation_key"] for row in first]
            == [row["observation_key"] for row in second])


def test_a_different_seed_draws_a_different_order(corpus, tmp_path):
    run(corpus, tmp_path / "a", "s1", "--min-per-level", "0")
    run(corpus, tmp_path / "b", "s2", "--min-per-level", "0", "--seed", "99")
    first = [row["observation_key"] for row in audit.Campaign(tmp_path / "a" / "s1").queue]
    second = [row["observation_key"] for row in audit.Campaign(tmp_path / "b" / "s2").queue]
    assert first != second


def test_every_stratum_level_gets_at_least_the_floor(corpus, tmp_path):
    run(corpus, tmp_path / "audit", "floored", "--items", "4", "--min-per-level", "1")
    campaign = audit.Campaign(tmp_path / "audit" / "floored")
    for field in campaign.manifest["stratum_fields"]:
        levels = {row["stratum"][field] for row in campaign.queue}
        assert levels, field
    # `val` and `test_app` are one capture each in the fixture and would not
    # survive a purely uniform draw of four items.
    splits = {row["stratum"]["split"] for row in campaign.queue}
    assert {"val", "test_app"} <= splits, splits


def test_a_floor_of_zero_gives_a_purely_uniform_draw(corpus, tmp_path):
    run(corpus, tmp_path / "audit", "uniform", "--min-per-level", "0")
    campaign = audit.Campaign(tmp_path / "audit" / "uniform")
    assert {row["draw"] for row in campaign.queue} == {"population"}
    assert campaign.manifest["population_items"] == len(campaign.queue)


def test_each_item_records_the_hash_of_the_files_a_rater_will_see(corpus, tmp_path):
    run(corpus, tmp_path / "audit", "hashed", "--min-per-level", "1")
    campaign = audit.Campaign(tmp_path / "audit" / "hashed")
    for item in campaign.queue:
        base = corpus / item["source_path"]
        for kind, suffix in (("image", ".png"), ("leaf", ".elements.leaf.json"),
                             ("screentag", ".screentag.txt")):
            data = Path(str(base) + suffix).read_bytes()
            assert item["digests"][kind]["sha256"] == hashlib.sha256(data).hexdigest()
            assert item["digests"][kind]["bytes"] == len(data)


def test_the_sampled_elements_exist_in_the_capture(corpus, tmp_path):
    run(corpus, tmp_path / "audit", "sampled", "--min-per-level", "1")
    campaign = audit.Campaign(tmp_path / "audit" / "sampled")
    for item in campaign.queue:
        payload = json.loads(
            Path(str(corpus / item["source_path"]) + ".elements.leaf.json").read_text())
        keys = {row["key"] for row in audit.project_elements(payload)}
        assert set(item["element_keys"]) <= keys
        assert len(item["element_keys"]) == campaign.manifest["element_sample"]


def test_the_manifest_carries_what_the_draw_depended_on(corpus, tmp_path):
    run(corpus, tmp_path / "audit", "manifested", "--min-per-level", "2",
        "--split", "train")
    manifest = audit.Campaign(tmp_path / "audit" / "manifested").manifest
    assert manifest["seed"] == 5
    assert manifest["filters"]["splits"] == ["train"]
    assert "publishable = 1" in manifest["filters"]["where"]
    assert manifest["rubric_ref"] == audit.rubric_ref(manifest["rubric"])
    assert manifest["population_total"] > 0


def test_a_split_filter_restricts_the_queue(corpus, tmp_path):
    run(corpus, tmp_path / "audit", "trainonly", "--split", "train",
        "--min-per-level", "0")
    campaign = audit.Campaign(tmp_path / "audit" / "trainonly")
    assert {row["stratum"]["split"] for row in campaign.queue} == {"train"}


def test_an_app_filter_matches_whole_names_not_substrings(corpus, tmp_path):
    run(corpus, tmp_path / "audit", "byapp", "--app", "eog", "--min-per-level", "0")
    campaign = audit.Campaign(tmp_path / "audit" / "byapp")
    assert campaign.queue
    for row in campaign.queue:
        assert "eog" in row["apps"]


def test_a_campaign_is_not_redrawn_over_itself(corpus, tmp_path):
    run(corpus, tmp_path / "audit", "once", "--min-per-level", "1")
    output = run(corpus, tmp_path / "audit", "once", "--min-per-level", "1", expect=1)
    assert "campaigns are frozen once drawn" in output


def test_a_campaign_name_that_could_escape_the_root_is_refused(corpus, tmp_path):
    output = run(corpus, tmp_path / "audit", "../escape", expect=1)
    assert "invalid campaign" in output


def test_a_rubric_file_is_used_and_copied_into_the_manifest(corpus, tmp_path):
    spec = json.loads(json.dumps(audit.DEFAULT_RUBRIC))
    spec["id"] = "custom_v9"
    spec["element_sample"] = 3
    path = tmp_path / "rubric.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    run(corpus, tmp_path / "audit", "custom", "--rubric", str(path),
        "--min-per-level", "0")
    campaign = audit.Campaign(tmp_path / "audit" / "custom")
    assert campaign.rubric["id"] == "custom_v9"
    assert campaign.manifest["element_sample"] == 3
    assert all(len(row["element_keys"]) == 3 for row in campaign.queue)
