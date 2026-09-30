"""Health checks for the desktop session (display, D-Bus, AT-SPI)."""

from __future__ import annotations



import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, List, Tuple


if TYPE_CHECKING:  # referenced only from quoted annotations
    from gi.repository import Atspi


def check_display() -> Tuple[bool, str]:
    """Check if DISPLAY is set and responsive."""
    display = os.environ.get("DISPLAY")
    if not display:
        return False, "DISPLAY not set"

    # Try GDK first (most reliable)
    try:
        import gi
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk

        gdk_display = Gdk.Display.open(display)
        if gdk_display is not None:
            return True, f"Display {display} OK (GDK)"
    except Exception:
        pass

    # Fallback to xdpyinfo
    try:
        result = subprocess.run(
            ["xdpyinfo", "-display", display],
            capture_output=True, timeout=3,
        )
        if result.returncode == 0:
            return True, f"Display {display} OK (xdpyinfo)"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Last resort: check if we can create a simple X connection
    try:
        import gi
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk
        Gdk.init([])
        return True, f"Display {display} OK (Gdk.init)"
    except Exception as e:
        return False, f"Display {display} not responding: {e}"


def check_dbus() -> Tuple[bool, str]:
    """Check if D-Bus session bus is active."""
    bus_addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if not bus_addr:
        return False, "DBUS_SESSION_BUS_ADDRESS not set"

    try:
        result = subprocess.run(
            ["dbus-send", "--session", "--print-reply",
             "--dest=org.freedesktop.DBus",
             "/org/freedesktop/DBus",
             "org.freedesktop.DBus.ListNames"],
            capture_output=True, timeout=3,
            env=os.environ,
        )
        if result.returncode == 0:
            return True, "D-Bus session bus OK"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return False, f"D-Bus session bus not responding at {bus_addr}"


def check_atspi() -> Tuple[bool, str]:
    """Check if AT-SPI2 registry is running and accessible."""
    try:
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi

        desktop = Atspi.get_desktop(0)
        if desktop is not None:
            child_count = desktop.get_child_count()
            return True, f"AT-SPI registry OK ({child_count} app(s) on desktop)"
    except Exception as e:
        return False, f"AT-SPI not accessible: {e}"

    return False, "AT-SPI desktop object is None"


def check_atspi_app(app_name: str) -> Tuple[bool, str]:
    """Check if a specific application is visible in AT-SPI tree."""
    try:
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi

        desktop = Atspi.get_desktop(0)
        if desktop is None:
            return False, "AT-SPI desktop is None"

        for i in range(desktop.get_child_count()):
            child = desktop.get_child_at_index(i)
            if child is not None:
                name = child.get_name()
                if name and app_name.lower() in name.lower():
                    return True, f"Found app '{name}' in AT-SPI tree"

        apps = []
        for i in range(desktop.get_child_count()):
            child = desktop.get_child_at_index(i)
            if child is not None:
                apps.append(child.get_name() or "<unnamed>")
        return False, f"App '{app_name}' not found. Available: {apps}"

    except Exception as e:
        return False, f"AT-SPI error: {e}"


def _panel_apps() -> list["Atspi.Accessible"]:
    """Return AT-SPI application nodes that look like panel processes."""
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    desktop = Atspi.get_desktop(0)
    if desktop is None:
        return []

    panel_nodes = []
    for i in range(desktop.get_child_count()):
        child = desktop.get_child_at_index(i)
        if child is None:
            continue
        name = (child.get_name() or "").lower()
        if "mate-panel" in name or "xfce4-panel" in name or name == "panel":
            panel_nodes.append(child)
    return panel_nodes


def check_xfce_panel() -> Tuple[bool, str]:
    """Check if a panel (XFCE or MATE) is visible in the AT-SPI tree."""
    try:
        panel_nodes = _panel_apps()
        if not panel_nodes:
            return False, "Panel not found in AT-SPI tree (checked xfce4-panel, mate-panel)"
        names = [p.get_name() or "<unnamed>" for p in panel_nodes]
        return True, f"Panel app(s) found: {names}"
    except Exception as e:
        return False, f"AT-SPI error checking panel: {e}"


