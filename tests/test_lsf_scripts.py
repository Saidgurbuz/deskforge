from __future__ import annotations

import os
import subprocess
import pytest
import sys
from pathlib import Path

from deskshot.config import PROJECT_ROOT


def test_submit_scene_batch_jobs_dry_run_uses_run_scoped_names_and_displays(tmp_path: Path) -> None:
    output_root = tmp_path / "lsf_out"
    env = os.environ.copy()
    env.update(
        {
            "DRY_RUN": "true",
            "MAX_CONCURRENT": "10",
            "RUN_ID": "unitrun",
            "RUN_DISPLAY_OFFSET": "9000",
            "DISPLAY_OFFSET": "400",
            "QUEUE": "normal",
            "WALL_TIME": "1:00",
        }
    )

    result = subprocess.run(
        [
            "bash",
            str(PROJECT_ROOT / "scripts" / "submit_scene_batch_jobs.sh"),
            "3",
            "5",
            str(output_root),
            "2",
            "3",
            "100",
            "firefox",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert "DRY_RUN bsub" in result.stdout
    assert "dsd_unitrun_part_2" in result.stdout
    assert "dsd_unitrun_part_3" in result.stdout
    assert "-q normal" in result.stdout
    assert "-W 1:00" in result.stdout
    assert "base_display=9464" in result.stdout
    assert "base_display=9496" in result.stdout
    assert str(output_root) in result.stdout


def test_submit_scene_batch_jobs_honors_env_range_and_seed(tmp_path: Path) -> None:
    output_root = tmp_path / "lsf_out"
    env = os.environ.copy()
    env.update(
        {
            "DRY_RUN": "true",
            "MAX_CONCURRENT": "1",
            "RUN_ID": "envrun",
            "RUN_DISPLAY_OFFSET": "0",
            "DISPLAY_OFFSET": "400",
            "SCENES_PER_JOB": "1",
            "PARALLEL_WORKERS_PER_JOB": "1",
            "OUTPUT_ROOT": str(output_root),
            "START_PART": "5",
            "END_PART": "5",
            "START_SEED": "12345",
            "APP_ALLOWLIST": "mousepad,xarchiver",
            "SESSION_GROUP_SIZE_PER_JOB": "3",
        }
    )

    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "scripts" / "submit_scene_batch_jobs.sh")],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert "parts=5-5" in result.stdout
    assert "start_seed=12345" in result.stdout
    assert "desktop_chrome=true" in result.stdout
    assert "session_group_size=3" in result.stdout
    assert "apps=mousepad,xarchiver" in result.stdout
    assert "dsd_envrun_part_5" in result.stdout
    assert "run_scene_batch_lsf_job.sh" in result.stdout
    assert f"{output_root} 5 12345 1 1 560 20 true mousepad\\,xarchiver 3" in result.stdout


def test_submit_scene_batch_jobs_can_disable_desktop_chrome_explicitly(tmp_path: Path) -> None:
    output_root = tmp_path / "lsf_out"
    env = os.environ.copy()
    env.update(
        {
            "DRY_RUN": "true",
            "MAX_CONCURRENT": "1",
            "RUN_ID": "nochrome",
            "RUN_DISPLAY_OFFSET": "0",
            "DISPLAY_OFFSET": "400",
            "SCENES_PER_JOB": "1",
            "PARALLEL_WORKERS_PER_JOB": "1",
            "OUTPUT_ROOT": str(output_root),
            "START_PART": "0",
            "END_PART": "0",
            "START_SEED": "100",
            "INCLUDE_DESKTOP_CHROME": "false",
        }
    )

    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "scripts" / "submit_scene_batch_jobs.sh")],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert "desktop_chrome=false" in result.stdout
    assert f"{output_root} 0 100 1 1 400 20 false '' 1" in result.stdout


