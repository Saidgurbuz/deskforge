"""Tests for app qualification metrics and manifest resolution."""

from pathlib import Path

from deskshot.audit.app_audit import (
    DEFAULT_LIVE_BROWSER_WAIT_SECONDS,
    FALLBACK_PROBES,
    _normalized_text,
    browser_audit_page_uri,
    discover_probe_specs,
    prepare_interaction_for_audit,
    prepare_manifest_for_audit,
    resolve_probe_manifests,
    select_probe_interaction,
    summarize_app_elements,
)
from deskshot.config import Action, AppManifest, InteractionSequence


def _elem(idx, elem_type, role, rect, *, text="", parent=None, app_name="thunar", source="app"):
    return {
        "_dom_index": idx,
        "_parent_dom_index": parent,
        "parent_index": parent,
        "_children_dom_indices": [],
        "children_indices": [],
        "type": elem_type,
        "role": role,
        "rect": dict(rect),
        "inner_text": text,
        "app_name": app_name,
        "source": source,
    }


def test_summarize_app_elements_passes_dense_app() -> None:
    elements = [
        _elem(0, "Window", "frame", {"x": 0, "y": 0, "w": 800, "h": 600}, text="Thunar"),
        _elem(1, "Toolbar", "tool bar", {"x": 0, "y": 0, "w": 800, "h": 40}),
        _elem(2, "Button", "push button", {"x": 10, "y": 8, "w": 32, "h": 24}, text="Back"),
        _elem(3, "Button", "push button", {"x": 46, "y": 8, "w": 32, "h": 24}, text="Forward"),
        _elem(4, "Button", "push button", {"x": 82, "y": 8, "w": 32, "h": 24}, text="Home"),
        _elem(5, "Text Input", "entry", {"x": 120, "y": 8, "w": 250, "h": 24}, text="/home/said"),
        _elem(6, "Side Bar", "split pane", {"x": 0, "y": 40, "w": 180, "h": 560}),
        _elem(7, "List Item", "list item", {"x": 8, "y": 60, "w": 160, "h": 24}, text="Home"),
        _elem(8, "List Item", "list item", {"x": 8, "y": 90, "w": 160, "h": 24}, text="Desktop"),
        _elem(9, "List", "list", {"x": 180, "y": 40, "w": 620, "h": 560}),
        _elem(10, "File Icon", "icon", {"x": 220, "y": 80, "w": 64, "h": 72}, text="Documents"),
        _elem(11, "File Icon", "icon", {"x": 310, "y": 80, "w": 64, "h": 72}, text="Downloads"),
        _elem(12, "Menu", "menu", {"x": 650, "y": 8, "w": 60, "h": 24}, text="View"),
        _elem(13, "Tab", "page tab", {"x": 390, "y": 8, "w": 120, "h": 24}, text="Home"),
    ]

    summary = summarize_app_elements(elements, viewport_w=800, viewport_h=600)
    assert summary["status"] == "pass"
    assert summary["metrics"]["num_interactive_elements"] >= 6


def test_summarize_app_elements_rejects_sparse_app() -> None:
    elements = [
        _elem(0, "Window", "frame", {"x": 0, "y": 0, "w": 300, "h": 200}, text="Viewer"),
        _elem(1, "Image", "image", {"x": 20, "y": 20, "w": 260, "h": 160}),
        _elem(2, "Text", "label", {"x": 20, "y": 184, "w": 80, "h": 12}, text="Preview"),
    ]

    summary = summarize_app_elements(elements, viewport_w=300, viewport_h=200)
    assert summary["status"] == "reject"
    assert "elements<14" in summary["hard_failures"]


def test_summarize_app_elements_flags_same_area_clusters_for_review() -> None:
    base = {"x": 20, "y": 20, "w": 400, "h": 300}
    elements = [
        _elem(0, "Window", "frame", {"x": 0, "y": 0, "w": 500, "h": 400}, text="Firefox"),
        _elem(1, "Scroll", "scroll pane", base, parent=0),
        _elem(2, "Text", "document web", base, parent=0),
        _elem(3, "Text", "section", base, parent=0),
        _elem(4, "Button", "push button", {"x": 30, "y": 30, "w": 60, "h": 30}, text="Back"),
        _elem(5, "Button", "push button", {"x": 95, "y": 30, "w": 60, "h": 30}, text="Forward"),
        _elem(6, "Text Input", "entry", {"x": 160, "y": 30, "w": 180, "h": 30}, text="about:blank"),
        _elem(7, "Menu", "menu", {"x": 345, "y": 30, "w": 70, "h": 30}, text="File"),
        _elem(8, "Tab", "page tab", {"x": 30, "y": 65, "w": 120, "h": 30}, text="New Tab"),
        _elem(9, "Link", "link", {"x": 30, "y": 120, "w": 160, "h": 20}, text="Example"),
        _elem(10, "Button", "push button", {"x": 95, "y": 65, "w": 60, "h": 30}, text="Reload"),
        _elem(11, "Button", "push button", {"x": 160, "y": 65, "w": 60, "h": 30}, text="Home"),
        _elem(12, "Button", "push button", {"x": 225, "y": 65, "w": 60, "h": 30}, text="Downloads"),
        _elem(13, "Menu", "menu", {"x": 345, "y": 65, "w": 70, "h": 30}, text="Help"),
    ]

    summary = summarize_app_elements(elements, viewport_w=500, viewport_h=400)
    assert summary["status"] == "review"
    assert "largest_same_area_cluster>2" in summary["review_flags"]


