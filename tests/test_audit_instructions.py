"""The instruction audit: the grounding click, and what it is scored against.

The dense audit asks whether every element on a screen is annotated correctly.
This asks whether one action sample is sound supervision, and its first question
is the only one the machine can answer for itself - a rater shown the
instruction with no box either lands on the recorded target or does not. So the
scoring is what needs pinning: it happens on the server, in the frame the target
was recorded in, and a click a pixel outside the box is a miss.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from audit_fixtures import build_corpus, elements
from deskshot.inspector import audit
from deskshot.inspector.app import InspectorApp

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN = "instr"


def _stats_module():
    spec = importlib.util.spec_from_file_location(
        "audit_stats", PROJECT_ROOT / "scripts" / "audit_stats.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stats_module = _stats_module()

#: The fixture's window frame is 320x200; this box is one of its widgets.
BOX = [10, 10, 34, 28]
POINT = [22, 19]


@pytest.fixture()
def campaign(tmp_path):
    corpus = build_corpus(tmp_path / "corpus", captures=3)
    root = tmp_path / "audit" / CAMPAIGN
    root.mkdir(parents=True)
    made = audit.Campaign(root)
    payload = elements(12)
    keys = [row["key"] for row in audit.project_elements(payload)]
    items = []
    for index in range(3):
        source = "shards/shard-%04d/%s/%016x/scene-%016x-step%02d" % (
            index % 3, "ep" if index % 2 else "st", 0xa0000 + index,
            0xa0000 + index, index % 3)
        items.append({
            "schema": audit.QUEUE_SCHEMA,
            "kind": "instruction",
            "sample_id": "s%02d" % index,
            "transition_id": "t%02d" % index,
            "observation_key": "shard-%04d__scene-%016x-step%02d" % (
                index % 3, 0xa0000 + index, index % 3),
            "source_path": source,
            "after_source_path": source,
            "width": 320, "height": 200, "n_elements": len(payload),
            "instruction": "Click the widget labelled W%d." % index,
            "style": ["standard", "detailed_contextual"][index % 2],
            "apps": ["mousepad"],
            "draw": "population",
            "stratum": {"split": "train", "style": ["standard", "detailed_contextual"][index % 2],
                        "target_kind": "button", "app_class": "common_app",
                        "target_size": "widget"},
            "target": {
                "uid": keys[1].split(":")[-1],
                "role": "push button", "kind": "button", "text": "W1",
                "app": "mousepad",
                "bbox_px": list(BOX),
                "point_px": list(POINT),
                "visible_fragments": [{"x": 10, "y": 10, "w": 24, "h": 18}],
                "area_share": 0.00675,
            },
            "effect": {"changed": True},
            "digests": {"leaf": {"sha256": "a" * 64, "bytes": 1}},
        })
    made.write_queue(items)
    made.write_manifest({
        "schema": audit.SCHEMA, "name": CAMPAIGN, "kind": "instruction",
        "mode": "sample", "created_at": audit.now_stamp(), "seed": 1,
        "items": len(items), "corpus": str(corpus), "release": None,
        "stratum_fields": ["split", "style", "target_kind", "app_class", "target_size"],
        "rubric": audit.INSTRUCTION_RUBRIC,
        "rubric_ref": audit.rubric_ref(audit.INSTRUCTION_RUBRIC),
        "population_total": 1000, "population_items": len(items),
    })
    return made


def ground(campaign, index, x, y, **extra):
    payload = dict(extra, scope="ground", point={"x": x, "y": y})
    row = audit.build_label_row(campaign, index, payload, "said")
    campaign.append_label(row)
    return row


def screen(campaign, index, question, value):
    row = audit.build_label_row(
        campaign, index, {"scope": "screen", "question": question, "value": value}, "said")
    campaign.append_label(row)


# ------------------------------------------------------------------- scoring


def test_a_click_in_the_box_is_scored_a_hit(campaign):
    row = ground(campaign, 0, 22, 19)
    assert row["in_bbox"] is True
    assert row["in_fragment"] is True
    assert row["distance_px"] == 0.0


def test_a_click_one_pixel_outside_the_box_is_a_miss(campaign):
    row = ground(campaign, 0, BOX[2] + 1, 19)
    assert row["in_bbox"] is False
    assert row["distance_px"] == pytest.approx(13.0, abs=0.1)


def test_the_edge_of_the_box_counts_as_inside(campaign):
    assert ground(campaign, 0, BOX[0], BOX[1])["in_bbox"] is True
    assert ground(campaign, 0, BOX[2], BOX[3])["in_bbox"] is True


def test_a_click_in_the_box_but_not_on_a_visible_part(campaign):
    """The box is amodal; the visible fragments are what a person can see. A
    click inside a covered part of the target is a different thing from a hit."""
    item = campaign.item(0)
    item["target"]["visible_fragments"] = [{"x": 10, "y": 10, "w": 6, "h": 18}]
    row = ground(campaign, 0, 30, 19)
    assert row["in_bbox"] is True
    assert row["in_fragment"] is False


def test_giving_up_is_its_own_outcome(campaign):
    row = ground(campaign, 0, 0, 0, gave_up=True)
    assert row["gave_up"] is True
    assert "in_bbox" not in row and "distance_px" not in row


def test_a_click_outside_the_screen_is_refused(campaign):
    with pytest.raises(audit.AuditError):
        ground(campaign, 0, 5000, 5)


def test_a_grounding_click_needs_a_point(campaign):
    with pytest.raises(audit.AuditError):
        audit.build_label_row(campaign, 0, {"scope": "ground"}, "said")


def test_the_campaign_knows_it_is_an_instruction_campaign(campaign):
    assert campaign.kind == "instruction"
    assert campaign.rubric["id"] == "instruction_v1"
    assert [q["id"] for q in campaign.rubric["questions"]][:2] == \
        ["instruction_ok", "target_match"]


def test_the_instruction_rubric_is_all_screen_scope(campaign):
    """There is one target per item, so nothing here is an element question."""
    assert {q["scope"] for q in campaign.rubric["questions"]} == {"screen"}


# ------------------------------------------------------------------- serving


@pytest.fixture()
def app(campaign, tmp_path):
    (tmp_path / "runs").mkdir(exist_ok=True)
    return InspectorApp(
        root=tmp_path / "runs", golden_root=tmp_path / "golden",
        cache_dir=tmp_path / "cache", project_root=tmp_path,
        corpus_root=Path(campaign.manifest["corpus"]),
        audit_root=campaign.root.parent, rater="said")


def _get(app, path, **params):
    return app.handle("GET", path, {k: [str(v)] for k, v in params.items()}, None,
                      {"accept-encoding": ""})


def test_the_queue_says_it_is_an_instruction_campaign(app):
    body = _get(app, "/api/audit/queue", campaign=CAMPAIGN).json()
    assert body["kind"] == "instruction"
    assert body["rubric"]["id"] == "instruction_v1"


def test_an_item_carries_the_instruction_and_the_target(app):
    body = _get(app, "/api/audit/item", campaign=CAMPAIGN, index=0).json()
    assert body["kind"] == "instruction"
    assert body["item"]["instruction"].startswith("Click the widget")
    assert body["item"]["target"]["bbox_px"] == BOX
    assert body["item"]["target"]["point_px"] == POINT


def test_the_after_screen_is_servable(app):
    before = _get(app, "/api/audit/image", campaign=CAMPAIGN, index=0, max=64)
    after = _get(app, "/api/audit/image", campaign=CAMPAIGN, index=0, max=64,
                 which="after")
    assert before.status == 200 and after.status == 200


def test_an_unknown_which_is_refused(app):
    assert _get(app, "/api/audit/image", campaign=CAMPAIGN, index=0,
                which="sideways").status == 400


def test_a_grounding_click_round_trips_through_http(app):
    response = app.handle("POST", "/api/audit/label", {}, json.dumps({
        "campaign": CAMPAIGN, "index": 0, "scope": "ground",
        "point": {"x": 22, "y": 19},
    }).encode("utf-8"), {})
    assert response.status == 200
    state = response.json()["state"]
    assert state["ground"]["in_bbox"] is True


# ---------------------------------------------------------------- statistics


def _report(campaign):
    return stats_module.Report(campaign).build()


def test_the_grounding_rate_is_reported(campaign):
    ground(campaign, 0, 22, 19)              # hit
    ground(campaign, 1, 300, 190)            # miss
    ground(campaign, 2, 0, 0, gave_up=True)  # gave up
    built = _report(campaign)["instruction"]["grounding"]["queue_sample"]
    assert built["shown"] == 3
    assert built["attempted"] == 2
    assert built["hit"]["k"] == 1
    assert built["hit"]["rate"] == pytest.approx(0.5)
    assert built["gave_up"]["rate"] == pytest.approx(1 / 3)
    assert built["misses"] == 1
    assert built["median_miss_distance_px"] > 0


def test_a_hit_does_not_contribute_to_the_miss_distance(campaign):
    """The distance on a hit measures where inside a box somebody clicked,
    which is not a quality signal."""
    ground(campaign, 0, 22, 19)
    built = _report(campaign)["instruction"]["grounding"]["queue_sample"]
    assert built["misses"] == 0
    assert built["median_miss_distance_px"] is None


def test_grounding_is_broken_down_by_style_and_target_size(campaign):
    ground(campaign, 0, 22, 19)
    ground(campaign, 1, 300, 190)
    built = _report(campaign)["instruction"]["grounding"]["queue_sample"]
    assert set(built["by_style"]) == {"standard", "detailed_contextual"}
    assert built["by_target_size"]["widget"]["n"] == 2


def test_a_sample_is_sound_only_when_all_four_hold(campaign):
    for question, value in (("instruction_ok", "good"), ("target_match", "match"),
                            ("unique", "one"), ("target_visible", "visible")):
        screen(campaign, 0, question, value)
    for question, value in (("instruction_ok", "good"), ("target_match", "match"),
                            ("unique", "several"), ("target_visible", "visible")):
        screen(campaign, 1, question, value)
    built = _report(campaign)["instruction"]["quality"]["queue_sample"]
    assert built["fully_answered"] == 2
    assert built["sound"]["k"] == 1
    assert built["why_not"] == {"unique=several": 1}


def test_a_partly_answered_sample_is_not_counted_either_way(campaign):
    screen(campaign, 0, "instruction_ok", "good")
    built = _report(campaign)["instruction"]["quality"]["queue_sample"]
    assert built["fully_answered"] == 0
    assert built["sound"]["n"] == 0


def test_a_sample_can_fail_for_more_than_one_reason(campaign):
    for question, value in (("instruction_ok", "vague"), ("target_match", "too_big"),
                            ("unique", "one"), ("target_visible", "visible")):
        screen(campaign, 0, question, value)
    built = _report(campaign)["instruction"]["quality"]["queue_sample"]
    assert built["sound"]["k"] == 0
    assert built["why_not"] == {"instruction_ok=vague": 1, "target_match=too_big": 1}


def test_the_report_leads_with_the_grounding_and_skips_the_dense_sections(campaign):
    ground(campaign, 0, 22, 19)
    for question, value in (("instruction_ok", "good"), ("target_match", "match"),
                            ("unique", "one"), ("target_visible", "visible")):
        screen(campaign, 0, question, value)
    text = stats_module.render(_report(campaign))
    assert "Could a person act on the instruction?" in text
    assert "Is the sample sound supervision?" in text
    assert "What the annotation missed" not in text
    assert "Swept element correctness" not in text


def test_an_observation_campaign_has_no_instruction_block(tmp_path):
    corpus = build_corpus(tmp_path / "corpus", captures=2)
    root = tmp_path / "audit" / "obs"
    root.mkdir(parents=True)
    made = audit.Campaign(root)
    made.write_queue([{
        "schema": audit.QUEUE_SCHEMA, "observation_key": "k", "source_path": "x",
        "width": 320, "height": 200, "n_elements": 5, "draw": "population",
        "stratum": {"split": "train"}, "element_keys": [], "digests": {},
    }])
    made.write_manifest({
        "schema": audit.SCHEMA, "name": "obs", "kind": "observation",
        "mode": "sweep", "seed": 1, "items": 1, "corpus": str(corpus),
        "stratum_fields": ["split"], "rubric": audit.DEFAULT_RUBRIC,
        "rubric_ref": audit.rubric_ref(audit.DEFAULT_RUBRIC),
    })
    audit.build_label_row(made, 0, {"scope": "item", "status": "done"}, "said")
    made.append_label(audit.build_label_row(
        made, 0, {"scope": "item", "status": "done"}, "said"))
    assert _report(made)["instruction"] is None


# ------------------------------------------------------------------- builder


def test_the_builder_refuses_a_sample_dir_with_misaligned_sources(tmp_path):
    """`<split>.sources.txt` is line-aligned with `<split>.jsonl`; if one was
    regenerated without the other the pairing is silently wrong."""
    corpus = build_corpus(tmp_path / "corpus", captures=2)
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "train.jsonl").write_text(
        "\n".join(json.dumps({"sample_id": "a%d" % n, "transition_id": "t%d" % n,
                              "observation_key": "k%d" % n, "split": "train",
                              "style": "standard", "instruction": "click",
                              "point": [0.5, 0.5], "target_role": "push button",
                              "target_app": "mousepad"})
                  for n in range(3)) + "\n", encoding="utf-8")
    (samples / "train.sources.txt").write_text("only/one/line\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "build_instruction_queue.py"),
         "--campaign", "bad", "--samples", str(samples), "--corpus", str(corpus),
         "--audit-root", str(tmp_path / "audit"), "--items", "2", "--dry-run"],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=120)
    assert result.returncode != 0
    assert "not aligned" in result.stdout + result.stderr


# ------------------------------------------------------------------- accept


def test_the_rubric_says_what_nothing_wrong_means(campaign):
    """The fast path is read off the rubric, not hardcoded, so a changed
    question changes what accepting a sample asserts."""
    accepts = audit.accept_answers(campaign.rubric)
    assert [(row["question"], row["value"]) for row in accepts] == [
        ("instruction_ok", "good"), ("target_match", "match"),
        ("unique", "one"), ("target_visible", "visible"), ("effect", "match")]


def test_a_rubric_with_no_accept_option_yields_nothing(campaign):
    spec = json.loads(json.dumps(campaign.rubric))
    for question in spec["questions"]:
        for option in question["options"]:
            option.pop("accept", None)
    assert audit.accept_answers(spec) == []


def test_accepting_a_sample_is_one_request(app, campaign):
    answers = [dict(row, scope="screen", accepted=True)
               for row in audit.accept_answers(campaign.rubric)]
    answers.append({"scope": "item", "status": "done"})
    response = app.handle("POST", "/api/audit/label", {}, json.dumps({
        "campaign": CAMPAIGN, "index": 0, "answers": answers,
    }).encode("utf-8"), {})
    assert response.status == 200
    body = response.json()
    assert body["written"] == 6
    assert len(body["state"]["screen"]) == 5
    assert body["state"]["item"]["status"] == "done"
    assert body["progress"]["done"] == 1


def test_an_accepted_answer_records_that_it_was_accepted(campaign):
    """One keystroke accepting the whole sample is a different act from
    answering five questions one at a time, even though the answers match."""
    row = audit.build_label_row(campaign, 0, {
        "scope": "screen", "question": "unique", "value": "one", "accepted": True,
    }, "said")
    assert row["accepted"] is True
    plain = audit.build_label_row(campaign, 0, {
        "scope": "screen", "question": "unique", "value": "one"}, "said")
    assert "accepted" not in plain


def test_a_batch_is_validated_before_anything_is_written(app):
    """Half an item answered is worse than an error."""
    response = app.handle("POST", "/api/audit/label", {}, json.dumps({
        "campaign": CAMPAIGN, "index": 0, "answers": [
            {"scope": "screen", "question": "unique", "value": "one"},
            {"scope": "screen", "question": "unique", "value": "impossible"},
        ],
    }).encode("utf-8"), {})
    assert response.status == 400
    state = _get(app, "/api/audit/state", campaign=CAMPAIGN, indices="0").json()
    assert state["states"]["0"]["screen"] == {}


def test_a_batch_cannot_answer_a_different_item(app):
    response = app.handle("POST", "/api/audit/label", {}, json.dumps({
        "campaign": CAMPAIGN, "index": 0, "answers": [
            {"scope": "screen", "question": "unique", "value": "one", "index": 1},
        ],
    }).encode("utf-8"), {})
    assert response.status == 400


def test_an_empty_batch_is_refused(app):
    response = app.handle("POST", "/api/audit/label", {}, json.dumps({
        "campaign": CAMPAIGN, "index": 0, "answers": [],
    }).encode("utf-8"), {})
    assert response.status == 400


def test_a_repeated_batch_stores_nothing_twice(app, campaign):
    answers = [dict(row, scope="screen", accepted=True, event_id="fixed-%d" % n)
               for n, row in enumerate(audit.accept_answers(campaign.rubric))]
    body = json.dumps({"campaign": CAMPAIGN, "index": 0, "answers": answers}).encode()
    first = app.handle("POST", "/api/audit/label", {}, body, {}).json()
    second = app.handle("POST", "/api/audit/label", {}, body, {}).json()
    assert first["written"] == 5 and second["written"] == 0
    assert second["duplicate"] == 5


def test_accepted_samples_count_as_sound(campaign):
    for row in audit.accept_answers(campaign.rubric):
        screen(campaign, 0, row["question"], row["value"])
    built = _report(campaign)["instruction"]["quality"]["queue_sample"]
    assert built["fully_answered"] == 1
    assert built["sound"]["k"] == 1
    assert built["why_not"] == {}
