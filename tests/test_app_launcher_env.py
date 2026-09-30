"""Tests for extracted-app runtime env augmentation."""

from pathlib import Path

from deskshot.automation import app_launcher
from deskshot.config import AppManifest


def test_build_app_env_includes_private_libdir_and_typelib(monkeypatch, tmp_path: Path) -> None:
    extracted = tmp_path / "extracted"
    tools = tmp_path / "tools"
    monkeypatch.setattr(app_launcher, "TOOLS_DIR", tools)
    monkeypatch.setattr(app_launcher, "EXTRACTED_DIR", extracted)
    monkeypatch.setattr(app_launcher, "EXTRACTED_BIN", extracted / "usr" / "bin")

    (extracted / "usr" / "lib64" / "girepository-1.0").mkdir(parents=True, exist_ok=True)
    (extracted / "usr" / "lib64" / "gedit" / "girepository-1.0").mkdir(parents=True, exist_ok=True)

    manifest = AppManifest(
        app_name="gnome-text-editor",
        binary="gedit",
        atspi_name="gedit",
    )
    env = app_launcher.build_app_env(str(extracted / "usr" / "bin" / "gedit"), manifest)

    ld_parts = env["LD_LIBRARY_PATH"].split(":")
    gi_parts = env["GI_TYPELIB_PATH"].split(":")
    assert str(extracted / "usr" / "lib64") in ld_parts
    assert str(extracted / "usr" / "lib64" / "gedit") in ld_parts
    assert str(extracted / "usr" / "lib64" / "girepository-1.0") in gi_parts
    assert str(extracted / "usr" / "lib64" / "gedit" / "girepository-1.0") in gi_parts


def test_build_app_env_uses_binary_parent_for_libexec_style_app(monkeypatch, tmp_path: Path) -> None:
    extracted = tmp_path / "extracted"
    tools = tmp_path / "tools"
    monkeypatch.setattr(app_launcher, "TOOLS_DIR", tools)
    monkeypatch.setattr(app_launcher, "EXTRACTED_DIR", extracted)
    monkeypatch.setattr(app_launcher, "EXTRACTED_BIN", extracted / "usr" / "bin")

    (extracted / "usr" / "lib64" / "firefox").mkdir(parents=True, exist_ok=True)
    binary = extracted / "usr" / "lib64" / "firefox" / "firefox-bin"
    binary.write_text("", encoding="utf-8")

    manifest = AppManifest(
        app_name="firefox",
        binary="firefox-bin",
        atspi_name="firefox",
    )
    env = app_launcher.build_app_env(str(binary), manifest)

    assert str(binary.parent) in env["LD_LIBRARY_PATH"].split(":")
    assert env["MOZILLA_FIVE_HOME"] == str(binary.parent)


def test_build_app_env_includes_samba_subdirs(monkeypatch, tmp_path: Path) -> None:
    extracted = tmp_path / "extracted"
    tools = tmp_path / "tools"
    monkeypatch.setattr(app_launcher, "TOOLS_DIR", tools)
    monkeypatch.setattr(app_launcher, "EXTRACTED_DIR", extracted)
    monkeypatch.setattr(app_launcher, "EXTRACTED_BIN", extracted / "usr" / "bin")

    (extracted / "usr" / "lib64" / "samba" / "wbclient").mkdir(parents=True, exist_ok=True)

    manifest = AppManifest(
        app_name="chromium-browser",
        binary="chromium-browser",
        atspi_name="chromium-browser",
    )
    env = app_launcher.build_app_env(str(extracted / "usr" / "bin" / "chromium-browser"), manifest)
    ld_parts = env["LD_LIBRARY_PATH"].split(":")

    assert str(extracted / "usr" / "lib64" / "samba") in ld_parts
    assert str(extracted / "usr" / "lib64" / "samba" / "wbclient") in ld_parts


def test_build_app_env_prefers_runtime_override_dirs(monkeypatch, tmp_path: Path) -> None:
    extracted = tmp_path / "extracted"
    tools = tmp_path / "tools"
    monkeypatch.setattr(app_launcher, "TOOLS_DIR", tools)
    monkeypatch.setattr(app_launcher, "EXTRACTED_DIR", extracted)
    monkeypatch.setattr(app_launcher, "EXTRACTED_BIN", extracted / "usr" / "bin")

    override_lib = tools / "runtime_overrides" / "chromium-browser" / "lib"
    override_lib.mkdir(parents=True, exist_ok=True)
    (extracted / "usr" / "lib64").mkdir(parents=True, exist_ok=True)

    manifest = AppManifest(
        app_name="chromium-browser",
        binary="chromium-browser",
        atspi_name="chromium-browser",
    )
    env = app_launcher.build_app_env(str(extracted / "usr" / "bin" / "chromium-browser"), manifest)
    ld_parts = env["LD_LIBRARY_PATH"].split(":")

    assert str(override_lib) in ld_parts
    assert str(extracted / "usr" / "lib64") in ld_parts
    assert ld_parts.index(str(override_lib)) < ld_parts.index(str(extracted / "usr" / "lib64"))


