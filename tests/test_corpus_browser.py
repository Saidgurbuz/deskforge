"""Browsing a corpus that must never be walked.

`SampleIndex` walks and caches; over 300 shards and ~11M files that walk does
not finish. Everything here exists to keep the browser bounded by page size
rather than by corpus size, and to keep a read-only browser read-only.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from deskshot.inspector.app import InspectorApp
from deskshot.inspector.corpus import CorpusBrowser, CorpusError

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _capture(directory: Path, stem: str, elements: int = 3) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (200, 150), (30, 40, 60)).save(directory / (stem + ".png"))
    (directory / (stem + ".elements.leaf.json")).write_text(json.dumps([
        {"type": "Button", "rect": {"x": 5 + 10 * i, "y": 5, "w": 30, "h": 12}}
        for i in range(elements)
    ]))
    (directory / (stem + ".meta.json")).write_text(json.dumps(
        {"launched_apps": ["homebank"], "num_elements_leaf": elements}))
    (directory / (stem + ".screentag.txt")).write_text("<screentag></screentag>")


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "v1"
    scene = root / "shards" / "shard-0000" / "ep" / "abc123"
    _capture(scene, "scene-abc123-step00")
    _capture(scene, "scene-abc123-step01")
    _capture(root / "shards" / "shard-0000" / "st" / "0a", "scene-def456-step00")
    (root / "shards" / "shard-0001" / "ep").mkdir(parents=True)
    # The noise a real shard carries beside its three useful directories.
    for i in range(40):
        (root / "shards" / "shard-0000" / ("scene_seed%d.log" % i)).write_text("x")
    (root / "plan").mkdir(parents=True, exist_ok=True)
    return root


def test_listing_a_level_never_descends(corpus: Path, monkeypatch) -> None:
    """The whole point: no os.walk, ever."""
    import os as os_module
    monkeypatch.setattr(os_module, "walk", _explode)
    browser = CorpusBrowser(corpus)
    assert [d["name"] for d in browser.ls("")["dirs"]] == ["plan", "shards"]
    assert [d["name"] for d in browser.ls("shards")["dirs"]] == ["shard-0000", "shard-0001"]


def _explode(*_args, **_kwargs):
    raise AssertionError("the corpus browser must never walk")


def test_a_directory_reports_what_is_under_it(corpus: Path) -> None:
    page = CorpusBrowser(corpus).ls("shards/shard-0000/ep")
    entry = page["dirs"][0]
    assert entry["name"] == "abc123"
    assert entry["captures"] == 2
    assert entry["children"] == 2 * 4  # png, leaf list, meta, screentag


def test_log_noise_is_counted_not_listed(corpus: Path) -> None:
    page = CorpusBrowser(corpus).ls("shards/shard-0000")
    assert {d["name"] for d in page["dirs"]} == {"ep", "st"}
    assert page["total_samples"] == 0
    assert {row["extension"]: row["count"] for row in page["other_files"]} == {".log": 40}


def test_capture_files_are_not_listed_beside_their_sample(corpus: Path) -> None:
    page = CorpusBrowser(corpus).ls("shards/shard-0000/ep/abc123")
    assert page["total_samples"] == 2
    assert page["other_files"] == [], "the PNG and JSONs are already the sample"


def test_a_sample_needs_both_a_png_and_a_leaf_list(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "orphan.elements.leaf.json").write_text("[]")
    assert CorpusBrowser(tmp_path).ls("d")["total_samples"] == 0


def test_paging_is_bounded(corpus: Path) -> None:
    page = CorpusBrowser(corpus).ls("shards", limit=1)
    assert len(page["dirs"]) == 1 and page["total_dirs"] == 2


def test_the_breadcrumb_walks_back_up(corpus: Path) -> None:
    crumbs = CorpusBrowser(corpus).ls("shards/shard-0000/ep")["parents"]
    assert [c["path"] for c in crumbs] == [
        "", "shards", "shards/shard-0000", "shards/shard-0000/ep"]


# ------------------------------------------------------------- path safety

@pytest.mark.parametrize("bad", ["../..", "../../etc", ".ssh", "./.."])
def test_a_path_outside_the_corpus_is_refused(corpus: Path, bad: str) -> None:
    with pytest.raises(CorpusError):
        CorpusBrowser(corpus).ls(bad)


def test_a_symlink_out_of_the_corpus_is_refused(corpus: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secrets"
    outside.mkdir()
    (corpus / "escape").symlink_to(outside)
    with pytest.raises(CorpusError):
        CorpusBrowser(corpus).ls("escape")


def test_a_sample_path_needs_a_directory_and_a_stem(corpus: Path) -> None:
    with pytest.raises(CorpusError):
        CorpusBrowser(corpus).sample("just-a-stem")


# ------------------------------------------------------------------ index

def _index(corpus: Path) -> Path:
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "build_corpus_index.py"),
         "--corpus", str(corpus)],
        check=True, capture_output=True, text=True,
    )
    return corpus / "plan" / "index.sqlite"


def _write_manifest(corpus: Path, rows) -> None:
    (corpus / "plan" / "manifest.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n")


def test_without_an_index_browsing_works_and_search_says_why(corpus: Path) -> None:
    browser = CorpusBrowser(corpus)
    assert browser.ls("shards")["indexed"] is False
    assert browser.facets() == {"indexed": False}
    with pytest.raises(CorpusError) as caught:
        browser.search(split="train")
    assert "build_corpus_index" in caught.value.message


def test_the_index_covers_samples_the_splits_leave_out(corpus: Path) -> None:
    """The bug: indexing splits alone lost the ~199k rejected captures."""
    _write_manifest(corpus, [
        {"shard": "shard-0000", "path": "ep/abc123/scene-abc123-step00",
         "apps": ["homebank"], "publishable": True, "train_eligible": True,
         "n_elements": 3, "occluded_ratio": 0.5, "scene_id": "abc123"},
        {"shard": "shard-0000", "path": "ep/abc123/scene-abc123-step01",
         "apps": ["eog"], "publishable": False, "train_eligible": False,
         "n_elements": 2, "occluded_ratio": 0.1, "scene_id": "abc123"},
    ])
    (corpus / "plan" / "splits_v3.jsonl").write_text(json.dumps(
        {"shard": "shard-0000", "path": "ep/abc123/scene-abc123-step00",
         "split": "train", "apps": ["homebank"]}) + "\n")
    index = _index(corpus)
    rows = sqlite3.connect(str(index)).execute(
        "SELECT path, split, publishable FROM samples ORDER BY path").fetchall()
    assert len(rows) == 2, "the manifest, not the splits, is the base"
    assert rows[0][1] == "train"
    assert rows[1][1] is None and rows[1][2] == 0


def test_an_app_filter_does_not_match_a_substring(corpus: Path) -> None:
    """`LIKE '%logs%'` would match `gnome-logs`; the delimiters stop it."""
    _write_manifest(corpus, [
        {"shard": "shard-0000", "path": "ep/abc123/scene-abc123-step00",
         "apps": ["gnome-logs"], "publishable": True, "train_eligible": True},
    ])
    _index(corpus)
    browser = CorpusBrowser(corpus)
    assert browser.search(app="logs")["total"] == 0
    assert browser.search(app="gnome-logs")["total"] == 1


def test_shuffling_with_the_same_seed_gives_the_same_sample(corpus: Path) -> None:
    _write_manifest(corpus, [
        {"shard": "shard-0000", "path": "ep/abc123/scene-abc123-step%02d" % i,
         "apps": ["homebank"], "publishable": True, "train_eligible": True}
        for i in range(30)
    ])
    _index(corpus)
    browser = CorpusBrowser(corpus)
    first = browser.search(random_order=True, seed=5, limit=3)["results"]
    again = browser.search(random_order=True, seed=5, limit=3)["results"]
    other = browser.search(random_order=True, seed=6, limit=3)["results"]
    assert [r["path"] for r in first] == [r["path"] for r in again]
    assert [r["path"] for r in first] != [r["path"] for r in other]


def test_a_listing_carries_index_facts_when_there_is_an_index(corpus: Path) -> None:
    _write_manifest(corpus, [
        {"shard": "shard-0000", "path": "ep/abc123/scene-abc123-step00",
         "apps": ["homebank"], "theme": "ubuntu_like", "n_windows": 4,
         "occluded_ratio": 0.42, "publishable": True, "train_eligible": True},
    ])
    (corpus / "plan" / "splits_v3.jsonl").write_text(json.dumps(
        {"shard": "shard-0000", "path": "ep/abc123/scene-abc123-step00",
         "split": "test_id"}) + "\n")
    _index(corpus)
    page = CorpusBrowser(corpus).ls("shards/shard-0000/ep/abc123")
    first = [s for s in page["samples"] if s["stem"].endswith("step00")][0]
    assert first["split"] == "test_id"
    assert first["occluded_ratio"] == 0.42 and first["n_windows"] == 4


# ------------------------------------------------------------------ routes

def _app(corpus: Path, tmp_path: Path) -> InspectorApp:
    runs = tmp_path / "runs"
    runs.mkdir(exist_ok=True)
    return InspectorApp(root=runs, golden_root=tmp_path / "golden",
                        corpus_root=corpus, read_only=True)


def test_the_routes_answer(corpus: Path, tmp_path: Path) -> None:
    app = _app(corpus, tmp_path)
    listing = app.handle("GET", "/api/corpus/ls", {"path": ["shards"]})
    assert listing.status == 200
    assert b"shard-0000" in listing.body

    sample = app.handle("GET", "/api/corpus/sample",
                        {"path": ["shards/shard-0000/ep/abc123/scene-abc123-step00"]})
    assert sample.status == 200
    assert b"screentag" in sample.body


def test_a_figure_is_rendered_and_never_written(corpus: Path, tmp_path: Path) -> None:
    scene = corpus / "shards" / "shard-0000" / "ep" / "abc123"
    before = sorted(p.name for p in scene.iterdir())
    app = _app(corpus, tmp_path)
    response = app.handle("GET", "/api/corpus/figure", {
        "path": ["shards/shard-0000/ep/abc123/scene-abc123-step00"],
        "mode": ["overlay"], "width": ["600"],
    })
    assert response.status == 200
    assert response.body[:8] == b"\x89PNG\r\n\x1a\n"
    assert response.headers["Cache-Control"] == "no-store"
    assert sorted(p.name for p in scene.iterdir()) == before


def test_a_figure_revalidates_by_etag(corpus: Path, tmp_path: Path) -> None:
    app = _app(corpus, tmp_path)
    query = {"path": ["shards/shard-0000/ep/abc123/scene-abc123-step00"], "width": ["600"]}
    first = app.handle("GET", "/api/corpus/figure", query)
    again = app.handle("GET", "/api/corpus/figure", query,
                       headers={"If-None-Match": first.headers["ETag"]})
    assert again.status == 304 and not again.body


@pytest.mark.parametrize("query,status", [
    # A leading dot is refused before the path is even resolved, so this is a
    # 400 rather than the 403 an in-root-but-escaping path would get.
    ({"path": ["../../etc/passwd"]}, 400),
    ({"path": ["shards/shard-0000/ep/abc123/scene-abc123-step00"], "mode": ["evil"]}, 400),
    ({"path": ["nope/nothing"]}, 404),
])
def test_a_bad_figure_request_is_refused(corpus: Path, tmp_path: Path, query, status) -> None:
    assert _app(corpus, tmp_path).handle("GET", "/api/corpus/figure", query).status == status


def test_without_a_corpus_the_routes_explain_themselves(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    app = InspectorApp(root=runs, golden_root=tmp_path / "golden")
    response = app.handle("GET", "/api/corpus/ls", {})
    assert response.status == 404
    assert b"--corpus" in response.body
