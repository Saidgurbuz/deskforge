"""Planning a corpus once, so shards cannot duplicate each other.

The batch planner deduplicates within its own batch; twenty shards each doing
that know nothing about the other nineteen. These cover the properties the
corpus plan has to hold instead - and one of them is a scaling property, because
the original rules made a large corpus impossible to plan rather than merely
slow.
"""

from __future__ import annotations

import json
from pathlib import Path

from deskshot.cli import _read_plan_shard
from deskshot.generation.scene_composer import (
    BARE_DESKTOP_LAYOUT,
    SCENE_APP_COUNT_WEIGHTS,
    compose_corpus_plan,
    compose_scene,
    scene_capture_stem,
    scene_signature,
)


def test_the_plan_has_no_repeated_scene() -> None:
    accepted, _rejected, _next = compose_corpus_plan(start_seed=700000, count=200)
    assert len(accepted) == 200
    signatures = [scene_signature(p.scene) for p in accepted]
    assert len(set(signatures)) == len(signatures)
    seeds = [p.scene.seed for p in accepted]
    assert len(set(seeds)) == len(seeds)


def test_planning_does_not_stall_as_the_corpus_grows() -> None:
    """The combination rule used to make this impossible, not just slow.

    Charging 2 per previous use of an app combination means that with 21 apps -
    21 one-app combinations, ~210 two-app ones - every candidate scores >= 4
    once the corpus is a few thousand scenes, and no scene can be accepted. The
    charge is scale-free now, so the attempts needed per accepted scene stays
    bounded instead of climbing without limit.
    """
    small, small_rejected, _ = compose_corpus_plan(start_seed=710000, count=100)
    large, large_rejected, _ = compose_corpus_plan(start_seed=710000, count=1200)
    assert len(small) == 100 and len(large) == 1200
    small_cost = (len(small) + len(small_rejected)) / len(small)
    large_cost = (len(large) + len(large_rejected)) / len(large)
    assert large_cost < small_cost * 3


def test_a_scene_is_a_pure_function_of_its_seed() -> None:
    """What makes a plan of seeds enough to reconstruct the corpus."""
    first = compose_scene(720001)
    second = compose_scene(720001)
    assert scene_signature(first) == scene_signature(second)
    assert [a.app_name for a in first.apps] == [a.app_name for a in second.apps]
    assert [a.state_ref for a in first.apps] == [a.state_ref for a in second.apps]


def test_capture_stems_are_deterministic_and_separate_steps() -> None:
    scene = compose_scene(720002)
    assert scene_capture_stem(scene) == scene_capture_stem(compose_scene(720002))
    stems = {scene_capture_stem(scene, step) for step in range(4)}
    assert len(stems) == 4


def test_shards_partition_the_plan(tmp_path: Path) -> None:
    plan = tmp_path / "plan.jsonl"
    accepted, _rejected, _next = compose_corpus_plan(start_seed=730000, count=37)
    plan.write_text(
        "\n".join(
            json.dumps({"index": i, "seed": p.scene.seed, "steps": i % 3})
            for i, p in enumerate(accepted)
        ),
        encoding="utf-8",
    )
    everything = _read_plan_shard(str(plan), "")
    shards = [_read_plan_shard(str(plan), f"{k}/4") for k in range(4)]
    assert sorted(sum(shards, [])) == sorted(everything)
    # Striding, not blocking: no shard is a contiguous region of the corpus.
    assert [row[0] for row in shards[0]] != [
        row[0] for row in everything[: len(shards[0])]
    ]


def test_the_plan_carries_the_episode_decision(tmp_path: Path) -> None:
    plan = tmp_path / "plan.jsonl"
    plan.write_text(
        '{"index": 0, "seed": 1, "steps": 3}\n{"index": 1, "seed": 2, "steps": 0}\n',
        encoding="utf-8",
    )
    # Rows now also carry the parameters the scene was composed with, so a
    # batch rebuilds the planned scene instead of a generic one.
    assert _read_plan_shard(str(plan), "") == [
        (1, 3, None, False), (2, 0, None, False)
    ]