def check_panel_children(
    min_semantic_children: int = 3,
    min_edge_children: int = 2,
) -> Tuple[bool, str]:
    """Check that panel exposes non-trivial accessible descendants.

    This catches cases where a panel process starts but only exposes a frame
    (no applet children), which produces sparse/low-value chrome annotations.
    """
    try:
        panel_nodes = _panel_apps()
        if not panel_nodes:
            return False, "No panel app found in AT-SPI tree"

        ignored_roles = {
            "application", "frame", "panel", "filler",
            "desktop-frame", "unknown", "invalid",
        }

        def _count_semantic_descendants(node) -> tuple[int, int]:
            total = 0
            semantic_boxes: list[tuple[int, int, int, int]] = []
            stack = [node]
            while stack:
                cur = stack.pop()
                try:
                    child_count = cur.get_child_count()
                except Exception:
                    continue
                for idx in range(child_count):
                    try:
                        child = cur.get_child_at_index(idx)
                        if child is None:
                            continue
                        stack.append(child)
                        role = (child.get_role_name() or "").replace(" ", "-").lower()
                        if role not in ignored_roles:
                            total += 1
                            try:
                                comp = child.get_component_iface()
                                if comp is not None:
                                    ext = comp.get_extents(0)  # SCREEN coordinates
                                    semantic_boxes.append((ext.x, ext.y, ext.width, ext.height))
                            except Exception:
                                pass
                    except Exception:
                        continue
            if not semantic_boxes:
                return total, 0

            max_bottom = max(y + h for _, y, _, h in semantic_boxes)
            edge_threshold = max(50, int(max_bottom * 0.08))
            edge_count = 0
            for _, y, _, h in semantic_boxes:
                top_dist = y
                bottom_dist = max_bottom - (y + h)
                if top_dist <= edge_threshold or bottom_dist <= edge_threshold:
                    edge_count += 1
            return total, edge_count

        per_panel = []
        for panel in panel_nodes:
            name = panel.get_name() or "<unnamed>"
            count, edge_count = _count_semantic_descendants(panel)
            per_panel.append((name, count, edge_count))

        best_name, best_count, best_edge_count = max(per_panel, key=lambda x: x[1])
        if best_count < min_semantic_children:
            return (
                False,
                f"Panel '{best_name}' has only {best_count} semantic descendants "
                f"(min {min_semantic_children})",
            )
        if best_edge_count < min_edge_children:
            return (
                False,
                f"Panel '{best_name}' descendants are off panel edge "
                f"(edge={best_edge_count}, min {min_edge_children})",
            )
        return (
            True,
            f"Panel '{best_name}' exposes {best_count} semantic descendants "
            f"({best_edge_count} near panel edge)",
        )
    except Exception as e:
        return False, f"AT-SPI error checking panel children: {e}"


