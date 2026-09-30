"""Helpers for persistent scene-worker processes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import signal
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, TextIO, Tuple

from deskshot.generation.scene_composer import SceneApp, SceneConfig, WindowRect


def deserialize_scene_config(data: Dict[str, Any]) -> SceneConfig:
    return SceneConfig(
        scene_id=str(data["scene_id"]),
        seed=int(data["seed"]),
        theme_preset=str(data["theme_preset"]),
        display_preset=str(data["display_preset"]),
        panel_variant=str(data["panel_variant"]),
        desktop_profile=str(data["desktop_profile"]),
        desktop_layout_template=str(data["desktop_layout_template"]),
        desktop_content_pack=str(data["desktop_content_pack"]),
        wallpaper_seed=int(data["wallpaper_seed"]),
        desktop_seed=int(data["desktop_seed"]),
        layout=str(data["layout"]),
        apps=[
            SceneApp(
                app_name=str(app["app_name"]),
                state_ref=str(app["state_ref"]),
                rect=WindowRect(
                    x=int(app["rect"]["x"]),
                    y=int(app["rect"]["y"]),
                    width=int(app["rect"]["width"]),
                    height=int(app["rect"]["height"]),
                ),
            )
            for app in data["apps"]
        ],
    )


def stripe_scene_jobs(
    scenes: Sequence[SceneConfig],
    worker_count: int,
) -> List[List[Tuple[int, SceneConfig]]]:
    worker_count = max(1, int(worker_count))
    lanes: List[List[Tuple[int, SceneConfig]]] = [[] for _ in range(worker_count)]
    for index, scene in enumerate(scenes):
        lanes[index % worker_count].append((index, scene))
    return lanes


@dataclass
class WorkerSceneResponse:
    scene: SceneConfig
    row: Optional[Dict[str, Any]]
    ok: bool
    elapsed_s: float
    worker_id: str
    display_number: int
    error: str = ""
    traceback: str = ""


class PersistentSceneWorker:
    """Long-lived subprocess worker pinned to one X display."""

    def __init__(
        self,
        *,
        worker_id: str,
        display_number: int,
        output_dir: Path,
        project_root: Path,
        env: Dict[str, str],
        tools_dir: Optional[str],
        include_desktop_chrome: bool,
    ) -> None:
        self.worker_id = worker_id
        self.display_number = int(display_number)
        self.output_dir = output_dir
        self.project_root = project_root
        self.env = dict(env)
        self.tools_dir = tools_dir
        self.include_desktop_chrome = include_desktop_chrome
        self.proc: Optional[subprocess.Popen[str]] = None
        self.stderr_handle = None
        self.stderr_path = output_dir / f"scene_worker_{worker_id}.log"
        self._lock = threading.Lock()

    def start(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.stderr_handle = self.stderr_path.open("w", encoding="utf-8")
        cmd = [
            sys.executable,
            "-u",
            "-m",
            "deskshot.cli",
            "scene-worker",
            "--output",
            str(self.output_dir),
            "--display-number",
            str(self.display_number),
            "--worker-id",
            self.worker_id,
        ]
        if self.tools_dir:
            cmd.extend(["--tools-dir", self.tools_dir])
        cmd.append("--include-chrome" if self.include_desktop_chrome else "--no-include-chrome")

        self.proc = subprocess.Popen(
            cmd,
            cwd=str(self.project_root),
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr_handle,
            text=True,
            bufsize=1,
        )
        ready = self._read_response()
        if ready.get("cmd") != "ready":
            raise RuntimeError(f"Worker {self.worker_id} failed to initialize: {ready}")

    def close(self) -> None:
        """Ask the worker to exit, and only signal it if that fails.

        The worker owns this process's accessibility bus daemon, which it stops
        from an `atexit` handler. SIGTERM skips `atexit`, so terminating the
        worker straight away orphaned one dbus-daemon per worker per batch wave.
        """
        try:
            if self.proc and self.proc.poll() is None and self.proc.stdin:
                try:
                    self._send({"cmd": "shutdown"})
                    self.proc.wait(timeout=10)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        finally:
            if self.proc:
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=3)
                if self.proc.stdin:
                    self.proc.stdin.close()
                if self.proc.stdout:
                    self.proc.stdout.close()
            if self.stderr_handle:
                self.stderr_handle.close()
                self.stderr_handle = None

    def run_scene(self, scene: SceneConfig, steps: int = 0) -> WorkerSceneResponse:
        if self.proc is None or self.proc.stdin is None or self.proc.stdout is None:
            raise RuntimeError("Worker not started")
        with self._lock:
            self._send({"cmd": "run_scene", "scene": asdict(scene), "steps": int(steps)})
            payload = self._read_response()
        if payload.get("cmd") != "scene_result":
            raise RuntimeError(f"Unexpected worker response: {payload}")
        return WorkerSceneResponse(
            scene=scene,
            row=payload.get("row"),
            ok=bool(payload.get("ok")),
            elapsed_s=float(payload.get("elapsed_s", 0.0) or 0.0),
            worker_id=str(payload.get("worker_id") or self.worker_id),
            display_number=int(payload.get("display_number", self.display_number)),
            error=str(payload.get("error") or ""),
            traceback=str(payload.get("traceback") or ""),
        )

    def _send(self, payload: Dict[str, Any]) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def _read_response(self) -> Dict[str, Any]:
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                retcode = self.proc.poll()
                raise RuntimeError(f"Worker {self.worker_id} exited unexpectedly with code {retcode}")
            raw = line.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                if self.stderr_handle:
                    self.stderr_handle.write(f"[stdout-noise] {line}")
                    self.stderr_handle.flush()
                continue
            if isinstance(payload, dict):
                return payload
            if self.stderr_handle:
                self.stderr_handle.write(f"[stdout-noise] {line}")
                self.stderr_handle.flush()


def run_scene_worker_main(
    *,
    worker_id: str,
    config: Any,
    output_dir: Path,
    include_desktop_chrome: bool,
    preloaded_manifests: Dict[str, Any],
    protocol_out: TextIO | None = None,
) -> int:
    """Main loop for a persistent scene worker subprocess."""
    from time import perf_counter

    from deskshot.generation.scene_composer import (
        run_scene_episode,
        run_scene_extraction,
    )

    protocol_stream = protocol_out or sys.stdout
    sys.stdout = sys.stderr

    # A worker owns the process-wide accessibility bus daemon and stops it from
    # an atexit handler, which python skips under the default SIGTERM handling.
    # Turning the signal into a normal exit is what keeps a killed worker from
    # orphaning its dbus-daemon.
    def _exit_on_sigterm(_signum: int, _frame: Any) -> None:
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, _exit_on_sigterm)
    except ValueError:  # not on the main thread; atexit still covers clean exits
        pass

    def _emit(payload: Dict[str, Any]) -> None:
        protocol_stream.write(json.dumps(payload) + "\n")
        protocol_stream.flush()

    _emit(
        {
            "cmd": "ready",
            "worker_id": worker_id,
            "display_number": int(config.session.display.display_number),
        }
    )

    for line in sys.stdin:
        raw = line.strip()
        if not raw:
            continue
        payload = json.loads(raw)
        cmd = str(payload.get("cmd") or "")
        if cmd == "shutdown":
            return 0
        if cmd != "run_scene":
            _emit(
                {
                    "cmd": "scene_result",
                    "ok": False,
                    "worker_id": worker_id,
                    "display_number": int(config.session.display.display_number),
                    "error": f"unknown_command:{cmd}",
                }
            )
            continue

        scene = deserialize_scene_config(payload["scene"])
        steps = int(payload.get("steps") or 0)
        started = perf_counter()
        try:
            # Episodes run here too, not only in the one-scene-per-subprocess
            # path: the parallel workers are what a real batch uses, and the
            # plan is what decides which scenes are stepped.
            if steps > 0:
                row = run_scene_episode(
                    scene,
                    config,
                    steps=steps,
                    output_dir=output_dir,
                    include_desktop_chrome=include_desktop_chrome,
                    preloaded_manifests=preloaded_manifests,
                )
            else:
                row = run_scene_extraction(
                    scene,
                    config,
                    output_dir=output_dir,
                    include_desktop_chrome=include_desktop_chrome,
                    preloaded_manifests=preloaded_manifests,
                )
            response = {
                "cmd": "scene_result",
                "ok": row is not None,
                "row": row,
                "elapsed_s": round(perf_counter() - started, 3),
                "worker_id": worker_id,
                "display_number": int(config.session.display.display_number),
            }
        except Exception as exc:  # pragma: no cover - exercised by live worker mode
            response = {
                "cmd": "scene_result",
                "ok": False,
                "row": None,
                "elapsed_s": round(perf_counter() - started, 3),
                "worker_id": worker_id,
                "display_number": int(config.session.display.display_number),
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        _emit(response)
    return 0