def test_a_zero_window_scene_is_composable() -> None:
    assert 0 in SCENE_APP_COUNT_WEIGHTS
    bare = [
        compose_scene(seed)
        for seed in range(740000, 741000)
        if not compose_scene(seed).apps
    ]
    assert bare, "zero-window scenes should be reachable"
    for scene in bare:
        assert scene.layout == BARE_DESKTOP_LAYOUT
        assert scene.apps == []
        # Still a full desktop: theme, wallpaper and icons are all chosen.
        assert scene.theme_preset and scene.display_preset and scene.desktop_profile


def test_window_counts_span_zero_to_eight() -> None:
    counts = {len(compose_scene(seed).apps) for seed in range(750000, 751000)}
    assert counts == set(range(9))


def test_layout_geometry_is_the_same_in_a_fresh_process() -> None:
    """This is the one that was broken, and it broke silently.

    `_layout_rects` seeded its generator with `hash((family, count, w, h))`, and
    Python salts the hash of a string per process. So the plan recorded one set
    of window rectangles and the capture worker - a different process - produced
    another. Every generative layout was affected: 62% of two-to-four-window
    scenes and all of the larger ones.
    """
    import json
    import subprocess
    import sys
    from pathlib import Path

    probe = (
        "from deskshot.generation.scene_composer import compose_scene, scene_signature;"
        "import json;"
        "s=compose_scene(946004);"
        "print(json.dumps([scene_signature(s),"
        "[[a.rect.x,a.rect.y,a.rect.width,a.rect.height] for a in s.apps]]))"
    )
    env_root = str(Path(__file__).resolve().parents[1] / "src")
    seen = set()
    for salt in ("1", "2", "3"):
        out = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            env={"PYTHONPATH": env_root, "PYTHONHASHSEED": salt, "PATH": "/usr/bin:/bin"},
            timeout=120,
        )
        assert out.returncode == 0, out.stderr
        seen.add(out.stdout.strip())
    assert len(seen) == 1, f"geometry differs between processes: {seen}"


def test_scenes_that_would_look_the_same_are_rejected() -> None:
    """`scene_signature` holds raw pixel coordinates, so two scenes a pixel
    apart both pass it while being the same sample to a reader and to a model.
    The visual key is the coarse check that catches those, and it is cheap
    enough to enforce over the whole corpus rather than a recent window."""
    from deskshot.generation.scene_composer import scene_visual_key

    accepted, _rejected, _next = compose_corpus_plan(start_seed=760000, count=600)
    keys = [scene_visual_key(p.scene) for p in accepted]
    assert len(set(keys)) == len(keys)


def test_the_visual_key_ignores_pixel_jitter_but_not_arrangement() -> None:
    """The key buckets coordinates into twelfths of the screen, so it is a
    bucketing and not an exact tolerance: a window sitting on a bucket boundary
    does flip when nudged by one pixel.

    That asymmetry is the safe direction. A collision always means the two
    scenes look alike, so nothing is thrown away wrongly; a boundary case just
    means an occasional look-alike pair is kept. Asserted as a rate over many
    scenes rather than on one, because one scene proves nothing either way.
    """
    from dataclasses import replace

    from deskshot.generation.scene_composer import scene_visual_key

    scenes = [
        scene
        for scene in (compose_scene(seed) for seed in range(770000, 770200))
        if scene.apps
    ]
    assert len(scenes) > 100

    def shifted(scene, dx):
        return replace(
            scene,
            apps=[replace(a, rect=replace(a.rect, x=a.rect.x + dx)) for a in scene.apps],
        )

    unchanged = sum(
        1 for s in scenes if scene_visual_key(shifted(s, 1)) == scene_visual_key(s)
    )
    assert unchanged / len(scenes) > 0.85, unchanged / len(scenes)

    # A window moved by a third of the screen is a different picture, always.
    for scene in scenes[:40]:
        assert scene_visual_key(shifted(scene, 600)) != scene_visual_key(scene)


