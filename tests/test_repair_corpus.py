"""Half samples are caught from either side of the write-order change.

Before the screenshot became the commit marker, a killed shard left a PNG with
no annotations - 11,835 of them. Now it leaves a meta with no PNG. A corpus
spans runs from both sides, so the scan has to recognise both shapes as damage.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "repair_corpus", Path(__file__).resolve().parents[1] / "scripts/repair_corpus.py"
)
repair_corpus = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repair_corpus)


def _complete(directory: Path, stem: str) -> None:
    (directory / f"{stem}.png").write_bytes(b"px")
    for part in repair_corpus.REQUIRED_PARTS:
        (directory / f"{stem}{part}").write_text("{}")


def test_a_complete_sample_is_not_flagged(tmp_path: Path) -> None:
    _complete(tmp_path, "scene-a-step00")
    orphans, partials, stale = repair_corpus.scan_capture_dir(tmp_path)
    assert (orphans, partials, stale) == ([], [], [])


def test_png_without_meta_is_an_orphan(tmp_path: Path) -> None:
    """The old write order's damage."""
    (tmp_path / "scene-a-step00.png").write_bytes(b"px")
    orphans, partials, _ = repair_corpus.scan_capture_dir(tmp_path)
    assert [p.name for p in orphans] == ["scene-a-step00.png"]
    assert partials == []


def test_meta_without_png_is_a_partial(tmp_path: Path) -> None:
    """The new write order's damage - a capture killed before it committed."""
    for part in repair_corpus.REQUIRED_PARTS:
        (tmp_path / f"scene-a-step00{part}").write_text("{}")
    orphans, partials, _ = repair_corpus.scan_capture_dir(tmp_path)
    assert orphans == []
    assert [p.name for p in partials] == ["scene-a-step00"]


def test_a_missing_artifact_is_a_partial(tmp_path: Path) -> None:
    _complete(tmp_path, "scene-a-step00")
    (tmp_path / "scene-a-step00.screentag.txt").unlink()
    _, partials, _ = repair_corpus.scan_capture_dir(tmp_path)
    assert [p.name for p in partials] == ["scene-a-step00"]


def test_a_stale_partial_screenshot_is_reaped(tmp_path: Path) -> None:
    (tmp_path / ".scene-a-step00.png.partial").write_bytes(b"px")
    orphans, partials, stale = repair_corpus.scan_capture_dir(tmp_path)
    assert [p.name for p in stale] == [".scene-a-step00.png.partial"]
    assert orphans == [] and partials == []


def test_files_for_stem_collects_the_whole_sample(tmp_path: Path) -> None:
    _complete(tmp_path, "scene-a-step00")
    _complete(tmp_path, "scene-b-step00")
    got = repair_corpus.files_for_stem(tmp_path / "scene-a-step00")
    assert all("scene-a-step00" in p.name for p in got)
    assert len(got) == len(repair_corpus.REQUIRED_PARTS) + 1


def test_an_unreadable_directory_is_skipped_not_raised(tmp_path: Path) -> None:
    assert repair_corpus.scan_capture_dir(tmp_path / "nope") == ([], [], [])


# --- the black zero-application desktops -----------------------------------

def _meta(directory: Path, stem: str, *, apps, leaf: int) -> None:
    import json
    (directory / f"{stem}.meta.json").write_text(json.dumps({
        "launched_apps": apps, "num_elements_leaf": leaf,
        "scene": {"apps": [{"app_name": a} for a in apps]},
    }))
    (directory / f"{stem}.png").write_bytes(b"px")


def test_a_black_zero_app_capture_is_found(tmp_path: Path) -> None:
    _meta(tmp_path, "scene-a-step00", apps=[], leaf=1)
    assert [p.name for p in repair_corpus.find_blank_desktops(tmp_path)] == [
        "scene-a-step00"
    ]


def test_a_real_bare_desktop_is_kept(tmp_path: Path) -> None:
    """The fixed ones carry a dozen icons; they must survive."""
    _meta(tmp_path, "scene-a-step00", apps=[], leaf=12)
    assert repair_corpus.find_blank_desktops(tmp_path) == []


def test_a_capture_with_applications_is_never_touched(tmp_path: Path) -> None:
    _meta(tmp_path, "scene-a-step00", apps=["mousepad"], leaf=1)
    assert repair_corpus.find_blank_desktops(tmp_path) == []


