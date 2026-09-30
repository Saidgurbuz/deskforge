"""The audit page, driven in a real browser, live and as an offline bundle.

`tests/test_audit_app.py` covers the request layer as function calls. What it
cannot see is the half that only exists in a DOM, and that half is where this
page's bugs were: a stage that collapsed to zero height because `app.css`
already owned the `#stage` id, and an item-done row that raced the "saved"
indicator because it did not go through the same write path as every other
answer. Both passed every unit test.

The scenario lives in `static/audit_probe.html` so it can also be watched in a
browser when a check fails:

    PYTHONPATH=src python scripts/inspect_annotations.py \
        --corpus <corpus> --audit audit
    # then http://localhost:8000/static/audit_probe.html?campaign=<name>

The bundle is driven through the same file, which is the point: it loads the
same `audit.js`, so the same scenario has to pass with the server taken out.
"""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import browser_support
from audit_fixtures import build_corpus
from deskshot.inspector import audit
from deskshot.inspector.server import build_server

PROJECT_ROOT = Path(__file__).resolve().parents[1]
#: Enough for the whole scenario - three passes over an item, a dozen saves and
#: a prefetch - with room for a cold PIL decode on a slow filesystem.
BUDGET = 60000
PROBE_TIMEOUT = 40000

pytestmark = pytest.mark.skipif(browser_support.chromium() is None, reason="no chromium on this machine")


#: How many checks each scenario emits, so a run that stopped early is caught
#: rather than passing on a subset.
EMITS = {"sample": 35, "sweep": 26, "instruction": 17,
         "instruction_grounded": 19}


def _build_campaign(tmp_path, mode, items=3):
    corpus = build_corpus(tmp_path / "corpus")
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "build_audit_queue.py"),
         "--campaign", "probe", "--corpus", str(corpus), "--audit-root", str(audit_root),
         "--items", str(items), "--seed", "3", "--min-per-level", "0", "--mode", mode],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    return corpus, audit_root


def _drive(url, profile, expected):
    process = subprocess.run(
        [browser_support.chromium(), "--headless", "--disable-gpu", "--no-sandbox",
         "--disable-dev-shm-usage", "--disable-extensions", "--no-first-run",
         "--disable-features=Gcm,OptimizationHints",
         "--user-data-dir=%s" % profile, "--window-size=1500,1000",
         "--virtual-time-budget=%d" % BUDGET, "--dump-dom", url],
        env=browser_support.environment(), capture_output=True, timeout=300)
    html = process.stdout.decode("utf-8", "replace")
    rows = ["%s %s" % (state.upper(), re.sub(r"<[^>]+>", " ", body).strip())
            for state, body in browser_support.ROW.findall(html)]
    detail = "\n".join(rows) or process.stderr.decode("utf-8", "replace")[-2000:]
    found = browser_support.SUMMARY.search(html)
    assert found, "the probe never finished:\n%s" % detail
    passed, failed, skipped = (int(value) for value in found.groups())
    assert failed == 0, "browser checks failed:\n%s" % detail
    assert skipped == 0, "browser checks were skipped:\n%s" % detail
    # The scenario is sequential, so a count under what it emits means it
    # stopped early rather than that a check was dropped.
    assert passed >= expected, ("expected %d checks, got %d:\n%s"
                                % (expected, passed, detail))
    return passed


@contextlib.contextmanager
def _serving(handler_factory):
    server = handler_factory()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("mode,expected_scopes", [
    ("sample", {"missed", "screen", "element", "item"}),
    ("sweep", {"sweep", "element", "missed", "item"}),
])
def test_the_live_page_labels_a_whole_item(tmp_path, mode, expected_scopes):
    corpus, audit_root = _build_campaign(tmp_path, mode)
    (tmp_path / "runs").mkdir()

    def make():
        return build_server(root=tmp_path / "runs", golden_root=tmp_path / "golden",
                            host="127.0.0.1", port=0, author="probe",
                            cache_dir=tmp_path / "cache", project_root=tmp_path,
                            corpus_root=corpus, audit_root=audit_root, rater="probe")

    with _serving(make) as base:
        _drive("%s/static/audit_probe.html?campaign=probe&timeout=%d"
               % (base, PROBE_TIMEOUT), tmp_path / "profile-live", EMITS[mode])

    campaign = audit.Campaign(audit_root / "probe")
    assert campaign.mode == mode
    scopes = {row["scope"] for row in campaign.labels()}
    assert expected_scopes <= scopes, scopes
    assert campaign.progress("probe")["done"] == 1
    assert campaign.torn_bytes == 0
    if mode == "sweep":
        sweep = [row for row in campaign.labels() if row["scope"] == "sweep"][0]
        # A census, not a sample: the row accounts for every element on the
        # screen, and its class histogram is what makes per-class denominators
        # exact rather than inferred.
        assert sweep["n_elements"] == sum(sweep["census"].values())
        assert sweep["declared_correct"] == sweep["n_elements"] - len(sweep["flagged"])
        assert all(row.get("source") == "sweep" for row in campaign.labels()
                   if row["scope"] == "element")


