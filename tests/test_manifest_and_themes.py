"""Tests for manifest parsing and theme presets."""

from pathlib import Path

from deskshot.config import AppManifest, PROJECT_ROOT, ThemeConfig
from deskshot.environment.panel_assets import build_macos_panel_assets, dock_background_path
from deskshot.environment.stylepacks import install_style_packs
from deskshot.environment.themes import (
    get_theme_preset,
    list_theme_presets,
    resolve_firefox_theme_hint,
    resolve_theme_config,
)


def test_manifest_parses_companion_apps_and_cwd(tmp_path: Path) -> None:
    yaml_path = tmp_path / "sample.yaml"
    yaml_path.write_text(
        "app_name: calc\n"
        "binary: gnome-calculator\n"
        "atspi_name: gnome-calculator\n"
        "cwd: /tmp\n"
        "window_titles: [Calculator]\n"
        "interactions:\n"
        "  - name: multi\n"
        "    companion_apps: [firefox]\n"
        "    actions_after_companions: true\n"
        "    actions:\n"
        "      - type: wait\n"
        "        value: \"1.0\"\n",
        encoding="utf-8",
    )

    manifest = AppManifest.from_yaml(yaml_path)
    assert manifest.cwd == "/tmp"
    assert manifest.window_titles == ["Calculator"]
    assert len(manifest.interactions) == 1
    assert manifest.interactions[0].companion_apps == ["firefox"]
    assert manifest.interactions[0].actions_after_companions is True


def test_manifest_parses_chains(tmp_path: Path) -> None:
    yaml_path = tmp_path / "sample.yaml"
    yaml_path.write_text(
        "app_name: calc\n"
        "binary: gnome-calculator\n"
        "atspi_name: gnome-calculator\n"
        "chains:\n"
        "  - name: progressive\n"
        "    description: one session many captures\n"
        "    steps:\n"
        "      - name: empty\n"
        "        capture: true\n"
        "        actions:\n"
        "          - type: wait\n"
        "            value: \"1.0\"\n"
        "      - name: with_menu\n"
        "        companion_apps: [firefox]\n"
        "        actions_after_companions: true\n"
        "        actions:\n"
        "          - type: key\n"
        "            value: F10\n",
        encoding="utf-8",
    )

    manifest = AppManifest.from_yaml(yaml_path)
    assert len(manifest.chains) == 1
    chain = manifest.chains[0]
    assert chain.name == "progressive"
    assert chain.description == "one session many captures"
    assert len(chain.steps) == 2
    assert chain.steps[0].name == "empty"
    assert chain.steps[0].capture is True
    assert chain.steps[1].companion_apps == ["firefox"]
    assert chain.steps[1].actions_after_companions is True


def test_manifest_expands_browser_scene_templates(tmp_path: Path) -> None:
    yaml_path = tmp_path / "chromium.yaml"
    yaml_path.write_text(
        "app_name: chromium-browser\n"
        "binary: chromium-browser\n"
        "atspi_name: Chromium\n"
        "args: [about:blank]\n"
        "interactions:\n"
        "  - name: default\n"
        "    actions:\n"
        "      - type: wait\n"
        "        value: \"2.0\"\n"
        "scene_templates:\n"
        "  - type: chromium_live_windows\n"
        "    name: split_docs\n"
        "    windows:\n"
        "      - url: https://www.python.org/\n"
        "        x: 150\n"
        "        y: 70\n"
        "        width: 820\n"
        "        height: 880\n"
        "      - url: https://docs.python.org/3/\n"
        "        x: 980\n"
        "        y: 85\n"
        "        width: 900\n"
        "        height: 860\n"
        "        post_actions:\n"
        "          - type: key\n"
        "            value: Page_Down\n"
        "            delay: 0.8\n",
        encoding="utf-8",
    )

    manifest = AppManifest.from_yaml(yaml_path)

    assert [interaction.name for interaction in manifest.interactions] == ["default", "split_docs"]
    generated = manifest.interactions[1]
    assert any(action.type == "window_resize_active" for action in generated.actions)
    assert any(action.type == "window_move_active" for action in generated.actions)
    assert any(action.value == "https://docs.python.org/3/" for action in generated.actions)
    assert any(action.value == "Page_Down" for action in generated.actions)