def check_xfce_wm(tracked_pids: list[int] | None = None) -> Tuple[bool, str]:
    """Check if xfwm4 is running (only check our tracked PIDs)."""
    if tracked_pids:
        for pid in tracked_pids:
            try:
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
                if b"xfwm4" in cmdline:
                    return True, f"xfwm4 running (PID {pid})"
            except (FileNotFoundError, PermissionError):
                continue
        return False, "xfwm4 not found among tracked PIDs"

    # Fallback: check /proc for our user's xfwm4 processes
    import getpass
    username = getpass.getuser()
    try:
        result = subprocess.run(
            ["pgrep", "-u", username, "xfwm4"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            pid = result.stdout.strip().split()[0]
            return True, f"xfwm4 running (PID {pid})"
    except FileNotFoundError:
        pass

    return False, "xfwm4 not running"


def check_xfce_desktop(tracked_pids: list[int] | None = None) -> Tuple[bool, str]:
    """Check if xfdesktop or caja desktop is running (only check our tracked PIDs)."""
    if tracked_pids:
        for pid in tracked_pids:
            try:
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
                if b"xfdesktop" in cmdline:
                    return True, f"xfdesktop running (PID {pid})"
                if b"caja" in cmdline:
                    return True, f"caja desktop running (PID {pid})"
            except (FileNotFoundError, PermissionError):
                continue
        return False, "Desktop manager not found among tracked PIDs"

    # Fallback: check /proc for our user's desktop processes
    import getpass
    username = getpass.getuser()
    for proc_name in ("caja", "xfdesktop"):
        try:
            result = subprocess.run(
                ["pgrep", "-u", username, proc_name],
                capture_output=True, text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                pid = result.stdout.strip().split()[0]
                return True, f"{proc_name} running (PID {pid})"
        except FileNotFoundError:
            pass

    return False, "Desktop manager not running (checked xfdesktop, caja)"


def check_mate_panel(tracked_pids: list[int] | None = None) -> Tuple[bool, str]:
    """Check if mate-panel is running (only check our tracked PIDs)."""
    if tracked_pids:
        for pid in tracked_pids:
            try:
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
                if b"mate-panel" in cmdline:
                    return True, f"mate-panel running (PID {pid})"
            except (FileNotFoundError, PermissionError):
                continue
        return False, "mate-panel not found among tracked PIDs"

    import getpass
    username = getpass.getuser()
    try:
        result = subprocess.run(
            ["pgrep", "-u", username, "mate-panel"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            pid = result.stdout.strip().split()[0]
            return True, f"mate-panel running (PID {pid})"
    except FileNotFoundError:
        pass

    return False, "mate-panel not running"


def check_caja(tracked_pids: list[int] | None = None) -> Tuple[bool, str]:
    """Check if caja is running (only check our tracked PIDs)."""
    if tracked_pids:
        for pid in tracked_pids:
            try:
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
                if b"caja" in cmdline:
                    return True, f"caja running (PID {pid})"
            except (FileNotFoundError, PermissionError):
                continue
        return False, "caja not found among tracked PIDs"

    import getpass
    username = getpass.getuser()
    try:
        result = subprocess.run(
            ["pgrep", "-u", username, "caja"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            pid = result.stdout.strip().split()[0]
            return True, f"caja running (PID {pid})"
    except FileNotFoundError:
        pass

    return False, "caja not running"


def run_all_checks(
    desktop_env: str = "metacity",
    tracked_pids: list[int] | None = None,
) -> List[Tuple[str, bool, str]]:
    """Run all health checks. Returns list of (name, passed, message).

    Args:
        desktop_env: Desktop environment type ("none", "metacity", or "xfce").
        tracked_pids: Session-managed PIDs for process-scoped checks.
    """
    checks = [
        ("Display", check_display()),
        ("D-Bus", check_dbus()),
        ("AT-SPI", check_atspi()),
    ]

    if desktop_env == "xfce":
        checks.extend([
            ("Panel (MATE/XFCE)", check_xfce_panel()),
            ("Panel descendants", check_panel_children()),
            ("Window Manager", check_xfce_wm(tracked_pids=tracked_pids)),
            ("Desktop (Caja/XFCE)", check_xfce_desktop(tracked_pids=tracked_pids)),
            ("mate-panel process", check_mate_panel(tracked_pids=tracked_pids)),
            ("caja process", check_caja(tracked_pids=tracked_pids)),
        ])

    return [(name, ok, msg) for name, (ok, msg) in checks]


def print_health_report(checks: List[Tuple[str, bool, str]] | None = None) -> bool:
    """Print formatted health report. Returns True if all checks pass."""
    if checks is None:
        checks = run_all_checks()

    all_ok = True
    for name, ok, msg in checks:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}: {msg}")
        if not ok:
            all_ok = False

    return all_ok