def test_the_visual_key_does_not_include_per_scene_seeds() -> None:
    """The first version included `wallpaper_seed`, which is derived from the
    scene seed and therefore unique per scene - so every key was distinct and
    the check reported a flawless zero while doing nothing."""
    from dataclasses import replace

    from deskshot.generation.scene_composer import scene_visual_key

    scene = compose_scene(780001)
    assert scene_visual_key(replace(scene, wallpaper_seed=scene.wallpaper_seed + 1)) == (
        scene_visual_key(scene)
    )
    assert scene_visual_key(replace(scene, desktop_seed=scene.desktop_seed + 1)) == (
        scene_visual_key(scene)
    )


def test_a_finished_capture_is_found_in_the_sharded_tree(tmp_path) -> None:
    """The corpus layout nests captures; rebuilding the path from the shard root
    aborted a whole shard on its first episode.

    `evaluate_scene_capture` reads three element files back after a capture. It
    built `output_dir / f"{stem}..."`, which is right for a flat run and wrong
    for `ep/<scene_id>/` - and because it raised rather than skipped, one
    episode took the entire shard down with it.
    """
    from deskshot.generation.scene_composer import _capture_dir_for

    shard = tmp_path / "shard-0000"
    nested = shard / "ep" / "abc123"
    nested.mkdir(parents=True)
    stem = "scene-abc123-step00"
    (nested / f"{stem}.elements.unfiltered.json").write_text("[]", encoding="utf-8")

    # Found from a path the observation reported...
    assert _capture_dir_for(
        shard, {"elements_leaf": str(nested / f"{stem}.elements.leaf.json")}, stem
    ) == nested
    # ...and found by searching when the row carries no paths at all.
    assert _capture_dir_for(shard, {}, stem) == nested

    # A flat run still resolves to the root it was given.
    flat_stem = "scene-def456-step00"
    (shard / f"{flat_stem}.elements.unfiltered.json").write_text("[]", encoding="utf-8")
    assert _capture_dir_for(shard, {}, flat_stem) == shard


def test_the_corpus_layout_clusters_episodes_and_fans_out_statics() -> None:
    from deskshot.generation.scene_composer import corpus_relative_dir

    scene = compose_scene(790001)
    assert corpus_relative_dir(scene, is_episode=True) == f"ep/{scene.scene_id}"
    assert corpus_relative_dir(scene) == f"st/{scene.scene_id[:2]}"
    # 256 buckets: no directory holds a meaningful fraction of a million files.
    buckets = {corpus_relative_dir(compose_scene(s)) for s in range(790000, 791000)}
    assert len(buckets) > 200


def test_the_plan_respects_the_window_count_weights() -> None:
    """The balance rules must not quietly override the scene distribution.

    Three of them did, each for the same reason - comparing quantities that are
    not comparable:

    - The combo rule averaged reuse over app sets of *every* size. A zero-window
      scene has one possible app set and a four-window scene has thousands, so
      small scenes looked permanently overused: 98% of zero-window candidates
      and 60% of two-window ones were rejected against 15% of four-window.
    - The "underused app" rule fired when no chosen app was underused, which is
      likelier the fewer apps you choose.
    - The similarity score summed +2 per shared app in the same state, so it
      grew with window count and the fixed threshold bit dense scenes hardest -
      exactly the occlusion-heavy scenes worth most.
    """
    import collections

    from deskshot.generation.scene_composer import SCENE_APP_COUNT_WEIGHTS

    accepted, _rejected, _next = compose_corpus_plan(start_seed=800000, count=4000)
    got = collections.Counter(len(p.scene.apps) for p in accepted)
    weight_total = sum(SCENE_APP_COUNT_WEIGHTS.values())

    for count, weight in SCENE_APP_COUNT_WEIGHTS.items():
        share = got.get(count, 0) / len(accepted) * 100
        asked = weight / weight_total * 100
        assert abs(share - asked) < 6.0, (
            f"{count}-window scenes came out at {share:.1f}% against {asked:.1f}% asked"
        )
    # The tails specifically: they are the cases that were being squeezed out.
    assert got.get(0, 0) > 0, "zero-window scenes should survive planning"
    assert got.get(8, 0) / len(accepted) > 0.02, "the dense tail should survive too"