def test_launch_app_materializes_zim_notebook_dir(monkeypatch, tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "notebook.zim").write_text("[Notebook]\nname=DeskShot Notes\n", encoding="utf-8")
    fresh_dir = tmp_path / "session_zim_notebook_abcd"
    fresh_dir.mkdir(parents=True, exist_ok=True)
    launched = {}

    class _Proc:
        def __init__(self, *args, **kwargs):
            if args[0][0] == "/tools/bin/zim":
                launched["cmd"] = args[0]
            self._deskshot_cleanup_dirs = []
            self.returncode = None

        def poll(self):
            return None

    monkeypatch.setattr(app_launcher, "_ZIM_NOTEBOOK_FIXTURE", fixture)
    monkeypatch.setattr(app_launcher, "find_binary", lambda binary: "/tools/bin/zim")
    monkeypatch.setattr(app_launcher.tempfile, "mkdtemp", lambda prefix, dir: str(fresh_dir))
    monkeypatch.setattr(app_launcher.subprocess, "Popen", _Proc)
    monkeypatch.setattr(app_launcher, "_find_app_in_atspi", lambda atspi_name: True)

    manifest = AppManifest(
        app_name="zim",
        binary="zim",
        atspi_name="zim",
        args=["--standalone", "/tmp/session_zim_notebook"],
    )
    proc = app_launcher.launch_app(manifest)

    assert launched["cmd"] == ["/tools/bin/zim", "--standalone", str(fresh_dir)]
    assert (fresh_dir / "notebook.zim").is_file()
    assert proc._deskshot_cleanup_dirs == [fresh_dir]


def test_materialize_runtime_dir_args_replaces_managed_profile(monkeypatch, tmp_path: Path) -> None:
    fresh_dir = tmp_path / "deskshot_firefox_profile_abcd"
    fresh_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        app_launcher.tempfile,
        "mkdtemp",
        lambda prefix, dir: str(fresh_dir),
    )

    args, cleanup_dirs = app_launcher._materialize_runtime_dir_args(
        ["--profile", "/tmp/deskshot_firefox_profile", "--new-window", "about:blank"]
    )

    assert args == ["--profile", str(fresh_dir), "--new-window", "about:blank"]
    assert cleanup_dirs == [fresh_dir]


def test_materialize_runtime_dir_args_keeps_unmanaged_path(tmp_path: Path) -> None:
    profile = tmp_path / "custom_profile"
    args, cleanup_dirs = app_launcher._materialize_runtime_dir_args(
        ["--profile", str(profile)]
    )

    assert args == ["--profile", str(profile)]
    assert cleanup_dirs == []
    assert profile.is_dir()


def test_seed_firefox_profile_writes_deterministic_user_prefs(tmp_path: Path) -> None:
    profile = tmp_path / "firefox_profile"
    app_launcher._seed_firefox_profile(profile)

    user_js = (profile / "user.js").read_text(encoding="utf-8")
    assert 'browser.aboutwelcome.enabled", false' in user_js
    assert 'browser.tabs.drawInTitlebar", true' in user_js
    assert 'browser.toolbars.bookmarks.visibility", "never"' in user_js
    assert 'toolkit.legacyUserProfileCustomizations.stylesheets", true' in user_js
    assert 'mozilla.widget.use-argb-visuals", true' in user_js
    assert 'trailhead.firstrun.didSeeAboutWelcome", true' in user_js


def test_seed_firefox_profile_copies_mactahoe_theme_assets(monkeypatch, tmp_path: Path) -> None:
    theme_root = tmp_path / "firefox-themes"
    mactahoe = theme_root / "mactahoe"
    (mactahoe / "MacTahoe" / "icons").mkdir(parents=True, exist_ok=True)
    (mactahoe / "userChrome.css").write_text("@import 'MacTahoe/colors/light.css';\n", encoding="utf-8")
    (mactahoe / "userContent.css").write_text("body{}\n", encoding="utf-8")
    (mactahoe / "customChrome.css").write_text("/* custom */\n", encoding="utf-8")
    (mactahoe / "MacTahoe" / "icons" / "icon.svg").write_text("<svg/>", encoding="utf-8")

    monkeypatch.setattr(app_launcher, "_FIREFOX_THEME_ROOT", theme_root)

    profile = tmp_path / "firefox_profile"
    app_launcher._seed_firefox_profile(profile, "mactahoe-light")

    assert (profile / "chrome" / "userChrome.css").is_file()
    assert (profile / "chrome" / "userContent.css").is_file()
    assert (profile / "chrome" / "customChrome.css").is_file()
    assert (profile / "chrome" / "MacTahoe" / "icons" / "icon.svg").is_file()


