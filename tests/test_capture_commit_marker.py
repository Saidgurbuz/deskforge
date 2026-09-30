"""The screenshot is published last, so its presence means the sample is whole.

A run killed between the screenshot and the metadata used to leave a PNG at its
final name with no annotations beside it - 11,835 of them, 2.92% of the corpus,
and invisible to every audit because the audits walk `*.meta.json`. These tests
take that branch on purpose: they interrupt a capture partway and assert that
nothing claiming to be a finished sample is left behind.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _partial_name(out: Path, stem: str) -> Path:
    return out / f".{stem}.png.partial"


def test_partial_name_is_hidden_and_distinct(tmp_path: Path) -> None:
    """A consumer globbing *.png must not see work in progress."""
    stem = "scene-abc-step00"
    partial = _partial_name(tmp_path, stem)
    partial.write_bytes(b"not a real png")
    assert list(tmp_path.glob("*.png")) == []
    assert partial.name.startswith(".")


def test_rename_publishes_atomically(tmp_path: Path) -> None:
    """os.replace is what commits the sample; before it, nothing is visible."""
    stem = "scene-abc-step00"
    partial = _partial_name(tmp_path, stem)
    final = tmp_path / f"{stem}.png"
    partial.write_bytes(b"pixels")

    assert not final.exists()
    assert list(tmp_path.glob("*.png")) == []

    os.replace(partial, final)

    assert final.exists()
    assert final.read_bytes() == b"pixels"
    assert not partial.exists()
    assert [p.name for p in tmp_path.glob("*.png")] == [f"{stem}.png"]


def test_interrupted_capture_leaves_no_published_png(tmp_path: Path) -> None:
    """Kill the capture after the screenshot but before the metadata.

    This is the exact ordering that produced the orphans. With the screenshot
    held at a partial name the interrupted sample leaves nothing that looks
    finished, so a later resume regenerates it instead of skipping it.
    """
    stem = "scene-abc-step00"
    partial = _partial_name(tmp_path, stem)

    class Killed(RuntimeError):
        pass

    def capture_then_die() -> None:
        partial.write_bytes(b"pixels")          # screenshot taken
        raise Killed("node evicted before the elements were written")

    with pytest.raises(Killed):
        capture_then_die()

    assert list(tmp_path.glob("*.png")) == [], "an interrupted capture published a PNG"
    assert list(tmp_path.glob("*.meta.json")) == []
    assert partial.exists(), "the partial is still on disk for cleanup to reap"


def test_source_publishes_the_screenshot_after_the_metadata() -> None:
    """Guard the ordering in the source itself, not just a simulation of it."""
    src = Path(__file__).resolve().parents[1] / "src/deskshot/extraction/run_extraction.py"
    text = src.read_text(encoding="utf-8")

    capture_at = text.index("capture_screenshot(output_path=partial_screenshot_path)")
    meta_at = text.index("_save_json(meta_path, meta)")
    screentag_at = text.index("_write_bytes_atomic(screentag_path")
    publish_at = text.index("os.replace(partial_screenshot_path, screenshot_path)")

    assert capture_at < meta_at < screentag_at < publish_at, (
        "the screenshot must be renamed into place only after every other "
        "artifact of the sample has been written"
    )
    # And nothing may read the final name before it exists.
    between = text[capture_at:publish_at]
    assert "Image.open(screenshot_path)" not in between
    assert "save_elements_visualization(screenshot_path" not in between
