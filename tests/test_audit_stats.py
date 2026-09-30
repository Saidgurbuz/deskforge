"""The statistics, against labels whose answer is known by hand.

Every number an audit reports is a number somebody will quote, so each one here
is checked against an arithmetic expectation rather than a golden file: a
regression that shifts a denominator by one is invisible in a snapshot and
obvious in a fraction.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from deskshot.inspector import audit

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _module():
    spec = importlib.util.spec_from_file_location(
        "audit_stats", PROJECT_ROOT / "scripts" / "audit_stats.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stats_module = _module()

#: Four items; the first two are the uniform draw, the third and fourth were
#: added to floor a stratum, so they must stay out of the population estimate.
ITEMS = [
    {"key": "shard-0000__scene-a", "n_elements": 10, "draw": "population",
     "split": "train", "theme": "linux_classic"},
    {"key": "shard-0000__scene-b", "n_elements": 20, "draw": "population",
     "split": "train", "theme": "linux_classic"},
    {"key": "shard-0001__scene-c", "n_elements": 30, "draw": "floor:split=val",
     "split": "val", "theme": "quartz_night"},
    {"key": "shard-0001__scene-d", "n_elements": 40, "draw": "floor:theme=nord",
     "split": "test_app", "theme": "quartz_night_nord"},
]


@pytest.fixture()
def campaign(tmp_path):
    root = tmp_path / "audit" / "arith"
    root.mkdir(parents=True)
    made = audit.Campaign(root)
    made.write_queue([
        {
            "schema": audit.QUEUE_SCHEMA,
            "observation_key": row["key"],
            "source_path": "shards/x/st/y/%s" % row["key"],
            "width": 400, "height": 300,
            "n_elements": row["n_elements"], "n_windows": 1, "occluded_ratio": 0.2,
            "apps": ["mousepad"], "draw": row["draw"],
            "flags": {"publishable": True, "train_eligible": True,
                      "near_duplicate": False, "no_op_frame": False},
            "stratum": {"split": row["split"], "theme": row["theme"],
                        "resolution": "fhd_1920x1080", "density": "sparse",
                        "occlusion": "mid", "group": "st", "app_class": "common_app"},
            "element_keys": ["uid:%s%d" % (row["key"][-1], n) for n in range(4)],
            "digests": {"leaf": {"sha256": "a" * 64, "bytes": 1}},
        }
        for row in ITEMS
    ])
    made.write_manifest({
        "schema": audit.SCHEMA, "name": "arith", "seed": 1, "items": len(ITEMS),
        "element_sample": 4, "corpus": "/nowhere", "release": None,
        "stratum_fields": ["split", "theme"],
        "rubric": audit.DEFAULT_RUBRIC,
        "rubric_ref": audit.rubric_ref(audit.DEFAULT_RUBRIC),
        "population_total": 1000, "population_items": 2,
    })
    return made


def answer(campaign, index, rater="ann", **payload):
    row = audit.build_label_row(campaign, index, payload, rater)
    campaign.append_label(row)


def _element(campaign, index, position, values, rater="ann", kind="Button",
             source=None):
    key = campaign.item(index)["element_keys"][position]
    payload = {"scope": "element", "values": values, "element_key": key,
               "element_type": kind, "element_app": "mousepad"}
    if source:
        payload["source"] = source
    answer(campaign, index, rater=rater, **payload)


@pytest.fixture()
def labelled(campaign):
    """Ten judged elements: 7 correct, 1 phantom, 2 wrong class, 1 box wrong.

    One of the wrong ones carries two flags at once, which is the case a single
    verdict per element would lose.
    """
    _element(campaign, 0, 0, ["ok"])
    _element(campaign, 0, 1, ["ok"])
    _element(campaign, 0, 2, ["phantom"], kind="Text")
    _element(campaign, 0, 3, ["wrong_class", "bad_geometry"])
    _element(campaign, 1, 0, ["ok"])
    _element(campaign, 1, 1, ["ok"])
    _element(campaign, 1, 2, ["ok"], kind="Text")
    _element(campaign, 1, 3, ["wrong_class"], kind="Text")
    _element(campaign, 2, 0, ["ok"])
    _element(campaign, 2, 1, ["ok"])
    _element(campaign, 2, 2, ["unsure"])
    # The blind pass on the two population items: one real miss, one slip.
    answer(campaign, 0, scope="missed",
           points=[{"x": 5, "y": 5}, {"x": 9, "y": 9, "inside_key": "uid:a0"}])
    answer(campaign, 1, scope="missed", points=[])
    for question, value in (("screen_usable", "good"),
                            ("screen_missing_region", "no"),
                            ("screen_occlusion", "ok")):
        answer(campaign, 0, scope="screen", question=question, value=value)
    answer(campaign, 1, scope="screen", question="screen_usable", value="partial")
    answer(campaign, 0, scope="item", status="done")
    answer(campaign, 3, scope="item", status="skipped", reason="the app never started")
    return campaign


def report(campaign, **kwargs):
    return stats_module.Report(campaign, **kwargs).build()


# ---------------------------------------------------------------- elements


def test_precision_counts_flags_not_rows(labelled):
    summary = report(labelled)["elements"]["queue_sample"]
    assert summary["judged"] == 10
    assert summary["unsure"] == 1
    assert summary["correct"] == 7
    assert summary["wrong"] == 3
    assert summary["precision"]["rate"] == pytest.approx(0.7)
    assert summary["flags"]["phantom"]["k"] == 1
    assert summary["flags"]["wrong_class"]["k"] == 2
    assert summary["flags"]["bad_geometry"]["k"] == 1


def test_identified_and_localized_are_reported_separately(labelled):
    """"Is this the right thing" and "is this the right box" fail for different
    reasons; one accuracy number hides which."""
    summary = report(labelled)["elements"]["queue_sample"]
    assert summary["identified"]["rate"] == pytest.approx(0.7)   # 10 - 1 - 2
    assert summary["localized"]["rate"] == pytest.approx(0.8)    # 10 - 1 - 1


def test_the_population_estimate_leaves_out_the_stratum_floors(labelled):
    """Items added to guarantee a slice are not a uniform sample of anything."""
    population = report(labelled)["elements"]["population_sample"]
    assert population["judged"] == 8
    assert population["correct"] == 5
    assert population["precision"]["rate"] == pytest.approx(5 / 8)


def test_a_confidence_interval_stays_inside_zero_and_one(labelled):
    summary = report(labelled)["elements"]["queue_sample"]
    low, high = summary["flags"]["phantom"]["ci95"]
    assert 0.0 <= low <= high <= 1.0
    assert high > 0.0


def test_the_last_answer_to_an_element_wins(labelled):
    _element(labelled, 1, 0, ["phantom"])
    summary = report(labelled)["elements"]["queue_sample"]
    assert summary["judged"] == 10
    assert summary["correct"] == 6
    assert summary["flags"]["phantom"]["k"] == 2


def test_a_second_rater_does_not_double_the_denominator(labelled):
    """A double-rated element is one element. Pooling both ratings would shrink
    every interval on the strength of no extra evidence."""
    for position in range(4):
        _element(labelled, 0, position, ["ok"], rater="bob")
    built = report(labelled)
    assert built["primary_rater"] == "ann"
    assert built["elements"]["queue_sample"]["judged"] == 10
    assert sorted(built["raters"]) == ["ann", "bob"]


def test_the_primary_rater_can_be_chosen(labelled):
    for position in range(4):
        _element(labelled, 0, position, ["phantom"], rater="bob")
    built = report(labelled, primary="bob")
    assert built["elements"]["queue_sample"]["judged"] == 4
    assert built["elements"]["queue_sample"]["flags"]["phantom"]["k"] == 4


def test_breakdowns_split_by_class_and_by_stratum(labelled):
    built = report(labelled)["elements"]
    assert built["by_class"]["Text"]["judged"] == 3
    assert built["by_class"]["Text"]["flags"]["phantom"]["k"] == 1
    assert built["by_class"]["Button"]["judged"] == 7
    assert built["by_split"]["train"]["judged"] == 8
    assert built["by_split"]["val"]["judged"] == 2
    assert built["by_theme"]["quartz_night"]["judged"] == 2


# ------------------------------------------------------------------ recall


def test_a_click_that_landed_on_a_box_is_not_a_miss(labelled):
    missed = report(labelled)["missed"]["queue_sample"]
    assert missed["screens_checked"] == 2
    assert missed["missing_elements_found"] == 1
    assert missed["clicks_that_hit_an_existing_box"] == 1
    assert missed["screens_with_at_least_one_miss"]["k"] == 1


def test_recall_is_annotated_over_annotated_plus_found(labelled):
    recall = report(labelled)["recall"]["queue_sample"]
    assert recall["annotated_elements"] == 30       # items 0 and 1
    assert recall["human_found_unannotated"] == 1
    assert recall["recall_upper_bound"]["rate"] == pytest.approx(30 / 31)
    assert "bounds recall from above" in recall["note"]


def test_f1_is_the_harmonic_mean_of_the_two(labelled):
    built = report(labelled)
    precision = built["elements"]["queue_sample"]["precision"]["rate"]
    recall = built["recall"]["queue_sample"]["recall_upper_bound"]["rate"]
    assert built["f1"]["queue_sample"] == pytest.approx(
        2 * precision * recall / (precision + recall))


# ------------------------------------------------------------------ screens


def test_screen_answers_are_tallied_per_question(labelled):
    screens = report(labelled)["screens"]["queue_sample"]
    assert screens["screen_usable"]["counts"] == {"good": 1, "partial": 1}
    assert screens["screen_usable"]["shares"]["good"] == pytest.approx(0.5)
    assert screens["screen_occlusion"]["answered"] == 1


# ---------------------------------------------------------------- agreement


def test_agreement_and_kappa_come_from_the_shared_units(labelled):
    _element(labelled, 0, 0, ["ok"], rater="bob")
    _element(labelled, 0, 1, ["phantom"], rater="bob")
    _element(labelled, 0, 2, ["phantom"], rater="bob")
    _element(labelled, 0, 3, ["wrong_class", "bad_geometry"], rater="bob")
    pair = report(labelled)["agreement"]["pairs"]["ann|bob"]["element"]
    assert pair["n"] == 4
    assert pair["agreement"] == pytest.approx(0.75)
    assert pair["kappa"] is not None


def test_one_rater_reports_no_agreement_rather_than_a_number(labelled):
    agreement = report(labelled)["agreement"]
    assert agreement["pairs"] == {}
    assert "nothing to compare" in agreement["note"]


# ------------------------------------------------------------------ session


def test_the_session_reports_skips_with_their_reasons(labelled):
    session = report(labelled)["session"]
    assert session["items_done"] == 1
    assert session["items_skipped"] == 1
    assert session["skip_reasons"] == {"the app never started": 1}
    assert session["raters"]["ann"] > 0


def test_coverage_shows_the_queue_against_what_is_finished(labelled):
    coverage = report(labelled)["session"]["coverage"]
    assert coverage["split"]["queue"] == {"train": 2, "val": 1, "test_app": 1}
    assert coverage["split"]["done"] == {"train": 1}
def test_the_markdown_names_the_numbers_it_reports(labelled):
    text = stats_module.render(report(labelled))
    assert "# Human annotation audit — arith" in text
    assert "correct (identified and localized)" in text
    assert "Element correctness by class" in text
    assert "recall, upper bound" in text
    assert "the app never started" not in text  # a reason is not a headline
    assert "70.0%" in text


def test_a_torn_label_log_does_not_stop_the_statistics(labelled):
    with labelled.labels_path.open("a", encoding="utf-8") as handle:
        handle.write('{"scope": "element", "index": 0')
    fresh = audit.Campaign(labelled.root)
    assert fresh.torn_bytes > 0
    assert report(fresh)["elements"]["queue_sample"]["judged"] == 10


def test_stats_json_and_md_are_written_next_to_the_campaign(labelled, tmp_path):
    built = report(labelled)
    (labelled.root / "stats.json").write_text(json.dumps(built), encoding="utf-8")
    (labelled.root / "stats.md").write_text(stats_module.render(built), encoding="utf-8")
    assert json.loads((labelled.root / "stats.json").read_text())["campaign"] == "arith"
    assert (labelled.root / "stats.md").read_text().startswith("# Human annotation audit")


# -------------------------------------------------------------------- sweeps


@pytest.fixture()
def swept(campaign):
    """Two swept screens: 10 and 20 elements, three of them flagged wrong.

    30 elements judged from two screens, against the 8 a sample of the same two
    would have produced - that ratio is the whole argument for sweeping.
    """
    keys = campaign.item(0)["element_keys"]
    _element(campaign, 0, 0, ["phantom"], kind="Button", source="sweep")
    _element(campaign, 0, 1, ["bad_geometry"], kind="Text", source="sweep")
    _element(campaign, 1, 0, ["wrong_class"], kind="Button", source="sweep")
    answer(campaign, 0, scope="sweep", n_elements=10,
           flagged=[keys[0], keys[1]],
           census={"Button": 4, "Text": 3, "Window": 3})
    answer(campaign, 1, scope="sweep", n_elements=20,
           flagged=[campaign.item(1)["element_keys"][0]],
           census={"Button": 12, "Text": 8})
    answer(campaign, 0, scope="missed", points=[{"x": 5, "y": 5}])
    answer(campaign, 1, scope="missed", points=[])
    return campaign


def test_a_sweep_judges_every_element_on_the_screen(swept):
    built = report(swept)["swept"]["queue_sample"]
    assert built["screens"] == 2
    assert built["elements"] == 30
    assert built["judged"] == 30
    assert built["wrong"] == 3
    assert built["correct"] == 27
    assert built["precision"]["rate"] == pytest.approx(27 / 30)


def test_sweep_flags_are_counted_by_kind(swept):
    flags = report(swept)["swept"]["queue_sample"]["flags"]
    assert flags["phantom"]["k"] == 1
    assert flags["bad_geometry"]["k"] == 1
    assert flags["wrong_class"]["k"] == 1
    assert flags["wrong_text"]["k"] == 0


def test_per_class_denominators_come_from_the_census(swept):
    by_class = report(swept)["swept"]["queue_sample"]["by_class"]
    assert by_class["Button"]["judged"] == 16      # 4 + 12
    assert by_class["Button"]["wrong"] == 2        # phantom + wrong_class
    assert by_class["Button"]["precision"]["rate"] == pytest.approx(14 / 16)
    assert by_class["Text"]["judged"] == 11        # 3 + 8
    assert by_class["Text"]["wrong"] == 1
    assert by_class["Window"]["wrong"] == 0


def test_a_retracted_flag_stops_counting(swept):
    """The wrong elements come from the element rows, not from the sweep row's
    list: clearing a flag writes a second row, and trusting the list would keep
    counting the retraction."""
    _element(swept, 0, 0, ["ok"], kind="Button")
    built = report(swept)["swept"]["queue_sample"]
    assert built["wrong"] == 2
    assert built["by_class"]["Button"]["wrong"] == 1


def test_unsure_leaves_the_denominator(swept):
    _element(swept, 0, 2, ["unsure"], kind="Text")
    built = report(swept)["swept"]["queue_sample"]
    assert built["unsure"] == 1
    assert built["judged"] == 29
    assert built["by_class"]["Text"]["judged"] == 10


def test_swept_recall_counts_every_annotated_element(swept):
    recall = report(swept)["recall"]["queue_sample"]
    assert recall["annotated_elements"] == 30      # items 0 and 1 in the queue
    assert recall["human_found_unannotated"] == 1
    assert "anchors what a reader notices is absent" in recall["note"]


def test_the_swept_and_inspected_numbers_are_not_pooled(swept):
    """Three different populations, kept apart: the census over every element on
    the swept screens, the exceptions somebody stopped on, and - here, none -
    the elements a sample-mode pass judged individually."""
    built = report(swept)
    assert built["swept"]["queue_sample"]["judged"] == 30
    assert built["exceptions"]["flagged"] == 3
    assert built["elements"]["queue_sample"]["judged"] == 0


def test_a_campaign_run_in_both_modes_keeps_the_two_apart(swept):
    """The calibration case: some screens swept, some elements individually
    judged, and neither rate contaminating the other."""
    _element(swept, 2, 0, ["ok"], source="sampled")
    _element(swept, 2, 1, ["phantom"], source="sampled")
    built = report(swept)
    assert built["elements"]["queue_sample"]["judged"] == 2
    assert built["elements"]["queue_sample"]["correct"] == 1
    assert built["swept"]["queue_sample"]["judged"] == 30
    assert built["exceptions"]["flagged"] == 3


def test_the_markdown_reports_the_sweep(swept):
    text = stats_module.render(report(swept))
    assert "every one on every swept screen" in text
    assert "Swept element correctness by class" in text
    assert "each carrying a verdict" in text


def test_no_elapsed_time_is_ever_reported(swept):
    """A screen can sit open across an interruption, so elapsed time does not
    measure what "seconds per screen" would be read to mean. It stays in the
    log as provenance and appears in no statistic."""
    built = report(swept)
    text = stats_module.render(built)
    assert "seconds" not in text.replace("misses per screen", "")
    assert "seconds" not in json.dumps(built)
def test_an_element_with_two_errors_is_counted_once_in_each(campaign):
    """`phantom` and `bad_geometry` on the same element used to be subtracted
    twice from the element count, which drove the localized rate below zero and
    crashed the interval. A sweep makes that combination easy to produce."""
    _element(campaign, 0, 0, ["phantom", "bad_geometry"])
    built = report(campaign)["elements"]["queue_sample"]
    assert built["judged"] == 1
    assert built["identified"]["rate"] == pytest.approx(0.0)
    assert built["localized"]["rate"] == pytest.approx(0.0)
    assert built["flags"]["phantom"]["k"] == 1
    assert built["flags"]["bad_geometry"]["k"] == 1


def test_three_flags_on_one_element_still_leave_a_valid_rate(campaign):
    _element(campaign, 0, 0, ["phantom", "bad_geometry", "wrong_class"])
    _element(campaign, 0, 1, ["ok"])
    built = report(campaign)["elements"]["queue_sample"]
    assert built["identified"]["rate"] == pytest.approx(0.5)
    assert built["localized"]["rate"] == pytest.approx(0.5)
    for entry in (built["identified"], built["localized"], built["precision"]):
        assert 0.0 <= entry["ci95"][0] <= entry["ci95"][1] <= 1.0


def test_an_impossible_rate_is_named_rather_than_crashing_in_sqrt():
    with pytest.raises(ValueError) as error:
        stats_module.rate(3, 2)
    assert "not a rate" in str(error.value)


def test_sweep_exceptions_are_findings_not_a_precision(swept):
    """The elements a rater stops on during a sweep are the ones that looked
    wrong. A rate over them is a rate over elements selected for being wrong,
    and pairing it with recall produced an 8.7% F1 on a corpus measured at
    99.7% correct."""
    built = report(swept)
    assert built["exceptions"]["flagged"] == 3
    assert built["exceptions"]["by_flag"] == {
        "phantom": 1, "bad_geometry": 1, "wrong_class": 1}
    # The sampled block holds only rows a sample-mode pass produced.
    assert built["elements"]["queue_sample"]["judged"] == 0
    assert built["f1"]["source"] == "swept census"
    assert built["f1"]["queue_sample"] == pytest.approx(
        2 * (27 / 30) * built["recall"]["queue_sample"]["recall_upper_bound"]["rate"]
        / ((27 / 30) + built["recall"]["queue_sample"]["recall_upper_bound"]["rate"]))


def test_an_element_opened_and_cleared_is_not_a_finding(swept):
    _element(swept, 0, 0, ["ok"], source="sweep")
    built = report(swept)["exceptions"]
    assert built["flagged"] == 2
    assert built["opened_then_cleared"] == 1


def test_a_sample_mode_campaign_still_pairs_f1_with_its_own_precision(labelled):
    built = report(labelled)
    assert built["f1"]["source"] == "inspected sample"
    assert built["elements"]["queue_sample"]["judged"] == 10


def test_a_swept_screen_is_finished_even_without_its_item_row(campaign):
    """Three swept screens of two hundred had no done row, because it landed on
    the next item; the sweep row itself is the assertion that the screen is
    finished, and the count reads it."""
    answer(campaign, 0, scope="sweep", n_elements=4, flagged=[],
           census={"Button": 4})
    answer(campaign, 1, scope="sweep", n_elements=4, flagged=[],
           census={"Button": 4})
    answer(campaign, 1, scope="item", status="done")
    session = report(campaign)["session"]
    assert session["items_done"] == 2


def test_a_skip_still_wins_over_a_sweep(campaign):
    answer(campaign, 0, scope="sweep", n_elements=4, flagged=[],
           census={"Button": 4})
    answer(campaign, 0, scope="item", status="skipped", reason="black screen")
    session = report(campaign)["session"]
    assert session["items_done"] == 0
    assert session["items_skipped"] == 1
