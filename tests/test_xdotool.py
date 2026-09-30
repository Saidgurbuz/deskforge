import subprocess

from deskshot.automation import xdotool


def test_run_wraps_timeout_as_runtime_error(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["xdotool", "getactivewindow"], timeout=10)

    monkeypatch.setattr(xdotool.subprocess, "run", fake_run)

    try:
        xdotool._run(["getactivewindow"])
    except RuntimeError as exc:
        assert "timed out" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected RuntimeError")


def test_window_size_falls_back_to_async(monkeypatch):
    calls = []

    def fake_run(args, display=None, *, timeout=10.0):
        calls.append((tuple(args), timeout))
        if "--sync" in args:
            raise RuntimeError("sync timeout")
        return ""

    monkeypatch.setattr(xdotool, "_run", fake_run)
    monkeypatch.setattr(xdotool, "_wait_for_window_geometry", lambda *args, **kwargs: True)

    xdotool.window_size("42", 800, 600)

    assert calls == [
        (("windowsize", "--sync", "42", "800", "600"), 5.0),
        (("windowsize", "42", "800", "600"), 5.0),
    ]


def test_window_move_falls_back_to_async(monkeypatch):
    calls = []

    def fake_run(args, display=None, *, timeout=10.0):
        calls.append((tuple(args), timeout))
        if "--sync" in args:
            raise RuntimeError("sync timeout")
        return ""

    monkeypatch.setattr(xdotool, "_run", fake_run)
    monkeypatch.setattr(xdotool, "_wait_for_window_geometry", lambda *args, **kwargs: True)

    xdotool.window_move("84", 100, 200)

    assert calls == [
        (("windowmove", "--sync", "84", "100", "200"), 5.0),
        (("windowmove", "84", "100", "200"), 5.0),
    ]
