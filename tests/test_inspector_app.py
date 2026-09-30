"""The request layer, exercised without a browser.

The inspector writes to disk and is reachable over a tunnel, so the two things
worth pinning are that a request cannot address anything outside the root it
was pointed at, and that an edit survives the trip through HTTP and back out of
a freshly constructed app - the "restart the server and it is still there"
property, which is the whole point of storing overlays separately.
"""

from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request

import pytest
from PIL import Image

from deskshot.inspector import app as app_module
from deskshot.inspector import overlay
from deskshot.inspector.app import InspectorApp
from deskshot.inspector.server import build_server


def _elements():
    return [
        {
            "uid": "aaa", "role": "push button", "name": "Open", "app_name": "mousepad",
            "rect": {"x": 10, "y": 20, "w": 30, "h": 40}, "_dom_index": 1, "source": "app",
            "visible_text": "Open", "visible_text_status": "full_visible",
            "is_occluded": False, "interaction": {"actionable": True},
        },
        {
            "role": "static", "name": "Ready", "app_name": "mousepad",
            "rect": {"x": 5, "y": 5, "w": 100, "h": 12}, "_dom_index": 2, "source": "app",
            "visible_text": "Ready", "visible_text_status": "clipped", "is_occluded": True,
        },
    ]


@pytest.fixture()
def run_root(tmp_path):
    """One run holding one capture, with every sidecar a real run would have."""
    run = tmp_path / "runs" / "v900_test" / "batch"
    run.mkdir(parents=True)
    stem = "scene-testcapture"
    Image.new("RGB", (400, 300), (30, 60, 90)).save(str(run / (stem + ".png")))
    (run / (stem + ".elements.leaf.json")).write_text(json.dumps(_elements()), encoding="utf-8")
    (run / (stem + ".elements.unfiltered.json")).write_text(
        json.dumps({"elements": _elements()}), encoding="utf-8")
    (run / (stem + ".meta.json")).write_text(json.dumps({
        "num_elements_leaf": 2, "launched_apps": ["mousepad"],
        "scene": {"seed": 930000, "theme_preset": "macos_tahoe_like", "layout": "single"},
    }), encoding="utf-8")
    (run / (stem + ".screentag.txt")).write_text("<Button>Open</Button>", encoding="utf-8")
    (run / "quality.json").write_text(json.dumps({"captures": 1, "fn_uncovered_ink": 0.05}),
                                      encoding="utf-8")
    (run / "audit").mkdir()
    (run / "audit" / "pixel_audit.json").write_text(json.dumps({
        "rows": [{"capture": stem + ".png", "ink_uncovered_ratio": 0.07,
                  "phantoms": [{"role": "table cell", "rect": {"x": 1, "y": 2, "w": 3, "h": 4},
                                "text": "¥ 565"}], "drifted": []}]
    }), encoding="utf-8")
    return tmp_path


@pytest.fixture()
def app(run_root):
    return InspectorApp(
        root=run_root / "runs",
        golden_root=run_root / "golden",
        author="tester",
        cache_dir=run_root / "cache",
        project_root=run_root,
    )


RUN = "v900_test/batch"
STEM = "scene-testcapture"


def _get(app, path, **params):
    query = {key: [str(value)] for key, value in params.items()}
    return app.handle("GET", path, query)


def _post(app, payload):
    return app.handle("POST", "/api/overlay", {}, json.dumps(payload).encode("utf-8"))


# ------------------------------------------------------------------ browse


def test_config_names_the_editable_surface(app):
    body = _get(app, "/api/config").json()
    assert "leaf" in body["views"] and "rect" in body["editable_fields"]
    assert body["read_only"] is False


def test_runs_lists_only_directories_holding_captures(app):
    runs = _get(app, "/api/runs").json()["runs"]
    assert [entry["run"] for entry in runs] == [RUN]
    assert runs[0]["samples"] == 1


