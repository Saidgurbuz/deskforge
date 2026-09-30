from __future__ import annotations

from dataclasses import asdict
import io
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from deskshot.generation.scene_composer import SceneApp, SceneConfig, WindowRect
from deskshot.generation.scene_worker import (
    PersistentSceneWorker,
    deserialize_scene_config,
    run_scene_worker_main,
    stripe_scene_jobs,
)


def _scene(seed: int) -> SceneConfig:
    return SceneConfig(
        scene_id=f"scene-{seed}",
        seed=seed,
        theme_preset="ubuntu_like",
        display_preset="fhd_1920x1080",
        panel_variant="top_slim",
        desktop_profile="balanced",
        desktop_layout_template="center_cluster",
        desktop_content_pack="engineering_dev",
        wallpaper_seed=seed + 100,
        desktop_seed=seed + 200,
        layout="split",
        apps=[
            SceneApp(
                app_name="chromium-browser",
                state_ref="single_python_downloads_scrolled",
                rect=WindowRect(0, 0, 900, 600),
            ),
            SceneApp(
                app_name="vscode",
                state_ref="default",
                rect=WindowRect(920, 0, 800, 600),
            ),
        ],
    )


def test_deserialize_scene_config_roundtrips_asdict() -> None:
    scene = _scene(11)
    rebuilt = deserialize_scene_config(asdict(scene))
    assert rebuilt == scene


def test_stripe_scene_jobs_distributes_round_robin() -> None:
    scenes = [_scene(seed) for seed in range(5)]
    lanes = stripe_scene_jobs(scenes, 2)

    assert [[scene.seed for _idx, scene in lane] for lane in lanes] == [
        [0, 2, 4],
        [1, 3],
    ]


def test_read_response_skips_stdout_noise() -> None:
    worker = PersistentSceneWorker(
        worker_id="w00",
        display_number=99,
        output_dir=Path("/tmp"),
        project_root=Path("/tmp"),
        env={},
        tools_dir=None,
        include_desktop_chrome=True,
    )
    worker.proc = SimpleNamespace(
        stdout=io.StringIO("INFO noisy line\n{\"cmd\":\"ready\",\"worker_id\":\"w00\"}\n"),
        poll=lambda: None,
    )
    worker.stderr_handle = io.StringIO()

    payload = worker._read_response()

    assert payload["cmd"] == "ready"
    assert "[stdout-noise] INFO noisy line" in worker.stderr_handle.getvalue()


def test_run_scene_worker_main_keeps_protocol_on_original_stdout(monkeypatch, tmp_path: Path) -> None:
    protocol_out = io.StringIO()
    fake_stderr = io.StringIO()
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", fake_stderr)
    monkeypatch.setattr("sys.stdin", io.StringIO("{\"cmd\":\"shutdown\"}\n"))

    config = SimpleNamespace(
        session=SimpleNamespace(
            display=SimpleNamespace(display_number=123),
        )
    )

    rc = run_scene_worker_main(
        worker_id="w00",
        config=config,
        output_dir=tmp_path,
        include_desktop_chrome=True,
        preloaded_manifests={},
        protocol_out=protocol_out,
    )

    assert rc == 0
    lines = [line for line in protocol_out.getvalue().splitlines() if line.strip()]
    assert lines
    assert '"cmd": "ready"' in lines[0]


def test_close_asks_the_worker_to_shut_down_before_signalling_it() -> None:
    """A worker must get the chance to run its atexit handlers.

    The worker process owns this process's accessibility bus daemon and stops
    it from `atexit`, which python skips under default SIGTERM handling. Going
    straight to `terminate()` orphaned one `dbus-daemon --config-file=...
    accessibility.conf` per worker per batch wave - four were left behind by a
    single six-scene run.
    """
    worker = PersistentSceneWorker(
        worker_id="w00",
        display_number=99,
        output_dir=Path("/tmp"),
        project_root=Path("/tmp"),
        env={},
        tools_dir=None,
        include_desktop_chrome=True,
    )
    written: list[str] = []
    events: list[str] = []
    alive = {"value": True}

    class _Stdin:
        def write(self, data):
            written.append(data)

        def flush(self):
            pass

        def close(self):
            pass

    def _wait(timeout=None):
        alive["value"] = False
        events.append("waited")
        return 0

    worker.proc = SimpleNamespace(
        stdin=_Stdin(),
        stdout=io.StringIO(),
        poll=lambda: None if alive["value"] else 0,
        terminate=lambda: events.append("terminate"),
        kill=lambda: events.append("kill"),
        wait=_wait,
    )
    worker.stderr_handle = io.StringIO()

    worker.close()

    assert '"cmd": "shutdown"' in "".join(written)
    assert "terminate" not in events
    assert "kill" not in events


def test_close_still_terminates_a_worker_that_ignores_shutdown() -> None:
    """A hung worker must not hold up the batch just because we ask nicely."""
    worker = PersistentSceneWorker(
        worker_id="w01",
        display_number=99,
        output_dir=Path("/tmp"),
        project_root=Path("/tmp"),
        env={},
        tools_dir=None,
        include_desktop_chrome=True,
    )
    events: list[str] = []

    def _wait(timeout=None):
        events.append("wait")
        if "kill" in events:
            return -9
        raise subprocess.TimeoutExpired(cmd="worker", timeout=timeout or 0)

    worker.proc = SimpleNamespace(
        stdin=io.StringIO(),
        stdout=io.StringIO(),
        poll=lambda: None,
        terminate=lambda: events.append("terminate"),
        kill=lambda: events.append("kill"),
        wait=_wait,
    )
    worker.stderr_handle = io.StringIO()

    worker.close()

    assert "terminate" in events
    assert "kill" in events


def test_worker_main_installs_a_sigterm_handler_that_exits_normally(
    monkeypatch, tmp_path: Path
) -> None:
    """SIGTERM must unwind through SystemExit so `atexit` cleanup still runs.

    Without it, `close()`'s fallback signal leaves the accessibility bus daemon
    running with no parent.
    """
    installed: dict[int, object] = {}
    monkeypatch.setattr(
        signal, "signal", lambda sig, handler: installed.setdefault(sig, handler)
    )
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())
    monkeypatch.setattr("sys.stdin", io.StringIO("{\"cmd\":\"shutdown\"}\n"))

    run_scene_worker_main(
        worker_id="w00",
        config=SimpleNamespace(session=SimpleNamespace(display=SimpleNamespace(display_number=1))),
        output_dir=tmp_path,
        include_desktop_chrome=True,
        preloaded_manifests={},
        protocol_out=io.StringIO(),
    )

    handler = installed.get(signal.SIGTERM)
    assert handler is not None
    with pytest.raises(SystemExit):
        handler(signal.SIGTERM, None)