def test_manifest_resolves_the_project_root_token(tmp_path: Path) -> None:
    yaml_path = tmp_path / "sample.yaml"
    yaml_path.write_text(
        "app_name: mousepad\n"
        "binary: ${DESKFORGE_ROOT}/tools/external_apps/bundles/VSCode-linux-x64/code\n"
        "atspi_name: mousepad\n"
        "cwd: ${DESKFORGE_ROOT}/tools/external_apps/bundles/VSCode-linux-x64\n"
        "document_path: ${DESKFORGE_ROOT}/assets/audit/sample_report.pdf\n"
        "document_semantics: ${DESKFORGE_ROOT}/assets/audit/sample_report.structure.json\n"
        "args:\n"
        "  - ${DESKFORGE_ROOT}/assets/audit/sample_notes.md\n"
        "  - file://${DESKFORGE_ROOT}/assets/audit/sample_report.pdf\n"
        "env:\n"
        "  MOZILLA_FIVE_HOME: ${DESKFORGE_ROOT}/tools/extracted/usr/lib64/firefox\n",
        encoding="utf-8",
    )

    manifest = AppManifest.from_yaml(yaml_path)

    assert manifest.binary.startswith(str(PROJECT_ROOT))
    assert manifest.cwd.startswith(str(PROJECT_ROOT))
    assert manifest.document_path.startswith(str(PROJECT_ROOT))
    assert manifest.document_semantics.startswith(str(PROJECT_ROOT))
    assert manifest.args[0].startswith(str(PROJECT_ROOT))
    assert manifest.args[1].startswith(f"file://{PROJECT_ROOT}")
    assert manifest.env["MOZILLA_FIVE_HOME"].startswith(str(PROJECT_ROOT))
    assert "${DESKFORGE_ROOT}" not in str(manifest.args)


def test_manifest_rebases_legacy_checkout_paths(tmp_path: Path, monkeypatch) -> None:
    import deskshot.config as config

    monkeypatch.setattr(config, "LEGACY_PROJECT_ROOTS", (Path("/old/checkout/deskforge"),))
    yaml_path = tmp_path / "sample_legacy.yaml"
    yaml_path.write_text(
        "app_name: firefox\n"
        "binary: firefox-bin\n"
        "atspi_name: firefox\n"
        "document_path: /old/checkout/deskforge/assets/audit/sample_report.pdf\n"
        "args:\n"
        "  - file:///old/checkout/deskforge/assets/audit/sample_report.pdf\n"
        "env:\n"
        "  MOZILLA_FIVE_HOME: /old/checkout/deskforge/tools/extracted/usr/lib64/firefox\n",
        encoding="utf-8",
    )

    manifest = AppManifest.from_yaml(yaml_path)

    assert manifest.document_path.startswith(str(PROJECT_ROOT))
    assert manifest.args[0].startswith(f"file://{PROJECT_ROOT}")
    assert manifest.env["MOZILLA_FIVE_HOME"].startswith(str(PROJECT_ROOT))


