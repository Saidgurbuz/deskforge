"""The campaign store: the rubric, the element draw, and the append-only log.

The failures worth pinning here are the quiet ones. A rubric that lets two
options share a hotkey produces a session where one key silently answers the
wrong thing. An element sampler that is not seeded produces a queue nobody can
reproduce. And a log whose last line was cut off mid-write either refuses to
load or - much worse - swallows the next record written after it, which is how a
resumed session loses answers it appears to have saved.
"""

from __future__ import annotations

import json

import pytest

from deskshot.inspector import audit


def _elements(count=12):
    kinds = ["Button", "Text", "Checkbox", "Window"]
    return [
        {
            "uid": "uid%02d" % index,
            "type": kinds[index % len(kinds)],
            "role": "push button",
            "name": "Element %d" % index,
            "app_name": "mousepad",
            "rect": {"x": index * 5, "y": index * 3, "w": 20, "h": 12},
            "visible_fragments": [{"x": index * 5, "y": index * 3, "w": 20, "h": 12}],
            "is_occluded": False,
            "_dom_index": index,
            "source": "app",
            "interaction": {"actionable": True},
            "attrs": {"states": ["visible"]},
        }
        for index in range(count)
    ]


# ------------------------------------------------------------------ rubric


def test_the_default_rubric_validates():
    assert audit.validate_rubric(audit.DEFAULT_RUBRIC) is audit.DEFAULT_RUBRIC


def test_a_rubric_that_reuses_a_hotkey_is_refused():
    spec = json.loads(json.dumps(audit.DEFAULT_RUBRIC))
    spec["questions"][0]["options"][1]["key"] = spec["questions"][0]["options"][0]["key"]
    with pytest.raises(audit.AuditError) as error:
        audit.validate_rubric(spec)
    assert "twice" in error.value.message


def test_a_rubric_needs_a_scope_and_options():
    with pytest.raises(audit.AuditError):
        audit.validate_rubric({"id": "x", "questions": [{"id": "q", "scope": "nowhere",
                                                         "options": [{"value": "a"}]}]})
    with pytest.raises(audit.AuditError):
        audit.validate_rubric({"id": "x", "questions": [{"id": "q", "scope": "screen"}]})


def test_the_rubric_reference_changes_when_a_question_changes():
    spec = json.loads(json.dumps(audit.DEFAULT_RUBRIC))
    before = audit.rubric_ref(spec)
    spec["questions"][0]["prompt"] += "?"
    assert audit.rubric_ref(spec) != before


# ------------------------------------------------------ element projection


def test_projection_keeps_the_published_fields_and_drops_the_bookkeeping():
    projected = audit.project_elements(_elements(2))
    assert projected[0]["type"] == "Button"
    assert projected[0]["key"] == "uid:uid00"
    assert "attrs" not in projected[0]
    assert "_dom_index" not in projected[0]
    assert "interaction" not in projected[0]


def test_an_element_with_no_uid_still_gets_a_key():
    elements = _elements(1)
    del elements[0]["uid"]
    assert audit.project_elements(elements)[0]["key"] == "dom:mousepad:0:app"


# ---------------------------------------------------------- element sample


def test_the_element_draw_is_reproducible_from_the_seed():
    elements = _elements(20)
    first = audit.sample_element_keys(elements, 8, "seed|key")
    second = audit.sample_element_keys(elements, 8, "seed|key")
    assert first == second
    assert audit.sample_element_keys(elements, 8, "other|key") != first


def test_the_element_draw_spreads_over_classes():
    """Uniform sampling would spend a whole screen's budget on Button."""
    elements = _elements(4) + [dict(row, uid="extra%02d" % index, type="Button",
                                    _dom_index=100 + index)
                               for index, row in enumerate(_elements(40))]
    keys = audit.sample_element_keys(elements, 4, "seed")
    by_key = {row["key"]: row for row in audit.project_elements(elements)}
    kinds = {by_key[key]["type"] for key in keys}
    assert len(kinds) == 4, kinds