def test_run_listing_does_not_need_the_element_files(app, run_root):
    # The list view is built from the PNG header and meta.json; a 739KB element
    # file per sample is exactly what it must not read.
    (run_root / "runs" / RUN / (STEM + ".elements.leaf.json")).chmod(0o000)
    try:
        sample = _get(app, "/api/run", run=RUN).json()["samples"][0]
    finally:
        (run_root / "runs" / RUN / (STEM + ".elements.leaf.json")).chmod(0o644)
    assert sample["stem"] == STEM
    assert (sample["width"], sample["height"]) == (400, 300)
    assert sample["counts"]["leaf"] == 2
    assert sample["apps"] == ["mousepad"]


def test_the_tree_nests_runs_under_their_folders(app, run_root):
    later = run_root / "runs" / "v900_test" / "second"
    later.mkdir(parents=True)
    Image.new("RGB", (8, 8)).save(str(later / "scene-x.png"))
    (later / "scene-x.elements.leaf.json").write_text("[]", encoding="utf-8")

    body = _get(app, "/api/tree", refresh=1).json()
    assert body["runs"] == 2 and body["captures"] == 2
    top = body["tree"][0]
    assert top["name"] == "v900_test" and top["run"] is False and top["total"] == 2
    assert sorted(child["name"] for child in top["children"]) == ["batch", "second"]
    assert all(child["run"] is True for child in top["children"])


def test_the_tree_is_built_without_opening_a_capture(app, run_root):
    # 325 runs and 65,535 files: the sidebar cannot afford to read any of them.
    _get(app, "/api/tree")
    for name in (STEM + ".elements.leaf.json", STEM + ".png", STEM + ".meta.json"):
        (run_root / "runs" / RUN / name).chmod(0o000)
    try:
        body = _get(app, "/api/tree").json()
    finally:
        for name in (STEM + ".elements.leaf.json", STEM + ".png", STEM + ".meta.json"):
            (run_root / "runs" / RUN / name).chmod(0o644)
    assert body["tree"][0]["total"] == 1


def test_the_tree_badges_a_folder_holding_curated_samples(app):
    _post(app, {"run": RUN, "stem": STEM, "mark": {"status": "verified"},
                "edits": {"deleted": ["uid:aaa"]}})
    top = _get(app, "/api/tree").json()["tree"][0]
    assert (top["marked"], top["edited"]) == (1, 1)
    assert (top["children"][0]["marked"], top["children"][0]["edited"]) == (1, 1)


def test_a_new_run_appears_only_after_a_rescan(app, run_root):
    _get(app, "/api/runs")
    later = run_root / "runs" / "v901_later" / "batch"
    later.mkdir(parents=True)
    Image.new("RGB", (8, 8)).save(str(later / "scene-x.png"))
    (later / "scene-x.elements.leaf.json").write_text("[]", encoding="utf-8")
    assert len(_get(app, "/api/runs").json()["runs"]) == 1
    assert len(_get(app, "/api/runs", refresh=1).json()["runs"]) == 2


def test_sample_carries_elements_stats_audit_and_meta(app):
    body = _get(app, "/api/sample", run=RUN, stem=STEM).json()
    assert [element["_key"] for element in body["elements"]] == \
        ["uid:aaa", "dom:mousepad:2:app"]
    assert body["stats"]["elements"] == 2
    assert body["stats"]["occluded"] == 1
    assert dict(body["stats"]["by_role"])["push button"] == 1
    assert body["audit"]["pixel_audit"]["ink_uncovered_ratio"] == 0.07
    assert body["meta"]["scene"]["seed"] == 930000
    assert body["image"]["width"] == 400
    assert body["source"]["sha256"] and body["stale"] is False
    assert body["has_screentag"] is True


def test_the_unfiltered_view_is_readable_too(app):
    body = _get(app, "/api/sample", run=RUN, stem=STEM, view="unfiltered").json()
    assert len(body["elements"]) == 2