def test_submit_scene_batch_jobs_does_not_resubmit_failed_part_by_default(tmp_path: Path) -> None:
    output_root = tmp_path / "lsf_out"
    failed_dir = output_root / ".lsbatch" / "parts_failed"
    failed_dir.mkdir(parents=True)
    (failed_dir / "part_0005.failed").write_text("failed\n", encoding="utf-8")

    env = os.environ.copy()
    env.update(
        {
            "DRY_RUN": "true",
            "MAX_CONCURRENT": "1",
            "RUN_ID": "failrun",
            "RUN_DISPLAY_OFFSET": "0",
            "DISPLAY_OFFSET": "400",
            "SCENES_PER_JOB": "1",
            "PARALLEL_WORKERS_PER_JOB": "1",
            "OUTPUT_ROOT": str(output_root),
            "START_PART": "5",
            "END_PART": "5",
            "START_SEED": "12345",
        }
    )

    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "scripts" / "submit_scene_batch_jobs.sh")],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 1
    assert "all_parts_terminal done=0/1 failed=1/1" in result.stdout
    assert "submitter_done_with_failures failed=1/1" in result.stdout
    assert "DRY_RUN bsub" not in result.stdout


def test_run_scene_batch_lsf_job_uses_job_local_tmp_and_skips_merge(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python"
    fake_python.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                'if [[ "${1:-}" == "-" ]]; then',
                f'  exec "{sys.executable}" "$@"',
                "fi",
                'if [[ "${1:-}" == "-m" && "${2:-}" == "deskshot.cli" && "${3:-}" == "preflight" ]]; then',
                '  echo "PREFLIGHT_ARGS=$*"',
                "  exit 0",
                "fi",
                'if [[ "${1:-}" == "-m" && "${2:-}" == "deskshot.cli" ]]; then',
                '  echo "TMPDIR=${TMPDIR}"',
                '  echo "DESKSHOT_BIN_FIX_DIR=${DESKSHOT_BIN_FIX_DIR}"',
                '  echo "ARGS=$*"',
                "  exit 0",
                "fi",
                f'exec "{sys.executable}" "$@"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    output_root = tmp_path / "output"
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["MERGE_EACH_JOB"] = "false"

    result = subprocess.run(
        [
            "bash",
            str(PROJECT_ROOT / "scripts" / "run_scene_batch_lsf_job.sh"),
            str(output_root),
            "7",
            "7000",
            "2",
            "1",
            "900",
            "40",
            "true",
            "firefox",
            "2",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    job_dir = output_root / "jobs" / "part_0007"
    assert (output_root / ".lsbatch" / "parts_done" / "part_0007.done").is_file()
    log = (job_dir / "job_runner.log").read_text(encoding="utf-8")
    assert "PREFLIGHT_ARGS=-m deskshot.cli preflight --strict-compute --apps firefox" in log
    assert log.index("PREFLIGHT_ARGS=") < log.index("ARGS=-m deskshot.cli scene-batch")
    assert f"TMPDIR={job_dir / 'tmp'}" in log
    assert f"DESKSHOT_BIN_FIX_DIR={job_dir / 'tmp' / 'deskshot_bin_fix'}" in log
    assert "--include-chrome" in log
    assert "--session-group-size 2" in log
    assert "session_group_size=2" in log
    assert "Skipping per-job merge" in log


def test_run_scene_batch_lsf_job_fails_before_scene_batch_when_preflight_fails(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python"
    calls = tmp_path / "calls.log"
    fake_python.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                f'echo "$*" >> "{calls}"',
                'if [[ "${1:-}" == "-" ]]; then',
                f'  exec "{sys.executable}" "$@"',
                "fi",
                'if [[ "${1:-}" == "-m" && "${2:-}" == "deskshot.cli" && "${3:-}" == "preflight" ]]; then',
                '  echo "preflight failed"',
                "  exit 7",
                "fi",
                'if [[ "${1:-}" == "-m" && "${2:-}" == "deskshot.cli" && "${3:-}" == "scene-batch" ]]; then',
                '  echo "scene-batch should not run"',
                "  exit 0",
                "fi",
                f'exec "{sys.executable}" "$@"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    output_root = tmp_path / "output"
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"

    result = subprocess.run(
        [
            "bash",
            str(PROJECT_ROOT / "scripts" / "run_scene_batch_lsf_job.sh"),
            str(output_root),
            "1",
            "100",
            "1",
            "1",
            "900",
            "20",
            "true",
            "gnome-calculator",
            "1",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 7
    failed_marker = output_root / ".lsbatch" / "parts_failed" / "part_0001.failed"
    assert failed_marker.is_file()
    assert "preflight_failed" in failed_marker.read_text(encoding="utf-8")
    call_text = calls.read_text(encoding="utf-8")
    assert "preflight" in call_text
    assert "scene-batch" not in call_text


def _fake_python_bin(tmp_path: Path) -> Path:
    """A python stub that records the CLI args the job runner builds."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    fake_python = fake_bin / "python"
    fake_python.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                'if [[ "${1:-}" == "-" ]]; then',
                f'  exec "{sys.executable}" "$@"',
                "fi",
                'if [[ "${1:-}" == "-m" && "${2:-}" == "deskshot.cli" && "${3:-}" == "preflight" ]]; then',
                "  exit 0",
                "fi",
                'if [[ "${1:-}" == "-m" && "${2:-}" == "deskshot.cli" ]]; then',
                '  echo "ARGS=$*"',
                "  exit 0",
                "fi",
                f'exec "{sys.executable}" "$@"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    return fake_bin


@pytest.mark.parametrize("chrome_value", ["1", "true", "TRUE", "yes", "on"])
def test_job_runner_treats_truthy_chrome_values_as_include(tmp_path: Path, chrome_value: str) -> None:
    """INCLUDE_DESKTOP_CHROME=1 used to silently pass --no-include-chrome."""
    fake_bin = _fake_python_bin(tmp_path)
    output_root = tmp_path / "output"
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"

    result = subprocess.run(
        [
            "bash",
            str(PROJECT_ROOT / "scripts" / "run_scene_batch_lsf_job.sh"),
            str(output_root), "3", "3000", "1", "1", "900", "20", chrome_value, "", "1",
        ],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=30,
    )

    assert result.returncode == 0, result.stderr
    log = (output_root / "jobs" / "part_0003" / "job_runner.log").read_text(encoding="utf-8")
    assert "--include-chrome" in log
    assert "--no-include-chrome" not in log


@pytest.mark.parametrize("chrome_value", ["0", "false", "no", "off"])
def test_job_runner_treats_falsy_chrome_values_as_exclude(tmp_path: Path, chrome_value: str) -> None:
    """An omitted/empty arg still defaults to true; only explicit falsy excludes."""
    fake_bin = _fake_python_bin(tmp_path)
    output_root = tmp_path / "output"
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"

    result = subprocess.run(
        [
            "bash",
            str(PROJECT_ROOT / "scripts" / "run_scene_batch_lsf_job.sh"),
            str(output_root), "4", "4000", "1", "1", "900", "20", chrome_value, "", "1",
        ],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=30,
    )

    assert result.returncode == 0, result.stderr
    log = (output_root / "jobs" / "part_0004" / "job_runner.log").read_text(encoding="utf-8")
    assert "--no-include-chrome" in log


@pytest.mark.parametrize("dry_value", ["1", "true", "YES", "on"])
def test_submitter_dry_run_accepts_truthy_values(tmp_path: Path, dry_value: str) -> None:
    """DRY_RUN=1 previously fell through and submitted real jobs."""
    env = os.environ.copy()
    env.update(
        {
            "DRY_RUN": dry_value,
            "START_PART": "0",
            "END_PART": "0",
            "SCENES_PER_JOB": "1",
            "PARALLEL_WORKERS_PER_JOB": "1",
            "OUTPUT_ROOT": str(tmp_path / "out"),
            "START_SEED": "1000",
            "MAX_CONCURRENT": "1",
            "INCLUDE_DESKTOP_CHROME": "1",
        }
    )

    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "scripts" / "submit_scene_batch_jobs.sh")],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "DRY_RUN bsub" in result.stdout
    assert "dry_run_done" in result.stdout
    # Truthy chrome must reach the job runner as the literal the script tests for.
    assert " true " in result.stdout


def _dry_run_part_args(tmp_path: Path, **env_over) -> list[list[str]]:
    """Return the per-part positional args from a dry-run submission."""
    env = os.environ.copy()
    env.update(
        {
            "DRY_RUN": "1",
            "START_PART": "0",
            "END_PART": "3",
            "SCENES_PER_JOB": "2",
            "PARALLEL_WORKERS_PER_JOB": "1",
            "OUTPUT_ROOT": str(tmp_path / "out"),
            "START_SEED": "500000",
            "MAX_CONCURRENT": "4",
        }
    )
    env.update(env_over)
    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "scripts" / "submit_scene_batch_jobs.sh")],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    rows = []
    for line in result.stdout.splitlines():
        if not line.startswith("DRY_RUN"):
            continue
        marker = "run_scene_batch_lsf_job.sh "
        rows.append(line[line.index(marker) + len(marker):].split())
    return rows


def test_part_seed_blocks_do_not_overlap(tmp_path: Path) -> None:
    """A part burns one seed per rejected plan, so blocks must be attempt-sized.

    Striding by SCENES_PER_JOB let a part that rejected any scene walk into the
    next part's seeds and regenerate identical scenes.
    """
    rows = _dry_run_part_args(tmp_path)
    assert len(rows) == 4

    ranges = []
    for row in rows:
        start_seed, count, max_attempts = int(row[2]), int(row[3]), int(row[6])
        assert max_attempts >= count
        ranges.append((start_seed, start_seed + max_attempts))

    for (a_start, a_end), (b_start, _) in zip(ranges, ranges[1:]):
        assert b_start >= a_end, f"seed blocks overlap: {a_start}-{a_end} then {b_start}"


def test_seed_stride_tracks_attempt_multiplier(tmp_path: Path) -> None:
    rows = _dry_run_part_args(tmp_path, SEED_ATTEMPT_MULTIPLIER="50")

    starts = [int(r[2]) for r in rows]
    attempts = {int(r[6]) for r in rows}

    assert attempts == {100}  # 2 scenes * 50
    assert [s - starts[0] for s in starts] == [0, 100, 200, 300]


def _preflight_failing_python(tmp_path: Path, message: str) -> Path:
    """A python stub whose preflight fails with `message`."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    fake_python = fake_bin / "python"
    fake_python.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                'if [[ "${1:-}" == "-" ]]; then',
                f'  exec "{sys.executable}" "$@"',
                "fi",
                'if [[ "${1:-}" == "-m" && "${2:-}" == "deskshot.cli" && "${3:-}" == "preflight" ]]; then',
                f'  echo "{message}"',
                "  exit 1",
                "fi",
                'if [[ "${1:-}" == "-m" && "${2:-}" == "deskshot.cli" ]]; then',
                '  echo "ARGS=$*"',
                "  exit 0",
                "fi",
                f'exec "{sys.executable}" "$@"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    return fake_bin


def _run_part(tmp_path: Path, fake_bin: Path, part: str = "9"):
    output_root = tmp_path / "output"
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    result = subprocess.run(
        [
            "bash",
            str(PROJECT_ROOT / "scripts" / "run_scene_batch_lsf_job.sh"),
            str(output_root), part, "100", "1", "1", "900", "20", "true", "", "1",
        ],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=40,
    )
    return result, output_root


def test_transient_node_failure_leaves_part_unmarked_for_retry(tmp_path: Path) -> None:
    """A full disk on one node must not permanently lose the part.

    RESUBMIT_FAILED defaults to false, so a .failed marker means the part is
    never retried - observed at 8 jobs when three landed on a full host.
    """
    fake_bin = _preflight_failing_python(tmp_path, "OSError: [Errno 28] No space left on device")

    result, output_root = _run_part(tmp_path, fake_bin)

    assert result.returncode != 0
    failed = output_root / ".lsbatch" / "parts_failed" / "part_0009.failed"
    retry = output_root / ".lsbatch" / "parts_failed" / "part_0009.transient"
    assert not failed.exists(), "transient failure must not be marked permanently failed"
    assert retry.is_file() and retry.read_text().strip() == "1"


def test_transient_failure_gives_up_after_max_retries(tmp_path: Path) -> None:
    fake_bin = _preflight_failing_python(tmp_path, "OSError: [Errno 28] No space left on device")

    for _ in range(3):
        result, output_root = _run_part(tmp_path, fake_bin)

    failed = output_root / ".lsbatch" / "parts_failed" / "part_0009.failed"
    assert failed.is_file(), "a persistent transient failure must eventually terminate"


def test_real_preflight_failure_is_marked_immediately(tmp_path: Path) -> None:
    fake_bin = _preflight_failing_python(tmp_path, "fail  thunar  missing  Binary not found")

    result, output_root = _run_part(tmp_path, fake_bin)

    failed = output_root / ".lsbatch" / "parts_failed" / "part_0009.failed"
    retry = output_root / ".lsbatch" / "parts_failed" / "part_0009.transient"
    assert failed.is_file()
    assert not retry.exists()
