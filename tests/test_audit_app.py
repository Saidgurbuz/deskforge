"""The audit request layer, without a browser.

The properties that matter here are not the happy path - the browser probe
covers that - but the boundaries. The client addresses items by campaign and
index and never by path, so there is nothing to escape with; the corpus is
opened for reading and must stay untouched no matter what is posted; and an
answer has to survive a server restart, because that is the only thing standing
between a six-hundred-item session and starting again.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from audit_fixtures import build_corpus
from deskshot.inspector import audit
from deskshot.inspector.app import InspectorApp

CAMPAIGN = "unit"


def _draw(corpus: Path, audit_root: Path) -> audit.Campaign:
    """A two-item campaign, built the way the builder builds one."""
    import sqlite3
    db = sqlite3.connect("file:%s?mode=ro" % (corpus / "plan" / "index.sqlite"), uri=True)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT * FROM samples ORDER BY path LIMIT 2").fetchall()
    campaign = audit.Campaign(audit_root / CAMPAIGN)
    campaign.root.mkdir(parents=True)
    items = []
    for row in rows:
        leaf = Path(str(corpus / row["path"]) + ".elements.leaf.json")
        elements = json.loads(leaf.read_text())
        keys = audit.sample_element_keys(elements, 3, "seed|%s" % row["path"])
        items.append({
            "schema": audit.QUEUE_SCHEMA,
            "observation_key": "%s__%s" % (row["shard"], row["stem"]),
            "source_path": row["path"], "stem": row["stem"], "shard": row["shard"],
            "width": 320, "height": 200, "n_elements": len(elements),
            "n_windows": row["n_windows"], "occluded_ratio": row["occluded_ratio"],
            "apps": [part for part in (row["apps"] or "").split("|") if part],
            "flags": {"publishable": True, "train_eligible": True,
                      "near_duplicate": False, "no_op_frame": False},
            "stratum": {"split": row["split"], "theme": row["theme"],
                        "resolution": row["resolution"], "density": "sparse",
                        "occlusion": "none", "group": row["group"],
                        "app_class": "common_app"},
            "draw": "population",
            "element_keys": keys,
            "digests": {"leaf": {"sha256": "a" * 64, "bytes": leaf.stat().st_size}},
        })
    campaign.write_queue(items)
    campaign.write_manifest({
        "schema": audit.SCHEMA, "name": CAMPAIGN, "title": "unit campaign",
        "created_at": audit.now_stamp(), "seed": 5, "items": len(items),
        "element_sample": 3, "corpus": str(corpus), "release": None,
        "stratum_fields": ["split", "theme", "resolution", "density", "occlusion",
                           "group", "app_class"],
        "rubric": audit.DEFAULT_RUBRIC,
        "rubric_ref": audit.rubric_ref(audit.DEFAULT_RUBRIC),
        "population_total": 9, "population_items": len(items),
    })
    return campaign


@pytest.fixture()
def world(tmp_path):
    corpus = build_corpus(tmp_path / "corpus")
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    campaign = _draw(corpus, audit_root)
    return corpus, audit_root, campaign


@pytest.fixture()
def app(world, tmp_path):
    corpus, audit_root, _ = world
    (tmp_path / "runs").mkdir(exist_ok=True)
    return InspectorApp(
        root=tmp_path / "runs", golden_root=tmp_path / "golden", author="tester",
        cache_dir=tmp_path / "cache", project_root=tmp_path,
        corpus_root=corpus, audit_root=audit_root, rater="tester")


def _get(app, path, **params):
    query = {key: [str(value)] for key, value in params.items()}
    return app.handle("GET", path, query, None, {"accept-encoding": ""})


def _label(app, **payload):
    payload.setdefault("campaign", CAMPAIGN)
    return app.handle("POST", "/api/audit/label", {},
                      json.dumps(payload).encode("utf-8"), {})


# ------------------------------------------------------------------ absent


def test_without_audit_root_every_route_says_so(tmp_path):
    (tmp_path / "runs").mkdir()
    bare = InspectorApp(root=tmp_path / "runs", golden_root=tmp_path / "golden")
    response = _get(bare, "/api/audit/campaigns")
    assert response.status == 404
    assert "--audit" in response.json()["error"]


def test_an_unknown_campaign_is_a_404(app):
    assert _get(app, "/api/audit/queue", campaign="nope").status == 404


def test_a_campaign_name_that_could_escape_the_root_is_refused(app):
    for bad in ("../..", "a/b", ".hidden"):
        response = _get(app, "/api/audit/queue", campaign=bad)
        assert response.status in (400, 404), bad


# ------------------------------------------------------------------- reads


def test_campaigns_are_listed_with_progress(app):
    body = _get(app, "/api/audit/campaigns").json()
    assert [entry["name"] for entry in body["campaigns"]] == [CAMPAIGN]
    assert body["campaigns"][0]["progress"]["total"] == 2
    assert body["rater"] == "tester"


def test_the_queue_carries_the_rubric_and_the_strata(app):
    body = _get(app, "/api/audit/queue", campaign=CAMPAIGN).json()
    assert body["rubric"]["id"] == "annotation_v1"
    assert body["rubric_ref"].startswith("annotation_v1@")
    assert len(body["items"]) == 2
    assert body["items"][0]["stratum"]["split"]
    assert body["progress"]["next"] == 0


def test_an_item_is_addressed_by_index_and_carries_projected_elements(app):
    body = _get(app, "/api/audit/item", campaign=CAMPAIGN, index=0).json()
    assert body["index"] == 0 and body["count"] == 2
    assert body["elements"] and "attrs" not in body["elements"][0]
    assert "_dom_index" not in body["elements"][0]
    assert len(body["sampled"]) == 3
    assert body["sampled_missing"] == []
    assert body["screentag"].startswith("<Window>")


def test_an_item_carries_the_automated_verdict_and_the_shard_audit(app):
    body = _get(app, "/api/audit/item", campaign=CAMPAIGN, index=0).json()
    assert body["automated"]["verdict"]["status"] == "accepted"
    assert body["automated"]["shard_pixel_audit"]["phantom_rate"] == 0.033


def test_an_item_can_be_addressed_by_observation_key(app, world):
    _, _, campaign = world
    key = campaign.queue[1]["observation_key"]
    body = _get(app, "/api/audit/item", campaign=CAMPAIGN, key=key).json()
    assert body["index"] == 1


def test_an_index_outside_the_queue_is_a_404(app):
    assert _get(app, "/api/audit/item", campaign=CAMPAIGN, index=99).status == 404


def test_the_item_payload_revalidates_by_etag(app):
    first = _get(app, "/api/audit/item", campaign=CAMPAIGN, index=0)
    etag = first.headers["ETag"]
    again = app.handle("GET", "/api/audit/item",
                       {"campaign": [CAMPAIGN], "index": ["0"]}, None,
                       {"if-none-match": etag})
    assert again.status == 304
    assert not again.read()


def test_the_item_payload_holds_no_answers(app):
    """Answers change on every keystroke; keeping them out is what lets the
    item be cached and the next three prefetched usefully."""
    _label(app, index=0, scope="screen", question="screen_usable", value="good")
    body = _get(app, "/api/audit/item", campaign=CAMPAIGN, index=0).json()
    assert "state" not in body


def test_state_is_served_for_several_items_at_once(app):
    _label(app, index=0, scope="screen", question="screen_usable", value="good")
    body = _get(app, "/api/audit/state", campaign=CAMPAIGN, indices="0,1").json()
    assert body["states"]["0"]["screen"]["screen_usable"]["value"] == "good"
    assert body["states"]["1"]["screen"] == {}


def test_bad_indices_are_refused(app):
    assert _get(app, "/api/audit/state", campaign=CAMPAIGN, indices="a,b").status == 400


# ------------------------------------------------------------------ images


def test_the_screenshot_is_served_as_webp_with_a_long_cache(app):
    response = _get(app, "/api/audit/image", campaign=CAMPAIGN, index=0, max=256)
    assert response.status == 200
    assert response.headers["Content-Type"] == "image/webp"
    assert "max-age" in response.headers["Cache-Control"]
    assert response.read()[:4] == b"RIFF"


def test_a_native_crop_can_be_asked_for(app):
    response = _get(app, "/api/audit/image", campaign=CAMPAIGN, index=0,
                    x=10, y=10, w=48, h=32, fmt="webp_exact")
    assert response.status == 200 and response.read()[:4] == b"RIFF"


def test_the_image_route_takes_no_path_from_the_client(app):
    """There is no path parameter to escape with: the only way to name a
    capture is a campaign and an index the server looks up itself."""
    response = app.handle("GET", "/api/audit/image",
                          {"campaign": [CAMPAIGN], "path": ["../../etc/passwd"]},
                          None, {})
    assert response.status == 400


# ------------------------------------------------------------------ writes


def test_an_answer_survives_a_restart(app, world, tmp_path):
    corpus, audit_root, _ = world
    result = _label(app, index=0, scope="element", values=["wrong_class"],
                    element_key=_get(app, "/api/audit/item", campaign=CAMPAIGN,
                                     index=0).json()["sampled"][0]).json()
    assert result["stored"] is True
    fresh = InspectorApp(root=tmp_path / "runs", golden_root=tmp_path / "golden",
                         corpus_root=corpus, audit_root=audit_root, rater="tester")
    state = _get(fresh, "/api/audit/state", campaign=CAMPAIGN, indices="0").json()
    answers = state["states"]["0"]["elements"]
    assert list(answers.values())[0]["values"] == ["wrong_class"]


def test_a_write_returns_the_new_state_and_progress(app):
    body = _label(app, index=0, scope="item", status="done").json()
    assert body["progress"]["done"] == 1
    assert body["state"]["item"]["status"] == "done"


def test_the_same_event_id_is_not_stored_twice(app):
    first = _label(app, index=0, scope="screen", question="screen_usable",
                   value="good", event_id="abc").json()
    second = _label(app, index=0, scope="screen", question="screen_usable",
                    value="good", event_id="abc").json()
    assert first["stored"] and not second["stored"]


def test_an_answer_against_a_stale_rubric_is_refused(app):
    response = _label(app, index=0, scope="screen", question="screen_usable",
                      value="good", rubric_ref="annotation_v1@000000000000")
    assert response.status == 409
    assert "reload" in response.json()["error"]


def test_a_value_outside_the_rubric_is_refused(app):
    assert _label(app, index=0, scope="screen", question="screen_usable",
                  value="excellent").status == 400


def test_a_missing_index_is_refused(app):
    assert _label(app, scope="screen", question="screen_usable", value="good").status == 400


def test_audit_read_only_refuses_answers_but_still_serves_them(world, tmp_path):
    corpus, audit_root, _ = world
    (tmp_path / "runs").mkdir(exist_ok=True)
    app = InspectorApp(root=tmp_path / "runs", golden_root=tmp_path / "golden",
                       corpus_root=corpus, audit_root=audit_root, rater="tester",
                       audit_read_only=True)
    assert _get(app, "/api/audit/item", campaign=CAMPAIGN, index=0).status == 200
    response = _label(app, index=0, scope="item", status="done")
    assert response.status == 403


def test_read_only_gates_the_runs_editor_not_the_audit(world, tmp_path):
    """`--read-only` exists to protect `golden/`; it must not make an audit
    session impossible, or the corpus being read-only would mean nothing could
    ever be labelled."""
    corpus, audit_root, _ = world
    (tmp_path / "runs").mkdir(exist_ok=True)
    app = InspectorApp(root=tmp_path / "runs", golden_root=tmp_path / "golden",
                       corpus_root=corpus, audit_root=audit_root, rater="tester",
                       read_only=True)
    assert _label(app, index=0, scope="item", status="done").json()["stored"] is True


# ------------------------------------------------------------- corrections


def test_a_correction_is_stored_beside_the_judgement(app, world):
    _, _, campaign = world
    item = _get(app, "/api/audit/item", campaign=CAMPAIGN, index=0).json()
    key = item["sampled"][0]
    response = app.handle("POST", "/api/audit/correction", {}, json.dumps({
        "campaign": CAMPAIGN, "index": 0,
        "edits": {"modified": {key: {"type": "Checkbox"}}},
        "mark": {"status": "needs_work", "note": "misclassified"},
    }).encode("utf-8"), {})
    assert response.status == 200
    body = response.json()
    assert body["saved"] is True
    stored = json.loads(Path(body["path"]).read_text())
    assert stored["schema"] == "deskshot.golden/1"
    assert stored["edits"]["modified"][key]["type"] == "Checkbox"
    assert Path(body["path"]).parent == campaign.corrections_root


def test_a_correction_outside_the_editable_whitelist_is_refused(app):
    item = _get(app, "/api/audit/item", campaign=CAMPAIGN, index=0).json()
    response = app.handle("POST", "/api/audit/correction", {}, json.dumps({
        "campaign": CAMPAIGN, "index": 0,
        "edits": {"modified": {item["sampled"][0]: {"reading_order_index": 3}}},
    }).encode("utf-8"), {})
    assert response.status == 400


def test_nothing_under_the_corpus_is_written(app, world):
    """The whole audit writes to the campaign, and this is the check that says
    so: a snapshot of the corpus tree before and after a full item."""
    corpus, _, _ = world
    before = {path: path.stat().st_mtime_ns for path in sorted(corpus.rglob("*"))
              if path.is_file()}
    item = _get(app, "/api/audit/item", campaign=CAMPAIGN, index=0).json()
    _label(app, index=0, scope="missed", points=[{"x": 5, "y": 5}])
    for key in item["sampled"]:
        _label(app, index=0, scope="element", values=["ok"], element_key=key)
    for question, value in (("screen_usable", "good"), ("screen_missing_region", "no"),
                            ("screen_occlusion", "na")):
        _label(app, index=0, scope="screen", question=question, value=value)
    _label(app, index=0, scope="item", status="done")
    app.handle("POST", "/api/audit/correction", {}, json.dumps({
        "campaign": CAMPAIGN, "index": 0,
        "edits": {"modified": {item["sampled"][0]: {"name": "Renamed"}}},
    }).encode("utf-8"), {})
    _get(app, "/api/audit/image", campaign=CAMPAIGN, index=0, max=128)
    after = {path: path.stat().st_mtime_ns for path in sorted(corpus.rglob("*"))
             if path.is_file()}
    assert before == after
