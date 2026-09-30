import json

from deskshot.cli import _scene_batch_meta_row, _scene_batch_result_summary_line


def test_scene_batch_summary_line_handles_failed_row() -> None:
    assert _scene_batch_result_summary_line(None) == "  status=failed"


def test_scene_batch_summary_line_handles_partial_worker_row() -> None:
    assert (
        _scene_batch_result_summary_line({"stem": "scene-abc"})
        == "  stem=scene-abc num_elements=unknown"
    )


def test_scene_batch_summary_line_includes_element_count() -> None:
    assert (
        _scene_batch_result_summary_line({"stem": "scene-abc", "num_elements_filtered": 42})
        == "  stem=scene-abc num_elements=42"
    )


def test_scene_batch_meta_row_loads_capture_meta(tmp_path) -> None:
    meta = {"stem": "scene-abc", "num_elements_filtered": 42}
    meta_path = tmp_path / "scene-abc.meta.json"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    assert _scene_batch_meta_row({"stem": "scene-abc", "meta": str(meta_path)}) == meta


def test_scene_batch_meta_row_keeps_plain_meta_row() -> None:
    row = {"stem": "scene-abc", "num_elements_filtered": 42}
    assert _scene_batch_meta_row(row) is row


def test_terminate_scene_process_kills_whole_process_group() -> None:
    """A timed-out scene must not orphan its Xvfb/D-Bus/app children."""
    import os
    import subprocess
    import sys
    import time

    from deskshot.cli import _terminate_scene_process

    script = "import subprocess,time; subprocess.Popen(['sleep','120']); time.sleep(120)"
    proc = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    try:
        time.sleep(2)
        pgid = os.getpgid(proc.pid)
        before = subprocess.run(
            ["pgrep", "-g", str(pgid)], capture_output=True, text=True
        ).stdout.split()
        assert len(before) >= 2, "expected the child plus its grandchild"

        _terminate_scene_process(proc)
        time.sleep(1)

        after = subprocess.run(
            ["pgrep", "-g", str(pgid)], capture_output=True, text=True
        ).stdout.split()
        assert after == [], f"orphaned processes survived: {after}"
    finally:
        if proc.poll() is None:
            proc.kill()


def test_terminate_scene_process_tolerates_already_dead_process() -> None:
    import subprocess
    import sys

    from deskshot.cli import _terminate_scene_process

    proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    proc.wait()

    _terminate_scene_process(proc)  # must not raise


def test_scene_batch_parser_defaults_scene_timeout() -> None:
    from deskshot.cli import build_parser

    args = build_parser().parse_args(
        ["scene-batch", "--start-seed", "1", "--count", "1"]
    )

    assert args.scene_timeout == 900


def test_terminate_scene_process_reaps_children_that_ignore_sigint(tmp_path) -> None:
    """Xvfb-like stragglers can ignore SIGINT; the child exiting is not enough."""
    import os
    import subprocess
    import time

    from deskshot.cli import _terminate_scene_process, _scene_group_alive

    # bash makes background jobs ignore SIGINT, mimicking a stubborn Xvfb.
    script = tmp_path / "stubborn.sh"
    script.write_text("#!/usr/bin/env bash\nsleep 120 &\nwait\n", encoding="utf-8")
    script.chmod(0o755)

    proc = subprocess.Popen([str(script)], start_new_session=True)
    try:
        time.sleep(2)
        pgid = os.getpgid(proc.pid)
        before = subprocess.run(
            ["pgrep", "-g", str(pgid)], capture_output=True, text=True
        ).stdout.split()
        assert len(before) >= 2

        _terminate_scene_process(proc)
        time.sleep(1)

        after = subprocess.run(
            ["pgrep", "-g", str(pgid)], capture_output=True, text=True
        ).stdout.split()
        assert after == [], f"stragglers survived: {after}"
        assert _scene_group_alive(pgid) is False
    finally:
        if proc.poll() is None:
            proc.kill()


def test_scene_batch_summary_line_includes_scene_wall_clock() -> None:
    """Per-scene wall clock is what job sizing and cost-per-sample need."""
    from deskshot.cli import _scene_batch_result_summary_line

    line = _scene_batch_result_summary_line(
        {
            "stem": "scene-abc",
            "num_elements_filtered": 147,
            "scene_timing": {"scene_wall_sec": 61.2},
        }
    )

    assert line == "  stem=scene-abc num_elements=147 scene_sec=61.2"


def test_scene_batch_summary_line_without_timing_is_unchanged() -> None:
    from deskshot.cli import _scene_batch_result_summary_line

    line = _scene_batch_result_summary_line({"stem": "scene-abc", "num_elements_filtered": 5})

    assert line == "  stem=scene-abc num_elements=5"


def test_persist_scene_wall_clock_merges_and_writes(tmp_path) -> None:
    """Both the subprocess and worker paths must record wall clock."""
    import json as _json

    from deskshot.cli import _persist_scene_wall_clock

    meta = tmp_path / "scene.meta.json"
    meta.write_text(_json.dumps({"stem": "s"}), encoding="utf-8")
    row = {"stem": "s", "meta": str(meta), "scene_timing": {"app_launch_total_sec": 12.0}}

    out = _persist_scene_wall_clock(row, 61.2345)

    assert out["scene_timing"] == {"app_launch_total_sec": 12.0, "scene_wall_sec": 61.234}
    assert _json.loads(meta.read_text())["scene_timing"]["scene_wall_sec"] == 61.234


def test_persist_scene_wall_clock_tolerates_missing_row() -> None:
    from deskshot.cli import _persist_scene_wall_clock

    assert _persist_scene_wall_clock(None, 1.0) is None