def test_seed_vscode_user_dir_writes_deterministic_settings(tmp_path: Path) -> None:
    user_dir = tmp_path / "vscode_user"
    app_launcher._seed_vscode_user_dir(user_dir)

    settings = (user_dir / "User" / "settings.json").read_text(encoding="utf-8")
    assert '"git.openRepositoryInParentFolders": "never"' in settings
    assert '"security.workspace.trust.enabled": false' in settings
    assert '"workbench.startupEditor": "none"' in settings


def test_seed_zim_notebook_copies_deterministic_fixture(monkeypatch, tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "notebook.zim").write_text("[Notebook]\nname=DeskShot Notes\n", encoding="utf-8")
    (fixture / "Home.txt").write_text("====== DeskShot Notes ======\n", encoding="utf-8")
    target = tmp_path / "session_zim_notebook"
    target.mkdir()
    (target / "old.txt").write_text("stale\n", encoding="utf-8")

    monkeypatch.setattr(app_launcher, "_ZIM_NOTEBOOK_FIXTURE", fixture)

    app_launcher._seed_zim_notebook(target)

    assert not (target / "old.txt").exists()
    assert "DeskShot Notes" in (target / "notebook.zim").read_text(encoding="utf-8")
    assert "DeskShot Notes" in (target / "Home.txt").read_text(encoding="utf-8")


def test_seed_thunderbird_profile_writes_deterministic_prefs(tmp_path: Path) -> None:
    profile = tmp_path / "thunderbird_profile"
    app_launcher._seed_thunderbird_profile(profile)

    user_js = (profile / "user.js").read_text(encoding="utf-8")
    prefs_js = (profile / "prefs.js").read_text(encoding="utf-8")
    assert 'mail.shell.checkDefaultClient", false' in user_js
    assert 'mail.provider.enabled", false' in user_js
    assert 'datareporting.healthreport.uploadEnabled", false' in user_js
    assert 'alex.rivera@example.test' in user_js
    assert (profile / "Mail" / "Local Folders" / "Inbox").is_file()
    assert "Q2 launch checklist" in (profile / "Mail" / "Local Folders" / "Inbox").read_text(encoding="utf-8")
    assert user_js == prefs_js


def test_materialize_runtime_dir_args_replaces_equals_form_user_data_dir(
    monkeypatch, tmp_path: Path
) -> None:
    fresh_dir = tmp_path / "deskshot_chromium_profile_abcd"
    fresh_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        app_launcher.tempfile,
        "mkdtemp",
        lambda prefix, dir: str(fresh_dir),
    )

    args, cleanup_dirs = app_launcher._materialize_runtime_dir_args(
        ["--user-data-dir=/tmp/deskshot_chromium_profile", "https://www.python.org/"]
    )

    assert args == [f"--user-data-dir={fresh_dir}", "https://www.python.org/"]
    assert cleanup_dirs == [fresh_dir]


def test_extract_runtime_dir_arg_supports_equals_form() -> None:
    path = app_launcher._extract_runtime_dir_arg(
        ["--user-data-dir=/tmp/deskshot_chromium_profile"],
        "--user-data-dir",
    )
    assert path == Path("/tmp/deskshot_chromium_profile")


def test_find_binary_supports_external_apps_bin_symlink(monkeypatch, tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    extracted = tmp_path / "extracted"
    external_bin = tools / "external_apps" / "bin"
    bundle_dir = tools / "external_apps" / "bundles" / "VSCode-linux-x64"
    external_bin.mkdir(parents=True, exist_ok=True)
    bundle_dir.mkdir(parents=True, exist_ok=True)
    target = bundle_dir / "code"
    target.write_text("", encoding="utf-8")
    target.chmod(0o755)
    (external_bin / "code").symlink_to(target)

    monkeypatch.setattr(app_launcher, "TOOLS_DIR", tools)
    monkeypatch.setattr(app_launcher, "EXTRACTED_DIR", extracted)
    monkeypatch.setattr(app_launcher, "EXTRACTED_BIN", extracted / "usr" / "bin")
    monkeypatch.setattr(app_launcher, "_EXTERNAL_BIN", external_bin)
    app_launcher._BINARY_CACHE.clear()

    assert app_launcher.find_binary("code") == str(target.resolve())


def test_resolve_binary_classifies_extracted_binary(monkeypatch, tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    extracted = tmp_path / "extracted"
    monkeypatch.setattr(app_launcher, "TOOLS_DIR", tools)
    monkeypatch.setattr(app_launcher, "EXTRACTED_DIR", extracted)
    monkeypatch.setattr(app_launcher, "EXTRACTED_BIN", extracted / "usr" / "bin")
    monkeypatch.setattr(app_launcher, "_EXTERNAL_BIN", tools / "external_apps" / "bin")
    app_launcher._BINARY_CACHE.clear()

    binary = extracted / "usr" / "bin" / "mousepad"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)

    resolution = app_launcher.resolve_binary("mousepad")
    assert resolution.source == "extracted_tools"
    assert resolution.compute_safe is True
    assert resolution.path == str(binary.resolve())


def test_resolve_binary_classifies_path_fallback_as_not_compute_safe(
    monkeypatch, tmp_path: Path
) -> None:
    tools = tmp_path / "tools"
    extracted = tmp_path / "extracted"
    path_bin = tmp_path / "pathbin"
    path_bin.mkdir()
    binary = path_bin / "login-only-app"
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)

    monkeypatch.setattr(app_launcher, "TOOLS_DIR", tools)
    monkeypatch.setattr(app_launcher, "EXTRACTED_DIR", extracted)
    monkeypatch.setattr(app_launcher, "EXTRACTED_BIN", extracted / "usr" / "bin")
    monkeypatch.setattr(app_launcher, "_EXTERNAL_BIN", tools / "external_apps" / "bin")
    monkeypatch.setenv("PATH", str(path_bin))
    app_launcher._BINARY_CACHE.clear()

    resolution = app_launcher.resolve_binary("login-only-app")
    assert resolution.source == "system_path"
    assert resolution.compute_safe is False


