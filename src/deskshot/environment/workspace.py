"""The documents a capture opens, staged where a real user would keep them.

Every editor in the pool is launched with an absolute path into this checkout:

    mousepad <checkout>/assets/audit/sample_notes.md

Mousepad, Bluefish and Pluma all draw that path in their title bar, so the
account name and the institution's filesystem layout end up in the pixels of
most captures - 83 of 95 in the runs measured. Pixels cannot be redacted after
the fact, so the path has to be different *before* the screenshot is taken.

Two things happen here, and they are separable:

**Location.** The tree is copied into the session's own HOME under the
directories a person actually uses, so the title bar draws
`~/Documents/meeting_notes.md` (or the session home's absolute path, for the
apps that do not abbreviate). Nothing outside the session is referenced.

**Naming.** `sample_notes.md`, `workspace_probe`, `vscode_probe` are the names
of test fixtures, and they are drawn on screen in file managers, title bars and
editor tabs. A corpus that teaches a model what a desktop looks like should not
teach it that every document is called `sample_*`. The published name is
therefore chosen here, once, rather than by renaming files in the repository -
so the fixture keeps the name the tests use and the screen shows the other one.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from deskshot.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

#: The tree that gets staged. Everything an app opens lives under it.
AUDIT_ASSETS_DIR = PROJECT_ROOT / "assets" / "audit"

#: Set on the session environment so `launch_app` can rewrite manifest paths
#: without being handed the session object.
WORKSPACE_MAP_ENV = "DESKSHOT_WORKSPACE_MAP"

#: Documents that live outside `assets/audit` but are still opened by an app and
#: therefore drawn. Keyed by path relative to the project root.
EXTRA_STAGED: Dict[str, tuple] = {
    "tools/extracted/usr/share/homebank/datas/example.xhb": (
        "Documents", "household_accounts.xhb",
    ),
    "tools/extracted/usr/share/backgrounds/evergreen-dark1.jpg": (
        "Pictures", "evergreen.jpg",
    ),
}

#: asset name -> (user directory, published name). Anything not listed keeps its
#: own name and lands in Documents, so adding an asset needs no change here -
#: only a rename does.
PUBLISHED_AS: Dict[str, tuple] = {
    "sample_notes.md": ("Documents", "meeting_notes.md"),
    "sample_page.html": ("Documents", "landing_page.html"),
    "sample_report.pdf": ("Documents", "quarterly_report.pdf"),
    "sample_report.structure.json": ("Documents", "quarterly_report.structure.json"),
    "sample_script.py": ("Documents", "pipeline_utils.py"),
    "browser_dense_probe.html": ("Documents", "news_digest.html"),
    "homebank_sample.csv": ("Documents", "transactions.csv"),
    "meld_left.txt": ("Documents", "config_v1.txt"),
    "meld_right.txt": ("Documents", "config_v2.txt"),
    "workspace_probe": ("Documents", "project_files"),
    "vscode_probe": ("Documents", "analytics_service"),
    "zim_notebook": ("Documents", "notebook"),
    "file_roller_sample_src": ("Documents", "photo_originals"),
    "file_roller_sample.zip": ("Downloads", "photos_backup.zip"),
    "workspace_probe_archive.zip": ("Downloads", "project_files.zip"),
    "sample_transfer.torrent": ("Downloads", "dataset_shard.torrent"),
}

#: Where an unlisted asset goes, by suffix.
_BY_SUFFIX: Dict[str, str] = {
    ".zip": "Downloads",
    ".tar": "Downloads",
    ".gz": "Downloads",
    ".torrent": "Downloads",
    ".png": "Pictures",
    ".jpg": "Pictures",
    ".jpeg": "Pictures",
}

#: Words the staged copies must not contain, and what to say instead. The files
#: are drawn open in an editor, so their *contents* are on screen too.
_CONTENT_SUBSTITUTIONS = (
    ("DeskShot Notes", "Project Notes"),
    ("DeskShot", "Northwind"),
    ("deskshot", "northwind"),
)

_TEXT_SUFFIXES = frozenset({".md", ".txt", ".html", ".htm", ".py", ".json", ".csv", ".yaml", ".yml", ".zim"})


def _destination_for(name: str) -> tuple:
    if name in PUBLISHED_AS:
        return PUBLISHED_AS[name]
    return _BY_SUFFIX.get(Path(name).suffix.lower(), "Documents"), name


def _rewrite_text_content(path: Path) -> None:
    """Strip the project's own name from a staged file that will be read on screen."""
    if path.suffix.lower() not in _TEXT_SUFFIXES:
        return
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    updated = text
    for needle, replacement in _CONTENT_SUBSTITUTIONS:
        updated = updated.replace(needle, replacement)
    if updated != text:
        path.write_text(updated, encoding="utf-8")


