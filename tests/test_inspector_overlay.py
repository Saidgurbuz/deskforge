"""The overlay is the only part of the inspector whose loss would hurt.

`incremental_checks/` is disposable and regenerated; `golden/` is hand-made and
committed. So these tests pin the three things that would silently destroy that
work: an element addressed by the wrong key, an edit that does not survive a
round trip through disk, and a capture that changed underneath an overlay
without anything saying so.
"""

from __future__ import annotations

import json

import pytest

from deskshot.inspector import overlay


def _element(uid=None, dom=1, app="mousepad", **extra):
    element = {
        "role": "push button",
        "name": "Open",
        "rect": {"x": 10, "y": 20, "w": 30, "h": 40},
        "app_name": app,
        "_dom_index": dom,
        "source": "app",
    }
    if uid:
        element["uid"] = uid
    element.update(extra)
    return element


# --------------------------------------------------------------- identity


def test_uid_is_preferred_when_present():
    assert overlay.element_key(_element(uid="abc")) == "uid:abc"


def test_elements_without_uid_fall_back_to_dom_identity():
    # 705 of 5,201 leaf elements in v224_verify carry no uid, so this path is
    # the common one, not a corner case.
    key = overlay.element_key(_element(dom=7, app="thunar"))
    assert key == "dom:thunar:7:app"


def test_repeated_identities_get_an_occurrence_ordinal():
    # The unfiltered view repeats (app, dom index, source) for desktop chrome.
    duplicates = [_element(dom=3, app="ata_"), _element(dom=3, app="ata_")]
    keys = overlay.element_keys(duplicates)
    assert keys == ["dom:ata_:3:app", "dom:ata_:3:app#1"]
    assert len(set(keys)) == 2


# ------------------------------------------------------------------ merge


def test_modification_records_what_it_replaced():
    elements = [_element(uid="a")]
    document = {"edits": {"modified": {"uid:a": {"role": "link"}}}}
    merged, applied = overlay.apply(elements, document)
    assert merged[0]["role"] == "link"
    assert merged[0]["_golden"]["original"] == {"role": "push button"}
    assert applied["modified"] == 1


def test_deletion_drops_the_element_unless_asked_for():
    elements = [_element(uid="a"), _element(uid="b")]
    document = {"edits": {"deleted": ["uid:a"]}}
    merged, _ = overlay.apply(elements, document)
    assert [item["_key"] for item in merged] == ["uid:b"]
    merged, _ = overlay.apply(elements, document, include_deleted=True)
    assert merged[0]["_golden"]["status"] == "deleted"


def test_deleting_an_edited_element_keeps_its_original_values():
    # The browser reconstructs source values from `original`; losing them on
    # delete would make an edit un-revertable in the UI.
    elements = [_element(uid="a")]
    document = {"edits": {"modified": {"uid:a": {"role": "link"}}, "deleted": ["uid:a"]}}
    merged, _ = overlay.apply(elements, document, include_deleted=True)
    assert merged[0]["_golden"]["status"] == "deleted"
    assert merged[0]["_golden"]["original"] == {"role": "push button"}


def test_added_elements_are_appended_with_their_own_keys():
    document = {"edits": {"added": [{"golden_id": "add-1", "role": "icon",
                                     "rect": {"x": 1, "y": 2, "w": 3, "h": 4}}]}}
    merged, applied = overlay.apply([_element(uid="a")], document)
    assert merged[-1]["_key"] == "add:add-1"
    assert merged[-1]["_golden"]["status"] == "added"
    assert applied["added"] == 1


def test_edits_that_match_nothing_are_reported_not_dropped_silently():
    document = {"edits": {"modified": {"uid:gone": {"role": "link"}},
                          "deleted": ["uid:also-gone"]}}
    _, applied = overlay.apply([_element(uid="a")], document)
    assert applied["unmatched"] == ["uid:also-gone", "uid:gone"]


# ------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "payload",
    [
        {"modified": {"uid:a": {"_dom_index": 5}}},        # not editable
        {"modified": {"uid:a": {"rect": {"x": 1, "y": 2, "w": 0, "h": 4}}}},
        {"modified": {"uid:a": {"rect": {"x": 1, "y": 2}}}},
        {"modified": "nonsense"},
        {"deleted": [{"uid": "a"}]},
        {"added": [{"role": "icon"}]},                     # no rect
        {"added": [{"rect": {"x": 1, "y": 1, "w": 2, "h": 2}}]},  # no role
    ],
)
def test_bad_edits_are_rejected(payload):
    with pytest.raises(overlay.OverlayError):
        overlay.normalise_edits(payload)


