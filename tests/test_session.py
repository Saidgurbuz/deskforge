"""Integration tests for desktop session management.

Tests Xvfb + D-Bus + AT-SPI session startup/shutdown.
"""

import os
import tempfile
from pathlib import Path


class TestDesktopSession:
    """Tests for DesktopSession context manager."""

    def test_session_starts_and_stops(self):
        """Session should start, set env vars, and clean up on exit."""
        from deskshot.environment.session import DesktopSession
        from deskshot.config import SessionConfig, DisplayConfig

        # Use a high display number to avoid conflicts
        display_config = DisplayConfig(display_number=98, width=800, height=600)
        config = SessionConfig(display=display_config)

        original_display = os.environ.get("DISPLAY")

        with DesktopSession(config) as session:
            # Check env vars are set
            assert os.environ.get("DISPLAY") == ":98"
            assert os.environ.get("DBUS_SESSION_BUS_ADDRESS") is not None
            assert os.environ.get("GTK_MODULES") == "gail:atk-bridge"

        # Check env vars are restored
        if original_display:
            assert os.environ.get("DISPLAY") == original_display
        else:
            assert "DISPLAY" not in os.environ

    def test_health_checks_pass_in_session(self):
        """All health checks should pass within a session."""
        from deskshot.environment.session import DesktopSession
        from deskshot.environment.health import run_all_checks
        from deskshot.config import SessionConfig, DisplayConfig

        display_config = DisplayConfig(display_number=97)
        config = SessionConfig(display=display_config)

        with DesktopSession(config):
            checks = run_all_checks()
            for name, ok, msg in checks:
                if name == "AT-SPI":
                    # AT-SPI might not have registryd in all environments
                    continue
                assert ok, f"{name} failed: {msg}"

    def test_setup_xdg_isolation_passes_display_and_panel_variant(self, monkeypatch):
        """Desktop fixture should receive actual session geometry and panel placement."""
        from deskshot.environment.session import DesktopSession
        from deskshot.config import SessionConfig, DisplayConfig, ThemeConfig

        captured = {}

        def _fake_setup_xdg_isolation(root):
            return {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_RUNTIME_DIR": str(root / "runtime"),
            }

        def _fake_materialize_desktop_fixture(
            *,
            root,
            xdg_config_home,
            fixture,
            display_width,
            display_height,
            panel_variant,
            home_root=None,
            desktop_style="linux",
        ):
            captured["root"] = root
            captured["home_root"] = home_root
            captured["desktop_style"] = desktop_style
            captured["xdg_config_home"] = xdg_config_home
            captured["display_width"] = display_width
            captured["display_height"] = display_height
            captured["panel_variant"] = panel_variant
            home_dir = root / "home"
            desktop_dir = home_dir / "Desktop"
            home_dir.mkdir(parents=True, exist_ok=True)
            desktop_dir.mkdir(parents=True, exist_ok=True)
            return {
                "home_dir": home_dir,
                "desktop_dir": desktop_dir,
                "downloads_dir": home_dir / "Downloads",
                "documents_dir": home_dir / "Documents",
                "user_dirs": xdg_config_home / "user-dirs.dirs",
                "layout": "test",
            }

        config = SessionConfig(
            display=DisplayConfig(display_number=96, width=1600, height=900),
            desktop_env="xfce",
            theme=ThemeConfig(desktop_style="windows", panel_variant=""),
        )
        session = DesktopSession(config)
        session._tmpdir = tempfile.TemporaryDirectory(prefix="deskshot_test_")

        monkeypatch.setattr(
            "deskshot.environment.themes.setup_xdg_isolation",
            _fake_setup_xdg_isolation,
        )
        monkeypatch.setattr(
            "deskshot.environment.desktop_fixture.materialize_desktop_fixture",
            _fake_materialize_desktop_fixture,
        )
        monkeypatch.setattr(
            session,
            "_start_xfce_component",
            lambda *args, **kwargs: None,
        )

        try:
            session._setup_xdg_isolation()
        finally:
            if session._tmpdir is not None:
                session._tmpdir.cleanup()

        assert captured["display_width"] == 1600
        assert captured["display_height"] == 900
        assert captured["panel_variant"] == "bottom_tall"

    def test_session_owned_pids_matches_markers(self, monkeypatch):
        from deskshot.environment.session import DesktopSession
        from deskshot.config import SessionConfig

        session = DesktopSession(SessionConfig())
        session._session_markers = {
            "DISPLAY": ":321",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/dbus-abc",
        }

        current_pid = os.getpid()
        monkeypatch.setattr(
            os,
            "listdir",
            lambda path: [str(current_pid), "10", "11", "12", "not-a-pid"],
        )
        env_map = {
            10: "DISPLAY=:321\0",
            11: "DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/dbus-abc\0",
            12: "DISPLAY=:99\0",
        }
        monkeypatch.setattr(
            session,
            "_read_proc_environ",
            lambda pid: env_map[pid],
        )

        assert session._session_owned_pids() == {10, 11}

    def test_start_dbus_tracks_private_bus(self, monkeypatch):
        from deskshot.environment.session import DesktopSession
        from deskshot.config import SessionConfig

        class _FakeStdout:
            def __init__(self, lines):
                self._lines = list(lines)

            def readline(self):
                return self._lines.pop(0) if self._lines else ""

        class _FakeProc:
            def __init__(self):
                self.pid = 12345
                self.stdout = _FakeStdout(["unix:path=/tmp/dbus-test\n"])

            def poll(self):
                return None

        session = DesktopSession(SessionConfig())
        session._tmpdir = tempfile.TemporaryDirectory(prefix="deskshot_test_")
        monkeypatch.setattr(session, "_find_binary", lambda name: f"/usr/bin/{name}")

        fake_proc = _FakeProc()

        def _fake_popen(*args, **kwargs):
            assert kwargs.get("start_new_session") is True
            return fake_proc

        monkeypatch.setattr("deskshot.environment.session.subprocess.Popen", _fake_popen)

        try:
            session._start_dbus()
        finally:
            if session._tmpdir is not None:
                session._tmpdir.cleanup()

        assert session._procs[-1] is fake_proc
        assert os.environ["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/tmp/dbus-test"
        assert session._session_markers["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/tmp/dbus-test"

    def test_setup_env_prepends_external_apps_bin(self, tmp_path: Path, monkeypatch) -> None:
        from deskshot.environment.session import DesktopSession
        from deskshot.config import SessionConfig

        tools = tmp_path / "tools"
        extracted = tools / "extracted"
        external_bin = tools / "external_apps" / "bin"
        (extracted / "usr" / "bin").mkdir(parents=True, exist_ok=True)
        (extracted / "usr" / "lib64").mkdir(parents=True, exist_ok=True)
        external_bin.mkdir(parents=True, exist_ok=True)

        session = DesktopSession(SessionConfig(tools_dir=tools))
        monkeypatch.setenv("PATH", "/usr/bin")
        session._setup_env()

        assert os.environ["PATH"].split(":")[0] == str(external_bin)

    def test_start_xfce_session_uses_optional_picom_and_plank(self, monkeypatch, tmp_path: Path) -> None:
        from deskshot.environment.session import DesktopSession
        from deskshot.config import DisplayConfig, SessionConfig, ThemeConfig

        calls: list[tuple[str, tuple[str, ...]]] = []
        session = DesktopSession(
            SessionConfig(
                display=DisplayConfig(display_number=96),
                desktop_env="xfce",
                theme=ThemeConfig(
                    gtk_theme="MacTahoe-Light-solid",
                    icon_theme="WhiteSur-light",
                    wm_theme="MacTahoe-Light-solid",
                    cursor_theme="WhiteSur-cursors",
                    desktop_style="macos",
                    panel_variant="top_slim_dock",
                ),
            )
        )
        session._tmpdir = tempfile.TemporaryDirectory(prefix="deskshot_test_")

        monkeypatch.setattr(
            session,
            "_find_binary",
            lambda name: f"/usr/bin/{name}",
        )
        monkeypatch.setattr(
            session,
            "_start_xfce_component",
            lambda name, args=None, **kwargs: calls.append((name, tuple(args or ()))) or object(),
        )
        monkeypatch.setattr(
            "deskshot.environment.themes.resolve_theme_config",
            lambda theme: theme,
        )
        monkeypatch.setattr(
            "deskshot.environment.themes.resolve_firefox_theme_hint",
            lambda theme: "mactahoe-light",
        )
        monkeypatch.setattr(
            "deskshot.environment.themes.write_gtk_settings_ini",
            lambda *args, **kwargs: tmp_path / "settings.ini",
        )
        monkeypatch.setattr(
            "deskshot.environment.backgrounds.apply_desktop_background",
            lambda *args, **kwargs: None,
        )
        xfce_args = {}
        monkeypatch.setattr(
            "deskshot.environment.xfce_config.write_all_xfce_configs",
            lambda theme, config_dir, external_compositor=False: xfce_args.setdefault("external_compositor", external_compositor) or [],
        )
        mate_args = {}
        monkeypatch.setattr(
            "deskshot.environment.mate_config.write_mate_dconf_config",
            lambda theme, root, use_plank=False: mate_args.setdefault("write_use_plank", use_plank) or (tmp_path / "layout"),
        )
        monkeypatch.setattr(
            "deskshot.environment.mate_config.get_mate_env",
            lambda root: {},
        )
        monkeypatch.setattr(
            "deskshot.environment.mate_config.apply_mate_panel_settings",
            lambda theme, env=None, use_plank=False: mate_args.setdefault("apply_use_plank", use_plank),
        )
        monkeypatch.setattr(
            "deskshot.environment.picom_config.write_picom_config",
            lambda theme, config_root: tmp_path / "picom.conf",
        )
        monkeypatch.setattr(
            "deskshot.environment.plank_config.write_plank_launchers",
            lambda config_root: [tmp_path / "dock1"],
        )
        monkeypatch.setattr(
            "deskshot.environment.plank_config.apply_plank_settings",
            lambda theme, env=None: None,
        )
        monkeypatch.setattr(
            session,
            "_start_dock_backdrop",
            lambda theme, **kwargs: calls.append(("dock-backdrop", ())) or object(),
        )

        class _Result:
            returncode = 0
            # The session now reads settings back to check they held, so the
            # double has to look like a completed process, not just a status.
            stdout = "false\n"
            stderr = ""

        run_calls = []

        import shutil

        monkeypatch.setattr(
            shutil,
            "which",
            lambda name: f"/usr/bin/{name}" if name == "dconf" else None,
        )
        monkeypatch.setattr(
            "deskshot.environment.session.subprocess.run",
            lambda *args, **kwargs: run_calls.append(tuple(args[0])) or _Result(),
        )
        try:
            session._start_xfce_session()
        finally:
            if session._tmpdir is not None:
                session._tmpdir.cleanup()

        started = [name for name, _ in calls]
        assert xfce_args["external_compositor"] is True
        assert mate_args["write_use_plank"] is True
        assert mate_args["apply_use_plank"] is True
        assert (
            "gsettings",
            "set",
            "org.mate.panel",
            "default-layout",
            "deskshot-macos-top_slim_dock-plank",
        ) in run_calls
        assert os.environ["DESKSHOT_FIREFOX_THEME"] == "mactahoe-light"
        assert "picom" in started
        assert "dock-backdrop" in started
        assert "bamfdaemon" in started
        assert "plank" in started
        assert started.index("picom") > started.index("xfwm4")
        assert started.index("dock-backdrop") > started.index("mate-panel")
        assert started.index("bamfdaemon") > started.index("mate-panel")
        assert started.index("plank") > started.index("bamfdaemon")
        assert started.index("dock-backdrop") > started.index("plank")


class TestSharedA11yBus:
    """The accessibility bus must outlive the session that started it.

    Root cause of "App 'X' did not appear in AT-SPI tree within Ns" for every
    scene after the first one in a persistent scene worker: libatspi caches its
    connection to the a11y bus in a process-global on first use and never
    re-reads AT_SPI_BUS_ADDRESS. Measured on at-spi2-core 2.40.3, after the
    first session's bus died, `Atspi.get_desktop(0)` raised
    GLib.Error('The application no longer exists') for the rest of the process
    - through `Atspi.exit()` + `Atspi.init()`, and also when `Atspi.exit()` was
    called while the old bus was still alive. Every app launched into the next
    session then timed out even though it had registered fine (mapping worker to
    scene order gave 1st=OK, 2nd=FAIL, 3rd=FAIL, seven for seven), while
    --parallel-workers 1, which forks a fresh process per scene, passed 3/3.
    """

    @staticmethod
    def _fake_bus_proc(pid=4242, alive=True):
        class _Proc:
            def __init__(self):
                self.pid = pid
                self.terminated = False

            def poll(self):
                return None if alive else 0

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                return 0

        return _Proc()

    def test_second_session_in_one_process_reuses_the_first_a11y_bus(self, monkeypatch):
        """Two sequential sessions must share one a11y bus daemon.

        Starting a second daemon is what strands libatspi on the dead first one,
        so the fix is only real if the second session gets the *same* address
        and spawns no second accessibility dbus-daemon.
        """
        from deskshot.config import DisplayConfig, SessionConfig
        from deskshot.environment import session as session_mod

        session_mod.set_shared_a11y_bus(None)

        spawned: list[list[str]] = []

        class _FakeProc:
            pid = 5555

            def __init__(self, cmd):
                self.cmd = cmd
                self.stdout = self

            def readline(self):
                return b"unix:path=/tmp/dbus-shared-a11y\n"

            def poll(self):
                return None

        def _fake_popen(cmd, *args, **kwargs):
            spawned.append(list(cmd))
            return _FakeProc(cmd)

        monkeypatch.setattr(session_mod.subprocess, "Popen", _fake_popen)
        monkeypatch.setattr(session_mod.time, "sleep", lambda _s: None)
        monkeypatch.setattr(
            session_mod.DesktopSession, "_find_binary", lambda self, name: f"/usr/bin/{name}"
        )

        addresses = []
        try:
            for display_number in (95, 94):
                session = session_mod.DesktopSession(
                    SessionConfig(display=DisplayConfig(display_number=display_number))
                )
                session._start_atspi()
                addresses.append(os.environ.get("AT_SPI_BUS_ADDRESS"))
                for key, old in session._env_backup.items():
                    if old is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = old
        finally:
            session_mod.set_shared_a11y_bus(None)

        a11y_daemons = [
            cmd for cmd in spawned
            if any("accessibility.conf" in part for part in cmd)
        ]
        assert len(a11y_daemons) == 1, spawned
        assert addresses == [
            "unix:path=/tmp/dbus-shared-a11y",
            "unix:path=/tmp/dbus-shared-a11y",
        ]

    def test_a11y_bus_is_not_tracked_as_a_session_process(self, monkeypatch):
        """The bus daemon must not sit in `_procs`, which __exit__ kills.

        It used to, which is exactly how the process lost the only bus libatspi
        would ever talk to.
        """
        from deskshot.config import DisplayConfig, SessionConfig
        from deskshot.environment import session as session_mod

        session_mod.set_shared_a11y_bus(None)

        class _FakeProc:
            pid = 5556

            def __init__(self, cmd):
                self.cmd = cmd
                self.stdout = self

            def readline(self):
                return b"unix:path=/tmp/dbus-shared-a11y\n"

            def poll(self):
                return None

        monkeypatch.setattr(session_mod.subprocess, "Popen", lambda cmd, *a, **k: _FakeProc(cmd))
        monkeypatch.setattr(session_mod.time, "sleep", lambda _s: None)
        monkeypatch.setattr(
            session_mod.DesktopSession, "_find_binary", lambda self, name: f"/usr/bin/{name}"
        )

        session = session_mod.DesktopSession(
            SessionConfig(display=DisplayConfig(display_number=93))
        )
        try:
            session._start_atspi()
            tracked = [proc.cmd for proc in session._procs]
            assert not any(
                any("accessibility.conf" in part for part in cmd) for cmd in tracked
            ), tracked
            assert session_mod.get_shared_a11y_bus() is not None
        finally:
            session_mod.set_shared_a11y_bus(None)
            for key, old in session._env_backup.items():
                if old is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old

    def test_marker_sweep_never_signals_the_shared_a11y_bus(self, monkeypatch):
        """The bus inherits the first session's markers and must survive anyway.

        `__exit__` reaps every process whose environ carries a session marker.
        The bus daemon is started from inside the first session, so it carries
        DESKSHOT_SESSION_TOKEN and DISPLAY too - and reaping it takes down every
        later session in the process.
        """
        import signal as signal_mod

        from deskshot.config import DisplayConfig, SessionConfig
        from deskshot.environment import session as session_mod

        bus_pid = 4242
        session_mod.set_shared_a11y_bus(
            session_mod.SharedA11yBus(self._fake_bus_proc(pid=bus_pid), "unix:path=/tmp/x")
        )
        session = session_mod.DesktopSession(
            SessionConfig(display=DisplayConfig(display_number=92))
        )
        session._session_markers = {"DESKSHOT_SESSION_TOKEN": "tok-1"}

        monkeypatch.setattr(os, "listdir", lambda path: [str(bus_pid), "7777", "notapid"])
        monkeypatch.setattr(
            session_mod.DesktopSession,
            "_read_proc_environ",
            lambda self, pid: "DESKSHOT_SESSION_TOKEN=tok-1\x00",
        )
        signalled: list[int] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: signalled.append(pid))

        try:
            owned = session._session_owned_pids()
            assert owned == {7777}
            session._signal_session_pids({bus_pid, 7777}, signal_mod.SIGTERM)
            assert signalled == [7777]
        finally:
            session_mod.set_shared_a11y_bus(None)

    def test_dead_a11y_bus_is_replaced_rather_than_reused(self, monkeypatch):
        """A bus whose daemon exited must not be handed to the next session.

        Reusing a dead address would make every later session fail silently with
        an empty tree; starting a replacement at least lets a fresh process work.
        """
        from deskshot.config import DisplayConfig, SessionConfig
        from deskshot.environment import session as session_mod

        session_mod.set_shared_a11y_bus(
            session_mod.SharedA11yBus(
                self._fake_bus_proc(pid=4243, alive=False), "unix:path=/tmp/dead"
            )
        )

        class _FakeProc:
            pid = 5557

            def __init__(self, cmd):
                self.cmd = cmd
                self.stdout = self

            def readline(self):
                return b"unix:path=/tmp/dbus-fresh\n"

            def poll(self):
                return None

        monkeypatch.setattr(session_mod.subprocess, "Popen", lambda cmd, *a, **k: _FakeProc(cmd))
        monkeypatch.setattr(
            session_mod.DesktopSession, "_find_binary", lambda self, name: f"/usr/bin/{name}"
        )

        session = session_mod.DesktopSession(
            SessionConfig(display=DisplayConfig(display_number=91))
        )
        try:
            address = session._start_a11y_bus()
            assert address == "unix:path=/tmp/dbus-fresh"
            assert session_mod.get_shared_a11y_bus().address == "unix:path=/tmp/dbus-fresh"
        finally:
            session_mod.set_shared_a11y_bus(None)
