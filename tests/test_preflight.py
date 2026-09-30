from __future__ import annotations

from pathlib import Path

from deskshot.automation import app_launcher
from deskshot import preflight


def _write_manifest(config_dir: Path, name: str, binary: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / f"{name}.yaml").write_text(
        "\n".join(
            [
                f"app_name: {name}",
                f"binary: {binary}",
                f"atspi_name: {name}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_preflight_passes_project_staged_manifest(monkeypatch, tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    extracted = tmp_path / "extracted"
    binary = extracted / "usr" / "bin" / "mousepad"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)
    config_dir = tmp_path / "configs" / "apps"
    _write_manifest(config_dir, "mousepad", "mousepad")

    monkeypatch.setattr(app_launcher, "TOOLS_DIR", tools)
    monkeypatch.setattr(app_launcher, "EXTRACTED_DIR", extracted)
    monkeypatch.setattr(app_launcher, "EXTRACTED_BIN", extracted / "usr" / "bin")
    monkeypatch.setattr(app_launcher, "_EXTERNAL_BIN", tools / "external_apps" / "bin")
    monkeypatch.setattr(preflight, "TOOLS_DIR", tools)
    monkeypatch.setattr(preflight, "PROJECT_ROOT", tmp_path)
    app_launcher._BINARY_CACHE.clear()

    results = preflight.run_app_binary_preflight(
        ["mousepad"], config_dir, strict_compute=True, include_session_binaries=False
    )

    assert len(results) == 1
    assert results[0].status == "ok"
    assert results[0].source == "extracted_tools"


def test_preflight_fails_system_path_manifest_in_strict_compute(monkeypatch, tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    extracted = tmp_path / "extracted"
    path_bin = tmp_path / "pathbin"
    path_bin.mkdir()
    binary = path_bin / "login-only-app"
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)
    config_dir = tmp_path / "configs" / "apps"
    _write_manifest(config_dir, "login-only", "login-only-app")

    monkeypatch.setattr(app_launcher, "TOOLS_DIR", tools)
    monkeypatch.setattr(app_launcher, "EXTRACTED_DIR", extracted)
    monkeypatch.setattr(app_launcher, "EXTRACTED_BIN", extracted / "usr" / "bin")
    monkeypatch.setattr(app_launcher, "_EXTERNAL_BIN", tools / "external_apps" / "bin")
    monkeypatch.setenv("PATH", str(path_bin))
    app_launcher._BINARY_CACHE.clear()

    results = preflight.run_app_binary_preflight(
        ["login-only"], config_dir, strict_compute=True, include_session_binaries=False
    )

    assert results[0].status == "fail"
    assert results[0].source == "system_path"
    assert "not compute-portable" in results[0].message


def test_preflight_reports_missing_manifest(tmp_path: Path) -> None:
    results = preflight.run_app_binary_preflight(
        ["missing-app"], tmp_path / "configs" / "apps", strict_compute=True, include_session_binaries=False
    )

    assert results[0].status == "fail"
    assert results[0].message == "app manifest not found"
