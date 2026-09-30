"""Record what a capture was produced from, so it can be reproduced or retired.

A seed does not identify a scene on its own. `compose_scene(930098)` is
perfectly deterministic within a given build - three fresh processes return the
same five apps - but it indexes into the app pool, so removing one app changes
what every seed means. Measured on the same seed across three runs: two produced
mousepad and thunar, the third nautilus and thunderbird, purely because FileZilla
had been dropped from the pool in between. Nothing was flaky; the seed's meaning
had changed and nothing recorded that.

The same applies to code. This project has twice shipped a fix that changed
ground truth for every capture already collected - popup promotion, and the
multi-line text drop. Afterwards there was no way to answer "which captures are
affected" except regenerating everything.

So each capture carries the three things a seed is resolved against: the commit,
the effective app pool, and the theme/config surface. Two captures with equal
stamps and equal seeds are comparable; unequal stamps explain a difference that
would otherwise look like nondeterminism, and let a corpus be filtered when a
bug is found instead of rebuilt.

Deliberately cheap: a few subprocess calls and a hash, computed once per run.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _git(*args: str) -> Optional[str]:
    # `-c safe.directory` because captures run in a session that redirects HOME
    # to a scratch dir. Git then cannot read the user's global config, decides
    # the repo has "dubious ownership", and refuses every command - so the
    # commit came back null in exactly the place it was needed, inside the
    # scene subprocess, while working fine from an interactive shell.
    try:
        out = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", str(PROJECT_ROOT), *args],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


@lru_cache(maxsize=1)
def code_version() -> Dict[str, Any]:
    """Commit, and whether the tree had uncommitted changes when it ran.

    `dirty` matters more than it looks: a capture produced from a modified tree
    cannot be reproduced from its commit, so it should not be trusted as a
    baseline no matter how good its numbers are.
    """
    status = _git("status", "--porcelain")
    return {
        "commit": _git("rev-parse", "HEAD"),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(status) if status is not None else None,
    }


def hash_strings(values: Sequence[str]) -> str:
    """Order-sensitive digest. Order matters: the pool is indexed by the seed."""
    digest = hashlib.sha256()
    for value in values:
        digest.update(str(value).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:16]


def hash_config(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


def stamp(
    *,
    app_pool: Optional[Sequence[str]] = None,
    config: Any = None,
) -> Dict[str, Any]:
    """The provenance block to embed in a capture's metadata."""
    out: Dict[str, Any] = {"code": code_version(), "schema": 1}
    if app_pool is not None:
        pool = list(app_pool)
        out["app_pool"] = {"hash": hash_strings(pool), "size": len(pool), "apps": pool}
    if config is not None:
        out["config_hash"] = hash_config(config)
    return out


def same_scene_inputs(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Do these two captures depict the same *scenes*, whatever the code?

    This is the question a quality gate asks, and it is deliberately NOT
    `comparable`. A gate exists to compare a new build against a baseline, so
    the commits differ by definition - requiring them to match would make the
    gate unpassable the moment anything is fixed. What must match is the scene
    definition: the pool a seed indexes into, and the config that dresses it.
    Then a metric that moved says something about the code rather than about
    which windows happened to be drawn.
    """
    if not a or not b:
        return False
    if a.get("app_pool", {}).get("hash") != b.get("app_pool", {}).get("hash"):
        return False
    return a.get("config_hash") == b.get("config_hash")


def comparable(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Were these two captures produced from the same build AND the same pool?

    The strict form, for corpus questions - "can these two rows sit in the same
    dataset and be treated as produced identically". For gating a change, use
    `same_scene_inputs`, which ignores the commit on purpose.
    """
    if not a or not b:
        return False
    if a.get("code", {}).get("commit") != b.get("code", {}).get("commit"):
        return False
    return a.get("app_pool", {}).get("hash") == b.get("app_pool", {}).get("hash")