def test_find_binary_supports_nested_libreoffice_and_thunderbird_bins(
    monkeypatch, tmp_path: Path
) -> None:
    tools = tmp_path / "tools"
    extracted = tmp_path / "extracted"
    monkeypatch.setattr(app_launcher, "TOOLS_DIR", tools)
    monkeypatch.setattr(app_launcher, "EXTRACTED_DIR", extracted)
    monkeypatch.setattr(app_launcher, "EXTRACTED_BIN", extracted / "usr" / "bin")
    monkeypatch.setattr(app_launcher, "_EXTERNAL_BIN", tools / "external_apps" / "bin")
    app_launcher._BINARY_CACHE.clear()

    soffice = extracted / "usr" / "lib64" / "libreoffice" / "program" / "soffice"
    thunderbird = extracted / "usr" / "lib64" / "thunderbird" / "thunderbird-bin"
    soffice.parent.mkdir(parents=True, exist_ok=True)
    thunderbird.parent.mkdir(parents=True, exist_ok=True)
    soffice.write_text("", encoding="utf-8")
    thunderbird.write_text("", encoding="utf-8")
    soffice.chmod(0o755)
    thunderbird.chmod(0o755)

    assert app_launcher.find_binary("soffice") == str(soffice.resolve())
    assert app_launcher.find_binary("thunderbird-bin") == str(thunderbird.resolve())


def test_build_app_env_uses_local_bundle_parent_for_tool_managed_binary(monkeypatch, tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    extracted = tmp_path / "extracted"
    monkeypatch.setattr(app_launcher, "TOOLS_DIR", tools)
    monkeypatch.setattr(app_launcher, "EXTRACTED_DIR", extracted)
    monkeypatch.setattr(app_launcher, "EXTRACTED_BIN", extracted / "usr" / "bin")

    bundle_dir = tools / "external_apps" / "bundles" / "VSCode-linux-x64"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    binary = bundle_dir / "code"
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)

    manifest = AppManifest(
        app_name="vscode",
        binary="code",
        atspi_name="Code",
    )
    env = app_launcher.build_app_env(str(binary), manifest)

    assert str(bundle_dir) in env["LD_LIBRARY_PATH"].split(":")


def test_build_app_env_includes_extracted_python_site_packages(monkeypatch, tmp_path: Path) -> None:
    extracted = tmp_path / "extracted"
    tools = tmp_path / "tools"
    monkeypatch.setattr(app_launcher, "TOOLS_DIR", tools)
    monkeypatch.setattr(app_launcher, "EXTRACTED_DIR", extracted)
    monkeypatch.setattr(app_launcher, "EXTRACTED_BIN", extracted / "usr" / "bin")

    site_packages = extracted / "usr" / "lib" / "python3.9" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)

    manifest = AppManifest(
        app_name="meld",
        binary="meld",
        atspi_name="meld",
    )
    env = app_launcher.build_app_env(str(extracted / "usr" / "bin" / "meld"), manifest)

    assert str(site_packages) in env["PYTHONPATH"].split(":")


def test_wait_for_app_absent_from_atspi_polls_until_gone(monkeypatch) -> None:
    states = iter([True, True, False])
    monkeypatch.setattr(app_launcher, "_find_app_in_atspi", lambda name: next(states))
    monkeypatch.setattr(app_launcher.time, "sleep", lambda _: None)

    assert app_launcher.wait_for_app_absent_from_atspi("meld", timeout=1.0, poll_interval=0.0) is True