def test_the_draw_stops_when_the_capture_runs_out_of_elements():
    assert len(audit.sample_element_keys(_elements(3), 8, "seed")) == 3


# ------------------------------------------------------------------- jsonl


def test_a_torn_trailing_line_is_dropped_not_fatal(tmp_path):
    path = tmp_path / "labels.jsonl"
    path.write_text('{"a": 1}\n{"b": 2}\n{"c": 3', encoding="utf-8")
    rows, torn = audit.read_jsonl(path)
    assert [row for row in rows] == [{"a": 1}, {"b": 2}]
    assert torn > 0


def test_appending_after_a_torn_line_does_not_glue_two_records(tmp_path):
    """The failure this exists for: a kill mid-write leaves no newline, and a
    naive append turns two records into one unparseable line - losing both."""
    path = tmp_path / "labels.jsonl"
    path.write_text('{"a": 1}\n{"b": 2}', encoding="utf-8")
    audit.append_jsonl(path, [{"c": 3}])
    rows, torn = audit.read_jsonl(path)
    assert rows == [{"a": 1}, {"b": 2}, {"c": 3}]
    assert torn == 0


def test_a_broken_line_in_the_middle_is_an_error(tmp_path):
    path = tmp_path / "labels.jsonl"
    path.write_text('{"a": 1}\nnot json\n{"b": 2}\n', encoding="utf-8")
    with pytest.raises(audit.AuditError):
        audit.read_jsonl(path)


# ---------------------------------------------------------------- campaign


@pytest.fixture()
def campaign(tmp_path):
    root = tmp_path / "audit" / "unit"
    root.mkdir(parents=True)
    made = audit.Campaign(root)
    items = [
        {
            "schema": audit.QUEUE_SCHEMA,
            "observation_key": "shard-0000__scene-%02d" % index,
            "source_path": "shards/shard-0000/st/aa/scene-%02d" % index,
            "width": 200, "height": 100, "n_elements": 12,
            "stratum": {"split": "train", "theme": "linux_classic"},
            "draw": "population" if index else "floor:split=val",
            "element_keys": ["uid:uid00", "uid:uid01"],
            "digests": {"leaf": {"sha256": "a" * 64, "bytes": 10}},
        }
        for index in range(3)
    ]
    made.write_queue(items)
    made.write_manifest({
        "schema": audit.SCHEMA, "name": "unit", "seed": 1, "items": len(items),
        "element_sample": 2, "rubric": audit.DEFAULT_RUBRIC,
        "rubric_ref": audit.rubric_ref(audit.DEFAULT_RUBRIC),
        "stratum_fields": ["split", "theme"], "corpus": str(tmp_path / "corpus"),
    })
    return made


def _answer(campaign, index, **payload):
    row = audit.build_label_row(campaign, index, payload, "said")
    return campaign.append_label(row)


def test_a_queue_is_frozen_once_written(campaign):
    with pytest.raises(audit.AuditError):
        campaign.write_queue([])


def test_an_answer_round_trips_through_a_fresh_campaign_object(campaign):
    _answer(campaign, 0, scope="screen", question="screen_usable", value="good")
    again = audit.Campaign(campaign.root)
    state = again.item_state(0, "said")
    assert state["screen"]["screen_usable"]["value"] == "good"


def test_the_last_answer_to_a_question_wins(campaign):
    _answer(campaign, 0, scope="screen", question="screen_usable", value="good")
    _answer(campaign, 0, scope="screen", question="screen_usable", value="unusable")
    state = campaign.item_state(0, "said")
    assert state["screen"]["screen_usable"]["value"] == "unusable"
    # Both are still on disk: a revision is provenance, not a deletion.
    rows = [row for row in campaign.labels() if row.get("question") == "screen_usable"]
    assert len(rows) == 2