def test_editor_manifests_load_with_file_backed_defaults() -> None:
    geany = AppManifest.from_yaml(PROJECT_ROOT / "configs/apps/geany.yaml")
    mousepad = AppManifest.from_yaml(PROJECT_ROOT / "configs/apps/mousepad.yaml")
    thunar = AppManifest.from_yaml(PROJECT_ROOT / "configs/apps/thunar.yaml")
    pluma = AppManifest.from_yaml(PROJECT_ROOT / "configs/apps/pluma.yaml")
    bluefish = AppManifest.from_yaml(PROJECT_ROOT / "configs/apps/bluefish.yaml")
    meld = AppManifest.from_yaml(PROJECT_ROOT / "configs/apps/meld.yaml")
    evolution = AppManifest.from_yaml(PROJECT_ROOT / "configs/apps/evolution.yaml")
    thunderbird = AppManifest.from_yaml(PROJECT_ROOT / "configs/apps/thunderbird.yaml")
    zim = AppManifest.from_yaml(PROJECT_ROOT / "configs/apps/zim.yaml")

    assert geany.args and geany.args[0].endswith("sample_script.py")
    assert mousepad.args and mousepad.args[0].endswith("sample_notes.md")
    assert thunar.args and thunar.args[0].endswith("workspace_probe")
    assert pluma.args and pluma.args[0].endswith("sample_notes.md")
    assert bluefish.args and bluefish.args[0].endswith("sample_page.html")
    assert meld.args and meld.args[0].endswith("meld_left.txt")
    assert meld.args[1].endswith("meld_right.txt")
    assert evolution.binary == "evolution"
    assert thunderbird.binary == "thunderbird-bin"
    assert "--profile" in thunderbird.args
    assert any(arg.startswith("/tmp/session_thunderbird_profile") for arg in thunderbird.args)
    assert zim.binary == "zim"
    assert zim.args == ["--standalone", "/tmp/session_zim_notebook"]
    assert geany.interactions[0].name == "default"
    assert mousepad.interactions[0].name == "default"
    assert {chain.name for chain in mousepad.chains} >= {"editor_progressive"}
    assert {chain.name for chain in thunar.chains} >= {"search_progressive", "workspace_progressive"}
    assert pluma.interactions[1].name == "audit_probe"
    assert bluefish.interactions[1].name == "audit_probe"
    assert meld.interactions[1].name == "audit_probe"
    assert evolution.interactions[1].name == "audit_probe"
    assert [interaction.name for interaction in thunderbird.interactions] == ["default", "local_inbox"]
    assert [interaction.name for interaction in zim.interactions] == ["default", "search", "file_menu"]


def test_native_candidate_manifests_load() -> None:
    gucharmap = AppManifest.from_yaml(PROJECT_ROOT / "configs/apps/gucharmap.yaml")
    xarchiver = AppManifest.from_yaml(PROJECT_ROOT / "configs/apps/xarchiver.yaml")
    xournalpp = AppManifest.from_yaml(PROJECT_ROOT / "configs/apps/xournalpp.yaml")
    file_roller = AppManifest.from_yaml(PROJECT_ROOT / "configs/apps/file-roller.yaml")

    assert gucharmap.binary == "gucharmap"
    assert gucharmap.atspi_name == "gucharmap"
    assert xarchiver.args and xarchiver.args[0].endswith("workspace_probe_archive.zip")
    assert file_roller.args and file_roller.args[0].endswith("workspace_probe_archive.zip")
    assert [interaction.name for interaction in xarchiver.interactions] == ["default", "menu_open", "heuristic_probe"]
    assert [interaction.name for interaction in file_roller.interactions] == ["default", "menu_open", "heuristic_probe"]
    assert {chain.name for chain in xarchiver.chains} >= {"archive_progressive"}
    assert {chain.name for chain in file_roller.chains} >= {"archive_progressive"}
    assert xournalpp.binary == "xournalpp"
    assert xournalpp.atspi_name == "com.github.xournalpp.xournalpp"