def test_normalized_text_strips_placeholder_glyphs() -> None:
    assert _normalized_text("\ufffc  Open \u200b") == "open"


def test_resolve_probe_manifests_includes_fallback_spec(tmp_path: Path) -> None:
    manifests = resolve_probe_manifests(tmp_path, app_names=["thunar"])
    assert len(manifests) == 1
    assert manifests[0].app_name == FALLBACK_PROBES["thunar"].app_name


def test_select_probe_interaction_prefers_audit_probe() -> None:
    manifest = AppManifest(
        app_name="mousepad",
        binary="mousepad",
        atspi_name="mousepad",
        interactions=[
            InteractionSequence(name="default", actions=[Action(type="wait", value="1.0")]),
            InteractionSequence(name="audit_probe", actions=[Action(type="key", value="alt+f")]),
        ],
    )

    chosen = select_probe_interaction(manifest)
    assert chosen.name == "audit_probe"


def _write_desktop_file(path: Path, body: str) -> None:
    path.write_text(body.strip() + "\n", encoding="utf-8")


def test_discover_probe_specs_filters_desktop_noise(tmp_path: Path, monkeypatch) -> None:
    apps_dir = tmp_path / "applications"
    apps_dir.mkdir()

    _write_desktop_file(
        apps_dir / "chromium-browser.desktop",
        """
        [Desktop Entry]
        Name=Chromium Web Browser
        Exec=/usr/bin/chromium-browser %U
        Type=Application
        Terminal=false
        Categories=Network;WebBrowser;
        StartupWMClass=Chromium-browser
        """,
    )
    _write_desktop_file(
        apps_dir / "panel.desktop",
        """
        [Desktop Entry]
        Name=Panel
        Exec=mate-panel
        Type=Application
        Terminal=false
        Categories=System;Core;
        """,
    )
    _write_desktop_file(
        apps_dir / "hidden.desktop",
        """
        [Desktop Entry]
        Name=Hidden App
        Exec=evince-previewer %U
        Type=Application
        Terminal=false
        NoDisplay=true
        Categories=Office;Viewer;
        """,
    )
    _write_desktop_file(
        apps_dir / "settings.desktop",
        """
        [Desktop Entry]
        Name=Appearance
        Exec=xfce4-appearance-settings
        Type=Application
        Terminal=false
        Categories=GTK;Settings;DesktopSettings;
        """,
    )

    def _fake_find_binary(binary_name: str) -> str:
        if binary_name in {"chromium-browser", "evince-previewer", "xfce4-appearance-settings", "mate-panel"}:
            return f"/fake/{binary_name}"
        raise FileNotFoundError(binary_name)

    monkeypatch.setattr("deskshot.audit.app_audit.find_binary", _fake_find_binary)

    specs = discover_probe_specs(apps_dir)
    assert [spec.app_name for spec in specs] == ["chromium-browser"]
    assert specs[0].binary == "chromium-browser"
    assert specs[0].atspi_name == "Chromium"
    assert specs[0].window_titles[:3] == ["Chromium Web Browser", "Chromium-browser", "Chromium"]
    assert "--no-sandbox" in specs[0].args
    assert "--user-data-dir=/tmp/deskshot_chromium_profile" in specs[0].args
    assert specs[0].env["ACCESSIBILITY_ENABLED"] == "1"


def test_discover_probe_specs_accepts_development_editors(tmp_path: Path, monkeypatch) -> None:
    apps_dir = tmp_path / "applications"
    apps_dir.mkdir()

    _write_desktop_file(
        apps_dir / "geany.desktop",
        """
        [Desktop Entry]
        Name=Geany
        Exec=/usr/bin/geany %F
        Type=Application
        Terminal=false
        Categories=GTK;Development;IDE;
        StartupWMClass=Geany
        """,
    )

    monkeypatch.setattr("deskshot.audit.app_audit.find_binary", lambda binary_name: f"/fake/{binary_name}")

    specs = discover_probe_specs(apps_dir)
    assert [spec.app_name for spec in specs] == ["geany"]
    assert specs[0].binary == "geany"
    assert specs[0].window_titles[:2] == ["Geany", "geany"]