def test_the_same_event_id_is_stored_once(campaign):
    first = _answer(campaign, 0, scope="screen", question="screen_usable",
                    value="good", event_id="fixed")
    second = _answer(campaign, 0, scope="screen", question="screen_usable",
                     value="good", event_id="fixed")
    assert first["stored"] and not second["stored"] and second["duplicate"]


def test_two_raters_can_answer_the_same_item(campaign):
    _answer(campaign, 0, scope="screen", question="screen_usable", value="good")
    other = audit.build_label_row(
        campaign, 0, {"scope": "screen", "question": "screen_usable", "value": "partial"},
        "other")
    campaign.append_label(other)
    assert campaign.item_state(0, "said")["screen"]["screen_usable"]["value"] == "good"
    assert campaign.item_state(0, "other")["screen"]["screen_usable"]["value"] == "partial"


def test_an_element_answer_records_whether_it_was_sampled(campaign):
    _answer(campaign, 0, scope="element", values=["ok"], element_key="uid:uid00")
    _answer(campaign, 0, scope="element", values=["phantom"], element_key="uid:elsewhere")
    rows = [row for row in campaign.labels() if row["scope"] == "element"]
    assert [row["sampled"] for row in rows] == [True, False]


def test_ok_cannot_be_combined_with_an_error_flag(campaign):
    with pytest.raises(audit.AuditError) as error:
        _answer(campaign, 0, scope="element", values=["ok", "phantom"],
                element_key="uid:uid00")
    assert "cannot be combined" in error.value.message


def test_two_error_flags_can_be_combined(campaign):
    """An element can be both misclassified and mislocalized, and collapsing
    that to one verdict is what loses the per-class error."""
    _answer(campaign, 0, scope="element", values=["wrong_class", "bad_geometry"],
            element_key="uid:uid00")
    answer = campaign.item_state(0, "said")["elements"]["uid:uid00"]
    assert answer["values"] == ["wrong_class", "bad_geometry"]


def test_a_value_outside_the_rubric_is_refused(campaign):
    with pytest.raises(audit.AuditError):
        _answer(campaign, 0, scope="screen", question="screen_usable", value="excellent")
    with pytest.raises(audit.AuditError):
        _answer(campaign, 0, scope="screen", question="not_a_question", value="good")


def test_a_missed_point_outside_the_screen_is_refused(campaign):
    with pytest.raises(audit.AuditError):
        _answer(campaign, 0, scope="missed", points=[{"x": 5000, "y": 5}])


def test_missed_points_are_rounded_and_counted(campaign):
    _answer(campaign, 0, scope="missed",
            points=[{"x": 10.6, "y": 20.2}, {"x": 1, "y": 1, "inside_key": "uid:uid00"}])
    row = [row for row in campaign.labels() if row["scope"] == "missed"][-1]
    assert row["n_points"] == 2
    assert row["points"][0] == {"x": 11, "y": 20}
    assert row["points"][1]["inside_key"] == "uid:uid00"


def test_a_skip_needs_a_reason(campaign):
    with pytest.raises(audit.AuditError):
        _answer(campaign, 0, scope="item", status="skipped")
    _answer(campaign, 0, scope="item", status="skipped", reason="the app never started")


def test_progress_counts_done_and_points_at_the_next_item(campaign):
    _answer(campaign, 0, scope="item", status="done")
    _answer(campaign, 2, scope="item", status="skipped", reason="black screen")
    progress = campaign.progress("said")
    assert progress["done"] == 1 and progress["skipped"] == 1
    assert progress["next"] == 1
    assert progress["done_indices"] == [0] and progress["skipped_indices"] == [2]


def test_reopening_an_item_takes_it_out_of_done(campaign):
    _answer(campaign, 1, scope="item", status="done")
    _answer(campaign, 1, scope="item", status="skipped", reason="changed my mind")
    progress = campaign.progress("said")
    assert progress["done"] == 0 and progress["skipped_indices"] == [1]