def test_worker_candidate_manifests_load() -> None:
    claws = AppManifest.from_yaml(PROJECT_ROOT / "configs/apps/claws_mail.yaml")
    filezilla = AppManifest.from_yaml(PROJECT_ROOT / "configs/apps/filezilla.yaml")
    logs = AppManifest.from_yaml(PROJECT_ROOT / "configs/apps/gnome_logs.yaml")
    homebank = AppManifest.from_yaml(PROJECT_ROOT / "configs/apps/homebank.yaml")
    qalculate = AppManifest.from_yaml(PROJECT_ROOT / "configs/apps/qalculate_gtk.yaml")
    seahorse = AppManifest.from_yaml(PROJECT_ROOT / "configs/apps/seahorse.yaml")
    transmission = AppManifest.from_yaml(PROJECT_ROOT / "configs/apps/transmission_gtk.yaml")

    assert claws.binary == "claws-mail"
    assert [interaction.name for interaction in claws.interactions] == ["default", "account_wizard", "file_menu"]
    assert filezilla.binary == "filezilla"
    assert [interaction.name for interaction in filezilla.interactions] == ["default", "site_manager", "file_menu"]
    assert logs.binary == "gnome-logs"
    assert [interaction.name for interaction in logs.interactions] == ["default", "search", "menu_open"]
    assert homebank.args and homebank.args[0].endswith("usr/share/homebank/datas/example.xhb")
    assert [interaction.name for interaction in homebank.interactions] == ["default", "statistics", "file_menu"]
    assert qalculate.binary == "qalculate-gtk"
    assert [interaction.name for interaction in qalculate.interactions] == ["default", "expression", "menu_open"]
    assert seahorse.binary == "seahorse"
    assert [interaction.name for interaction in seahorse.interactions] == ["default", "search", "app_menu"]
    assert transmission.args and transmission.args[0].endswith("sample_transfer.torrent")
    assert [interaction.name for interaction in transmission.interactions] == ["default", "preferences", "file_menu"]


def test_pdf_manifests_load_with_document_metadata() -> None:
    firefox_pdf = AppManifest.from_yaml(PROJECT_ROOT / "configs/apps/firefox-pdf.yaml")
    chromium_pdf = AppManifest.from_yaml(PROJECT_ROOT / "configs/apps/chromium-pdf.yaml")

    assert firefox_pdf.document_path.endswith("sample_report.pdf")
    assert firefox_pdf.document_semantics.endswith("sample_report.structure.json")
    assert chromium_pdf.document_path.endswith("sample_report.pdf")
    assert chromium_pdf.document_semantics.endswith("sample_report.structure.json")


def test_theme_presets_exposed() -> None:
    presets = list_theme_presets()
    for name in (
        "linux_classic",
        "windows_redmond",
        "macos_tahoe_like",
        "macos_tahoe_glass",
        "quartz_night",
        "quartz_night_nord",
        "ubuntu_like",
    ):
        assert name in presets
        assert "desktop_style" in presets[name]


def test_get_theme_preset() -> None:
    cfg = get_theme_preset("windows_redmond", resolve=False)
    assert cfg.desktop_style == "windows"
    assert cfg.wm_theme == "Win11-Light"

    mac = get_theme_preset("macos_tahoe_like", resolve=False)
    assert mac.desktop_style == "macos"
    assert mac.icon_theme == "WhiteSur-light"
    assert mac.cursor_theme == "WhiteSur-cursors"
    assert mac.panel_variant == "top_slim_dock"
    assert mac.gtk_theme == "MacTahoe-Light-solid"

    mac_glass = get_theme_preset("macos_tahoe_glass", resolve=False)
    assert mac_glass.gtk_theme == "MacTahoe-Light"

    nord = get_theme_preset("quartz_night_nord", resolve=False)
    assert nord.gtk_theme == "MacTahoe-Dark-solid-nord"


def test_resolve_theme_config_fallback(tmp_path: Path) -> None:
    extracted = tmp_path / "extracted"
    themes_dir = extracted / "usr" / "share" / "themes"
    icons_dir = extracted / "usr" / "share" / "icons"

    # Minimal available defaults.
    (themes_dir / "Adwaita" / "gtk-3.0").mkdir(parents=True, exist_ok=True)
    (themes_dir / "Default" / "xfwm4").mkdir(parents=True, exist_ok=True)
    (icons_dir / "Papirus").mkdir(parents=True, exist_ok=True)
    (icons_dir / "Papirus" / "index.theme").write_text("[Icon Theme]\nName=Papirus\n", encoding="utf-8")

    cfg = ThemeConfig(
        gtk_theme="Tahoe-Light",
        icon_theme="Win11",
        wm_theme="Win11-Light",
    )
    resolved = resolve_theme_config(cfg, extracted_dir=extracted)

    assert resolved.gtk_theme == "Adwaita"
    assert resolved.icon_theme == "Papirus"
    assert resolved.wm_theme == "Default"


