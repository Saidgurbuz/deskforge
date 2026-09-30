"""Launch desktop applications and wait for AT-SPI readiness."""

from __future__ import annotations

import logging
import os
import signal
import shutil
import subprocess
import tempfile
import time
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi

from deskshot.config import (
    AppManifest,
    EXTRACTED_BIN,
    EXTRACTED_DIR,
    TOOLS_DIR,
    bin_fix_dir,
    runtime_tmp_dir,
)

from deskshot.environment.workspace import rewrite_asset_path, rewrite_asset_paths

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BinaryResolution:
    binary_name: str
    path: str
    source: str
    compute_safe: bool


_BINARY_CACHE: Dict[str, BinaryResolution] = {}
_MANAGED_RUNTIME_DIR_FLAGS = {"--profile", "--user-data-dir"}
_EXTERNAL_BIN = TOOLS_DIR / "external_apps" / "bin"
_FIREFOX_THEME_ROOT = EXTRACTED_DIR / "usr" / "share" / "deskshot" / "firefox-themes"
_ZIM_NOTEBOOK_FIXTURE = Path(__file__).resolve().parents[3] / "assets" / "audit" / "zim_notebook"
#: Zim shows this directory's path in its own UI, so it does not name the project.
_ZIM_NOTEBOOK_DIR_NAME = "session_zim_notebook"
_FIREFOX_PROFILE_PREFS = [
    'user_pref("app.normandy.first_run", false);',
    'user_pref("browser.aboutConfig.showWarning", false);',
    'user_pref("browser.aboutwelcome.enabled", false);',
    'user_pref("browser.bookmarks.restore_default_bookmarks", false);',
    'user_pref("browser.newtabpage.activity-stream.feeds.section.topstories", false);',
    'user_pref("browser.newtabpage.activity-stream.feeds.topsites", false);',
    'user_pref("browser.newtabpage.activity-stream.showSponsoredTopSites", false);',
    'user_pref("browser.rights.3.shown", true);',
    'user_pref("browser.shell.checkDefaultBrowser", false);',
    'user_pref("browser.startup.firstrunSkipsHomepage", true);',
    'user_pref("browser.startup.homepage_override.mstone", "ignore");',
    'user_pref("browser.startup.page", 0);',
    'user_pref("browser.tabs.drawInTitlebar", true);',
    'user_pref("browser.toolbars.bookmarks.visibility", "never");',
    'user_pref("datareporting.healthreport.uploadEnabled", false);',
    'user_pref("layers.acceleration.force-enabled", true);',
    'user_pref("mozilla.widget.use-argb-visuals", true);',
    'user_pref("datareporting.policy.dataSubmissionPolicyAccepted", true);',
    'user_pref("startup.homepage_welcome_url", "");',
    'user_pref("startup.homepage_welcome_url.additional", "");',
    'user_pref("toolkit.legacyUserProfileCustomizations.stylesheets", true);',
    'user_pref("toolkit.telemetry.enabled", false);',
    'user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);',
    'user_pref("trailhead.firstrun.didSeeAboutWelcome", true);',
    'user_pref("browser.uidensity", 0);',
    'user_pref("svg.context-properties.content.enabled", true);',
    'user_pref("widget.gtk.rounded-bottom-corners.enabled", true);',
]
_VSCODE_USER_SETTINGS = {
    "extensions.autoCheckUpdates": False,
    "extensions.autoUpdate": False,
    "git.openRepositoryInParentFolders": "never",
    "security.workspace.trust.enabled": False,
    "telemetry.feedback.enabled": False,
    "telemetry.telemetryLevel": "off",
    "update.mode": "none",
    "workbench.startupEditor": "none",
}
_THUNDERBIRD_PROFILE_PREFS = [
    'user_pref("app.update.auto", false);',
    'user_pref("app.update.enabled", false);',
    'user_pref("datareporting.healthreport.uploadEnabled", false);',
    'user_pref("datareporting.policy.dataSubmissionPolicyAccepted", true);',
    'user_pref("mail.provider.enabled", false);',
    'user_pref("mail.rights.version", 1);',
    'user_pref("mail.shell.checkDefaultClient", false);',
    'user_pref("mail.spotlight.firstRunDone", true);',
    'user_pref("mailnews.start_page.enabled", false);',
    'user_pref("mailnews.start_page.override_url", "");',
    'user_pref("mailnews.start_page.url", "about:blank");',
    'user_pref("messenger.startup.action", 0);',
    'user_pref("toolkit.telemetry.enabled", false);',
    'user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);',
    # Thunderbird 128's account-setup dialog pre-fills "Your full name" from
    # the OS, and it reads the passwd GECOS field - which on this host is a work
    # email address and an employee serial number, not a name. It appeared in
    # 5 of 24 captures. The profile already defines an account; these keep the
    # setup dialog from opening on top of it anyway.
    'user_pref("mail.provider.suppress_dialog_on_startup", true);',
    'user_pref("mail.import.in_new_account", false);',
    'user_pref("mail.accounthub.show_on_startup", false);',
    'user_pref("mail.accountwizard.lastspecifiedemail", "");',
    'user_pref("mail.identity.default.fullName", "Alex Rivera");',
    'user_pref("mail.identity.default.useremail", "alex.rivera@example.test");',
]
_THUNDERBIRD_SAMPLE_INBOX = """From alex.rivera@example.test Mon May 04 09:00:00 2026
Subject: Q2 launch checklist
From: Alex Rivera <alex.rivera@example.test>
To: Product Ops <product-ops@example.test>
Date: Mon, 04 May 2026 09:00:00 +0000
Message-ID: <launch-checklist@example.test>

Hi team,

Please review the Q2 launch checklist before the 2 PM planning sync.

- Confirm onboarding metrics dashboard
- Check desktop annotation export samples
- Update partner status notes

Thanks,
Alex

From morgan.lee@example.test Mon May 04 10:15:00 2026
Subject: Design review notes
From: Morgan Lee <morgan.lee@example.test>
To: Alex Rivera <alex.rivera@example.test>
Date: Mon, 04 May 2026 10:15:00 +0000
Message-ID: <design-review@example.test>

The review board approved the layout changes with two follow-ups:

1. Keep the message list compact for dense inbox screenshots.
2. Add a calendar invite for the accessibility audit.

Morgan
"""