@pytest.mark.parametrize("grounding", [False, True], ids=["default", "grounded"])
def test_the_instruction_page_judges_a_sample(tmp_path, grounding):
    """By default the target is on screen and the questions are the whole job.
    With --with-grounding there is a blind click first, scored on the server, so
    that variant drives the round trip: click the middle of the recorded box and
    the page must be told it hit."""
    corpus = build_corpus(tmp_path / "corpus", captures=3)
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    samples, release = _instruction_samples(tmp_path, corpus)
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "build_instruction_queue.py"),
         "--campaign", "probe", "--samples", str(samples), "--corpus", str(corpus),
         "--release", str(release),
         "--audit-root", str(audit_root), "--items", "3", "--seed", "3",
         "--min-per-level", "0"] + (["--with-grounding"] if grounding else []),
        capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    (tmp_path / "runs").mkdir()

    def make():
        return build_server(root=tmp_path / "runs", golden_root=tmp_path / "golden",
                            host="127.0.0.1", port=0, author="probe",
                            cache_dir=tmp_path / "cache", project_root=tmp_path,
                            corpus_root=corpus, audit_root=audit_root, rater="probe")

    with _serving(make) as base:
        _drive("%s/static/audit_probe.html?campaign=probe&timeout=%d"
               % (base, PROBE_TIMEOUT), tmp_path / "profile-instr",
               EMITS["instruction_grounded" if grounding else "instruction"])

    campaign = audit.Campaign(audit_root / "probe")
    assert campaign.kind == "instruction"
    rows = campaign.labels()
    scopes = {row["scope"] for row in rows}
    assert {"screen", "item"} <= scopes
    if not grounding:
        # The default flow shows the target from the start; a grounding click
        # nobody was asked for must not appear in the log.
        assert "ground" not in scopes, scopes
        # Accepting a sample is one keystroke and five recorded judgements,
        # written in one request rather than five round trips.
        accepted = [row for row in rows
                    if row["scope"] == "screen" and row.get("accepted")]
        assert len(accepted) == 5, accepted
        assert len({row["event_id"] for row in accepted}) == 5
        return
    landed = [row for row in rows if row["scope"] == "ground"][0]
    assert landed["in_bbox"] is True
    assert landed["in_fragment"] is True


def _instruction_samples(tmp_path, corpus):
    """A training view over the fixture corpus plus the transition index it
    joins against, both in the real file layout.

    The index is the point: the target box a rater is scored against comes from
    the release's own record, not from the sample row, so the join is part of
    what has to work.
    """
    pytest.importorskip("pyarrow")
    import pyarrow as pa
    import pyarrow.parquet as pq
    import sqlite3

    samples = tmp_path / "samples"
    samples.mkdir()
    release = tmp_path / "release"
    (release / "index" / "transitions").mkdir(parents=True)

    db = sqlite3.connect("file:%s?mode=ro" % (corpus / "plan" / "index.sqlite"), uri=True)
    db.row_factory = sqlite3.Row
    rows, sources, transitions = [], [], []
    for position, row in enumerate(db.execute("SELECT * FROM samples ORDER BY path")):
        key = "%s__%s" % (row["shard"], row["stem"])
        rows.append(json.dumps({
            "sample_id": "s%02d" % position,
            "transition_id": "t%02d" % position,
            "observation_key": key,
            "split": "train", "style": ["standard", "detailed_contextual"][position % 2],
            "instruction": "Click the widget labelled W1.",
            "point": [0.07, 0.10],
            "target": "pyautogui.click(x=0.07, y=0.10)",
            "target_role": "push button", "target_kind": "button",
            "target_app": "mousepad", "objective": "dense_grounding",
        }))
        sources.append(row["path"])
        transitions.append({
            "transition_id": "t%02d" % position, "action_type": "click",
            # uid u0000 is the fixture's first widget, at (10, 10) 24x18 - the
            # box below is that element's, so the visible-fragment lookup has
            # something to find and the click can be scored against real pixels.
            "action_target_uid": "u0000",
            "action_target_role": "push button", "action_target_kind": "button",
            "action_target_text": "W1", "action_target_app": "mousepad",
            "action_point_px": [22, 19], "action_target_bbox_px": [10, 10, 34, 28],
            "before_key": key, "after_key": key,
            "effect": {"changed": True},
        })
    (samples / "train.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (samples / "train.sources.txt").write_text("\n".join(sources) + "\n", encoding="utf-8")
    pq.write_table(pa.Table.from_pylist(transitions),
                   str(release / "index" / "transitions" / "train.parquet"))
    return samples, release


def test_the_offline_bundle_behaves_the_same(tmp_path):
    """The bundle loads the same audit.js with its four fetches replaced, so a
    divergence between live and offline is a bug in the shim, not a feature."""
    pytest.importorskip("PIL")
    corpus, audit_root = _build_campaign(tmp_path, "sweep")
    bundle = tmp_path / "bundle"
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "pack_audit_bundle.py"),
         "--campaign", str(audit_root / "probe"), "--out", str(bundle)],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (bundle / "bundle.json").is_file()
    assert "AUDIT_DEFER" in (bundle / "index.html").read_text(encoding="utf-8")
    assert json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))["mode"] == "sweep"

    probe = PROJECT_ROOT / "src" / "deskshot" / "inspector" / "static" / "audit_probe.html"
    (bundle / "audit_probe.html").write_bytes(probe.read_bytes())

    import functools
    import http.server
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(bundle))

    def make():
        return http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)

    with _serving(make) as base:
        _drive("%s/audit_probe.html?src=index.html%%3Frater%%3Dprobe&timeout=%d"
               % (base, PROBE_TIMEOUT), tmp_path / "profile-offline", EMITS["sweep"])

    # The bundle answers into IndexedDB, so the campaign on disk is untouched;
    # what this asserts is that the packed payloads were what the page needed.
    manifest = json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))
    assert manifest["items"]
    for row in manifest["items"]:
        assert (bundle / row["payload"]).is_file()
        assert (bundle / row["image"]).is_file()
    assert not audit.Campaign(audit_root / "probe").labels()
