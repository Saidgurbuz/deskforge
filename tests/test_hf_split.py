"""The split must not leak, and must reproduce the corpus mix.

Two failures matter. Putting one frame of an episode in test while its
neighbours are in train is not a held-out sample - the frames differ by a single
click. And a test set that happens to contain no 4K captures cannot answer
whether the model works at 4K.
"""

from __future__ import annotations

import importlib.util
import random
from collections import Counter, defaultdict
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "build_hf_split", Path(__file__).resolve().parents[1] / "scripts/build_hf_split.py"
)
split = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(split)


def _corpus(n_scenes=600, seed=5):
    """A synthetic corpus with the real one's shape."""
    rng = random.Random(seed)
    themes = ["linux", "ubuntu", "windows", "macos", "dark"]
    res = ["1366x768", "1920x1080", "2560x1440", "3840x2160"]
    rows = []
    for i in range(n_scenes):
        sid = f"scene{i:05d}"
        nwin = rng.choice([0, 1, 2, 2, 3, 3, 4, 5, 6, 7, 8])
        steps = rng.choice([1, 1, 1, 3, 5, 9])
        for s in range(steps):
            rows.append({
                "key": f"{sid}-step{s:02d}", "scene_id": sid, "step": s,
                "theme": rng.choice(themes) if s == 0 else rows[-1]["theme"],
                "resolution": rng.choice(res) if s == 0 else rows[-1]["resolution"],
                "group": "ep" if steps > 1 else "st",
                "n_windows": nwin, "apps": [f"app{j}" for j in range(nwin)],
                "shard": "shard-0000", "path": sid, "profile": "balanced",
            })
    return rows


def _assign(rows, test_size, seed=1):
    """Group into scenes and strata exactly as the script does, then allocate."""
    scenes = defaultdict(list)
    for r in rows:
        scenes[r["scene_id"]].append(r)
    strata = defaultdict(list)
    for sid, frames in scenes.items():
        head = min(frames, key=lambda r: r["step"])
        strata[(head["theme"], head["resolution"],
                split.window_bucket(head["n_windows"]),
                "episode" if len(frames) > 1 else "static")].append(sid)
    test = split.allocate(scenes, strata, test_size, len(rows), seed)
    for r in rows:
        r["split"] = "test" if r["scene_id"] in test else "train"
    return rows


def test_no_episode_is_split_across_train_and_test() -> None:
    rows = _assign(_corpus(), 300)
    by_scene = defaultdict(set)
    for r in rows:
        by_scene[r["scene_id"]].add(r["split"])
    straddling = [s for s, v in by_scene.items() if len(v) > 1]
    assert not straddling, f"{len(straddling)} scenes span both splits, e.g. {straddling[:3]}"


def test_size_is_honoured_once_the_target_clears_the_stratum_floor() -> None:
    """The regime the real corpus is in: 10,000 frames over ~490 strata."""
    rows = _corpus(4000)
    n_strata = len({
        (r["theme"], r["resolution"], split.window_bucket(r["n_windows"]), r["group"])
        for r in rows
    })
    target = 3000
    assert target > n_strata * 6, "test corpus is not in the proportional regime"
    _assign(rows, target)
    n = sum(1 for r in rows if r["split"] == "test")
    assert 0.75 * target <= n <= 1.35 * target, f"{n} vs target {target}"


def test_a_target_below_the_stratum_floor_is_floor_limited_not_broken() -> None:
    """Every stratum contributes at least one scene, so a very small target
    cannot be met exactly. That is the intended trade: coverage beats size."""
    rows = _corpus(600)
    n_strata = len({
        (r["theme"], r["resolution"], split.window_bucket(r["n_windows"]), r["group"])
        for r in rows
    })
    _assign(rows, 50)
    test_scenes = {r["scene_id"] for r in rows if r["split"] == "test"}
    assert len(test_scenes) >= n_strata, (
        f"{len(test_scenes)} scenes for {n_strata} strata - coverage was dropped"
    )
    assert len(test_scenes) < len({r["scene_id"] for r in rows}), "took everything"


def test_every_theme_and_resolution_appears_in_test() -> None:
    rows = _assign(_corpus(), 400)
    for field in ("theme", "resolution"):
        seen = {r[field] for r in rows if r["split"] == "test"}
        allv = {r[field] for r in rows}
        assert seen == allv, f"test is missing {field}: {allv - seen}"


def test_the_mix_is_reproduced_not_merely_sampled() -> None:
    rows = _assign(_corpus(1200), 800)
    for field in ("theme", "resolution"):
        tr, te = Counter(), Counter()
        for r in rows:
            (te if r["split"] == "test" else tr)[r[field]] += 1
        tn, en = sum(tr.values()), sum(te.values())
        for k in set(tr) | set(te):
            a, b = 100 * tr[k] / tn, 100 * te[k] / en
            assert abs(a - b) < 6.0, f"{field}={k}: train {a:.1f}% vs test {b:.1f}%"


def test_both_episodes_and_static_scenes_reach_test() -> None:
    rows = _assign(_corpus(), 400)
    kinds = {r["group"] for r in rows if r["split"] == "test"}
    assert kinds == {"ep", "st"}


def test_window_buckets_cover_every_count() -> None:
    assert [split.window_bucket(n) for n in range(9)] == [
        "0", "1", "2-3", "2-3", "4-5", "4-5", "6-8", "6-8", "6-8"
    ]
    assert split.window_bucket(99) == "6-8"


def test_the_assignment_is_deterministic() -> None:
    a = {r["key"]: r["split"] for r in _assign(_corpus(), 300, seed=7)}
    b = {r["key"]: r["split"] for r in _assign(_corpus(), 300, seed=7)}
    assert a == b