def test_rects_are_coerced_to_integers():
    clean = overlay.normalise_edits(
        {"modified": {"uid:a": {"rect": {"x": 1.6, "y": 2.4, "w": 3.5, "h": 4.0}}}}
    )
    assert clean["modified"]["uid:a"]["rect"] == {"x": 2, "y": 2, "w": 4, "h": 4}


def test_added_elements_get_an_id_when_the_client_omits_one():
    clean = overlay.normalise_edits(
        {"added": [{"role": "icon", "rect": {"x": 1, "y": 1, "w": 2, "h": 2}}]}
    )
    assert clean["added"][0]["golden_id"] == "add-1"


def test_unknown_mark_status_is_rejected():
    with pytest.raises(overlay.OverlayError):
        overlay.normalise_mark({"status": "probably fine"})


# ---------------------------------------------------------- persistence


def test_save_and_load_round_trip(tmp_path):
    path = overlay.overlay_path(tmp_path, "v1/batch", "scene-abc", "leaf")
    document = overlay.build_document(
        "v1/batch", "scene-abc", "leaf",
        stamp={"path": "x", "sha256": "deadbeef", "size": 1, "mtime": 2.0},
        edits=overlay.normalise_edits({"deleted": ["uid:a"]}),
        mark=overlay.normalise_mark({"status": "verified", "tags": ["golden"]}),
        author="said",
    )
    overlay.save_overlay(path, document)
    assert path.is_file()
    reloaded = overlay.load_overlay(path)
    assert reloaded == document
    assert reloaded["sample"] == {"run": "v1/batch", "stem": "scene-abc", "view": "leaf"}
    assert reloaded["created_by"] == "said"
    # Readable in a diff: this file is meant to be reviewed in a pull request.
    assert path.read_text(encoding="utf-8").endswith("\n")
    assert "\n  " in path.read_text(encoding="utf-8")


def test_rewriting_keeps_creation_provenance_and_appends_history(tmp_path):
    path = overlay.overlay_path(tmp_path, "v1/batch", "scene-abc", "leaf")
    stamp = {"path": "x", "sha256": "deadbeef", "size": 1, "mtime": 2.0}
    first = overlay.build_document(
        "v1/batch", "scene-abc", "leaf", stamp,
        overlay.normalise_edits({}), overlay.normalise_mark({"status": "verified"}), "said",
    )
    overlay.save_overlay(path, first)
    second = overlay.build_document(
        "v1/batch", "scene-abc", "leaf", stamp,
        overlay.normalise_edits({"deleted": ["uid:a"]}),
        overlay.normalise_mark({"status": "needs_work"}), "someone-else",
        existing=overlay.load_overlay(path),
    )
    assert second["created_by"] == "said"
    assert second["updated_by"] == "someone-else"
    assert len(second["history"]) == 2


def test_history_is_bounded(tmp_path):
    stamp = {"path": "x", "sha256": "d", "size": 1, "mtime": 2.0}
    document = None
    for _ in range(overlay.HISTORY_LIMIT + 10):
        document = overlay.build_document(
            "v1", "s", "leaf", stamp, overlay.normalise_edits({}),
            overlay.normalise_mark({}), "said", existing=document,
        )
    assert len(document["history"]) == overlay.HISTORY_LIMIT


def test_delete_removes_the_file_and_the_directories_it_emptied(tmp_path):
    path = overlay.overlay_path(tmp_path, "v1/batch", "scene-abc", "leaf")
    overlay.save_overlay(path, {"schema": overlay.SCHEMA})
    assert overlay.delete_overlay(path, tmp_path)
    assert not (tmp_path / "v1").exists()
    assert tmp_path.is_dir()
    assert not overlay.delete_overlay(path, tmp_path)


def test_iter_overlays_ignores_foreign_json(tmp_path):
    overlay.save_overlay(tmp_path / "good.json", {"schema": overlay.SCHEMA, "mark": {}})
    (tmp_path / "quality.json").write_text(json.dumps({"captures": 3}), encoding="utf-8")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    found = [path.name for path, _ in overlay.iter_overlays(tmp_path)]
    assert found == ["good.json"]


# -------------------------------------------------------------- staleness