def _candidate_binary_paths(binary_name: str) -> List[Path]:
    """Fast deterministic candidate paths (no full-tree rglob)."""
    return [
        bin_fix_dir() / binary_name,
        _EXTERNAL_BIN / binary_name,
        EXTRACTED_DIR / "usr" / "lib64" / "firefox" / binary_name,
        EXTRACTED_DIR / "usr" / "lib64" / "thunderbird" / binary_name,
        EXTRACTED_DIR / "usr" / "lib64" / "libreoffice" / "program" / binary_name,
        EXTRACTED_DIR / "usr" / "lib" / "thunderbird" / binary_name,
        EXTRACTED_DIR / "usr" / "lib" / "libreoffice" / "program" / binary_name,
        EXTRACTED_BIN / binary_name,
        EXTRACTED_DIR / "usr" / "libexec" / binary_name,
        EXTRACTED_DIR / "usr" / "lib64" / "at-spi2-core" / binary_name,
        EXTRACTED_DIR / "usr" / "lib" / "at-spi2-core" / binary_name,
        EXTRACTED_DIR / "usr" / "lib64" / binary_name,
        EXTRACTED_DIR / "usr" / "lib" / binary_name,
    ]


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _binary_source(path: Path) -> tuple[str, bool]:
    resolved = path.resolve()
    fix_dir = bin_fix_dir()
    if _path_is_under(resolved, fix_dir):
        return "bin_fix_dir", True
    if _path_is_under(resolved, _EXTERNAL_BIN) or _path_is_under(resolved, TOOLS_DIR / "external_apps"):
        return "external_apps", True
    if _path_is_under(resolved, EXTRACTED_DIR):
        return "extracted_tools", True
    if _path_is_under(resolved, TOOLS_DIR):
        return "tools", True
    return "direct_path", False


def resolve_binary(binary_name: str) -> BinaryResolution:
    """Find a binary and report whether its path is portable to compute nodes."""
    if binary_name in _BINARY_CACHE:
        return _BINARY_CACHE[binary_name]

    requested = Path(binary_name).expanduser()
    if (requested.is_absolute() or "/" in binary_name) and requested.is_file() and os.access(str(requested), os.X_OK):
        source, compute_safe = _binary_source(requested)
        resolution = BinaryResolution(binary_name, str(requested.resolve()), source, compute_safe)
        _BINARY_CACHE[binary_name] = resolution
        return resolution

    for candidate in _candidate_binary_paths(binary_name):
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            source, compute_safe = _binary_source(candidate)
            resolution = BinaryResolution(binary_name, str(candidate.resolve()), source, compute_safe)
            _BINARY_CACHE[binary_name] = resolution
            return resolution

    system = shutil.which(binary_name)
    if system:
        resolution = BinaryResolution(binary_name, str(Path(system).resolve()), "system_path", False)
        _BINARY_CACHE[binary_name] = resolution
        return resolution

    raise FileNotFoundError(f"Binary not found: {binary_name}")


