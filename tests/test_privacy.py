"""What must not reach a published capture.

These are regression tests for a measured leak: 83 of 95 captures in the
`v235`-`v239` runs carried at least one identifier - the project's absolute
path in editor title bars, the cluster's GPFS device name in file-manager
sidebars, and the real account in Caja's home icon. A screenshot cannot be
redacted afterwards, so the guard has to hold at capture time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deskshot import privacy
from deskshot.config import DesktopFixtureConfig
from deskshot.environment import workspace
from deskshot.environment.desktop_fixture import materialize_desktop_fixture


def test_watchlist_is_built_from_the_live_environment() -> None:
    literals = {entry["literal"] for entry in privacy.describe_watchlist()}
    # The project's own location is what editors draw in their title bars.
    assert str(Path(__file__).resolve().parents[1]) in literals
    labels = {entry["label"] for entry in privacy.describe_watchlist()}
    assert {"account", "hostname", "project_root"} <= labels


def test_the_accounts_real_name_is_watched() -> None:
    """glib's `g_get_real_name()` is the passwd GECOS field, which on this host
    is a work email address and an employee serial number rather than a name.
    MATE's panel draws it in "Log Out <name>...", and no environment override
    reaches it, so the strings themselves have to be known."""
    import pwd, os

    gecos = pwd.getpwuid(os.getuid()).pw_gecos or ""
    fields = [f.strip() for f in gecos.replace(",", ";").split(";") if len(f.strip()) >= 4]
    if not fields:
        pytest.skip("this account has no GECOS fields to leak")
    literals = {entry["literal"] for entry in privacy.describe_watchlist()}
    assert set(fields) <= literals
    assert privacy.scan_text(f"Log Out {fields[-1]}...")


def test_a_project_path_in_a_title_bar_is_a_leak() -> None:
    root = str(Path(__file__).resolve().parents[1])
    leaks = privacy.scan_text(f"meeting_notes.md ({root}/assets/audit) - Pluma")
    assert leaks
    # Attributed to the most specific identifier, not to a bare `/proj`.
    assert leaks[0].literal == root


def test_ordinary_ui_text_is_not_flagged() -> None:
    for text in ("File", "Open Recent", "/var/log/messages", "Documents"):
        assert privacy.is_clean(text), text


def test_personas_are_deterministic_and_are_not_the_real_account() -> None:
    assert privacy.session_persona(4242) == privacy.session_persona(4242)
    names = {privacy.session_persona(seed).username for seed in range(200)}
    assert len(names) > 1
    account = {entry["literal"] for entry in privacy.describe_watchlist()}
    assert not (names & account)


def test_home_path_shape_follows_the_desktop_style() -> None:
    assert privacy.home_container_for("macos", 1) == "Users"
    assert privacy.home_container_for("windows", 1) == "Users"
    assert privacy.home_container_for("ubuntu", 1) == "home"


def test_staged_documents_live_in_the_session_home(tmp_path: Path) -> None:
    home = tmp_path / "home" / "mira"
    home.mkdir(parents=True)
    mapping = workspace.stage_workspace(home)
    assert mapping, "nothing staged - the audit assets should be present"
    for source, target in mapping.items():
        assert Path(target).exists()
        # Staged under the persona's home, never referenced in the checkout.
        assert str(home) in target
        assert privacy.is_clean(Path(target).name, include_brand=True), target


def test_a_manifest_path_is_rewritten_to_the_staged_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home" / "mira"
    home.mkdir(parents=True)
    mapping = workspace.stage_workspace(home)
    monkeypatch.setenv(
        workspace.WORKSPACE_MAP_ENV, workspace.install_workspace_map(mapping)
    )
    source = str(workspace.AUDIT_ASSETS_DIR / "sample_notes.md")
    rewritten = workspace.rewrite_asset_path(source)
    assert rewritten != source
    assert rewritten.endswith("Documents/meeting_notes.md")
    # A path *inside* a staged directory resolves through its longest prefix.
    nested = str(workspace.AUDIT_ASSETS_DIR / "vscode_probe" / "main.py")
    assert workspace.rewrite_asset_path(nested).endswith("analytics_service/main.py")


def test_rewrite_is_a_no_op_without_a_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(workspace.WORKSPACE_MAP_ENV, raising=False)
    source = str(workspace.AUDIT_ASSETS_DIR / "sample_notes.md")
    assert workspace.rewrite_asset_path(source) == source


def test_staged_text_content_drops_the_project_name(tmp_path: Path) -> None:
    home = tmp_path / "home" / "mira"
    home.mkdir(parents=True)
    mapping = workspace.stage_workspace(home)
    notes = mapping.get(str(workspace.AUDIT_ASSETS_DIR / "sample_notes.md"))
    assert notes is not None
    assert "DeskShot" not in Path(notes).read_text(encoding="utf-8")


def test_the_session_home_is_not_named_for_the_running_account(tmp_path: Path) -> None:
    result = materialize_desktop_fixture(
        root=tmp_path / "xdg",
        xdg_config_home=tmp_path / "xdg" / "config",
        fixture=DesktopFixtureConfig(enabled=True, seed=99, profile="sparse"),
        home_root=tmp_path,
        desktop_style="macos",
    )
    home = result["home_dir"]
    assert home.parent.name == "Users"
    assert home.name == result["persona_username"]
    account = {
        entry["literal"]
        for entry in privacy.describe_watchlist()
        if entry["label"] == "account"
    }
    assert home.name not in account


def test_privacy_settings_are_written_not_requested(tmp_path, monkeypatch) -> None:
    """These must not be best-effort.

    They used to go through `gsettings`, a subprocess with a five-second
    timeout whose failure was logged at debug and otherwise ignored. On an idle
    machine that is fine; with eight sessions coming up at once on one node some
    calls do not finish in time, and the desktop then draws the real account
    name. Measured: clean in every three-worker run, then 10 occurrences across
    5 of 71 captures at eight workers.

    Writing the keyfile backend's file directly cannot time out and needs no
    daemon.
    """
    from deskshot.environment.session import DesktopSession

    class _Fake(DesktopSession):
        def __init__(self, root):
            self._root = root

        @property
        def _xdg_root(self):
            return self._root

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    fake = _Fake(tmp_path)
    DesktopSession._write_settings_keyfile(
        fake,
        {"org/mate/caja/desktop": {"home-icon-name": "'Mira\\'s Home'",
                                   "volumes-visible": "false"}},
    )
    written = (tmp_path / "config" / "glib-2.0" / "settings" / "keyfile").read_text()
    assert "[org/mate/caja/desktop]" in written
    assert "volumes-visible=false" in written
    assert "home-icon-name='Mira\\'s Home'" in written

    # Merging, not replacing: a second call must not drop the first group.
    DesktopSession._write_settings_keyfile(
        fake, {"org/gnome/nautilus/window-state": {"start-with-sidebar": "false"}}
    )
    written = (tmp_path / "config" / "glib-2.0" / "settings" / "keyfile").read_text()
    assert "volumes-visible=false" in written
    assert "start-with-sidebar=false" in written


def test_the_app_environment_does_not_carry_the_real_home() -> None:
    """VS Code listed `/u/<account>/android-sdk/...` and `~/.vscode-server/...`
    in its command palette - not because anything pointed it there, but because
    it inherited PATH from the account running the capture and displayed it.

    Every other de-identification measure is undone by that: the session has a
    persona home while the environment still names the real one. Scrubbing the
    environment fixes the class, instead of discovering one app at a time which
    parts of it they choose to show.
    """
    import getpass

    from deskshot.automation.app_launcher import _scrub_identifying_paths

    account = getpass.getuser()
    scrubbed = _scrub_identifying_paths(
        {
            "PATH": ":".join(
                [
                    f"/u/{account}/android-sdk/platform-tools",
                    "/usr/bin",
                    str(Path(__file__).resolve().parents[1] / "tools/extracted/usr/bin"),
                    f"/home/{account}/bin",
                    "/bin",
                ]
            ),
            "PYTHONPATH": f"/u/{account}/lib:/usr/lib/python3",
        }
    )

    assert f"/u/{account}" not in scrubbed["PATH"]
    assert f"/home/{account}" not in scrubbed["PATH"]
    assert f"/u/{account}" not in scrubbed["PYTHONPATH"]
    # The extracted toolchain must survive: removing it stops every app from
    # launching, which trades a string for a corpus that does not exist.
    assert "tools/extracted/usr/bin" in scrubbed["PATH"]
    assert "/usr/bin" in scrubbed["PATH"] and "/bin" in scrubbed["PATH"]


def test_scrubbing_leaves_an_environment_without_paths_alone() -> None:
    from deskshot.automation.app_launcher import _scrub_identifying_paths

    assert _scrub_identifying_paths({"DISPLAY": ":99"}) == {"DISPLAY": ":99"}