def test_resolve_theme_config_prefers_style_wallpaper(tmp_path: Path) -> None:
    extracted = tmp_path / "extracted"
    themes_dir = extracted / "usr" / "share" / "themes"
    icons_dir = extracted / "usr" / "share" / "icons"
    backgrounds_dir = extracted / "usr" / "share" / "backgrounds"

    (themes_dir / "Win11-Light" / "gtk-3.0").mkdir(parents=True, exist_ok=True)
    (themes_dir / "Win11-Light" / "xfwm4").mkdir(parents=True, exist_ok=True)
    (icons_dir / "Win11").mkdir(parents=True, exist_ok=True)
    (icons_dir / "Win11" / "index.theme").write_text("[Icon Theme]\nName=Win11\n", encoding="utf-8")
    wallpaper = backgrounds_dir / "deskshot-stylepacks" / "windows-eleven-wallpaper.jpg"
    wallpaper.parent.mkdir(parents=True, exist_ok=True)
    wallpaper.write_bytes(b"fake")

    cfg = get_theme_preset("windows_redmond", resolve=False)
    resolved = resolve_theme_config(cfg, extracted_dir=extracted)

    assert resolved.wallpaper == str(wallpaper)


def test_resolve_theme_config_prefers_mactahoe_for_macos(tmp_path: Path) -> None:
    extracted = tmp_path / "extracted"
    themes_dir = extracted / "usr" / "share" / "themes"
    icons_dir = extracted / "usr" / "share" / "icons"

    for theme_name in ("MacTahoe-Light-solid", "WhiteSur-Light-solid", "MacTahoe-Dark-solid"):
        (themes_dir / theme_name / "gtk-3.0").mkdir(parents=True, exist_ok=True)
        (themes_dir / theme_name / "xfwm4").mkdir(parents=True, exist_ok=True)
    (icons_dir / "WhiteSur-light").mkdir(parents=True, exist_ok=True)
    (icons_dir / "WhiteSur-light" / "index.theme").write_text("[Icon Theme]\nName=WhiteSur-light\n", encoding="utf-8")

    cfg = ThemeConfig(
        gtk_theme="MissingMacTheme",
        icon_theme="MissingMacIcon",
        wm_theme="MissingMacTheme",
        desktop_style="macos",
    )
    resolved = resolve_theme_config(cfg, extracted_dir=extracted)

    assert resolved.gtk_theme == "MacTahoe-Light-solid"
    assert resolved.wm_theme == "MacTahoe-Light-solid"
    assert resolved.icon_theme == "WhiteSur-light"


def test_resolve_firefox_theme_hint_maps_macos_variants() -> None:
    assert resolve_firefox_theme_hint(
        ThemeConfig(gtk_theme="MacTahoe-Light-solid", desktop_style="macos")
    ) == "mactahoe-light"
    assert resolve_firefox_theme_hint(
        ThemeConfig(gtk_theme="MacTahoe-Dark", desktop_style="macos")
    ) == "monterey-darker"
    assert resolve_firefox_theme_hint(
        ThemeConfig(gtk_theme="WhiteSur-Dark-solid-nord", desktop_style="macos")
    ) == "whitesur-nord"
    assert resolve_firefox_theme_hint(
        ThemeConfig(gtk_theme="Adwaita", desktop_style="linux")
    ) == ""