def find_binary(binary_name: str) -> str:
    """Find a binary in extracted tools or system PATH."""
    return resolve_binary(binary_name).path


def _prepend_env_paths(env: Dict[str, str], key: str, paths: List[Path]) -> None:
    """Prepend existing directories to a colon-separated env var."""
    current = [p for p in env.get(key, "").split(":") if p]
    additions: List[str] = []
    for path in paths:
        p = str(path)
        if not path.is_dir():
            continue
        if p in additions or p in current:
            continue
        additions.append(p)
    if additions or current:
        env[key] = ":".join(additions + current)


def _candidate_private_runtime_dirs(binary_path: Path, manifest: AppManifest) -> Dict[str, List[Path]]:
    """Return app-specific library and typelib dirs for extracted binaries."""
    lib_candidates: List[Path] = []
    typelib_candidates: List[Path] = []
    python_candidates: List[Path] = []
    extracted_lib_roots = [EXTRACTED_DIR / "usr" / "lib64", EXTRACTED_DIR / "usr" / "lib"]
    local_roots = [EXTRACTED_DIR.resolve(), TOOLS_DIR.resolve()]

    names = {
        binary_path.stem,
        binary_path.name,
        Path(manifest.binary).stem,
        Path(manifest.binary).name,
    }

    if binary_path.is_absolute():
        parent = binary_path.resolve().parent
        if parent != EXTRACTED_BIN and any(str(parent).startswith(str(root)) for root in local_roots):
            lib_candidates.append(parent)
            typelib_candidates.append(parent / "girepository-1.0")

    for root in extracted_lib_roots:
        for name in sorted(names):
            candidate = root / name
            lib_candidates.append(candidate)
            typelib_candidates.append(candidate / "girepository-1.0")
        python_candidates.extend(sorted(root.glob("python*/site-packages")))

    # Always include the main extracted typelib root if present.
    for root in extracted_lib_roots:
        typelib_candidates.append(root / "girepository-1.0")

    return {
        "LD_LIBRARY_PATH": lib_candidates,
        "GI_TYPELIB_PATH": typelib_candidates,
        "PYTHONPATH": python_candidates,
    }


def _common_runtime_dirs() -> Dict[str, List[Path]]:
    """Return shared extracted runtime dirs needed across many apps."""
    lib_roots = [EXTRACTED_DIR / "usr" / "lib64", EXTRACTED_DIR / "usr" / "lib"]
    ld_candidates: List[Path] = []
    gi_candidates: List[Path] = []

    for root in lib_roots:
        ld_candidates.append(root)
        ld_candidates.append(root / "samba")
        ld_candidates.append(root / "samba" / "wbclient")
        gi_candidates.append(root / "girepository-1.0")

    return {"LD_LIBRARY_PATH": ld_candidates, "GI_TYPELIB_PATH": gi_candidates}


def _override_runtime_dirs(manifest: AppManifest) -> Dict[str, List[Path]]:
    """Return app-specific override dirs built under tools/runtime_overrides."""
    override_root = TOOLS_DIR / "runtime_overrides"
    names = {
        manifest.app_name,
        Path(manifest.binary).name,
        Path(manifest.binary).stem,
    }

    ld_candidates: List[Path] = []
    gi_candidates: List[Path] = []
    for name in sorted(n for n in names if n):
        root = override_root / name
        ld_candidates.append(root / "lib64")
        ld_candidates.append(root / "lib")
        gi_candidates.append(root / "lib64" / "girepository-1.0")
        gi_candidates.append(root / "lib" / "girepository-1.0")

    return {"LD_LIBRARY_PATH": ld_candidates, "GI_TYPELIB_PATH": gi_candidates}


#: Environment variables that carry lists of filesystem paths. An app that
#: enumerates any of these can print the entries, and the launching account's
#: entries name the launching account.
_PATH_LIST_VARS = ("PATH", "PYTHONPATH", "LD_LIBRARY_PATH", "XDG_DATA_DIRS",
                   "XDG_CONFIG_DIRS", "GI_TYPELIB_PATH", "MANPATH")