def test_large_json_is_gzipped_when_the_client_accepts_it(app):
    response = app.handle("GET", "/api/sample", {"run": [RUN], "stem": [STEM]},
                          headers={"Accept-Encoding": "gzip, deflate"})
    assert response.headers.get("Content-Encoding") == "gzip"
    assert response.json()["stem"] == STEM


def test_screentag_is_served_as_text(app):
    response = _get(app, "/api/screentag", run=RUN, stem=STEM)
    assert response.read() == b"<Button>Open</Button>"


def test_index_html_and_assets_are_served(app):
    assert b"DeskShot" in app.handle("GET", "/", {}).read()
    assert app.handle("GET", "/static/app.js", {}).status == 200
    assert app.handle("GET", "/static/app.css", {}).status == 200


# ------------------------------------------------------------------ images


def test_full_image_is_streamed_from_disk(app, run_root):
    response = _get(app, "/api/image", run=RUN, stem=STEM)
    assert response.path == run_root / "runs" / RUN / (STEM + ".png")
    assert response.read()[:8] == b"\x89PNG\r\n\x1a\n"


def test_a_region_query_returns_just_that_region(app):
    body = _get(app, "/api/image", run=RUN, stem=STEM, x=10, y=20, w=64, h=48).read()
    assert Image.open(io.BytesIO(body)).size == (64, 48)


def test_a_size_cap_downscales_for_the_sample_list(app):
    body = _get(app, "/api/image", run=RUN, stem=STEM, max=100).read()
    assert max(Image.open(io.BytesIO(body)).size) == 100


def test_a_region_is_clamped_to_the_image(app):
    body = _get(app, "/api/image", run=RUN, stem=STEM, x=380, y=290, w=999, h=999).read()
    assert Image.open(io.BytesIO(body)).size == (20, 10)


def test_unchanged_images_answer_304(app):
    first = _get(app, "/api/image", run=RUN, stem=STEM, max=100)
    again = app.handle("GET", "/api/image",
                       {"run": [RUN], "stem": [STEM], "max": ["100"]},
                       headers={"If-None-Match": first.headers["ETag"]})
    assert again.status == 304


def test_unknown_image_kind_is_refused(app):
    assert _get(app, "/api/image", run=RUN, stem=STEM, kind="/etc/passwd").status == 400


def test_a_frame_can_be_served_as_webp(app):
    # 8x fewer bytes than PNG for the same fitted frame, which is the whole
    # difference between browsing this over a tunnel and waiting for it.
    response = _get(app, "/api/image", run=RUN, stem=STEM, max=100, fmt="webp")
    assert response.headers["Content-Type"] == "image/webp"
    image = Image.open(io.BytesIO(response.read()))
    assert image.format == "WEBP" and max(image.size) == 100


def test_a_native_crop_can_be_served_losslessly(app, run_root):
    source = Image.open(str(run_root / "runs" / RUN / (STEM + ".png"))).convert("RGB")
    body = _get(app, "/api/image", run=RUN, stem=STEM,
                x=10, y=10, w=32, h=24, fmt="webp_exact").read()
    got = Image.open(io.BytesIO(body)).convert("RGB")
    assert got.size == (32, 24)
    assert list(got.getdata()) == list(source.crop((10, 10, 42, 34)).getdata())


def test_an_unknown_image_format_is_refused(app):
    assert _get(app, "/api/image", run=RUN, stem=STEM, fmt="tiff").status == 400


def test_formats_do_not_share_a_cache_entry(app):
    png = _get(app, "/api/image", run=RUN, stem=STEM, max=64)
    webp = _get(app, "/api/image", run=RUN, stem=STEM, max=64, fmt="webp")
    assert png.headers["ETag"] != webp.headers["ETag"]
    assert Image.open(io.BytesIO(png.read())).format == "PNG"
    assert Image.open(io.BytesIO(webp.read())).format == "WEBP"


def _fill_image_cache(app, count):
    for size in range(48, 48 + count):
        _get(app, "/api/image", run=RUN, stem=STEM, max=size, fmt="webp")