def test_install_style_packs_imports_local_quartz_and_windows_wallpaper(tmp_path: Path, monkeypatch) -> None:
    extracted = tmp_path / "extracted"
    cache_root = tmp_path / "cache"
    quartz_src = tmp_path / "Quartz Night"
    (quartz_src / "gtk-3.0").mkdir(parents=True, exist_ok=True)
    (quartz_src / "gtk-3.0" / "gtk.css").write_text("/* quartz */\n", encoding="utf-8")
    (quartz_src / "index.theme").write_text("[Desktop Entry]\nName=Quartz Night\n", encoding="utf-8")
    windows_wallpaper = tmp_path / "wallpaper.jpg"
    windows_wallpaper.write_bytes(b"jpg")

    monkeypatch.setattr(
        "deskshot.environment.stylepacks._LOCAL_QUARTZ_THEME_DIR",
        quartz_src,
    )
    monkeypatch.setattr(
        "deskshot.environment.stylepacks._LOCAL_WINDOWS_ELEVEN_WALLPAPER",
        windows_wallpaper,
    )
    monkeypatch.setattr(
        "deskshot.environment.stylepacks._resolve_repo",
        lambda *args, **kwargs: None,
    )

    summary = install_style_packs(extracted_dir=extracted, cache_root=cache_root)

    assert "Quartz Night" in summary["themes"]
    assert any("windows-eleven-wallpaper.jpg" in wp for wp in summary["wallpapers"])
    assert (extracted / "usr" / "share" / "themes" / "Quartz Night" / "gtk-3.0" / "gtk.css").is_file()
    assert (
        extracted
        / "usr"
        / "share"
        / "backgrounds"
        / "deskshot-stylepacks"
        / "windows-eleven-wallpaper.jpg"
    ).is_file()


def test_build_macos_panel_assets_renders_dock_background(tmp_path: Path) -> None:
    themes_dir = tmp_path / "themes"
    theme_dir = themes_dir / "MacTahoe-Light"
    (theme_dir / "plank").mkdir(parents=True, exist_ok=True)
    (theme_dir / "plank" / "dock.theme").write_text(
        "\n".join(
            [
                "[PlankTheme]",
                "TopRoundness=23",
                "BottomRoundness=23",
                "LineWidth=0",
                "OuterStrokeColor=0;;0;;0;;0",
                "FillStartColor=209;;209;;209;;150",
                "FillEndColor=209;;209;;209;;150",
                "InnerStrokeColor=210;;210;;210;;50",
                "",
            ]
        ),
        encoding="utf-8",
    )
    assets_dir = tmp_path / "panel-assets"

    created = build_macos_panel_assets(themes_dir, assets_dir)

    assert created
    dock_png = dock_background_path("MacTahoe-Light", assets_dir)
    assert dock_png is not None
    assert dock_png.is_file()


def test_patch_plank_theme_makes_light_theme_more_visible(tmp_path: Path) -> None:
    from deskshot.environment.stylepacks import _patch_plank_theme

    theme_dir = tmp_path / "MacTahoe-Light"
    theme_dir.mkdir(parents=True, exist_ok=True)
    dock_theme = theme_dir / "dock.theme"
    dock_theme.write_text(
        "\n".join(
            [
                "[PlankTheme]",
                "TopRoundness=23",
                "BottomRoundness=23",
                "LineWidth=0",
                "OuterStrokeColor=0;;0;;0;;0",
                "FillStartColor=209;;209;;209;;150",
                "FillEndColor=209;;209;;209;;150",
                "InnerStrokeColor=210;;210;;210;;50",
                "",
                "[PlankDockTheme]",
                "HorizPadding=1",
                "TopPadding=2",
                "BottomPadding=2",
                "ItemPadding=3",
                "",
            ]
        ),
        encoding="utf-8",
    )

    assert _patch_plank_theme(theme_dir) is True
    text = dock_theme.read_text(encoding="utf-8")
    assert "FillStartColor = 244;;244;;248;;210" in text
    assert "FillEndColor = 234;;234;;240;;220" in text
    assert "HorizPadding = 35" in text
    assert "TopPadding = 110" in text
    assert "BottomPadding = 14" in text