def test_an_item_outside_the_queue_is_refused(campaign):
    with pytest.raises(audit.AuditError):
        campaign.item(99)
    with pytest.raises(audit.AuditError):
        campaign.index_of("shard-0000__scene-nope")


def test_campaigns_are_listed_with_their_progress(campaign):
    _answer(campaign, 0, scope="item", status="done")
    listed = audit.list_campaigns(campaign.root.parent)
    assert [entry["name"] for entry in listed] == ["unit"]
    assert listed[0]["progress"]["done"] == 1


def test_a_name_that_could_escape_a_path_is_refused():
    for bad in ("../elsewhere", "with/slash", ".hidden", "", "a" * 200):
        with pytest.raises(audit.AuditError):
            audit.safe_name(bad, "campaign")


def test_a_campaign_records_its_mode(campaign):
    assert campaign.mode == audit.LEGACY_MODE
    campaign.write_manifest(dict(campaign.manifest, mode="sweep"))
    assert audit.Campaign(campaign.root).mode == "sweep"


def test_a_manifest_with_no_mode_is_the_one_it_was_worked_in(campaign):
    """A queue drawn before sweeping existed was answered in sample mode. A
    change of default must not reinterpret finished work."""
    assert "mode" not in campaign.manifest
    assert campaign.mode == "sample"
    assert audit.DEFAULT_MODE == "sweep"


def test_an_unknown_mode_falls_back_rather_than_crashing(campaign):
    campaign.write_manifest(dict(campaign.manifest, mode="telepathy"))
    assert audit.Campaign(campaign.root).mode == audit.LEGACY_MODE


def test_a_sweep_row_must_account_for_every_element(campaign):
    with pytest.raises(audit.AuditError) as error:
        _answer(campaign, 0, scope="sweep", n_elements=10, flagged=[],
                census={"Button": 3})
    assert "census sums to 3" in error.value.message


def test_a_sweep_derives_what_it_declared_correct(campaign):
    _answer(campaign, 0, scope="sweep", n_elements=10, flagged=["uid:uid00"],
            census={"Button": 6, "Text": 4})
    row = [row for row in campaign.labels() if row["scope"] == "sweep"][-1]
    assert row["declared_correct"] == 9
    assert campaign.item_state(0, "said")["sweep"]["n_elements"] == 10


def test_an_element_row_records_how_it_came_to_be_judged(campaign):
    _answer(campaign, 0, scope="element", values=["ok"], element_key="uid:uid00")
    _answer(campaign, 0, scope="element", values=["phantom"],
            element_key="uid:elsewhere", source="sweep")
    rows = [row for row in campaign.labels() if row["scope"] == "element"]
    assert [row["source"] for row in rows] == ["sampled", "sweep"]


def test_a_swept_screen_counts_as_finished(campaign):
    """Committing a sweep asserts that every element on the screen carries a
    verdict, which is what finishing it means. It also survives the bug that
    put three item-done rows on the wrong item in a 200-screen campaign."""
    _answer(campaign, 1, scope="sweep", n_elements=4, flagged=[],
            census={"Button": 4})
    progress = campaign.progress("said")
    assert progress["done"] == 1
    assert progress["done_indices"] == [1]
    assert progress["next"] == 0


def test_skipping_a_screen_beats_having_swept_it(campaign):
    _answer(campaign, 1, scope="sweep", n_elements=4, flagged=[],
            census={"Button": 4})
    _answer(campaign, 1, scope="item", status="skipped", reason="black screen")
    progress = campaign.progress("said")
    assert progress["done"] == 0
    assert progress["skipped_indices"] == [1]


def test_a_done_row_for_another_item_does_not_finish_this_one(campaign):
    """The shape of the misattribution: the row exists but names item 2."""
    _answer(campaign, 2, scope="item", status="done")
    assert campaign.progress("said")["done_indices"] == [2]