def test_stamp_detects_a_regenerated_capture(tmp_path):
    source = tmp_path / "scene.elements.leaf.json"
    source.write_text(json.dumps([_element(uid="a")]), encoding="utf-8")
    stamp = overlay.file_stamp(source, tmp_path)
    assert stamp["path"] == "scene.elements.leaf.json"
    document = {"source": stamp}
    assert not overlay.is_stale(document, overlay.file_stamp(source, tmp_path))

    source.write_text(json.dumps([_element(uid="b")]), encoding="utf-8")
    assert overlay.is_stale(document, overlay.file_stamp(source, tmp_path))


def test_an_overlay_with_no_opinion_is_empty():
    assert overlay.is_empty({"mark": {"status": "unmarked"}, "edits": {}})
    assert not overlay.is_empty({"mark": {"status": "verified"}, "edits": {}})
    assert not overlay.is_empty({"mark": {"status": "unmarked", "note": "look at this"},
                                 "edits": {}})
    assert not overlay.is_empty({"mark": {}, "edits": {"deleted": ["uid:a"]}})


# ------------------------------------------------------------- fragments


def test_fragments_are_validated_like_rects():
    fields = overlay.normalise_edits({"modified": {"uid:a": {
        "visible_fragments": [{"x": 1.4, "y": 2, "w": 3, "h": 4}]}}})
    assert fields["modified"]["uid:a"]["visible_fragments"] == [{"x": 1, "y": 2, "w": 3, "h": 4}]
    with pytest.raises(overlay.OverlayError):
        overlay.normalise_edits({"modified": {"uid:a": {"visible_fragments": [{"x": 1}]}}})
    with pytest.raises(overlay.OverlayError):
        overlay.normalise_edits({"modified": {"uid:a": {"visible_fragments": "none"}}})


def test_an_edited_rect_invalidates_the_fragments_computed_for_the_old_one():
    # Coverage is `rect` UNION `visible_fragments`, so fragments left beside a
    # hand-moved rect keep crediting area the person just said is not covered.
    elements = [_element(uid="a", visible_fragments=[{"x": 0, "y": 0, "w": 5, "h": 5}])]
    edits = overlay.normalise_edits({"modified": {"uid:a": {
        "rect": {"x": 90, "y": 90, "w": 10, "h": 10}}}})
    assert overlay.clear_stale_fragments(edits, elements) == ["uid:a"]
    assert edits["modified"]["uid:a"]["visible_fragments"] == []

    merged, _ = overlay.apply(elements, {"edits": edits})
    assert merged[0]["visible_fragments"] == []
    assert merged[0]["_golden"]["original"]["visible_fragments"] == [{"x": 0, "y": 0, "w": 5, "h": 5}]


@pytest.mark.parametrize("edit, expected", [
    ({"role": "label"}, ["uid:a"]),                       # untouched rect: keep
    ({"rect": {"x": 1, "y": 1, "w": 2, "h": 2}, "visible_fragments": []}, []),
])
def test_fragments_survive_anything_that_is_not_a_bare_rect_edit(edit, expected):
    elements = [_element(uid="a", visible_fragments=[{"x": 0, "y": 0, "w": 5, "h": 5}])]
    edits = overlay.normalise_edits({"modified": {"uid:a": edit}})
    assert overlay.clear_stale_fragments(edits, elements) == []
    merged, _ = overlay.apply(elements, {"edits": edits})
    kept = [key for key in ["uid:a"] if merged[0].get("visible_fragments")]
    assert kept == expected


def test_an_element_with_no_fragments_gains_no_noise():
    elements = [_element(uid="a")]
    edits = overlay.normalise_edits({"modified": {"uid:a": {
        "rect": {"x": 1, "y": 1, "w": 2, "h": 2}}}})
    assert overlay.clear_stale_fragments(edits, elements) == []
    assert "visible_fragments" not in edits["modified"]["uid:a"]


def test_a_stale_edit_matching_nothing_is_left_alone():
    edits = overlay.normalise_edits({"modified": {"uid:gone": {
        "rect": {"x": 1, "y": 1, "w": 2, "h": 2}}}})
    assert overlay.clear_stale_fragments(edits, [_element(uid="a")]) == []


# --------------------------------------------------------------- versions


def test_a_version_changes_with_content_and_not_with_key_order():
    document = {"schema": overlay.SCHEMA, "mark": {"status": "verified"}, "edits": {}}
    reordered = {"edits": {}, "mark": {"status": "verified"}, "schema": overlay.SCHEMA}
    assert overlay.document_version(document) == overlay.document_version(reordered)
    assert overlay.document_version(document) != overlay.document_version(
        {"schema": overlay.SCHEMA, "mark": {"status": "rejected"}, "edits": {}})
    assert overlay.document_version(None) is None