def test_planning_stays_cheap_and_delivers_what_was_asked() -> None:
    accepted, rejected, _next = compose_corpus_plan(start_seed=810000, count=3000)
    assert len(accepted) == 3000
    attempts_per_scene = (len(accepted) + len(rejected)) / len(accepted)
    assert attempts_per_scene < 3.0, attempts_per_scene


def test_apps_stay_near_their_weighted_share() -> None:
    """The rules were loosened; this is the property they existed to hold."""
    import collections

    from deskshot.generation.scene_composer import SCENE_APP_WEIGHTS

    accepted, _rejected, _next = compose_corpus_plan(start_seed=820000, count=4000)
    used = collections.Counter(a.app_name for p in accepted for a in p.scene.apps)
    total = sum(used.values())
    weight_total = sum(SCENE_APP_WEIGHTS.get(name, 1.0) for name in used)
    for name, count in used.items():
        share = count / total * 100
        asked = SCENE_APP_WEIGHTS.get(name, 1.0) / weight_total * 100
        assert abs(share - asked) < 3.0, f"{name}: {share:.2f}% vs {asked:.2f}%"


def test_a_half_written_capture_is_not_treated_as_finished(tmp_path) -> None:
    """A SIGKILL mid-write left a 0-byte meta.json in a real four-shard run.

    That is worse than leaving nothing behind: --resume decides a scene is done
    by the presence of its meta file, so the scene would be skipped forever and
    the incomplete sample would stay in the corpus. Writes are atomic now, and
    resume validates rather than trusting existence - either alone would have
    prevented it, and a multi-day preemptable run deserves both.
    """
    import json as _json

    from deskshot.cli import _capture_looks_complete

    root = tmp_path / "shard-0000"
    nested = root / "ep" / "abc123"
    nested.mkdir(parents=True)

    assert not _capture_looks_complete(root, "abc123"), "nothing written yet"

    # Complete: meta parses, names its stem, and its screenshot exists.
    (nested / "scene-abc123-step00.meta.json").write_text(
        _json.dumps({"stem": "scene-abc123-step00"}), encoding="utf-8"
    )
    (nested / "scene-abc123-step00.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    assert _capture_looks_complete(root, "abc123")

    # The exact failure seen: a second step whose meta is empty.
    (nested / "scene-abc123-step01.meta.json").write_text("", encoding="utf-8")
    assert not _capture_looks_complete(root, "abc123")

    # And a meta that parses but whose screenshot never landed.
    (nested / "scene-abc123-step01.meta.json").write_text(
        _json.dumps({"stem": "scene-abc123-step01"}), encoding="utf-8"
    )
    assert not _capture_looks_complete(root, "abc123")


def test_json_artifacts_are_written_atomically(tmp_path) -> None:
    from deskshot.extraction.run_extraction import _save_json

    target = tmp_path / "x.json"
    _save_json(target, {"a": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    # No temporary files left behind, and nothing else in the directory.
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


def test_a_blocked_pointer_move_degrades_instead_of_ending_the_episode() -> None:
    """`xdotool mousemove --sync` waits for the X server to confirm the pointer
    arrived, and a GTK menu holding a pointer grab means that never happens - so
    the call blocked until its timeout and raised, aborting the whole episode.

    Measured on a 64-shard run: 1,807 step failures, almost all of them this,
    and roughly half of all episodes ended early. Falling back to the
    unsynchronised move gives up the arrival guarantee, not the move.
    """
    from unittest import mock

    from deskshot.automation import xdotool

    calls = []

    def fake_run(args, display=None, timeout=10.0):
        calls.append(list(args))
        if "--sync" in args:
            raise xdotool.XdotoolTimeout("blocked by a pointer grab")
        return ""

    with mock.patch.object(xdotool, "_run", fake_run), mock.patch.object(
        xdotool.time, "sleep", lambda _s: None
    ):
        xdotool.click(50, 60)

    assert calls[0][:2] == ["mousemove", "--sync"]
    assert calls[1] == ["mousemove", "50", "60"], "should retry unsynchronised"
    assert calls[2] == ["click", "1"], "the click must still happen"


def test_the_per_sample_verdict_is_actually_writable(tmp_path) -> None:
    """This function only runs in a shard's end-of-run audit pass, so a
    NameError in it survived the whole test suite and killed all 64 shards of a
    live run after they had finished capturing - the crash was in the audit, not
    the work. A rarely-taken branch needs a test that takes it.
    """
    from deskshot.cli import _write_sample_verdict

    scene = compose_scene(5000)
    here = tmp_path / "st" / "ab"
    here.mkdir(parents=True)
    stem = "scene-abc-step00"
    (here / f"{stem}.elements.unfiltered.json").write_text("[]", encoding="utf-8")

    _write_sample_verdict(
        tmp_path,
        scene,
        {"stem": stem, "elements_leaf": str(here / f"{stem}.elements.leaf.json")},
        {
            "accepted": False,
            "status": "rejected_post",
            "reasons": ["missing_apps"],
            "missing_apps": ["xarchiver"],
            "quality_ok": True,
            "quality_checks": [{"name": "min_elements", "ok": False}],
            "screenshot_hash": "abcd",
        },
    )

    written = json.loads((here / f"{stem}.verdict.json").read_text(encoding="utf-8"))
    assert written["accepted"] is False
    assert written["reasons"] == ["missing_apps"]
    assert written["missing_apps"] == ["xarchiver"]
    assert written["failed_checks"] == ["min_elements"]
    assert written["seed"] == scene.seed


def test_one_failed_action_does_not_end_the_episode(tmp_path) -> None:
    """A failed step used to `break`, throwing away every remaining frame of an
    episode whose session setup - the expensive part - was already paid for.

    Measured on a 52,000-scene run: 1,807 step failures, roughly half of all
    episodes cut short, and a corpus at 64% of its planned size. Most failures
    are one unreachable target and the next action is fine, so the episode now
    carries on and only gives up after several failures in a row.
    """
    from unittest import mock

    import deskshot.generation.actions as actions
    import deskshot.generation.scene_composer as composer

    class _Env:
        def __init__(self, *a, **k):
            self.calls = 0
            self.last_elements = [{"uid": "u1"}]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def reset(self, scene):
            return {"stem": "scene-x-step00"}

        def step(self, action):
            self.calls += 1
            if self.calls in (1, 2):
                raise RuntimeError("xdotool mousemove timed out on a pointer grab")
            return {
                "action": action.to_dict(),
                "observation": {"stem": f"scene-x-step{self.calls:02d}"},
                "diff": {},
                "step_index": self.calls,
            }

    with mock.patch.object(composer, "DesktopEnv", _Env), mock.patch.object(
        composer, "load_manifests", lambda _p: []
    ), mock.patch.object(
        actions,
        "sample_actionable_targets",
        lambda els, limit=12: [{"uid": "u1", "role": "push button", "inner_text": "B"}],
    ):
        scene = compose_scene(9001)
        composer.run_scene_episode(
            scene, composer.PipelineConfig(), steps=5, output_dir=tmp_path
        )

    episode = json.loads(
        next(tmp_path.rglob("episode*.json")).read_text(encoding="utf-8")
    )
    # Two of five actions failed; the other three still produced frames.
    assert episode["captured_steps"] >= 4, episode["captured_steps"]


def test_an_audit_failure_cannot_lose_the_capture(tmp_path) -> None:
    """The audit runs after a shard has captured every one of its scenes - hours
    of work already on disk - and a NameError in it killed all 64 shards of a
    live run. The audit is derivable from files that already exist; nothing
    derivable should be able to destroy something that is not."""
    from unittest import mock

    import deskshot.cli as cli

    accepted: list = []
    row = {"stem": "scene-abc-step00"}

    with mock.patch(
        "deskshot.generation.scene_composer.evaluate_scene_capture",
        side_effect=RuntimeError("boom"),
    ):
        cli._audit_one_capture(
            compose_scene(5000),
            row,
            output_dir=tmp_path,
            accepted_hashes={},
            accepted_rows=accepted,
        )

    assert accepted == [row], "an unjudged capture is kept, not discarded"
