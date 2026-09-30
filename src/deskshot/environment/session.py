"""Desktop session manager: Xvfb + D-Bus + AT-SPI.

Provides a `DesktopSession` context manager that starts up the full
accessibility stack needed for AT-SPI extraction.
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from deskshot.config import PROJECT_ROOT, SessionConfig, bin_fix_dir

logger = logging.getLogger(__name__)

#: Five seconds was the old value and it was too short. With eight desktop
#: sessions coming up at once on one node, a `gsettings` call can genuinely take
#: longer, and every one of these was best-effort: a timeout was logged at debug
#: and the setting silently did not apply.
GSETTINGS_TIMEOUT_SEC = 25

#: Dropped in every session temp directory. The directory name is deliberately
#: neutral - the session HOME sits inside it and its absolute path is drawn in
#: app title bars - so this file, not the name, is what marks it as ours for
#: `scripts/cleanup_stale_sessions.py`.
SESSION_MARKER_FILE = ".deskshot_session"


class SharedA11yBus:
    """The one accessibility bus a python process may ever talk to.

    libatspi caches its D-Bus connection to the accessibility bus in a
    process-global the first time any AT-SPI call is made, and it never
    re-reads `AT_SPI_BUS_ADDRESS` afterwards. Measured on at-spi2-core 2.40.3:
    after the bus that a process first bound to dies, `Atspi.get_desktop(0)`
    raises `GLib.Error('The application no longer exists')` forever - even
    across `Atspi.exit()` + `Atspi.init()`, and even when `Atspi.exit()` is
    called while the old bus is still alive. A second session started in the
    same process therefore sees an empty accessibility tree, and every app it
    launches fails with "did not appear in AT-SPI tree within Ns".

    So the a11y bus daemon has to outlive the session that started it and be
    shared by every later session in the process. Registryd does *not*: it is
    reached through the well-known name `org.a11y.atspi.Registry`, so replacing
    it is transparent to a client whose bus connection stays valid.

    Isolation between concurrently running sessions is unaffected, because
    those live in separate processes and so get separate buses.
    """

    def __init__(self, proc: subprocess.Popen, address: str) -> None:
        self.proc = proc
        self.address = address

    @property
    def pid(self) -> Optional[int]:
        return self.proc.pid

    def is_alive(self) -> bool:
        return self.proc.poll() is None

    def shutdown(self) -> None:
        if not self.is_alive():
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                self.proc.terminate()
            except (OSError, ProcessLookupError):
                return
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass


_shared_a11y_bus: Optional[SharedA11yBus] = None


def get_shared_a11y_bus() -> Optional[SharedA11yBus]:
    """Return the process-wide a11y bus, if one has been started."""
    return _shared_a11y_bus


def set_shared_a11y_bus(bus: Optional[SharedA11yBus]) -> None:
    """Install (or clear) the process-wide a11y bus. For tests and startup."""
    global _shared_a11y_bus
    _shared_a11y_bus = bus


def shutdown_shared_a11y_bus() -> None:
    """Stop the process-wide a11y bus. Registered at exit; safe to call twice."""
    bus = _shared_a11y_bus
    if bus is None:
        return
    set_shared_a11y_bus(None)
    bus.shutdown()


atexit.register(shutdown_shared_a11y_bus)


class DesktopSession:
    """Context manager for a headless desktop session with AT-SPI accessibility.

    Starts:
    1. Xvfb virtual display
    2. D-Bus session bus (via dbus-run-session or standalone dbus-daemon)
    3. AT-SPI2 accessibility bus + registryd

    Sets environment variables: DISPLAY, DBUS_SESSION_BUS_ADDRESS,
    GTK_MODULES, AT_SPI_BUS_ADDRESS, GSETTINGS_SCHEMA_DIR, etc.
    """

    def __init__(self, config: Optional[SessionConfig] = None,
                 log_dir: Optional[Path] = None):
        self.config = config or SessionConfig()
        self.log_dir = log_dir
        self._procs: list[subprocess.Popen] = []
        self._env_backup: dict[str, Optional[str]] = {}
        self._session_markers: dict[str, str] = {}
        self._tmpdir: Optional[tempfile.TemporaryDirectory] = None

    @property
    def _bin(self) -> Path:
        return self.config.tools_dir / "extracted" / "usr" / "bin"

    @property
    def _lib(self) -> Path:
        return self.config.tools_dir / "extracted" / "usr" / "lib64"

    @property
    def _lib_usr(self) -> Path:
        return self.config.tools_dir / "extracted" / "usr" / "lib"

    @property
    def _extracted(self) -> Path:
        return self.config.tools_dir / "extracted"

    @property
    def _xdg_root(self) -> Path:
        if self._tmpdir is not None:
            return Path(self._tmpdir.name) / "xdg"
        return self.config.tools_dir / "xfce_config"

    def _find_binary(self, name: str) -> str:
        """Find a binary in extracted tools, permission-fix dir, or system PATH."""
        def _is_broken_fix_wrapper(path: Path) -> bool:
            try:
                head = path.read_text(encoding="utf-8", errors="ignore")[:256]
            except OSError:
                return False
            return (
                f"exec {bin_fix_dir() / name} " in head
                or f"exec /tmp/deskshot_bin_fix/{name} " in head
            )

        candidates = [
            self.config.tools_dir / "external_apps" / "bin" / name,
            self._bin / name,
            self._extracted / "usr" / "libexec" / name,
            self._extracted / "usr" / "libexec" / "bamf" / name,
            self._extracted / "usr" / "lib64" / "xfce4" / "xfconf" / name,
            self._extracted / "usr" / "lib" / "xfce4" / "xfconf" / name,
            self._extracted / "usr" / "lib64" / name,
            self._extracted / "usr" / "lib" / name,
            # Permissions-fix copies (see setup._ensure_binaries_executable).
            bin_fix_dir() / name,
        ]
        for extracted in candidates:
            if extracted.is_file() and os.access(str(extracted), os.X_OK) and not _is_broken_fix_wrapper(extracted):
                return str(extracted)
        # Bounded fallback: check a few known deep paths, not rglob.
        for sub in ("usr/lib64/at-spi2-core", "usr/lib/at-spi2-core"):
            deep = self._extracted / sub / name
            if deep.is_file() and os.access(str(deep), os.X_OK) and not _is_broken_fix_wrapper(deep):
                return str(deep)
        # Fall back to system PATH
        import shutil
        system = shutil.which(name)
        if system:
            return system
        raise FileNotFoundError(f"Binary not found: {name}")

    def _set_env(self, key: str, value: str) -> None:
        """Set environment variable, saving backup for restore."""
        if key not in self._env_backup:
            self._env_backup[key] = os.environ.get(key)
        os.environ[key] = value

    def _remember_session_marker(self, key: str, value: Optional[str]) -> None:
        if value:
            self._session_markers[key] = value

    def _read_proc_environ(self, pid: int) -> str:
        return (Path("/proc") / str(pid) / "environ").read_bytes().decode(
            "utf-8", errors="ignore"
        )

    def _protected_pids(self) -> set[int]:
        """Pids the session must never signal, however they are matched.

        The shared a11y bus is started by whichever session runs first and
        inherits that session's markers, so a marker sweep would reap it on the
        first teardown - taking every later session in the process down with
        it, since libatspi cannot bind to a replacement bus.
        """
        bus = get_shared_a11y_bus()
        if bus is None or bus.pid is None:
            return set()
        return {bus.pid}

    def _session_owned_pids(self) -> set[int]:
        markers = {
            key: value for key, value in self._session_markers.items() if value
        }
        if not markers:
            return set()

        protected = self._protected_pids()
        owned: set[int] = set()
        current_pid = os.getpid()
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == current_pid or pid in protected:
                continue
            try:
                environ = self._read_proc_environ(pid)
            except OSError:
                continue
            if any(f"{key}={value}" in environ for key, value in markers.items()):
                owned.add(pid)
        return owned

    def _signal_tracked_proc(self, proc: subprocess.Popen, sig: signal.Signals) -> None:
        if proc.pid is None or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, sig)
            return
        except (OSError, ProcessLookupError):
            pass
        try:
            os.kill(proc.pid, sig)
        except (OSError, ProcessLookupError):
            pass

    def _signal_session_pids(self, pids: set[int], sig: signal.Signals) -> None:
        protected = self._protected_pids()
        for pid in sorted(pids):
            if pid == os.getpid() or pid in protected:
                continue
            try:
                os.kill(pid, sig)
            except (OSError, ProcessLookupError):
                continue

    def _start_xvfb(self) -> None:
        """Start Xvfb virtual display."""
        xvfb = self._find_binary("Xvfb")
        display = self.config.display
        cmd = [
            xvfb, display.display_str,
            "-screen", "0", display.screen_str,
            "-ac",  # disable access control
            "-nolisten", "tcp",
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._procs.append(proc)
        self._set_env("DISPLAY", display.display_str)
        self._remember_session_marker("DISPLAY", display.display_str)

        # Wait for display to be ready
        deadline = time.monotonic() + self.config.startup_timeout
        while time.monotonic() < deadline:
            try:
                result = subprocess.run(
                    ["xdpyinfo", "-display", display.display_str],
                    capture_output=True, timeout=2,
                )
                if result.returncode == 0:
                    return
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
            # Fallback: check if Xvfb process is still alive
            if proc.poll() is not None:
                raise RuntimeError(f"Xvfb exited with code {proc.returncode}")
            time.sleep(0.2)

        # If xdpyinfo is not available, just check process is alive
        if proc.poll() is None:
            time.sleep(0.5)  # Extra grace period
            return

        raise RuntimeError("Xvfb failed to start")

    def _start_dbus(self) -> None:
        """Start D-Bus session daemon."""
        dbus_daemon = self._find_binary("dbus-daemon")

        # Start a private session bus that stays tracked by this session.
        proc = subprocess.Popen(
            [dbus_daemon, "--session", "--nofork",
             "--print-address=1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=os.environ,
            text=True,
            start_new_session=True,
        )
        self._procs.append(proc)

        # Read address from stdout
        deadline = time.monotonic() + self.config.startup_timeout
        while time.monotonic() < deadline:
            line = proc.stdout.readline().strip()
            if line:
                self._set_env("DBUS_SESSION_BUS_ADDRESS", line)
                self._remember_session_marker("DBUS_SESSION_BUS_ADDRESS", line)
                return
            if proc.poll() is not None:
                break
            time.sleep(0.1)

        raise RuntimeError("D-Bus daemon failed to start")

    def _start_a11y_bus(self) -> Optional[str]:
        """Return the address of this process's accessibility bus, starting it once.

        The daemon deliberately outlives this session and is not tracked in
        `self._procs`: see `SharedA11yBus` for why a process that binds to a
        second a11y bus can never see an accessibility tree again.
        """
        existing = get_shared_a11y_bus()
        if existing is not None and existing.is_alive():
            return existing.address
        if existing is not None:
            logger.warning(
                "Accessibility bus (pid=%s) died; AT-SPI in this process is "
                "already bound to it and cannot be rebound",
                existing.pid,
            )
            set_shared_a11y_bus(None)

        # Find the AT-SPI bus launcher config
        a11y_conf_paths = [
            "/usr/share/defaults/at-spi2/accessibility.conf",
            "/usr/share/dbus-1/accessibility-services/org.a11y.Bus.service",
        ]
        a11y_conf = None
        for p in a11y_conf_paths:
            if Path(p).exists():
                a11y_conf = p
                break
        if not a11y_conf:
            return None

        dbus_daemon = self._find_binary("dbus-daemon")
        proc = subprocess.Popen(
            [dbus_daemon, f"--config-file={a11y_conf}",
             "--nofork", "--print-address"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=os.environ,
            start_new_session=True,
        )

        deadline = time.monotonic() + self.config.startup_timeout
        while time.monotonic() < deadline:
            line = proc.stdout.readline().decode().strip()
            if line:
                set_shared_a11y_bus(SharedA11yBus(proc, line))
                return line
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        return None

    def _start_atspi(self) -> None:
        """Point the session at the process a11y bus and start its registryd."""
        address = self._start_a11y_bus()
        if address:
            self._set_env("AT_SPI_BUS_ADDRESS", address)

        # Start registryd
        registryd_paths = [
            str(self._extracted / "usr" / "libexec" / "at-spi2-registryd"),
            str(self._extracted / "usr" / "lib64" / "at-spi2-core" / "at-spi2-registryd"),
            str(self._extracted / "usr" / "lib" / "at-spi2-core" / "at-spi2-registryd"),
            "/usr/libexec/at-spi2-registryd",
            "/usr/lib/at-spi2-registryd",
        ]

        for rpath in registryd_paths:
            if Path(rpath).is_file():
                env = dict(os.environ)
                if "AT_SPI_BUS_ADDRESS" in env:
                    env["DBUS_SESSION_BUS_ADDRESS"] = env["AT_SPI_BUS_ADDRESS"]
                proc = subprocess.Popen(
                    [rpath],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env=env,
                    start_new_session=True,
                )
                self._procs.append(proc)
                break

        # Enable GTK accessibility bridge
        self._set_env("GTK_MODULES", "gail:atk-bridge")

        # Small delay for registryd to initialize
        time.sleep(0.3)

    def _setup_env(self) -> None:
        """Set up environment variables for the session."""
        # Library path for extracted libs (plus common plugin subdirs).
        lib_candidates = [
            self._lib,
            self._lib_usr,
            self._lib / "gvfs",
            self._lib_usr / "gvfs",
            self._lib / "samba",
            self._lib / "samba" / "wbclient",
            self._lib / "chromium-browser",
        ]
        current_ld = os.environ.get("LD_LIBRARY_PATH", "")
        ld_parts = [p for p in current_ld.split(":") if p]
        for lib_dir in lib_candidates:
            lib_str = str(lib_dir)
            if lib_dir.is_dir() and lib_str not in ld_parts:
                ld_parts.insert(0, lib_str)
        if ld_parts:
            self._set_env("LD_LIBRARY_PATH", ":".join(ld_parts))

        # GSettings schemas
        schemas_dir = self._extracted / "usr" / "share" / "glib-2.0" / "schemas"
        if schemas_dir.is_dir():
            current = os.environ.get("GSETTINGS_SCHEMA_DIR", "")
            schema_str = str(schemas_dir)
            if schema_str not in current:
                self._set_env("GSETTINGS_SCHEMA_DIR",
                              f"{schema_str}:{current}" if current else schema_str)

        # XDG data dirs for icons, themes, etc.
        xdg_data = str(self._extracted / "usr" / "share")
        current_xdg = os.environ.get("XDG_DATA_DIRS", "/usr/share")
        if xdg_data not in current_xdg:
            self._set_env("XDG_DATA_DIRS", f"{xdg_data}:{current_xdg}")

        # PATH for extracted binaries
        bin_candidates = [
            self.config.tools_dir / "external_apps" / "bin",
            self._bin,
        ]
        current_path = os.environ.get("PATH", "")
        path_parts = [p for p in current_path.split(":") if p]
        for bin_dir in reversed(bin_candidates):
            bin_str = str(bin_dir)
            if bin_dir.is_dir() and bin_str not in path_parts:
                path_parts.insert(0, bin_str)
        if path_parts:
            self._set_env("PATH", ":".join(path_parts))

        # Force the isolated desktop session onto X11. The caller may be
        # inside a Wayland login shell on the host, but the rendered session
        # itself runs on Xvfb and X11-only desktop chrome like Plank depends on
        # that being unambiguous.
        self._set_env("GDK_BACKEND", "x11")
        self._set_env("CLUTTER_BACKEND", "x11")
        self._set_env("QT_QPA_PLATFORM", "xcb")
        self._set_env("XDG_SESSION_TYPE", "x11")
        self._set_env("WAYLAND_DISPLAY", "")

        # ImageMagick runtime config for extracted `display`/`convert`.
        magick_config = self._extracted / "etc" / "ImageMagick-6"
        magick_lib_root = next(iter(sorted(self._lib.glob("ImageMagick-*"))), None)
        if magick_config.is_dir():
            config_parts = [str(magick_config)]
            if magick_lib_root and (magick_lib_root / "config-Q16").is_dir():
                config_parts.append(str(magick_lib_root / "config-Q16"))
            current_magick = os.environ.get("MAGICK_CONFIGURE_PATH", "")
            if current_magick:
                config_parts.append(current_magick)
            self._set_env("MAGICK_CONFIGURE_PATH", ":".join(config_parts))

        if magick_lib_root and (magick_lib_root / "modules-Q16" / "coders").is_dir():
            self._set_env(
                "MAGICK_CODER_MODULE_PATH",
                str(magick_lib_root / "modules-Q16" / "coders"),
            )
        if magick_lib_root and (magick_lib_root / "modules-Q16" / "filters").is_dir():
            self._set_env(
                "MAGICK_CODER_FILTER_PATH",
                str(magick_lib_root / "modules-Q16" / "filters"),
            )

    def _start_window_manager(self) -> None:
        """Start a lightweight window manager (metacity) for input focus."""
        try:
            metacity = self._find_binary("metacity")
        except FileNotFoundError:
            # No WM available — input focus will require manual management
            return

        proc = subprocess.Popen(
            [metacity, "--replace"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=os.environ,
            start_new_session=True,
        )
        self._procs.append(proc)
        time.sleep(0.5)

        if proc.poll() is not None:
            # metacity failed to start — non-fatal
            self._procs.remove(proc)
            return

    def _setup_xdg_isolation(self) -> None:
        """Set up project-local XDG directories to avoid polluting ~/.config."""
        from deskshot.environment.themes import setup_xdg_isolation
        from deskshot.environment.desktop_fixture import materialize_desktop_fixture
        from deskshot.environment.diversity import (
            resolve_desktop_fixture_config,
            resolve_panel_variant,
        )

        xdg_root = self._xdg_root
        xdg_env = setup_xdg_isolation(xdg_root)
        for key, val in xdg_env.items():
            self._set_env(key, val)
        # Use a deterministic local GSettings store instead of relying on
        # a system dconf service that is not present on this server image.
        self._set_env("GSETTINGS_BACKEND", "keyfile")
        self._set_env("GVFS_DISABLE_FUSE", "1")

        # Caja icon-position metadata depends on the GVFS metadata daemon.
        # Start it once the private D-Bus and XDG homes exist so deterministic
        # icon clustering can be applied before Caja reads the desktop.
        self._start_xfce_component("gvfsd-metadata", wait=0.3)

        self.config.desktop_fixture = resolve_desktop_fixture_config(
            self.config.desktop_fixture
        )
        panel_variant = resolve_panel_variant(
            self.config.theme.panel_variant,
            self.config.theme.desktop_style,
        )
        fixture = materialize_desktop_fixture(
            root=xdg_root,
            home_root=xdg_root.parent,
            desktop_style=self.config.theme.desktop_style,
            xdg_config_home=Path(xdg_env["XDG_CONFIG_HOME"]),
            fixture=self.config.desktop_fixture,
            display_width=self.config.display.width,
            display_height=self.config.display.height,
            panel_variant=panel_variant,
        )
        self._set_env("HOME", str(fixture["home_dir"]))
        # Apps are launched with absolute paths into this checkout; the map lets
        # `launch_app` point them at the staged copies instead, so no title bar
        # draws the project's location. See environment/workspace.py.
        from deskshot.environment.workspace import (
            WORKSPACE_MAP_ENV,
            install_workspace_map,
        )

        self._set_env(
            WORKSPACE_MAP_ENV,
            install_workspace_map(fixture.get("workspace_map") or {}),
        )
        self._persona_username = str(fixture.get("persona_username") or "")
        self._persona_full_name = str(fixture.get("persona_full_name") or "")

        # Point XDG_CONFIG_DIRS to include extracted /etc/xdg for default configs
        extracted_xdg = str(self._extracted / "etc" / "xdg")
        current = os.environ.get("XDG_CONFIG_DIRS", "/etc/xdg")
        if extracted_xdg not in current:
            self._set_env("XDG_CONFIG_DIRS", f"{extracted_xdg}:{current}")



    def _write_settings_keyfile(self, entries: dict) -> None:
        """Set GSettings keys by writing the keyfile backend's file directly.

        The privacy-critical settings used to go through `gsettings`, a
        subprocess with a five-second timeout whose failure was logged at debug
        and otherwise ignored. That is fine on an idle machine and wrong at
        scale: with eight sessions coming up at once on one node, some of those
        calls do not finish in five seconds, and a capture then draws
        `<account>'s Home` on the desktop. It showed up as 10 occurrences across 5 of
        71 captures in an eight-worker run, having been clean in every
        three-worker run before it.

        `GSETTINGS_BACKEND=keyfile` means the whole store is one INI file, so
        writing it is deterministic, needs no daemon, and cannot time out.

        `entries` is {"org/mate/caja/desktop": {"key": "value as GVariant"}}.
        """
        config_home = Path(
            os.environ.get("XDG_CONFIG_HOME", str(self._xdg_root / "config"))
        )
        keyfile = config_home / "glib-2.0" / "settings" / "keyfile"
        keyfile.parent.mkdir(parents=True, exist_ok=True)

        groups: dict = {}
        order: list = []
        current = None
        if keyfile.is_file():
            for line in keyfile.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    current = stripped[1:-1]
                    groups.setdefault(current, {})
                    if current not in order:
                        order.append(current)
                elif current and "=" in stripped:
                    key, _, value = stripped.partition("=")
                    groups[current][key.strip()] = value.strip()

        for group, pairs in entries.items():
            groups.setdefault(group, {})
            if group not in order:
                order.append(group)
            groups[group].update(pairs)

        text = ""
        for group in order:
            text += f"[{group}]\n"
            for key, value in sorted(groups[group].items()):
                text += f"{key}={value}\n"
            text += "\n"
        tmp = keyfile.with_name(f".{keyfile.name}.{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(keyfile)

    def _apply_caja_desktop_privacy(self) -> None:
        """Keep the host's identity off the desktop.

        Volume, computer and network icons are turned off outright: they carry
        the machine's mount table, which on a shared cluster also lists other
        people's home directories. Home and trash stay - a desktop without them
        does not look like a desktop - but are relabelled for the session
        persona, so what is drawn matches the home directory path the file
        managers show.
        """
        settings = [
            ("volumes-visible", "false"),
            ("computer-icon-visible", "false"),
            ("network-icon-visible", "false"),
            # Off, and not renamed. Caja labels this icon from
            # `g_get_real_name()` unless told otherwise, and telling it
            # otherwise did not hold: `gsettings get` read back the persona name
            # while the desktop still drew the account's, in 12 of 86 captures
            # at eight workers. Rather than keep guessing at why, the icon goes.
            # The desktop still has Trash and the whole fixture of files and
            # folders, so what is lost is one icon - against publishing a real
            # person's name, which is not a trade worth making.
            ("home-icon-visible", "false"),
            ("trash-icon-visible", "true"),
        ]
        persona_name = getattr(self, "_persona_full_name", "")

        # Written, then verified, then written again if it did not hold.
        #
        # The keyfile backend is one INI file shared by every GSettings client in
        # the session, and `gsettings set` rewrites the whole file from whatever
        # it read a moment earlier. So a running mate-panel or plank writing any
        # setting of its own can silently drop these keys - a read-modify-write
        # race between processes on one file. Measured: the desktop drew the
        # account's real name in 5 of 48 captures that had desktop icons, which
        # is exactly the shape of an occasional race rather than a setting that
        # does not work.
        payload = {
            "org/mate/caja/desktop": {
                key: ("true" if value == "true" else "false" if value == "false"
                      else "'" + value.replace("'", "\\'") + "'")
                for key, value in settings
            },
            "org/gnome/nautilus/window-state": {"start-with-sidebar": "false"},
            "org/mate/caja/window-state": {"start-with-sidebar": "false"},
            "org/gnome/gnome-system-monitor/proctree": {"col-1-visible": "false"},
            "org/mate/panel/menubar": {
                "show-desktop": "false", "show-places": "false",
            },
        }
        for attempt in range(4):
            self._write_settings_keyfile(payload)
            if self._caja_desktop_settings_hold():
                break
            logger.warning(
                "caja desktop privacy settings did not hold (attempt %d); rewriting",
                attempt + 1,
            )
            time.sleep(0.4)
        else:
            logger.error(
                "caja desktop privacy settings would not stick; captures from "
                "this session may draw the account name"
            )
        for key, value in settings:
            try:
                subprocess.run(
                    ["gsettings", "set", "org.mate.caja.desktop", key, value],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=os.environ,
                    timeout=GSETTINGS_TIMEOUT_SEC,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                logger.debug("Could not set org.mate.caja.desktop %s", key)

        # Verify, and fail safe. Writing the keyfile removed the timeout that
        # made this best-effort, but one capture in 32 still drew the account
        # name at eight workers and the cause was not pinned down. Rather than
        # keep guessing: read the value back, and if the rename did not take,
        # hide the home icon altogether. Losing one desktop icon in a rare
        # capture is a smaller loss than publishing somebody's account name.
        self._hide_file_manager_device_panes()


    def _home_icon_renamed(self, persona_name: str) -> bool:
        """Did the home-icon rename actually reach the settings store?"""
        expected = persona_name.split()[0]
        try:
            result = subprocess.run(
                ["gsettings", "get", "org.mate.caja.desktop", "home-icon-name"],
                capture_output=True,
                text=True,
                env=os.environ,
                timeout=GSETTINGS_TIMEOUT_SEC,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and expected in result.stdout


    def _caja_desktop_settings_hold(self) -> bool:
        """Are the keys we just wrote still the ones in effect?"""
        try:
            result = subprocess.run(
                ["gsettings", "get", "org.mate.caja.desktop", "home-icon-visible"],
                capture_output=True,
                text=True,
                env=os.environ,
                timeout=GSETTINGS_TIMEOUT_SEC,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and result.stdout.strip() == "false"

    def _hide_file_manager_device_panes(self) -> None:
        """Start the file managers without their device side pane.

        The pane is populated from GVolumeMonitor, which reports this host's
        own mounts - the GPFS device behind the cluster. GIO offers
        no way to filter that (GIO_USE_VOLUME_MONITOR, LIBMOUNT_MTAB and
        LIBMOUNT_FSTAB were all measured to have no effect), and the name is
        drawn in the sidebar of every Thunar and Nautilus capture.

        Hiding the pane is also a configuration real users choose, so this costs
        no realism. It does not reach GTK's file-chooser places sidebar, which
        has no setting at all - `scripts/audit_privacy.py` is what catches those.
        """
        for schema, key, value in (
            ("org.gnome.nautilus.window-state", "start-with-sidebar", "false"),
            ("org.mate.caja.window-state", "start-with-sidebar", "false"),
            # gnome-system-monitor's process list has a User column, and every
            # row of it is the account running the capture - 247 occurrences in
            # a single capture, the largest single leak measured. COL_USER is
            # index 1 in its column enum.
            ("org.gnome.gnome-system-monitor.proctree", "col-1-visible", "false"),
        ):
            try:
                subprocess.run(
                    ["gsettings", "set", schema, key, value],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=os.environ,
                    timeout=GSETTINGS_TIMEOUT_SEC,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                logger.debug("Could not set %s %s", schema, key)

        # Thunar keeps this in xfconf, which has no daemon here, so the channel
        # file is written directly.
        xfconf_dir = (
            Path(os.environ.get("XDG_CONFIG_HOME", str(self._xdg_root / "config")))
            / "xfce4" / "xfconf" / "xfce-perchannel-xml"
        )
        try:
            xfconf_dir.mkdir(parents=True, exist_ok=True)
            (xfconf_dir / "thunar.xml").write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<channel name="thunar" version="1.0">\n'
                '  <property name="last-side-pane" type="string" value="void"/>\n'
                '</channel>\n',
                encoding="utf-8",
            )
        except OSError:
            logger.debug("Could not write thunar xfconf channel")

    def _start_xfce_component(self, name: str, args: list[str] | None = None,
                               wait: float = 0.5,
                               stdout_file: Path | None = None,
                               stderr_file: Path | None = None) -> subprocess.Popen | None:
        """Start a single XFCE component process.

        Args:
            name: Binary name to find and start.
            args: Additional arguments for the binary.
            wait: Seconds to wait after starting.
            stdout_file: If set, redirect stdout to this file path.
            stderr_file: If set, redirect stderr to this file path.

        Returns:
            Popen process, or None if binary not found.
        """
        try:
            binary = self._find_binary(name)
        except FileNotFoundError:
            import logging
            logging.getLogger(__name__).warning(f"XFCE component not found: {name}")
            return None

        cmd = [binary] + (args or [])

        stdout_sink = subprocess.DEVNULL
        stderr_sink = subprocess.DEVNULL
        stdout_fh = None
        stderr_fh = None

        if stdout_file:
            stdout_file.parent.mkdir(parents=True, exist_ok=True)
            stdout_fh = open(stdout_file, "w")
            stdout_sink = stdout_fh
        if stderr_file:
            stderr_file.parent.mkdir(parents=True, exist_ok=True)
            stderr_fh = open(stderr_file, "w")
            stderr_sink = stderr_fh

        proc = subprocess.Popen(
            cmd,
            stdout=stdout_sink,
            stderr=stderr_sink,
            env=os.environ,
            start_new_session=True,
        )
        # Close parent handles; child keeps inherited FDs.
        if stdout_fh:
            stdout_fh.close()
        if stderr_fh:
            stderr_fh.close()

        self._procs.append(proc)
        time.sleep(wait)

        if proc.poll() is not None:
            import logging
            logging.getLogger(__name__).warning(
                f"XFCE component {name} exited with code {proc.returncode}"
            )
            self._procs.remove(proc)
            return None

        return proc

    def _start_dock_backdrop(
        self,
        theme,
        *,
        stdout_file: Path | None = None,
        stderr_file: Path | None = None,
    ) -> subprocess.Popen | None:
        """Start the visual dock shelf used for macOS-style Plank sessions."""
        stdout_sink = subprocess.DEVNULL
        stderr_sink = subprocess.DEVNULL
        stdout_fh = None
        stderr_fh = None
        if stdout_file:
            stdout_file.parent.mkdir(parents=True, exist_ok=True)
            stdout_fh = open(stdout_file, "w")
            stdout_sink = stdout_fh
        if stderr_file:
            stderr_file.parent.mkdir(parents=True, exist_ok=True)
            stderr_fh = open(stderr_file, "w")
            stderr_sink = stderr_fh

        backdrop_env = dict(os.environ)
        src_path = str(PROJECT_ROOT / "src")
        current_pythonpath = backdrop_env.get("PYTHONPATH", "")
        if src_path not in current_pythonpath.split(os.pathsep):
            backdrop_env["PYTHONPATH"] = (
                f"{src_path}{os.pathsep}{current_pythonpath}"
                if current_pythonpath else src_path
            )

        cmd = [
            sys.executable,
            "-m",
            "deskshot.environment.dock_backdrop",
            "--icon-size",
            "54",
            "--screen-width",
            str(self.config.display.width),
            "--screen-height",
            str(self.config.display.height),
        ]
        if "dark" in (theme.gtk_theme or "").strip().lower():
            cmd.append("--dark")

        proc = subprocess.Popen(
            cmd,
            stdout=stdout_sink,
            stderr=stderr_sink,
            env=backdrop_env,
            cwd=str(PROJECT_ROOT),
            start_new_session=True,
        )
        if stdout_fh:
            stdout_fh.close()
        if stderr_fh:
            stderr_fh.close()

        self._procs.append(proc)
        time.sleep(0.5)
        if proc.poll() is not None:
            import logging
            logging.getLogger(__name__).warning(
                "dock backdrop exited with code %s", proc.returncode
            )
            self._procs.remove(proc)
            return None
        return proc

    def _start_xfce_session(self, log_dir: Path | None = None) -> None:
        """Start XFCE desktop components with MATE panel + Caja for chrome.

        Uses xfwm4 as window manager (already patched), but replaces
        xfce4-panel and xfdesktop with mate-panel and caja, which expose
        proper AT-SPI accessibility for panel applets and desktop icons.

        Args:
            log_dir: If set, capture each component's stdout/stderr to files here.
        """
        from deskshot.environment.xfce_config import write_all_xfce_configs
        from deskshot.environment.mate_config import (
            apply_mate_panel_settings,
            get_mate_env,
            panel_layout_name,
            write_mate_dconf_config,
        )
        from deskshot.environment.picom_config import write_picom_config
        from deskshot.environment.plank_config import (
            apply_plank_settings,
            write_plank_launchers,
        )
        from deskshot.environment.themes import (
            resolve_firefox_theme_hint,
            resolve_theme_config,
            write_gtk_settings_ini,
        )
        from deskshot.environment.backgrounds import apply_desktop_background

        import logging
        logger = logging.getLogger(__name__)

        theme = resolve_theme_config(self.config.theme)
        self.config.theme = theme
        macos_style = (theme.desktop_style or "").strip().lower() == "macos"
        use_picom = False
        use_plank = False
        if macos_style:
            try:
                self._find_binary("picom")
            except FileNotFoundError:
                pass
            else:
                use_picom = True
            try:
                self._find_binary("plank")
                self._find_binary("bamfdaemon")
            except FileNotFoundError:
                pass
            else:
                use_plank = True

        # Set XFCE desktop session env vars (xfwm4 still needs these)
        self._set_env("XDG_CURRENT_DESKTOP", "XFCE")
        self._set_env("XDG_SESSION_DESKTOP", "xfce")
        self._set_env("DESKTOP_SESSION", "xfce")
        self._set_env("DESKSHOT_FIREFOX_THEME", resolve_firefox_theme_hint(theme))

        # Write pre-configuration files
        xdg_config = os.environ.get("XDG_CONFIG_HOME", str(self._xdg_root / "config"))
        write_all_xfce_configs(
            theme,
            Path(xdg_config),
            external_compositor=use_picom,
        )
        write_gtk_settings_ini(theme, Path(xdg_config), extracted_dir=self._extracted)

        # Write MATE dconf config for panel + caja
        write_mate_dconf_config(theme, self._xdg_root, use_plank=use_plank)
        mate_layout_name = panel_layout_name(theme, use_plank=use_plank)
        mate_env = get_mate_env(self._xdg_root)
        for key, val in mate_env.items():
            self._set_env(key, val)
        self._set_env("DESKSHOT_DISABLE_ROOT_WALLPAPER_PAINT", "1")
        apply_desktop_background(
            theme,
            env=os.environ,
            size=(self.config.display.width, self.config.display.height),
        )
        if use_plank:
            write_plank_launchers(Path(xdg_config))
            apply_plank_settings(theme, env=os.environ)
        picom_config_path = write_picom_config(theme, Path(xdg_config)) if use_picom else None

        def _log_paths(name: str):
            """Return (stdout_file, stderr_file) or (None, None) if no log_dir."""
            if log_dir is None:
                return None, None
            log_dir.mkdir(parents=True, exist_ok=True)
            return log_dir / f"{name}.stdout.log", log_dir / f"{name}.stderr.log"

        # Start components sequentially with waits
        # xfconfd — config daemon (reads our pre-written XML files)
        logger.info("Starting xfconfd...")
        so, se = _log_paths("xfconfd")
        self._start_xfce_component("xfconfd", wait=0.5, stdout_file=so, stderr_file=se)

        # xfsettingsd — applies xsettings to X
        logger.info("Starting xfsettingsd...")
        so, se = _log_paths("xfsettingsd")
        self._start_xfce_component("xfsettingsd", ["--replace"], wait=0.5,
                                    stdout_file=so, stderr_file=se)

        # xfwm4 — window manager (keep — already patched and working)
        logger.info("Starting xfwm4...")
        so, se = _log_paths("xfwm4")
        self._start_xfce_component("xfwm4", ["--replace"], wait=1.0,
                                    stdout_file=so, stderr_file=se)

        if use_picom and picom_config_path is not None:
            logger.info("Starting picom...")
            so, se = _log_paths("picom")
            self._start_xfce_component(
                "picom",
                ["--config", str(picom_config_path)],
                wait=0.8,
                stdout_file=so,
                stderr_file=se,
            )

        mate_settings_started = False
        try:
            self._find_binary("mate-settings-daemon")
        except FileNotFoundError:
            logger.info("mate-settings-daemon not available; keeping screenshot wallpaper fallback")
        else:
            logger.info("Starting mate-settings-daemon...")
            so, se = _log_paths("mate-settings-daemon")
            proc = self._start_xfce_component(
                "mate-settings-daemon",
                ["--replace"],
                wait=1.5,
                stdout_file=so,
                stderr_file=se,
            )
            mate_settings_started = proc is not None
            if mate_settings_started:
                # Re-apply background settings now that the MATE background plugin is alive.
                apply_desktop_background(
                    theme,
                    env=os.environ,
                    size=(self.config.display.width, self.config.display.height),
                )
                self._set_env("DESKSHOT_LIVE_WALLPAPER_ACTIVE", "1")

        def _reset_mate_panel_state() -> None:
            """Clear panel keys and reload layout defaults."""
            try:
                import shutil

                mate_panel_bin = self._find_binary("mate-panel")

                def _run_quiet(cmd: list[str]) -> subprocess.CompletedProcess:
                    return subprocess.run(
                        cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        env=os.environ,
                        timeout=GSETTINGS_TIMEOUT_SEC,
                    )

                dconf_bin = shutil.which("dconf")
                backend = os.environ.get("GSETTINGS_BACKEND", "").strip().lower()
                if dconf_bin and backend != "keyfile":
                    _run_quiet([dconf_bin, "reset", "-f", "/org/mate/panel/"])
                    _run_quiet([dconf_bin, "reset", "-f", "/org/mate/caja/"])
                    _run_quiet(
                        [
                            dconf_bin,
                            "write",
                            "/org/mate/panel/general/default-layout",
                            f"'{mate_layout_name}'",
                        ]
                    )
                else:
                    _run_quiet(["gsettings", "reset-recursively", "org.mate.panel"])
                    _run_quiet(["gsettings", "reset-recursively", "org.mate.caja.desktop"])

                _run_quiet(["gsettings", "set", "org.mate.panel", "default-layout", mate_layout_name])

                reset = _run_quiet([mate_panel_bin, "--reset"])
                if reset.returncode != 0:
                    logger.warning("mate-panel --reset returned %s", reset.returncode)
                _run_quiet(["gsettings", "set", "org.mate.panel", "default-layout", mate_layout_name])
            except (FileNotFoundError, subprocess.TimeoutExpired):
                logger.warning("Failed to reset mate-panel state")

        def _reload_mate_panel_layout() -> None:
            try:
                mate_panel_bin = self._find_binary("mate-panel")
                subprocess.run(
                    ["gsettings", "set", "org.mate.panel", "default-layout", mate_layout_name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=os.environ,
                    timeout=GSETTINGS_TIMEOUT_SEC,
                )
                reset = subprocess.run(
                    [mate_panel_bin, "--reset"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=os.environ,
                    timeout=GSETTINGS_TIMEOUT_SEC,
                )
                if reset.returncode != 0:
                    logger.warning("mate-panel --reset returned %s", reset.returncode)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                logger.warning("Failed to reload mate-panel layout")

        _reset_mate_panel_state()
        apply_mate_panel_settings(theme, env=os.environ, use_plank=use_plank)

        # mate-panel — replaces xfce4-panel (proper AT-SPI accessibility)
        logger.info("Starting mate-panel...")
        so, se = _log_paths("mate-panel")
        self._start_xfce_component("mate-panel", ["--replace"], wait=2.0,
                                    stdout_file=so, stderr_file=se)

        # Normalize once panel is alive, then immediately re-attach a tracked
        # panel process so health checks can follow session-owned PIDs.
        _reload_mate_panel_layout()
        so, se = _log_paths("mate-panel")
        self._start_xfce_component("mate-panel", ["--replace"], wait=1.5,
                                    stdout_file=so, stderr_file=se)

        if use_plank:
            logger.info("Starting bamfdaemon...")
            so, se = _log_paths("bamfdaemon")
            self._start_xfce_component(
                "bamfdaemon",
                wait=0.8,
                stdout_file=so,
                stderr_file=se,
            )
            logger.info("Starting plank...")
            so, se = _log_paths("plank")
            self._start_xfce_component(
                "plank",
                wait=1.0,
                stdout_file=so,
                stderr_file=se,
            )

            logger.info("Starting dock backdrop...")
            so, se = _log_paths("dock-backdrop")
            self._start_dock_backdrop(theme, stdout_file=so, stderr_file=se)
            try:
                subprocess.run(
                    [
                        self._find_binary("xdotool"),
                        "search",
                        "--name",
                        "plank",
                        "windowraise",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=os.environ,
                    timeout=3,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

        # Caja's desktop shows more than the fixture. Left alone it draws the
        # host's mount table as volume icons - the cluster's GPFS device,
        # `sys`, `proc`, `shm`, `pts` - and labels the home icon from the real
        # account, so `<account>'s Home` appeared in most captures. None of
        # that can be removed from a screenshot afterwards.
        self._apply_caja_desktop_privacy()

        # caja — replaces xfdesktop (desktop icons with AT-SPI)
        logger.info("Starting caja (desktop mode)...")
        so, se = _log_paths("caja")
        self._start_xfce_component(
            "caja", ["--force-desktop", "--no-default-window"],
            wait=1.5, stdout_file=so, stderr_file=se,
        )
        if mate_settings_started:
            apply_desktop_background(
                theme,
                env=os.environ,
                size=(self.config.display.width, self.config.display.height),
            )
        else:
            os.environ.pop("DESKSHOT_LIVE_WALLPAPER_ACTIVE", None)

        logger.info("XFCE + MATE chrome session started")

    def __enter__(self) -> "DesktopSession":
        # The prefix is neutral because the session HOME lives inside this
        # directory and its absolute path is drawn in app title bars. The marker
        # file is what `cleanup_stale_sessions.py` matches on instead.
        self._tmpdir = tempfile.TemporaryDirectory(prefix="session-")
        (Path(self._tmpdir.name) / SESSION_MARKER_FILE).write_text("", encoding="utf-8")
        session_token = f"deskshot-{os.getpid()}-{int(time.time() * 1000)}"
        self._set_env("DESKSHOT_SESSION_TOKEN", session_token)
        self._remember_session_marker("DESKSHOT_SESSION_TOKEN", session_token)

        from deskshot.environment.setup import ensure_runtime_path_bridges

        ensure_runtime_path_bridges(self._extracted)
        self._setup_env()
        self._start_xvfb()
        self._start_dbus()
        self._start_atspi()

        desktop_env = self.config.desktop_env
        if desktop_env == "xfce":
            self._setup_xdg_isolation()
            self._start_xfce_session(log_dir=self.log_dir)
        elif desktop_env == "metacity":
            self._start_window_manager()
        # desktop_env == "none" → skip WM entirely

        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        owned_before = self._session_owned_pids()

        # Kill all child processes in reverse order
        for proc in reversed(self._procs):
            self._signal_tracked_proc(proc, signal.SIGTERM)

        # Wait briefly for graceful shutdown
        deadline = time.monotonic() + 3.0
        for proc in self._procs:
            remaining = max(0.1, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                try:
                    self._signal_tracked_proc(proc, signal.SIGKILL)
                    proc.wait(timeout=0.5)
                except (OSError, subprocess.TimeoutExpired):
                    pass

        time.sleep(0.2)
        owned_after = self._session_owned_pids()
        self._signal_session_pids(owned_before | owned_after, signal.SIGTERM)
        time.sleep(0.3)
        self._signal_session_pids(self._session_owned_pids(), signal.SIGKILL)

        self._procs.clear()

        # Restore environment
        for key, old_val in self._env_backup.items():
            if old_val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_val
        self._env_backup.clear()
        self._session_markers.clear()

        # Clean up tmpdir
        if self._tmpdir:
            self._tmpdir.cleanup()
            self._tmpdir = None

    @property
    def display(self) -> str:
        return self.config.display.display_str

    @property
    def env(self) -> dict[str, str]:
        """Return current environment (for subprocess.Popen)."""
        return dict(os.environ)

    @property
    def tracked_pids(self) -> list[int]:
        """Return alive process IDs launched by this session."""
        return [
            proc.pid for proc in self._procs
            if proc.poll() is None and proc.pid is not None
        ]