def _cache_bytes(run_root):
    images = run_root / "cache" / "images"
    return sum(path.stat().st_size for path in images.iterdir() if path.is_file())


def test_the_generated_image_cache_is_trimmed_to_its_ceiling(run_root):
    """Nothing trimmed this before, and it grows one file per zoom step.

    The trim is deliberately rare - once every `IMAGE_CACHE_PRUNE_EVERY` writes,
    because measuring on every write would `iterdir` the cache once per image
    request - so this drives exactly that many and checks the state right after
    the trim. A branch nothing exercises is where a NameError waits until it
    kills something expensive.
    """
    app = InspectorApp(
        root=run_root / "runs", golden_root=run_root / "golden", author="tester",
        cache_dir=run_root / "cache", project_root=run_root, cache_max_bytes=4096)
    _fill_image_cache(app, app_module.IMAGE_CACHE_PRUNE_EVERY)
    assert _cache_bytes(run_root) <= 4096, _cache_bytes(run_root)


def test_the_ceiling_holds_across_several_trims(run_root):
    app = InspectorApp(
        root=run_root / "runs", golden_root=run_root / "golden", author="tester",
        cache_dir=run_root / "cache", project_root=run_root, cache_max_bytes=4096)
    _fill_image_cache(app, app_module.IMAGE_CACHE_PRUNE_EVERY * 3)
    assert _cache_bytes(run_root) <= 4096, _cache_bytes(run_root)


def test_an_unbounded_cache_is_left_alone(run_root):
    """`--cache-max-bytes 0` keeps the old behaviour, deliberately."""
    app = InspectorApp(
        root=run_root / "runs", golden_root=run_root / "golden", author="tester",
        cache_dir=run_root / "cache", project_root=run_root, cache_max_bytes=0)
    _fill_image_cache(app, 70)
    files = [path for path in (run_root / "cache" / "images").iterdir() if path.is_file()]
    assert len(files) == 70


def test_a_frame_is_decoded_once_and_reused(app, run_root):
    # The frame cache is what makes panning at zoom cheap: every crop after the
    # first is taken from memory, not from a fresh decode of a 1MB PNG.
    _get(app, "/api/image", run=RUN, stem=STEM, max=100)
    (run_root / "runs" / RUN / (STEM + ".png")).chmod(0o000)
    try:
        body = _get(app, "/api/image", run=RUN, stem=STEM, x=0, y=0, w=40, h=40).read()
    finally:
        (run_root / "runs" / RUN / (STEM + ".png")).chmod(0o644)
    assert Image.open(io.BytesIO(body)).size == (40, 40)


# ------------------------------------------------------------------ safety


@pytest.mark.parametrize("run", ["../../etc", "..", "v900_test/../../..", ""])
def test_a_run_cannot_address_anything_outside_the_root(app, run):
    assert _get(app, "/api/run", run=run).status in (400, 403, 404)


@pytest.mark.parametrize("stem", ["../secret", "a/b", ".hidden"])
def test_a_stem_cannot_contain_a_path(app, stem):
    assert _get(app, "/api/sample", run=RUN, stem=stem).status in (400, 404)


def test_static_assets_cannot_escape_the_package(app):
    assert app.handle("GET", "/static/../../../etc/passwd", {}).status in (400, 404)


def test_unknown_view_and_missing_sample_are_distinguished(app):
    assert _get(app, "/api/sample", run=RUN, stem=STEM, view="nope").status == 400
    assert _get(app, "/api/sample", run=RUN, stem="scene-absent").status == 404
    assert app.handle("GET", "/api/nothing", {}).status == 404


def test_read_only_refuses_writes(run_root):
    app = InspectorApp(root=run_root / "runs", golden_root=run_root / "golden",
                       author="tester", project_root=run_root, read_only=True)
    response = _post(app, {"run": RUN, "stem": STEM, "mark": {"status": "verified"}})
    assert response.status == 403