def test_resolve_probe_manifests_can_discover_unknown_candidate(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    apps_dir = tmp_path / "applications"
    apps_dir.mkdir()

    (config_dir / "gnome_calculator.yaml").write_text(
        """
app_name: gnome-calculator
binary: gnome-calculator
atspi_name: gnome-calculator
interactions:
  - name: default
    actions:
      - type: wait
        value: "1.0"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    _write_desktop_file(
        apps_dir / "chromium-browser.desktop",
        """
        [Desktop Entry]
        Name=Chromium Web Browser
        Exec=/usr/bin/chromium-browser %U
        Type=Application
        Terminal=false
        Categories=Network;WebBrowser;
        StartupWMClass=Chromium-browser
        """,
    )
    _write_desktop_file(
        apps_dir / "org.gnome.Calculator.desktop",
        """
        [Desktop Entry]
        Name=Calculator
        Exec=gnome-calculator
        Type=Application
        Terminal=false
        Categories=GNOME;GTK;Utility;Calculator;
        """,
    )

    def _fake_find_binary(binary_name: str) -> str:
        if binary_name in {"chromium-browser", "gnome-calculator"}:
            return f"/fake/{binary_name}"
        raise FileNotFoundError(binary_name)

    monkeypatch.setattr("deskshot.audit.app_audit.find_binary", _fake_find_binary)

    manifests = resolve_probe_manifests(
        config_dir,
        discover=True,
        applications_dir=apps_dir,
    )
    assert [manifest.app_name for manifest in manifests] == ["chromium-browser"]
    assert manifests[0].binary == "chromium-browser"
    assert "--no-sandbox" in manifests[0].args


def test_prepare_manifest_for_audit_rewrites_chromium_target() -> None:
    manifest = AppManifest(
        app_name="chromium-browser",
        binary="chromium-browser",
        atspi_name="Chromium",
        args=[
            "--no-sandbox",
            "--user-data-dir=/tmp/deskshot_chromium_profile",
            "about:blank",
        ],
        window_titles=["Chromium"],
    )

    updated = prepare_manifest_for_audit(manifest)

    assert updated is not manifest
    assert "--force-renderer-accessibility" in updated.args
    assert "--test-type" in updated.args
    assert browser_audit_page_uri() in updated.args
    assert "about:blank" not in updated.args
    assert updated.env["ACCESSIBILITY_ENABLED"] == "1"
    assert updated.env["GNOME_ACCESSIBILITY"] == "1"
    assert "DeskShot Browser Audit" in updated.window_titles


def test_prepare_manifest_for_audit_rewrites_firefox_target_without_losing_env() -> None:
    manifest = AppManifest(
        app_name="firefox",
        binary="firefox-bin",
        atspi_name="firefox",
        args=[
            "--no-remote",
            "--profile",
            "/tmp/deskshot_firefox_profile",
            "--new-window",
            "about:blank",
        ],
        env={"MOZILLA_FIVE_HOME": "/tmp/firefox"},
        window_titles=["Mozilla Firefox"],
    )

    updated = prepare_manifest_for_audit(manifest)

    assert updated is not manifest
    assert updated.args[-1] == browser_audit_page_uri()
    assert "about:blank" not in updated.args
    assert updated.env["MOZILLA_FIVE_HOME"] == "/tmp/firefox"
    assert updated.env["ACCESSIBILITY_ENABLED"] == "1"
    assert updated.env["GNOME_ACCESSIBILITY"] == "1"
    assert updated.env["MOZ_ACCESSIBILITY_FORCE_DISABLED"] == "0"
    assert "DeskShot Browser Audit" in updated.window_titles


def test_prepare_manifest_for_audit_accepts_live_browser_url() -> None:
    manifest = AppManifest(
        app_name="firefox",
        binary="firefox-bin",
        atspi_name="firefox",
        args=["--new-window", "about:blank"],
        window_titles=["Mozilla Firefox"],
    )

    updated = prepare_manifest_for_audit(
        manifest,
        browser_url="https://www.python.org/",
    )

    assert "https://www.python.org/" in updated.args
    assert "about:blank" not in updated.args
    assert "DeskShot Browser Audit" not in updated.window_titles


def test_prepare_interaction_for_audit_extends_browser_wait() -> None:
    manifest = AppManifest(
        app_name="firefox",
        binary="firefox-bin",
        atspi_name="firefox",
        interactions=[],
    )
    interaction = InteractionSequence(
        name="default",
        actions=[Action(type="wait", value="2.0")],
    )

    updated = prepare_interaction_for_audit(
        interaction,
        manifest,
        browser_wait_seconds=DEFAULT_LIVE_BROWSER_WAIT_SECONDS,
    )

    assert updated.actions[0].value == f"{DEFAULT_LIVE_BROWSER_WAIT_SECONDS:.1f}"
