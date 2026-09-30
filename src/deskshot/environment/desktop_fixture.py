"""Managed desktop-content fixture for reproducible desktop diversity."""

from __future__ import annotations

import logging
import os
import random
import shutil
import subprocess
from pathlib import Path

from deskshot.config import DesktopFixtureConfig, EXTRACTED_DIR
from deskshot.environment.diversity import (
    DESKTOP_ICON_LAYOUTS as _LAYOUT_TEMPLATES,
    resolve_desktop_fixture_config,
)
from deskshot.environment.workspace import stage_workspace
from deskshot.privacy import home_container_for, session_persona

logger = logging.getLogger(__name__)

_CONTENT_PACKS = {
    "mixed_default": {
        "folders": [
            "Projects",
            "Notes",
            "Design",
            "Receipts",
            "Archive",
            "Personal",
            "Reports",
            "Media",
            "Experiments",
            "Invoices",
            "Ideas",
            "Datasets",
            "Screenshots",
            "Travel",
            "Music",
            "Research",
            "Taxes",
            "Clients",
        ],
        "files": [
            ("Roadmap", ".md", "# Milestones\n"),
            ("Budget", ".csv", "month,total\nJan,1200\n"),
            ("Draft", ".txt", "Draft notes\n"),
            ("Meeting", ".txt", "Agenda\n"),
            ("Todo", ".md", "- item\n"),
            ("Contacts", ".csv", "name,email\n"),
            ("Plan", ".pdf", "%PDF-1.4\n"),
            ("Slides", ".pptx", ""),
            ("Archive", ".zip", ""),
            ("Photo", ".png", ""),
            ("Metrics", ".json", "{\n  \"ok\": true\n}\n"),
            ("Readme", ".md", "# Desktop fixture\n"),
            ("Invoice", ".pdf", "%PDF-1.4\n"),
            ("Sketch", ".png", ""),
            ("Results", ".csv", "run,score\n1,0.91\n"),
            ("Config", ".json", "{\n  \"theme\": \"blue\"\n}\n"),
            ("Notes", ".md", "## Notes\n"),
        ],
    },
    "business_ops": {
        "folders": [
            "Clients",
            "Contracts",
            "Forecasts",
            "Invoices",
            "Reports",
            "Receipts",
            "Payroll",
            "Policies",
            "Legal",
            "Sales",
            "Board",
            "Archive",
            "Planning",
            "Vendors",
        ],
        "files": [
            ("Q1_Budget", ".csv", "department,total\nops,12000\n"),
            ("Client_Roster", ".csv", "client,owner\nApex,mina\n"),
            ("Weekly_Status", ".md", "## Weekly status\n"),
            ("Invoice_042", ".pdf", "%PDF-1.4\n"),
            ("Renewal_Dates", ".csv", "client,date\nApex,2026-04-01\n"),
            ("Sales_Deck", ".pptx", ""),
            ("Expense_Report", ".pdf", "%PDF-1.4\n"),
            ("Procurement", ".txt", "Pending vendors\n"),
            ("Forecast_Model", ".json", "{\n  \"forecast\": true\n}\n"),
            ("Hiring_Plan", ".md", "- analyst\n"),
            ("Partner_Notes", ".txt", "Call notes\n"),
            ("Cashflow", ".csv", "month,net\nJan,8500\n"),
            ("Approvals", ".md", "## Approvals\n"),
            ("Renewal_Playbook", ".md", "# Renewal playbook\n"),
            ("Quarterly_Risks", ".txt", "Risk register\n"),
            ("Pricing_Grid", ".csv", "tier,price\npro,99\n"),
        ],
    },
    "engineering_dev": {
        "folders": [
            "src",
            "configs",
            "scripts",
            "tests",
            "tmp",
            "logs",
            "docs",
            "bench",
            "fixtures",
            "build",
            "deploy",
            "notebooks",
            "infra",
            "assets",
        ],
        "files": [
            ("pyproject", ".toml", "[project]\nname = \"desk-app\"\n"),
            ("docker-compose", ".yml", "services:\n  app:\n    image: demo\n"),
            ("service", ".log", "INFO startup complete\n"),
            ("README", ".md", "# Engineering notes\n"),
            ("AGENTS", ".md", "# Workflow\n"),
            ("settings", ".json", "{\n  \"lint\": true\n}\n"),
            ("backlog", ".txt", "fix parser\n"),
            ("metrics", ".json", "{\n  \"latency_ms\": 14\n}\n"),
            ("runbook", ".md", "## Rollback\n"),
            ("sample_notes", ".md", "Verify extraction content.\n"),
            ("release_plan", ".md", "- tag rc1\n"),
            ("Makefile", "", "test:\n\tpytest\n"),
            ("CHANGELOG", ".md", "## 0.1.0\n"),
            ("incident", ".txt", "postmortem draft\n"),
            ("coverage", ".csv", "module,coverage\ncore,0.94\n"),
            ("devcontainer", ".json", "{\n  \"image\": \"python:3.12\"\n}\n"),
        ],
    },
    "research_lab": {
        "folders": [
            "Papers",
            "Datasets",
            "Experiments",
            "Figures",
            "Methods",
            "Results",
            "References",
            "Benchmarks",
            "Drafts",
            "Surveys",
            "Reviews",
            "Fieldwork",
            "Protocols",
            "Supplement",
        ],
        "files": [
            ("literature_review", ".md", "# Prior work\n"),
            ("experiment_plan", ".txt", "Trial A\n"),
            ("results_table", ".csv", "trial,score\nA,0.83\n"),
            ("analysis", ".ipynb", "{\n \"cells\": []\n}\n"),
            ("poster", ".pdf", "%PDF-1.4\n"),
            ("fig_overview", ".png", ""),
            ("citations", ".bib", "@article{demo}\n"),
            ("dataset_schema", ".json", "{\n  \"fields\": 12\n}\n"),
            ("meeting_notes", ".md", "## Lab meeting\n"),
            ("submission_todo", ".txt", "camera-ready\n"),
            ("appendix", ".pdf", "%PDF-1.4\n"),
            ("stat_tests", ".csv", "metric,p\nacc,0.01\n"),
            ("ablation", ".md", "### Ablation\n"),
            ("survey_responses", ".csv", "id,answer\n1,yes\n"),
            ("review_checklist", ".md", "- ethics\n"),
            ("error_analysis", ".txt", "failure clusters\n"),
        ],
    },
    "creative_media": {
        "folders": [
            "Assets",
            "Renders",
            "Exports",
            "Photos",
            "Audio",
            "Video",
            "Storyboards",
            "Moodboards",
            "Posters",
            "Clips",
            "Drafts",
            "Sketches",
            "Deliverables",
            "Sessions",
        ],
        "files": [
            ("shot_list", ".csv", "scene,take\nintro,3\n"),
            ("poster_sketch", ".png", ""),
            ("storyboard", ".pdf", "%PDF-1.4\n"),
            ("mix_notes", ".txt", "compress vocals\n"),
            ("render_queue", ".json", "{\n  \"jobs\": 4\n}\n"),
            ("moodboard", ".png", ""),
            ("deliverables", ".csv", "asset,status\nhero,done\n"),
            ("tracklist", ".txt", "opener\n"),
            ("color_palette", ".json", "{\n  \"accent\": \"orange\"\n}\n"),
            ("promo_slides", ".pptx", ""),
            ("contact_sheet", ".png", ""),
            ("edit_notes", ".md", "## Edit notes\n"),
            ("client_feedback", ".txt", "needs brighter intro\n"),
            ("thumbnail_options", ".png", ""),
            ("caption_copy", ".md", "Short caption.\n"),
            ("release_checklist", ".md", "- encode final\n"),
        ],
    },
    "personal_home": {
        "folders": [
            "Travel",
            "Bills",
            "Recipes",
            "Family",
            "Health",
            "Shopping",
            "Photos",
            "School",
            "Hobbies",
            "Garden",
            "Letters",
            "Moves",
            "Events",
            "Repairs",
        ],
        "files": [
            ("grocery_list", ".txt", "milk\nbread\n"),
            ("travel_plan", ".md", "# Trip ideas\n"),
            ("appointments", ".csv", "date,task\n2026-03-20,dentist\n"),
            ("lease", ".pdf", "%PDF-1.4\n"),
            ("recipes", ".md", "## Soup\n"),
            ("photos_backup", ".zip", ""),
            ("birthday_notes", ".txt", "gift ideas\n"),
            ("expenses", ".csv", "item,amount\nrent,900\n"),
            ("contacts", ".csv", "name,phone\nAda,123\n"),
            ("reminders", ".md", "- renew pass\n"),
            ("home_inventory", ".json", "{\n  \"rooms\": 5\n}\n"),
            ("wishlist", ".txt", "bike light\n"),
            ("school_calendar", ".csv", "date,event\n2026-04-01,holiday\n"),
            ("meal_plan", ".md", "## Week 1\n"),
            ("pet_schedule", ".txt", "vet on Friday\n"),
            ("party_checklist", ".md", "- snacks\n"),
        ],
    },
}

