"""The inspector UI, driven in a real browser.

Everything else about the inspector is testable without one: `test_inspector_app`
covers routing, path safety and the overlay round trip as plain function calls.
What that cannot see is the half that broke in use - a sidebar that renders
nothing, a click that lands on the wrong sample because another load was in
flight, an edit that never leaves the page. This runs the actual page in
headless Chromium against a real server and reads the verdict out of the DOM.

The scenario itself lives in `static/probe.html` rather than here, so the same
checks can be watched in a browser when one of them fails:

    PYTHONPATH=src python scripts/inspect_annotations.py
    # then open http://localhost:8000/static/probe.html?write=1

Chromium is not installed on this machine as a package - the pipeline extracts
it into `tools/` and stages a runnable copy under `/tmp/deskshot_bin_fix`. If
neither is there, this skips rather than fails: the UI check is worth having,
but it is not worth blocking the suite on somebody else's environment.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
from pathlib import Path

import browser_support
import pytest
from PIL import Image

from deskshot.extraction.run_extraction import _basic_screentag
from deskshot.inspector.server import build_server

PROJECT_ROOT = Path(__file__).resolve().parents[1]

SUMMARY = re.compile(r"PROBE pass=(\d+) fail=(\d+) skip=(\d+)")
ROW = re.compile(r'<li class="(\w+)">(.*?)</li>', re.S)


#: Both browser tests need the same staged binary and loader path, so the
#: discovery lives in `tests/browser_support.py` and these are aliases.
_chromium = browser_support.chromium
_environment = browser_support.environment


#: The probe's captures are the size of the frame it draws them in.
VIEWPORT = (640, 480)


def _elements(count):
    """A window holding boxes far enough apart to be clicked individually.

    The window is here so the ScreenTag has real nesting - a flat list would not
    exercise the innermost-block rule the tag pane depends on - and so the
    serializer emits a `<title>` token to render beside the block.
    """
    out = [{
        "uid": "uidwin",
        "role": "frame",
        "type": "Window",
        "name": "Probe window",
        "app_name": "mousepad",
        "rect": {"x": 20, "y": 20, "w": 600, "h": 420},
        "_dom_index": 0,
        "children_indices": list(range(1, count + 1)),
        "reading_order_index": 0,
        "source": "app",
        "visible_text": "",
        "visible_text_status": "name_only",
    }]
    for index in range(count):
        out.append({
            "uid": "uid%d" % index,
            "role": "push button",
            "type": "Button",
            "name": "Button %d" % index,
            "app_name": "mousepad",
            "rect": {"x": 40 + index * 90, "y": 60 + index * 40, "w": 70, "h": 30},
            "_dom_index": index + 1,
            "reading_order_index": index + 1,
            "source": "app",
            "visible_text": "Button %d" % index,
            "visible_text_status": "full_visible",
            "interaction": {"actionable": True},
        })
    return out


def _capture(directory: Path, stem: str, elements: int):
    Image.new("RGB", VIEWPORT, (40, 44, 52)).save(str(directory / (stem + ".png")))
    listing = _elements(elements)
    (directory / (stem + ".elements.leaf.json")).write_text(
        json.dumps(listing), encoding="utf-8")
    (directory / (stem + ".meta.json")).write_text(json.dumps({
        "num_elements_leaf": len(listing), "launched_apps": ["mousepad"],
        "screentag_source": "leaf",
        "viewport": {"width": VIEWPORT[0], "height": VIEWPORT[1]},
        "scene": {"seed": 1000 + elements, "theme_preset": "macos_tahoe_like"},
    }), encoding="utf-8")
    # Written by the pipeline's own serializer, so the span index is exercised
    # against the real format rather than against a fixture's idea of it.
    (directory / (stem + ".screentag.txt")).write_text(
        _basic_screentag(listing, VIEWPORT[0], VIEWPORT[1]), encoding="utf-8")


@pytest.fixture()
def live(tmp_path):
    """Two runs under one folder, because the race check needs a second run."""
    root = tmp_path / "runs"
    first = root / "v900_probe" / "batch"
    second = root / "v901_other" / "batch"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    for index in range(3):
        _capture(first, "scene-probe%d" % index, 3 + index)
    _capture(second, "scene-elsewhere", 2)

    server = build_server(root=root, golden_root=tmp_path / "golden", host="127.0.0.1",
                          port=0, author="probe", cache_dir=tmp_path / "cache",
                          project_root=tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1], tmp_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.skipif(_chromium() is None, reason="no chromium on this machine")
def test_the_ui_renders_navigates_and_saves(live, tmp_path):
    base, workspace = live
    target = base + "/static/probe.html?write=1&run=v900_probe/batch&timeout=15000"
    process = subprocess.run(
        [
            _chromium(), "--headless", "--disable-gpu", "--no-sandbox",
            "--disable-dev-shm-usage", "--disable-extensions",
            "--user-data-dir=%s" % (tmp_path / "chrome-profile"),
            # Virtual time runs the page's timers as fast as its fetches allow
            # and dumps the DOM when the budget is spent, so a scripted page can
            # be driven without a debugger protocol.
            "--virtual-time-budget=60000", "--dump-dom", target,
        ],
        env=_environment(), capture_output=True, timeout=240,
    )
    html = process.stdout.decode("utf-8", "replace")
    rows = ["%s %s" % (state.upper(), re.sub(r"<[^>]+>", " ", body).strip())
            for state, body in ROW.findall(html)]
    detail = "\n".join(rows) or process.stderr.decode("utf-8", "replace")[-2000:]

    found = SUMMARY.search(html)
    assert found, "the probe page never finished:\n%s" % detail
    passed, failed, skipped = (int(value) for value in found.groups())
    assert failed == 0, "browser checks failed:\n%s" % detail
    assert skipped == 0, "browser checks were skipped:\n%s" % detail
    # The story is sequential, so a count well below what it emits means it
    # stopped early rather than that a check was dropped. The tag-linking block
    # is five of them; losing it alone would land under this bar.
    assert passed >= 15, "expected the whole scenario to run:\n%s" % detail

    # The probe deletes the overlay it wrote, and nothing else may be left over.
    assert not list((workspace / "golden").rglob("*.json"))