# -------------------------------------------------------------- round trip


def test_an_edit_survives_a_restart(app, run_root):
    saved = _post(app, {
        "run": RUN, "stem": STEM, "view": "leaf",
        "mark": {"status": "verified", "tags": ["golden"], "note": "checked by hand"},
        "edits": {
            "modified": {"uid:aaa": {"role": "link", "rect": {"x": 1, "y": 2, "w": 3, "h": 4}}},
            "deleted": ["dom:mousepad:2:app"],
            "added": [{"role": "icon", "rect": {"x": 50, "y": 50, "w": 16, "h": 16},
                       "name": "missing icon"}],
        },
    })
    assert saved.status == 200 and saved.json()["saved"] is True

    stored = overlay.overlay_path(run_root / "golden", RUN, STEM, "leaf")
    assert stored.is_file()
    assert stored.parent == run_root / "golden" / RUN

    fresh = InspectorApp(root=run_root / "runs", golden_root=run_root / "golden",
                         author="someone", project_root=run_root)
    body = _get(fresh, "/api/sample", run=RUN, stem=STEM).json()
    by_key = {element["_key"]: element for element in body["elements"]}
    assert by_key["uid:aaa"]["role"] == "link"
    assert by_key["uid:aaa"]["rect"] == {"x": 1, "y": 2, "w": 3, "h": 4}
    assert by_key["uid:aaa"]["_golden"]["original"]["role"] == "push button"
    assert by_key["dom:mousepad:2:app"]["_golden"]["status"] == "deleted"
    assert by_key["add:add-1"]["name"] == "missing icon"
    assert body["overlay"]["mark"]["status"] == "verified"
    assert body["overlay"]["updated_by"] == "tester"
    assert body["applied"] == {"modified": 1, "deleted": 1, "added": 1, "unmatched": []}
    # Statistics follow the edits, not the file on disk.
    assert body["stats"]["elements"] == 2


def test_the_pipeline_output_is_never_touched(app, run_root):
    source = run_root / "runs" / RUN / (STEM + ".elements.leaf.json")
    before = source.read_bytes()
    _post(app, {"run": RUN, "stem": STEM, "edits": {"deleted": ["uid:aaa"]}})
    assert source.read_bytes() == before


def test_the_run_listing_badges_marked_samples(app):
    _post(app, {"run": RUN, "stem": STEM, "mark": {"status": "needs_work"}})
    sample = _get(app, "/api/run", run=RUN).json()["samples"][0]
    assert sample["overlay"]["mark"]["status"] == "needs_work"
    golden = _get(app, "/api/golden").json()["overlays"]
    assert golden[0]["stem"] == STEM and golden[0]["run"] == RUN


def test_an_overlay_with_nothing_in_it_is_removed_rather_than_stored(app, run_root):
    _post(app, {"run": RUN, "stem": STEM, "mark": {"status": "verified"}})
    stored = overlay.overlay_path(run_root / "golden", RUN, STEM, "leaf")
    assert stored.is_file()
    result = _post(app, {"run": RUN, "stem": STEM, "mark": {"status": "unmarked"}})
    body = result.json()
    assert (body["saved"], body["removed"], body["path"]) == (False, True, str(stored))
    assert not stored.exists()


def test_deleting_an_overlay_leaves_the_capture_alone(app, run_root):
    _post(app, {"run": RUN, "stem": STEM, "mark": {"status": "verified"}})
    response = app.handle("DELETE", "/api/overlay",
                          {"run": [RUN], "stem": [STEM], "view": ["leaf"]})
    assert response.json()["removed"] is True
    assert (run_root / "runs" / RUN / (STEM + ".png")).is_file()


