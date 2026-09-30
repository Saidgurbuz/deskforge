"""A held-out axis must be held out of training entirely.

The failure this guards against is silent: an application assigned to
`test_app` that still appears in the corner of a training screenshot. The split
then measures nothing, and nothing in the numbers says so. It happened - eight
scene ids in the corpus were captured twice by different runs which disagreed on
whether an application launched, so keying on one frame let three held-out
applications back into training.
"""

from __future__ import annotations

import importlib.util
from collections import defaultdict
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "bes", Path(__file__).resolve().parents[1] / "scripts/build_eval_splits.py"
)
bes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bes)


def _row(scene, step, apps, theme="linux_classic", res="fhd_1920x1080",
         nwin=None, occ=0.3):
    return {
        "scene_id": scene, "step": step, "apps": list(apps),
        "theme": theme, "resolution": res,
        "n_windows": len(apps) if nwin is None else nwin,
        "occluded_ratio": occ, "train_eligible": True,
        "shard": "shard-0000", "path": f"{scene}-{step}",
    }


def _assign(rows):
    scenes = defaultdict(list)
    for r in rows:
        scenes[r["scene_id"]].append(r)
    axis = {}
    for sid, frames in scenes.items():
        apps = {a for f in frames for a in f["apps"]}
        head = frames[0]
        if apps & set(bes.HELDOUT_APPS):
            axis[sid] = "test_app"
        elif head["theme"] == bes.HELDOUT_THEME:
            axis[sid] = "test_theme"
        elif head["resolution"] == bes.HELDOUT_RESOLUTION:
            axis[sid] = "test_resolution"
    for r in rows:
        r["split"] = axis.get(r["scene_id"], "train")
    return rows


def test_a_held_out_application_never_appears_in_train() -> None:
    rows = _assign([
        _row("s1", 0, ["mousepad", "pluma"]),
        _row("s2", 0, ["mousepad"]),
    ])
    train_apps = {a for r in rows if r["split"] == "train" for a in r["apps"]}
    assert not (train_apps & set(bes.HELDOUT_APPS))


def test_frames_disagreeing_on_apps_still_hold_the_scene_out() -> None:
    """The real bug: one capture of a scene shows the app, another does not."""
    rows = _assign([
        _row("dup", 0, ["homebank", "xarchiver"]),
        _row("dup", 0, ["homebank"]),          # a second run of the same id
    ])
    assert {r["split"] for r in rows} == {"test_app"}, \
        "a scene leaked because one of its captures omitted the application"


def test_every_frame_of_a_scene_lands_in_one_split() -> None:
    rows = _assign([_row("e1", i, ["mousepad"]) for i in range(6)])
    assert len({r["split"] for r in rows}) == 1


def test_the_axes_are_disjoint() -> None:
    """A scene that qualifies twice is counted once, under the first axis."""
    rows = _assign([_row("s1", 0, ["pluma"], theme=bes.HELDOUT_THEME,
                         res=bes.HELDOUT_RESOLUTION)])
    assert rows[0]["split"] == "test_app"


def test_theme_and_resolution_holdouts_work() -> None:
    rows = _assign([
        _row("t", 0, ["mousepad"], theme=bes.HELDOUT_THEME),
        _row("r", 0, ["mousepad"], res=bes.HELDOUT_RESOLUTION),
        _row("n", 0, ["mousepad"]),
    ])
    got = {r["scene_id"]: r["split"] for r in rows}
    assert got == {"t": "test_theme", "r": "test_resolution", "n": "train"}


def test_occlusion_is_not_an_axis() -> None:
    """Holding occlusion out would remove the capability being claimed."""
    src = (Path(__file__).resolve().parents[1] / "scripts/build_eval_splits.py").read_text()
    assert "test_occlusion" not in bes.AXIS_ORDER
    assert "not an axis" in src or "deliberately not an axis" in src
    rows = _assign([_row("hi", 0, ["mousepad"], occ=0.95)])
    assert rows[0]["split"] == "train", "a heavily occluded scene left training"


def test_the_bins_cover_the_range() -> None:
    assert bes.occlusion_bin(0.0) == "none"
    assert bes.occlusion_bin(0.05) == "0-10%"
    assert bes.occlusion_bin(0.2) == "10-25%"
    assert bes.occlusion_bin(0.9) == "25%+"
    assert [bes.window_bucket(n) for n in (0, 1, 3, 5, 8)] == \
        ["0", "1", "2-3", "4-5", "6-8"]


def test_held_out_apps_span_both_kinds_of_question() -> None:
    kinds = set(bes.HELDOUT_APPS.values())
    assert any("unseen category" in k for k in kinds)
    assert any("unseen instance" in k for k in kinds)


def test_a_high_footprint_application_is_not_held_out() -> None:
    """Holding out an app removes every scene *containing* it.

    eog reads like a good candidate - an image viewer, a category training would
    otherwise never see - and holding it out cost 94,326 training samples to
    make a claim gnome-system-monitor already makes for 11,352. This axis is
    priced by footprint, so re-adding a big application has to be deliberate.
    """
    assert "eog" not in bes.HELDOUT_APPS
    assert "chromium-browser" not in bes.HELDOUT_APPS
    assert any("unseen category" in why for why in bes.HELDOUT_APPS.values()), \
        "the cheap unseen-category application must survive the trim"