def _scrub_identifying_paths(env: Dict[str, str]) -> Dict[str, str]:
    """Remove the launching account's own directories from the app environment.

    VS Code listed `/u/<account>/android-sdk/platform-tools` and
    `~/.vscode-server/...` in its command palette - not because anything
    pointed it there, but because it inherited `PATH` from the account running
    the capture and showed what was on it. Every de-identification measure
    elsewhere is undone by that: the session has a persona home, but the
    environment still names the real one.

    Scrubbing the environment fixes the whole class at once, rather than
    discovering one app at a time which parts of it they choose to display.
    Entries under the real home directory or the project checkout go; everything
    else stays, so the extracted toolchain still resolves.
    """
    from deskshot.privacy import identifying_path_prefixes

    prefixes = identifying_path_prefixes()
    if not prefixes:
        return env

    def _keep(entry: str) -> bool:
        entry = entry.strip()
        if not entry:
            return False
        return not any(
            entry == prefix or entry.startswith(prefix.rstrip("/") + "/")
            for prefix in prefixes
        )

    for name in _PATH_LIST_VARS:
        value = env.get(name)
        if not value:
            continue
        kept = [part for part in value.split(os.pathsep) if _keep(part)]
        if kept:
            env[name] = os.pathsep.join(kept)
        else:
            env.pop(name, None)
    return env


def build_app_env(binary: str, manifest: AppManifest) -> Dict[str, str]:
    """Build the runtime environment for launching an extracted app."""
    env = dict(os.environ)
    env.update(manifest.env)

    binary_path = Path(binary)
    common = _common_runtime_dirs()
    _prepend_env_paths(env, "LD_LIBRARY_PATH", common["LD_LIBRARY_PATH"])
    _prepend_env_paths(env, "GI_TYPELIB_PATH", common["GI_TYPELIB_PATH"])
    extras = _candidate_private_runtime_dirs(binary_path, manifest)
    _prepend_env_paths(env, "LD_LIBRARY_PATH", extras["LD_LIBRARY_PATH"])
    _prepend_env_paths(env, "GI_TYPELIB_PATH", extras["GI_TYPELIB_PATH"])
    _prepend_env_paths(env, "PYTHONPATH", extras["PYTHONPATH"])
    overrides = _override_runtime_dirs(manifest)
    _prepend_env_paths(env, "LD_LIBRARY_PATH", overrides["LD_LIBRARY_PATH"])
    _prepend_env_paths(env, "GI_TYPELIB_PATH", overrides["GI_TYPELIB_PATH"])
    if manifest.app_name.startswith("firefox") and binary_path.name.startswith("firefox"):
        runtime_root = binary_path.parent
        if runtime_root.is_dir():
            env["MOZILLA_FIVE_HOME"] = str(runtime_root)
    # Last, so it also catches anything the steps above added.
    return _scrub_identifying_paths(env)


def _app_log_path(app_name: str) -> Path:
    """Where to capture one app launch's stdout/stderr."""
    root = Path(os.environ.get("DESKSHOT_APP_LOG_DIR") or runtime_tmp_dir())
    root.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in app_name)
    return root / f"deskshot_app_{safe}_{os.getpid()}.log"