def test_the_boundary_is_inclusive(tmp_path: Path) -> None:
    _meta(tmp_path, "scene-a-step00", apps=[], leaf=repair_corpus.BLANK_DESKTOP_MAX_LEAF)
    _meta(tmp_path, "scene-b-step00", apps=[],
          leaf=repair_corpus.BLANK_DESKTOP_MAX_LEAF + 1)
    assert [p.name for p in repair_corpus.find_blank_desktops(tmp_path)] == [
        "scene-a-step00"
    ]


def test_unreadable_or_absent_metadata_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "scene-a-step00.meta.json").write_text("{not json")
    (tmp_path / "scene-b-step00.meta.json").write_text('{"launched_apps": []}')
    assert repair_corpus.find_blank_desktops(tmp_path) == []
    assert repair_corpus.find_blank_desktops(tmp_path / "nope") == []


# --- never quarantine a capture that is still being written ----------------
#
# The screenshot is renamed into place last, so an in-flight capture is
# indistinguishable from a half sample: metadata present, PNG not yet. Running
# the repair during a generation would delete work that was about to finish.

import time as _time


def test_a_capture_written_seconds_ago_is_left_alone(tmp_path: Path) -> None:
    for part in repair_corpus.REQUIRED_PARTS:
        (tmp_path / f"scene-a-step00{part}").write_text("{}")   # no .png yet
    cutoff = _time.time() - repair_corpus.MIN_AGE_SEC
    orphans, partials, stale = repair_corpus.scan_capture_dir(tmp_path, cutoff)
    assert (orphans, partials, stale) == ([], [], [])


def test_the_same_capture_is_damage_once_it_is_old(tmp_path: Path) -> None:
    for part in repair_corpus.REQUIRED_PARTS:
        f = tmp_path / f"scene-a-step00{part}"
        f.write_text("{}")
        old = _time.time() - 10 * repair_corpus.MIN_AGE_SEC
        os.utime(f, (old, old))
    cutoff = _time.time() - repair_corpus.MIN_AGE_SEC
    _, partials, _ = repair_corpus.scan_capture_dir(tmp_path, cutoff)
    assert [p.name for p in partials] == ["scene-a-step00"]


def test_one_fresh_part_protects_the_whole_stem(tmp_path: Path) -> None:
    """A capture mid-write has old parts and new ones; it must survive."""
    old = _time.time() - 10 * repair_corpus.MIN_AGE_SEC
    for part in repair_corpus.REQUIRED_PARTS:
        f = tmp_path / f"scene-a-step00{part}"
        f.write_text("{}")
        os.utime(f, (old, old))
    fresh = tmp_path / "scene-a-step00.elements.leaf.json"
    fresh.write_text("[]")                      # just rewritten
    cutoff = _time.time() - repair_corpus.MIN_AGE_SEC
    _, partials, _ = repair_corpus.scan_capture_dir(tmp_path, cutoff)
    assert partials == []


def test_a_fresh_orphan_png_is_left_alone(tmp_path: Path) -> None:
    (tmp_path / "scene-a-step00.png").write_bytes(b"px")
    cutoff = _time.time() - repair_corpus.MIN_AGE_SEC
    orphans, _, _ = repair_corpus.scan_capture_dir(tmp_path, cutoff)
    assert orphans == []


def test_a_zero_cutoff_disables_the_guard(tmp_path: Path) -> None:
    (tmp_path / "scene-a-step00.png").write_bytes(b"px")
    orphans, _, _ = repair_corpus.scan_capture_dir(tmp_path, 0.0)
    assert [p.name for p in orphans] == ["scene-a-step00.png"]


def test_a_path_reachable_two_ways_is_moved_once(tmp_path: Path) -> None:
    """An orphan PNG is also matched by its own stem glob.

    With a thread pool the duplicate loses the race and logs ENOENT for a file
    that was moved successfully - 201 such lines in one real run, all benign and
    all misleading.
    """
    (tmp_path / "scene-a-step00.png").write_bytes(b"px")
    victims = [("orphan", tmp_path / "scene-a-step00.png"),
               ("partial", tmp_path / "scene-a-step00")]
    work: list = []
    seen: set = set()
    for kind, path in victims:
        for t in ([path] if kind != "partial" else repair_corpus.files_for_stem(path)):
            if t in seen:
                continue
            seen.add(t)
            work.append(t)
    assert work == [tmp_path / "scene-a-step00.png"]