def test_an_overlay_reports_itself_stale_when_the_capture_changes(app, run_root):
    _post(app, {"run": RUN, "stem": STEM, "edits": {"deleted": ["uid:aaa"]}})
    source = run_root / "runs" / RUN / (STEM + ".elements.leaf.json")
    changed = _elements()
    changed[0]["uid"] = "bbb"
    source.write_text(json.dumps(changed), encoding="utf-8")
    body = _get(app, "/api/sample", run=RUN, stem=STEM).json()
    assert body["stale"] is True
    assert body["applied"]["unmatched"] == ["uid:aaa"]


@pytest.mark.parametrize("edits", [
    {"modified": {"uid:aaa": {"uid": "hijack"}}},
    {"modified": {"uid:aaa": {"rect": {"x": 0, "y": 0, "w": -3, "h": 4}}}},
    {"added": [{"name": "no rect and no role"}]},
])
def test_a_rejected_edit_says_why_and_writes_nothing(app, run_root, edits):
    response = _post(app, {"run": RUN, "stem": STEM, "edits": edits})
    assert response.status == 400
    assert response.json()["error"]
    assert not (run_root / "golden").exists()


# ------------------------------------------------------- concurrent editors


def test_a_save_built_on_a_stale_version_is_refused(app, run_root):
    first = _post(app, {"run": RUN, "stem": STEM, "base_version": None,
                        "mark": {"status": "verified", "note": "mine"}})
    version = first.json()["overlay_version"]

    # A second editor who opened the sample before that save still holds None.
    clash = _post(app, {"run": RUN, "stem": STEM, "base_version": None,
                        "mark": {"status": "rejected", "note": "theirs"}})
    assert clash.status == 409
    body = clash.json()
    assert "while you were editing" in body["error"]
    assert body["conflict"]["current"]["mark"]["note"] == "mine"
    # And the file still holds the first editor's work, untouched.
    stored = overlay.load_overlay(overlay.overlay_path(run_root / "golden", RUN, STEM, "leaf"))
    assert stored["mark"]["note"] == "mine"

    # Re-reading the sample hands out the version that would let them through.
    fresh = _get(app, "/api/sample", run=RUN, stem=STEM).json()
    assert fresh["overlay_version"] == version
    ok = _post(app, {"run": RUN, "stem": STEM, "base_version": version,
                     "mark": {"status": "rejected", "note": "theirs"}})
    assert ok.status == 200 and ok.json()["overlay_version"] != version


def test_a_delete_under_an_editor_is_reported_rather_than_silently_recreated(app, run_root):
    saved = _post(app, {"run": RUN, "stem": STEM, "base_version": None,
                        "mark": {"status": "verified"}})
    version = saved.json()["overlay_version"]
    app.handle("DELETE", "/api/overlay", {"run": [RUN], "stem": [STEM], "view": ["leaf"]})
    clash = _post(app, {"run": RUN, "stem": STEM, "base_version": version,
                        "mark": {"status": "needs_work"}})
    assert clash.status == 409 and "deleted" in clash.json()["error"]


def test_a_client_that_sends_no_version_is_still_served(app):
    # curl and the tests are not the UI; they opt out by not sending one.
    _post(app, {"run": RUN, "stem": STEM, "mark": {"status": "verified"}})
    assert _post(app, {"run": RUN, "stem": STEM,
                       "mark": {"status": "rejected"}}).status == 200


# ------------------------------------------------------- fragment geometry


def _fragmented_run(run_root):
    """Rewrite the capture so its second element is split by an occluder."""
    elements = _elements()
    elements[1]["visible_fragments"] = [
        {"x": 5, "y": 5, "w": 40, "h": 12}, {"x": 70, "y": 5, "w": 35, "h": 12},
    ]
    (run_root / "runs" / RUN / (STEM + ".elements.leaf.json")).write_text(
        json.dumps(elements), encoding="utf-8")