def _format_app_log_tail(path: Path, max_lines: int = 12) -> str:
    """Render the tail of an app log for inclusion in a launch error."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if not text:
        return ""
    lines = text.splitlines()[-max_lines:]
    body = "\n".join(f"  {line}" for line in lines)
    return f"\n--- {path.name} (last {len(lines)} lines) ---\n{body}"


def _is_managed_runtime_dir(path_str: str) -> bool:
    """Return True for launcher-owned runtime dirs that are safe to recreate."""
    path = Path(path_str).expanduser()
    # `session_` is the neutral spelling: these paths are drawn in app UI
    # (Zim shows its notebook path), so they must not name the project.
    return path.is_absolute() and (
        str(path).startswith("/tmp/deskshot_") or str(path).startswith("/tmp/session_")
    )


def _materialize_runtime_dir_args(args: List[str]) -> tuple[List[str], List[Path]]:
    """Replace managed runtime dir args with fresh per-launch directories."""
    out: List[str] = []
    cleanup_dirs: List[Path] = []
    i = 0
    while i < len(args):
        arg = args[i]
        handled_equals = False
        for flag in _MANAGED_RUNTIME_DIR_FLAGS:
            prefix = f"{flag}="
            if not arg.startswith(prefix):
                continue
            original = arg[len(prefix):]
            if _is_managed_runtime_dir(original):
                template = Path(original).expanduser()
                template.parent.mkdir(parents=True, exist_ok=True)
                fresh_dir = Path(
                    tempfile.mkdtemp(prefix=f"{template.name}_", dir=str(template.parent))
                )
                out.append(f"{flag}={fresh_dir}")
                cleanup_dirs.append(fresh_dir)
            else:
                path = Path(original).expanduser()
                path.mkdir(parents=True, exist_ok=True)
                out.append(f"{flag}={path}")
            handled_equals = True
            i += 1
            break
        if handled_equals:
            continue

        if arg not in _MANAGED_RUNTIME_DIR_FLAGS or i + 1 >= len(args):
            out.append(arg)
            i += 1
            continue

        original = args[i + 1]
        out.append(arg)
        if _is_managed_runtime_dir(original):
            template = Path(original).expanduser()
            template.parent.mkdir(parents=True, exist_ok=True)
            fresh_dir = Path(
                tempfile.mkdtemp(prefix=f"{template.name}_", dir=str(template.parent))
            )
            out.append(str(fresh_dir))
            cleanup_dirs.append(fresh_dir)
        else:
            path = Path(original).expanduser()
            path.mkdir(parents=True, exist_ok=True)
            out.append(str(path))
        i += 2

    return out, cleanup_dirs


def _cleanup_runtime_dirs(paths: List[Path]) -> None:
    """Best-effort removal of launcher-created runtime dirs."""
    for path in paths:
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            logger.debug("Failed to cleanup runtime dir: %s", path, exc_info=True)


def _extract_runtime_dir_arg(args: List[str], flag: str) -> Optional[Path]:
    """Return the resolved value for a flag like --profile when present."""
    for i, arg in enumerate(args):
        if arg.startswith(f"{flag}="):
            return Path(arg.split("=", 1)[1]).expanduser()
        if arg == flag and i + 1 < len(args):
            return Path(args[i + 1]).expanduser()
    return None


def _copytree_if_dir(src: Path, dst: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)


def _copy_firefox_theme_assets(chrome_dir: Path, theme_hint: str | None) -> None:
    if not theme_hint:
        return

    theme_specs = {
        "mactahoe-light": {
            "root": _FIREFOX_THEME_ROOT / "mactahoe",
            "userChrome": "userChrome.css",
            "userContent": "userContent.css",
            "dirs": ["MacTahoe"],
        },
        "mactahoe-darker": {
            "root": _FIREFOX_THEME_ROOT / "mactahoe",
            "userChrome": "userChrome-darker.css",
            "userContent": "userContent-darker.css",
            "dirs": ["MacTahoe"],
        },
        "mactahoe-nord": {
            "root": _FIREFOX_THEME_ROOT / "mactahoe",
            "userChrome": "userChrome-nord.css",
            "userContent": "userContent-nord.css",
            "dirs": ["MacTahoe"],
        },
        "monterey-light": {
            "root": _FIREFOX_THEME_ROOT / "whitesur",
            "userChrome": "userChrome-Monterey.css",
            "userContent": "userContent-Monterey.css",
            "dirs": ["common", "Monterey"],
        },
        "monterey-darker": {
            "root": _FIREFOX_THEME_ROOT / "whitesur",
            "userChrome": "userChrome-Monterey-darker.css",
            "userContent": "userContent-Monterey-darker.css",
            "dirs": ["common", "Monterey"],
        },
        "whitesur-light": {
            "root": _FIREFOX_THEME_ROOT / "whitesur",
            "userChrome": "userChrome-WhiteSur.css",
            "userContent": "userContent-WhiteSur.css",
            "dirs": ["common", "WhiteSur", "Monterey"],
        },
        "whitesur-darker": {
            "root": _FIREFOX_THEME_ROOT / "whitesur",
            "userChrome": "userChrome-WhiteSur-darker.css",
            "userContent": "userContent-WhiteSur-darker.css",
            "dirs": ["common", "WhiteSur", "Monterey"],
        },
        "whitesur-nord": {
            "root": _FIREFOX_THEME_ROOT / "whitesur",
            "userChrome": "userChrome-WhiteSur-nord.css",
            "userContent": "userContent-WhiteSur-nord.css",
            "dirs": ["common", "WhiteSur", "Monterey"],
        },
    }
    spec = theme_specs.get(theme_hint)
    if spec is None:
        return

    root = spec["root"]
    chrome_dir.mkdir(parents=True, exist_ok=True)
    for src_name, dst_name in (
        (spec["userChrome"], "userChrome.css"),
        (spec["userContent"], "userContent.css"),
        ("customChrome.css", "customChrome.css"),
    ):
        src = root / src_name
        if src.is_file():
            shutil.copy2(src, chrome_dir / dst_name)
    for dirname in spec["dirs"]:
        _copytree_if_dir(root / dirname, chrome_dir / dirname)


def _seed_firefox_profile(profile_dir: Path, theme_hint: str | None = None) -> None:
    """Write deterministic Firefox prefs into a fresh managed profile."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    user_js = profile_dir / "user.js"
    user_js.write_text("\n".join(_FIREFOX_PROFILE_PREFS) + "\n", encoding="utf-8")
    _copy_firefox_theme_assets(profile_dir / "chrome", theme_hint)