_ICON_GRID_W = 92
_ICON_GRID_H = 104
_ICON_MARGIN = 18


def _write_text_file(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _write_binary_placeholder(path: Path, magic: bytes) -> None:
    path.write_bytes(magic)


def _materialize_file(path: Path, suffix: str, content: str) -> None:
    if suffix in {".md", ".txt", ".csv", ".json"}:
        _write_text_file(path, content)
        return
    if suffix == ".pdf":
        _write_binary_placeholder(path, b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        return
    if suffix == ".png":
        sample = EXTRACTED_DIR / "usr" / "share" / "help" / "de" / "eog" / "figures" / "plugins-all.png"
        if sample.is_file():
            shutil.copyfile(sample, path)
            return
        _write_binary_placeholder(path, b"\x89PNG\r\n\x1a\n")
        return
    if suffix == ".zip":
        _write_binary_placeholder(path, b"PK\x03\x04")
        return
    if suffix == ".pptx":
        _write_binary_placeholder(path, b"PK\x03\x04")
        return
    _write_text_file(path, content)


def _panel_margins(panel_variant: str) -> tuple[int, int, int, int]:
    variant = (panel_variant or "").strip().lower()
    if variant.startswith("left"):
        return 24, 24, (58 if "slim" in variant else 64), 24
    if "dock" in variant:
        return (44 if "slim" in variant else 50), (84 if "slim" in variant else 96), 18, 18
    if variant.startswith("top"):
        return (56 if "tall" in variant else 44 if "slim" in variant else 50), 24, 18, 18
    if variant.startswith("bottom"):
        return 24, (64 if "tall" in variant else 46 if "slim" in variant else 56), 18, 18
    return 32, 32, 18, 18


def _clamp(val: int, low: int, high: int) -> int:
    return max(low, min(high, val))


def compute_icon_positions(
    count: int,
    *,
    seed: int,
    display_width: int,
    display_height: int,
    panel_variant: str = "",
    layout_template: str = "",
) -> dict[str, object]:
    """Compute deterministic pseudo-random clustered icon positions."""
    top_margin, bottom_margin, left_margin, right_margin = _panel_margins(panel_variant)
    usable_left = max(_ICON_MARGIN, left_margin)
    usable_right = max(usable_left, display_width - max(_ICON_MARGIN, right_margin) - _ICON_GRID_W)
    usable_top = top_margin
    usable_bottom = max(usable_top, display_height - bottom_margin - _ICON_GRID_H)
    usable_w = max(_ICON_GRID_W, usable_right - usable_left)
    usable_h = max(_ICON_GRID_H, usable_bottom - usable_top)

    rng = random.Random(seed)
    chosen_layout = (layout_template or "").strip().lower()
    if chosen_layout and chosen_layout in _LAYOUT_TEMPLATES:
        layout_name = chosen_layout
        template = _LAYOUT_TEMPLATES[chosen_layout]
    else:
        layout_name = sorted(_LAYOUT_TEMPLATES)[rng.randrange(len(_LAYOUT_TEMPLATES))]
        template = _LAYOUT_TEMPLATES[layout_name]
    cluster_count = min(len(template), max(1, count))
    anchors = template[:cluster_count]

    weights = [rng.randint(1, 4) for _ in range(cluster_count)]
    total_weight = sum(weights)
    counts = [max(1, round(count * w / total_weight)) for w in weights]
    while sum(counts) > count:
        idx = max(range(cluster_count), key=lambda i: counts[i])
        if counts[idx] > 1:
            counts[idx] -= 1
        else:
            break
    while sum(counts) < count:
        idx = rng.randrange(cluster_count)
        counts[idx] += 1

    positions: list[tuple[int, int]] = []
    for (x_ratio, y_ratio, cols), cluster_items in zip(anchors, counts):
        base_x = usable_left + int(usable_w * x_ratio)
        base_y = usable_top + int(usable_h * y_ratio)
        for item_idx in range(cluster_items):
            col = item_idx % cols
            row = item_idx // cols
            jitter_x = rng.randint(-8, 8)
            jitter_y = rng.randint(-6, 10)
            x = _clamp(
                base_x + col * _ICON_GRID_W + jitter_x,
                usable_left,
                usable_right,
            )
            y = _clamp(
                base_y + row * _ICON_GRID_H + jitter_y,
                usable_top,
                usable_bottom,
            )
            positions.append((x, y))

    return {
        "layout": layout_name,
        "positions": positions[:count],
    }


def _get_content_pack(name: str) -> dict[str, list]:
    key = (name or "mixed_default").strip().lower()
    return _CONTENT_PACKS.get(key, _CONTENT_PACKS["mixed_default"])


def _set_gio_metadata(path: Path, attribute: str, value: str, *, value_type: str) -> bool:
    gio = shutil.which("gio")
    if not gio:
        return False
    result = subprocess.run(
        [gio, "set", "-t", value_type, str(path), attribute, value],
        env=os.environ,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.debug("gio set failed for %s %s=%s: %s", path, attribute, value, result.stderr.strip())
        return False
    return True


def _apply_caja_icon_layout(
    desktop_dir: Path,
    desktop_items: list[Path],
    *,
    seed: int,
    display_width: int,
    display_height: int,
    panel_variant: str = "",
    layout_template: str = "",
) -> dict[str, object]:
    """Apply deterministic Caja icon positions via GIO metadata."""
    layout = compute_icon_positions(
        len(desktop_items),
        seed=seed,
        display_width=display_width,
        display_height=display_height,
        panel_variant=panel_variant,
        layout_template=layout_template,
    )
    positions = layout["positions"]

    _set_gio_metadata(desktop_dir, "metadata::caja-icon-view-auto-layout", "false", value_type="boolean")
    _set_gio_metadata(desktop_dir, "metadata::caja-icon-view-keep-aligned", "false", value_type="boolean")
    _set_gio_metadata(desktop_dir, "metadata::caja-icon-view-sort-by", "manually", value_type="string")

    applied = 0
    for item, (x, y) in zip(desktop_items, positions):
        if _set_gio_metadata(
            item,
            "metadata::caja-icon-position",
            f"{x},{y}",
            value_type="string",
        ):
            applied += 1

    return {
        "layout": layout["layout"],
        "num_items": len(desktop_items),
        "num_positions_applied": applied,
    }


def materialize_desktop_fixture(
    *,
    root: Path,
    xdg_config_home: Path,
    fixture: DesktopFixtureConfig,
    home_root: Path | None = None,
    desktop_style: str = "linux",
    display_width: int = 1920,
    display_height: int = 1080,
    panel_variant: str = "",
) -> dict[str, Path]:
    """Create a session-local HOME/Desktop tree with varied files and folders."""
    resolved = resolve_desktop_fixture_config(fixture)

    # The home directory is named for a fictional persona rather than for the
    # account running the capture. Editors draw the full path in their title
    # bars, so this string is on screen in most captures.
    persona = session_persona(resolved.seed)
    # Kept out of the XDG tree on purpose: the home path is drawn in title bars,
    # and `.../xdg/home/mira` reads like a scaffold rather than a home.
    home_base = (home_root or root) / home_container_for(desktop_style, resolved.seed)
    home_dir = home_base / persona.username
    desktop_dir = home_dir / "Desktop"
    downloads_dir = home_dir / "Downloads"
    documents_dir = home_dir / "Documents"

    # Remove the whole home root, not just this persona's directory: a session
    # reused with a different seed would otherwise leave the previous persona's
    # home sitting next to the current one.
    if home_base.exists():
        shutil.rmtree(home_base)
    home_dir.mkdir(parents=True, exist_ok=True)
    desktop_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir.mkdir(parents=True, exist_ok=True)
    documents_dir.mkdir(parents=True, exist_ok=True)

    if resolved.enabled:
        rng = random.Random(resolved.seed)
        items: list[tuple[str, str, str, str]] = []
        desktop_items: list[Path] = []
        content_pack = _get_content_pack(resolved.content_pack)
        folder_pool = list(content_pack["folders"])
        file_pool = list(content_pack["files"])
        folder_names = rng.sample(folder_pool, k=min(resolved.num_folders, len(folder_pool)))
        for name in folder_names:
            items.append(("folder", name, "", f"{name} contents\n"))

        file_templates = rng.sample(file_pool, k=min(resolved.num_files, len(file_pool)))
        for stem, suffix, content in file_templates:
            items.append(("file", stem, suffix, content))

        rng.shuffle(items)

        # No numeric prefix. It used to be there to shuffle the alphabetical
        # order Caja falls back to, but the icons are placed explicitly now, and
        # `09_Reports` is not a name anyone's desktop has - it is drawn on
        # screen in every capture, so it taught the corpus a fiction.
        used: set[str] = set()
        for kind, name, suffix, content in items:
            leaf = f"{name}{suffix}"
            if leaf in used:
                continue
            used.add(leaf)
            if kind == "folder":
                folder = desktop_dir / leaf
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "README.txt").write_text(content, encoding="utf-8")
                desktop_items.append(folder)
            else:
                file_path = desktop_dir / leaf
                _materialize_file(file_path, suffix, content)
                desktop_items.append(file_path)

        layout_meta = _apply_caja_icon_layout(
            desktop_dir,
            desktop_items,
            seed=resolved.seed,
            display_width=display_width,
            display_height=display_height,
            panel_variant=panel_variant,
            layout_template=resolved.layout_template,
        )
    else:
        content_pack = _get_content_pack(resolved.content_pack)
        layout_meta = {"layout": "disabled", "num_items": 0, "num_positions_applied": 0}

    workspace_map = stage_workspace(home_dir)

    user_dirs = xdg_config_home / "user-dirs.dirs"
    user_dirs.parent.mkdir(parents=True, exist_ok=True)
    user_dirs.write_text(
        "\n".join(
            [
                f'XDG_DESKTOP_DIR="{desktop_dir}"',
                f'XDG_DOWNLOAD_DIR="{downloads_dir}"',
                f'XDG_DOCUMENTS_DIR="{documents_dir}"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    return {
        "home_dir": home_dir,
        "desktop_dir": desktop_dir,
        "downloads_dir": downloads_dir,
        "documents_dir": documents_dir,
        "user_dirs": user_dirs,
        "layout": layout_meta["layout"],
        "num_positions_applied": layout_meta["num_positions_applied"],
        "num_items": layout_meta["num_items"],
        "content_pack": resolved.content_pack,
        "num_content_folders_available": len(content_pack["folders"]),
        "num_content_files_available": len(content_pack["files"]),
        "persona_username": persona.username,
        "persona_full_name": persona.full_name,
        "workspace_map": workspace_map,
    }