def test_editing_a_rect_drops_the_fragments_it_invalidated(app, run_root):
    _fragmented_run(run_root)
    key = "dom:mousepad:2:app"
    saved = _post(app, {"run": RUN, "stem": STEM, "edits": {
        "modified": {key: {"rect": {"x": 400, "y": 400, "w": 60, "h": 20}}}}})
    assert saved.json()["fragments_cleared"] == [key]

    stored = overlay.load_overlay(overlay.overlay_path(run_root / "golden", RUN, STEM, "leaf"))
    # Explicit in the document, not only in the merge: the coverage audit reads
    # `rect` UNION `visible_fragments` straight out of what is served.
    assert stored["edits"]["modified"][key]["visible_fragments"] == []

    element = [item for item in _get(app, "/api/sample", run=RUN, stem=STEM).json()["elements"]
               if item["_key"] == key][0]
    assert element["visible_fragments"] == []
    assert len(element["_golden"]["original"]["visible_fragments"]) == 2  # revertible


def test_an_edit_that_leaves_the_rect_alone_keeps_the_fragments(app, run_root):
    _fragmented_run(run_root)
    key = "dom:mousepad:2:app"
    saved = _post(app, {"run": RUN, "stem": STEM,
                        "edits": {"modified": {key: {"role": "label"}}}})
    assert saved.json()["fragments_cleared"] == []
    element = [item for item in _get(app, "/api/sample", run=RUN, stem=STEM).json()["elements"]
               if item["_key"] == key][0]
    assert len(element["visible_fragments"]) == 2


def test_fragments_given_explicitly_are_kept_as_sent(app, run_root):
    _fragmented_run(run_root)
    key = "dom:mousepad:2:app"
    fragments = [{"x": 1, "y": 2, "w": 3, "h": 4}]
    saved = _post(app, {"run": RUN, "stem": STEM, "edits": {"modified": {key: {
        "rect": {"x": 0, "y": 0, "w": 10, "h": 10}, "visible_fragments": fragments}}}})
    assert saved.json()["fragments_cleared"] == []
    element = [item for item in _get(app, "/api/sample", run=RUN, stem=STEM).json()["elements"]
               if item["_key"] == key][0]
    assert element["visible_fragments"] == fragments


def test_a_write_to_a_sample_outside_the_root_is_refused(app, run_root):
    response = _post(app, {"run": "../../..", "stem": STEM, "edits": {}})
    assert response.status in (400, 403, 404)
    assert not (run_root / "golden").exists()


# ------------------------------------------------------------- over a socket


def test_end_to_end_over_http(run_root):
    server = build_server(root=run_root / "runs", golden_root=run_root / "golden",
                          host="127.0.0.1", port=0, author="tester",
                          cache_dir=run_root / "cache", project_root=run_root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    try:
        with urllib.request.urlopen(base + "/") as response:
            assert b"<title>DeskShot" in response.read()
        with urllib.request.urlopen(
            base + "/api/sample?run=%s&stem=%s" % (RUN, STEM)
        ) as response:
            assert len(json.loads(response.read())["elements"]) == 2
        with urllib.request.urlopen(base + "/api/image?run=%s&stem=%s" % (RUN, STEM)) as response:
            body = response.read()
            assert body[:8] == b"\x89PNG\r\n\x1a\n"
            assert len(body) == int(response.headers["Content-Length"])

        request = urllib.request.Request(
            base + "/api/overlay",
            data=json.dumps({"run": RUN, "stem": STEM,
                             "mark": {"status": "verified"}}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request) as response:
            assert json.loads(response.read())["saved"] is True

        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(base + "/api/run?run=../../etc")
        assert raised.value.code in (400, 403, 404)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_run_listing_does_not_leak_marks_into_the_index_cache(app, run_root):
    # The index hands out its cached records; stamping the current mark onto
    # them would persist a mark into the index file and outlive the overlay.
    _post(app, {"run": RUN, "stem": STEM, "mark": {"status": "verified"}})
    _get(app, "/api/run", run=RUN)
    cached = json.loads((run_root / "cache" / "index.json").read_text(encoding="utf-8"))
    sample = cached["runs"][RUN]["samples"][0]
    assert "overlay" not in sample