def _seed_vscode_user_dir(user_dir: Path) -> None:
    """Write deterministic VS Code user settings into a fresh managed dir."""
    settings_dir = user_dir / "User"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.json"
    settings_path.write_text(
        json.dumps(_VSCODE_USER_SETTINGS, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _seed_zim_notebook(notebook_dir: Path) -> None:
    """Copy the deterministic Zim notebook fixture into a writable runtime dir."""
    if notebook_dir.exists():
        shutil.rmtree(notebook_dir)
    # Prefer the staged copy: it is the one whose notebook name has had the
    # project's own branding removed, and Zim draws that name in its title bar.
    source = Path(rewrite_asset_path(str(_ZIM_NOTEBOOK_FIXTURE)))
    if not source.is_dir():
        source = _ZIM_NOTEBOOK_FIXTURE
    shutil.copytree(source, notebook_dir)


def _seed_thunderbird_profile(profile_dir: Path) -> None:
    """Write deterministic Thunderbird prefs into a fresh managed profile."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    local_folders = profile_dir / "Mail" / "Local Folders"
    local_folders.mkdir(parents=True, exist_ok=True)
    (local_folders / "Inbox").write_text(_THUNDERBIRD_SAMPLE_INBOX, encoding="utf-8")
    (local_folders / "Drafts").write_text("", encoding="utf-8")
    (local_folders / "Sent").write_text("", encoding="utf-8")
    prefs = list(_THUNDERBIRD_PROFILE_PREFS) + [
        'user_pref("mail.account.account1.identities", "id1");',
        'user_pref("mail.account.account1.server", "server1");',
        'user_pref("mail.accountmanager.accounts", "account1");',
        'user_pref("mail.accountmanager.defaultaccount", "account1");',
        'user_pref("mail.accountmanager.localfoldersserver", "server1");',
        'user_pref("mail.identity.id1.fullName", "Alex Rivera");',
        'user_pref("mail.identity.id1.useremail", "alex.rivera@example.test");',
        'user_pref("mail.server.server1.directory-rel", "[ProfD]Mail/Local Folders");',
        'user_pref("mail.server.server1.hostname", "Local Folders");',
        'user_pref("mail.server.server1.name", "Local Folders");',
        'user_pref("mail.server.server1.spamActionTargetFolder", "mailbox://nobody@Local%20Folders/Junk");',
        'user_pref("mail.server.server1.spamActionTargetAccount", "mailbox://nobody@Local%20Folders");',
        'user_pref("mail.server.server1.type", "none");',
        'user_pref("mail.server.server1.userName", "nobody");',
    ]
    prefs_text = "\n".join(prefs) + "\n"
    (profile_dir / "user.js").write_text(prefs_text, encoding="utf-8")
    (profile_dir / "prefs.js").write_text(prefs_text, encoding="utf-8")


def launch_app(
    manifest: AppManifest,
    timeout: float = 10.0,
    poll_interval: float = 0.3,
) -> subprocess.Popen:
    """Launch an app and wait until it appears in the AT-SPI tree.

    Args:
        manifest: App configuration.
        timeout: Max seconds to wait for AT-SPI readiness.
        poll_interval: Seconds between AT-SPI polls.

    Returns:
        The subprocess.Popen handle for the running app.

    Raises:
        RuntimeError: If the app doesn't appear in AT-SPI within timeout.
    """
    binary = find_binary(manifest.binary)
    # Manifests name their documents by absolute path into this checkout, and
    # editors draw that path in the title bar. Point them at the staged copies
    # in the session HOME first; outside a session the map is empty and this is
    # a no-op. See environment/workspace.py.
    launch_args, cleanup_dirs = _materialize_runtime_dir_args(
        rewrite_asset_paths(list(manifest.args))
    )
    if manifest.app_name == "zim":
        for i, arg in enumerate(launch_args):
            path = Path(arg).expanduser()
            if path.is_absolute() and path.name.startswith(_ZIM_NOTEBOOK_DIR_NAME):
                path.parent.mkdir(parents=True, exist_ok=True)
                fresh_dir = Path(tempfile.mkdtemp(prefix=f"{path.name}_", dir=str(path.parent)))
                launch_args[i] = str(fresh_dir)
                cleanup_dirs.append(fresh_dir)
                break
    cmd = [binary] + launch_args

    if "firefox" in Path(binary).name or manifest.app_name == "firefox":
        profile_dir = _extract_runtime_dir_arg(launch_args, "--profile")
        if profile_dir is not None:
            _seed_firefox_profile(profile_dir, os.environ.get("DESKSHOT_FIREFOX_THEME"))
    if "thunderbird" in Path(binary).name or manifest.app_name == "thunderbird":
        profile_dir = _extract_runtime_dir_arg(launch_args, "--profile")
        if profile_dir is not None:
            _seed_thunderbird_profile(profile_dir)
    if "code" in Path(binary).name.lower() or manifest.app_name == "vscode":
        user_dir = _extract_runtime_dir_arg(launch_args, "--user-data-dir")
        if user_dir is not None:
            _seed_vscode_user_dir(user_dir)
    if manifest.app_name == "zim":
        for arg in launch_args:
            path = Path(arg).expanduser()
            if path.is_absolute() and path.name.startswith(_ZIM_NOTEBOOK_DIR_NAME):
                _seed_zim_notebook(path)
                break

    # Build environment
    env = build_app_env(binary, manifest)

    logger.info(f"Launching: {' '.join(cmd)}")
    # Send app output to a file rather than DEVNULL. When an app fails to start
    # its own stderr is the only evidence of why, and discarding it left compute
    # node failures completely undiagnosable.
    app_log_path = _app_log_path(manifest.app_name)
    app_log_handle = open(app_log_path, "wb")
    try:
        proc = subprocess.Popen(
            cmd,
            env=env,
            # Default to the session HOME, not the launcher's own directory.
            # A GTK file chooser draws its current folder as a row of path-bar
            # buttons, so inheriting the checkout's cwd put `proj`,
            # `docling-vision`, `users` and the account name on screen in every
            # capture that opened one.
            cwd=rewrite_asset_path(manifest.cwd) or os.environ.get("HOME") or None,
            stdout=app_log_handle,
            stderr=subprocess.STDOUT,
        )
    finally:
        app_log_handle.close()
    setattr(proc, "_deskshot_cleanup_dirs", cleanup_dirs)
    setattr(proc, "_deskshot_app_log", app_log_path)

    # Wait for the app to appear in AT-SPI tree
    deadline = time.monotonic() + timeout
    exited_cleanly_at: Optional[float] = None
    grace = POST_EXIT_GRACE_SEC * _launch_scale()
    while time.monotonic() < deadline:
        # A *clean* exit is not a failure. Single-instance and D-Bus-activated
        # apps hand the request to the real instance and the launcher returns 0,
        # with the window appearing a moment later - so failing here reported
        # apps as missing that were on screen (xarchiver 44%, gnome-system-monitor
        # 32%, pluma 28%, against 6-8% for everything else). A non-zero exit is
        # still a real crash.
        #
        # But the window arrives within a second of the handoff or not at all,
        # so waiting the *whole* launch timeout for it is dead time. Waiting the
        # full 270s cost 719 worker-hours in the first 2.6 hours of a 64-shard
        # run - over half the compute - because these three apps often do not
        # register at all under load. After a clean exit the wait is a short
        # grace period instead.
        if proc.poll() is not None:
            if proc.returncode not in (0, None):
                _cleanup_runtime_dirs(cleanup_dirs)
                raise RuntimeError(
                    f"{manifest.binary} exited with code {proc.returncode}"
                    f"{_format_app_log_tail(app_log_path)}"
                )
            if exited_cleanly_at is None:
                exited_cleanly_at = time.monotonic()
            elif time.monotonic() - exited_cleanly_at > grace:
                _cleanup_runtime_dirs(cleanup_dirs)
                raise AppHandoffFailed(
                    f"App '{manifest.atspi_name}' handed off and exited 0 but no "
                    f"window appeared within {grace:.0f}s"
                    f"{_format_app_log_tail(app_log_path)}"
                )

        if _find_app_in_atspi(manifest.atspi_name):
            logger.info(f"App '{manifest.atspi_name}' ready in AT-SPI tree")
            # Extra settle time for UI to fully render
            time.sleep(0.5)
            # Focus the window so keyboard input works
            _focus_app_window(manifest)
            return proc

        time.sleep(poll_interval)

    # Timeout — kill and raise. Reaping must never mask why we got here, and
    # must never skip runtime dir cleanup, or a slow-dying app both hides the
    # real error and leaks its profile dir on every retry.
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                logger.warning(
                    "App '%s' did not exit after SIGKILL; continuing",
                    manifest.atspi_name,
                )
    except Exception:
        logger.debug("Error while terminating %s", manifest.atspi_name, exc_info=True)
    finally:
        _cleanup_runtime_dirs(cleanup_dirs)
    raise RuntimeError(
        f"App '{manifest.atspi_name}' did not appear in AT-SPI tree "
        f"within {timeout}s"
        f"{_format_app_log_tail(app_log_path)}"
    )


def _find_app_in_atspi(atspi_name: str) -> bool:
    """Check if an app with the given name exists in AT-SPI tree."""
    try:
        desktop = Atspi.get_desktop(0)
        if desktop is None:
            return False

        for i in range(desktop.get_child_count()):
            child = desktop.get_child_at_index(i)
            if child is None:
                continue
            name = child.get_name() or ""
            if atspi_name.lower() in name.lower():
                # Also check it has at least one FRAME child
                for j in range(child.get_child_count()):
                    grandchild = child.get_child_at_index(j)
                    if grandchild is not None:
                        try:
                            role = grandchild.get_role()
                            if role == Atspi.Role.FRAME:
                                return True
                        except Exception:
                            continue
                # Even without FRAME, if it has children it's likely ready
                if child.get_child_count() > 0:
                    return True
    except Exception:
        pass

    return False


def wait_for_app_absent_from_atspi(
    atspi_name: str,
    timeout: float = 3.0,
    poll_interval: float = 0.1,
) -> bool:
    """Wait until an AT-SPI app name is no longer present."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _find_app_in_atspi(atspi_name):
            return True
        time.sleep(poll_interval)
    return not _find_app_in_atspi(atspi_name)


def _focus_app_window(manifest: AppManifest) -> None:
    """Try to focus the app window via title candidates (fast path only)."""
    try:
        from deskshot.automation.xdotool import focus_window_by_name

        candidates: List[str] = []
        candidates.extend(manifest.window_titles or [])
        candidates.extend([manifest.atspi_name, manifest.app_name, manifest.binary])
        seen = set()
        for title in candidates:
            t = (title or "").strip()
            if not t or t in seen:
                continue
            seen.add(t)
            if focus_window_by_name(t):
                logger.debug("Focused window using title '%s'", t)
                return
    except Exception as e:
        logger.debug(f"Could not focus window: {e}")


class AppHandoffFailed(RuntimeError):
    """A single-instance app's launcher exited 0 but no window ever appeared.

    Distinct from a launch failure because **retrying is pointless**: the
    handoff already happened and went nowhere, so a second identical request
    goes to the same place. Retrying it three times at the full launch timeout
    is how 719 worker-hours disappeared into three apps.
    """


#: How long to keep looking for a window after the launcher exits cleanly.
#: Scaled by worker count like the launch timeout, because a loaded node is
#: slower to draw. A successful handoff registers almost immediately.
POST_EXIT_GRACE_SEC = 15.0


def _launch_scale() -> float:
    """The same parallel-load scale the scene launcher applies."""
    raw = os.environ.get("DESKSHOT_LAUNCH_TIMEOUT_SCALE", "")
    try:
        return max(1.0, min(3.0, float(raw))) if raw else 1.0
    except ValueError:
        return 1.0


def kill_apps_by_atspi_name(names: List[str]) -> int:
    """Kill this session's remaining processes for the named apps.

    Needed because `kill_app` can only act on the launcher process, and a
    single-instance app's launcher is already gone - so the window it handed off
    to survives teardown and the next scene inherits it.

    **Scoped by DISPLAY**, deliberately. Matching on a command-line pattern is
    how a cleanup ends up killing another worker's app, or another user's, on a
    shared machine; `/proc/<pid>/environ` gives the display a process was
    started on, and this session owns exactly one. Processes are collected first
    and signalled by pid, never by pattern.
    """
    display = os.environ.get("DISPLAY", "")
    if not display or not names:
        return 0

    wanted = {n.strip().lower() for n in names if n and n.strip()}
    marker = f"DISPLAY={display}".encode()
    victims: List[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == os.getpid():
            continue
        try:
            with open(f"/proc/{pid}/comm", "rb") as handle:
                comm = handle.read().strip().decode("utf-8", "replace").lower()
            if not comm:
                continue
            # `comm` is truncated to 15 characters, so compare both ways.
            if not any(comm == n or n.startswith(comm) or comm.startswith(n) for n in wanted):
                continue
            with open(f"/proc/{pid}/environ", "rb") as handle:
                if marker not in handle.read():
                    continue
        except (OSError, ValueError):
            continue
        victims.append(pid)

    for pid in victims:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    if victims:
        time.sleep(0.6)
        for pid in victims:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        logger.info("Killed %d leftover app process(es) on %s", len(victims), display)
    return len(victims)


def kill_app(proc: subprocess.Popen, timeout: float = 5.0) -> None:
    """Gracefully terminate an app, force-kill if needed."""
    if proc.poll() is not None:
        _cleanup_runtime_dirs(getattr(proc, "_deskshot_cleanup_dirs", []))
        return

    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
    _cleanup_runtime_dirs(getattr(proc, "_deskshot_cleanup_dirs", []))