def stage_workspace(home_dir: Path) -> Dict[str, str]:
    """Copy the audit assets into a session HOME and return source -> staged.

    The map is keyed by absolute source path so a manifest argument can be
    rewritten without knowing anything about the naming scheme.
    """
    mapping: Dict[str, str] = {}
    if not AUDIT_ASSETS_DIR.is_dir():
        logger.warning("No audit assets to stage at %s", AUDIT_ASSETS_DIR)
        return mapping

    for entry in sorted(AUDIT_ASSETS_DIR.iterdir()):
        subdir, published = _destination_for(entry.name)
        target_dir = home_dir / subdir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / published
        try:
            if entry.is_dir():
                shutil.copytree(entry, target, dirs_exist_ok=True)
                for child in target.rglob("*"):
                    if child.is_file():
                        _rewrite_text_content(child)
            else:
                shutil.copyfile(entry, target)
                _rewrite_text_content(target)
        except OSError:
            logger.warning("Could not stage %s", entry, exc_info=True)
            continue
        mapping[str(entry)] = str(target)

    for relative, (subdir, published) in EXTRA_STAGED.items():
        source = PROJECT_ROOT / relative
        if not source.is_file():
            continue
        target_dir = home_dir / subdir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / published
        try:
            shutil.copyfile(source, target)
            _rewrite_text_content(target)
        except OSError:
            logger.warning("Could not stage %s", source, exc_info=True)
            continue
        mapping[str(source)] = str(target)

    # `{home}` lets a manifest name the session home without knowing where it
    # is. Baobab is launched on a directory to scan, and pointing it at the
    # checkout put the project's whole tree - `.deskshot` included - on screen.
    mapping["{home}"] = str(home_dir)

    return mapping


def install_workspace_map(mapping: Dict[str, str]) -> str:
    """Serialize the map for the app environment. Returns the value to set."""
    return json.dumps(mapping, sort_keys=True)


def _load_map() -> Dict[str, str]:
    raw = os.environ.get(WORKSPACE_MAP_ENV, "")
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def rewrite_asset_path(value: str) -> str:
    """Point one manifest path or file:// URL at the staged copy.

    Falls through unchanged when nothing is staged, so a bare `deskshot run`
    outside a session behaves exactly as before.
    """
    if not value:
        return value
    mapping = _load_map()
    if not mapping:
        return value
    home = mapping.get("{home}")
    if home and "{home}" in value:
        return value.replace("{home}", home)
    prefix = ""
    path = value
    for scheme in ("file://",):
        if value.startswith(scheme):
            prefix, path = scheme, value[len(scheme):]
            break
    # Longest source first, so `.../vscode_probe/main.py` resolves through
    # `vscode_probe` rather than stopping at the assets directory.
    for source in sorted(mapping, key=len, reverse=True):
        if path == source:
            return prefix + mapping[source]
        if path.startswith(source + "/"):
            return prefix + mapping[source] + path[len(source):]
    return value


def rewrite_asset_paths(values: List[str]) -> List[str]:
    """`rewrite_asset_path` over a manifest's argument list."""
    return [rewrite_asset_path(v) for v in values]