def test_write_gtk_settings_ini_copies_gtk4_override(tmp_path: Path) -> None:
    from deskshot.environment.themes import write_gtk_settings_ini

    extracted = tmp_path / "extracted"
    theme_dir = extracted / "usr" / "share" / "themes" / "MacTahoe-Dark-solid"
    (theme_dir / "gtk-3.0").mkdir(parents=True, exist_ok=True)
    (theme_dir / "gtk-4.0").mkdir(parents=True, exist_ok=True)
    (theme_dir / "gtk-4.0" / "gtk.css").write_text("/* gtk4 */\n", encoding="utf-8")

    config_dir = tmp_path / "config"
    settings = write_gtk_settings_ini(
        ThemeConfig(
            gtk_theme="MacTahoe-Dark-solid",
            icon_theme="WhiteSur-dark",
            wm_theme="MacTahoe-Dark-solid",
            cursor_theme="WhiteSur-cursors",
            font="Sans 11",
            desktop_style="macos",
        ),
        config_dir,
        extracted_dir=extracted,
    )

    text = settings.read_text(encoding="utf-8")
    assert "gtk-application-prefer-dark-theme=1" in text
    assert (config_dir / "gtk-4.0" / "gtk.css").read_text(encoding="utf-8") == "/* gtk4 */\n"


def test_write_xfwm4_config_enables_macos_compositing(tmp_path: Path) -> None:
    from deskshot.environment.xfce_config import write_xfwm4_config

    path = write_xfwm4_config(
        ThemeConfig(
            gtk_theme="MacTahoe-Light",
            icon_theme="WhiteSur",
            wm_theme="MacTahoe-Light",
            cursor_theme="WhiteSur-cursors",
            font="Sans 11",
            desktop_style="macos",
        ),
        tmp_path,
    )

    text = path.read_text(encoding="utf-8")
    assert 'use_compositing" type="bool" value="true"' in text
    assert 'frame_opacity" type="int" value="90"' in text
    assert 'show_frame_shadow" type="bool" value="true"' in text


def test_write_xfwm4_config_disables_compositing_for_external_compositor(tmp_path: Path) -> None:
    from deskshot.environment.xfce_config import write_xfwm4_config

    path = write_xfwm4_config(
        ThemeConfig(
            gtk_theme="MacTahoe-Light",
            icon_theme="WhiteSur",
            wm_theme="MacTahoe-Light",
            cursor_theme="WhiteSur-cursors",
            font="Sans 11",
            desktop_style="macos",
        ),
        tmp_path,
        external_compositor=True,
    )

    text = path.read_text(encoding="utf-8")
    assert 'use_compositing" type="bool" value="false"' in text


def test_write_mate_dconf_config_omits_dock_when_using_plank(tmp_path: Path, monkeypatch) -> None:
    from deskshot.environment import mate_config

    theme = ThemeConfig(desktop_style="macos", panel_variant="top_slim_dock")
    monkeypatch.setattr(mate_config, "MATE_PANEL_LAYOUTS_DIR", tmp_path / "layouts")

    path = mate_config.write_mate_dconf_config(theme, tmp_path, use_plank=True)
    text = path.read_text(encoding="utf-8")
    assert "[Toplevel dock]" not in text
    assert "ClockAppletFactory::ClockApplet" in text
    assert path.name == "deskshot-macos-top_slim_dock-plank.layout"
    assert "default-layout='deskshot-macos-top_slim_dock-plank'" in (
        tmp_path / "mate_dconf_dump.ini"
    ).read_text(encoding="utf-8")


def test_write_mate_dconf_config_does_not_mutate_shared_default_layout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from deskshot.environment import mate_config

    layouts_dir = tmp_path / "layouts"
    monkeypatch.setattr(mate_config, "MATE_PANEL_LAYOUTS_DIR", layouts_dir)
    (layouts_dir / "default.layout").parent.mkdir(parents=True, exist_ok=True)
    (layouts_dir / "default.layout").write_text("system default\n", encoding="utf-8")

    theme = ThemeConfig(desktop_style="windows", panel_variant="")
    path = mate_config.write_mate_dconf_config(theme, tmp_path)

    assert path.name == "deskshot-windows-bottom_tall.layout"
    assert "orientation=bottom" in path.read_text(encoding="utf-8")
    assert (layouts_dir / "default.layout").read_text(encoding="utf-8") == "system default\n"
