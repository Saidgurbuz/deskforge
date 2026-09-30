"""Finding and running the extracted Chromium, for the tests that need a DOM.

Shared rather than copied because both browser tests need exactly the same
awkward setup: the extracted binary ships without the execute bit and its
libraries are not on the loader path, so a runnable copy and an
`LD_LIBRARY_PATH` have to be assembled before a page can be loaded at all.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: Where a usable chromium may be, best first. The staged copies are the ones
#: that actually run: the extracted tree ships the binary mode 664 and owned by
#: root, so it can be neither chmod-ed in place nor exec-ed.
CHROMIUM_CANDIDATES = (
    Path(os.environ.get("DESKSHOT_BIN_FIX_DIR") or "/tmp/deskshot_bin_fix") / "chromium-browser",
    # `scripts/stage_chromium.sh` writes this one; the pipeline's own
    # `/tmp/deskshot_bin_fix` is a shared flock-guarded cache to leave alone.
    Path(os.environ.get("DESKSHOT_CHROMIUM_STAGE")
         or str(Path(os.environ.get("TMPDIR") or "/tmp") / "deskshot_chromium")) / "chromium-browser",
    PROJECT_ROOT / "tools" / "extracted" / "usr" / "lib64" / "chromium-browser"
    / "chromium-browser.sh",
)

#: The extracted tree is not on the loader path, and chromium pulls in enough of
#: it that the list is worth pinning rather than discovering: a `find` over
#: `tools/extracted` for shared objects takes 6.6s, which is longer than the
#: whole test.
LIBRARY_DIRS = (
    "usr/lib64",
    "usr/lib64/samba",
    "usr/lib64/samba/wbclient",
    "usr/lib64/chromium-browser",
    "lib64",
    "usr/lib",
)

SUMMARY = re.compile(r"PROBE pass=(\d+) fail=(\d+) skip=(\d+)")
ROW = re.compile(r'<li class="(\w+)">(.*?)</li>', re.S)


def chromium() -> Optional[str]:
    override = os.environ.get("DESKSHOT_CHROMIUM")
    if override:
        return override if os.access(override, os.X_OK) else None
    for name in ("chromium-browser", "chromium", "google-chrome", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    for path in CHROMIUM_CANDIDATES:
        if path.exists() and os.access(str(path), os.X_OK):
            return str(path)
    return None


def environment() -> dict:
    env = os.environ.copy()
    extracted = PROJECT_ROOT / "tools" / "extracted"
    parts = [str(extracted / name) for name in LIBRARY_DIRS if (extracted / name).is_dir()]
    if env.get("LD_LIBRARY_PATH"):
        parts.append(env["LD_LIBRARY_PATH"])
    if parts:
        env["LD_LIBRARY_PATH"] = ":".join(parts)
    return env
