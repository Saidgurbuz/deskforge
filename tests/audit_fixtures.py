"""A tiny corpus, shared by the audit tests.

Not a fixture module by accident: the queue builder, the request layer and the
statistics all need the same thing - a corpus that looks like the real one down
to the sharded layout and the SQLite index, small enough to build in a
millisecond.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

from PIL import Image

THEMES = ("linux_classic", "windows_redmond", "quartz_night")
RESOLUTIONS = ("fhd_1920x1080", "wxga_1366x768")
APPS = (("mousepad",), ("nautilus", "eog"), ("pluma",))

INDEX_COLUMNS = (
    "path TEXT PRIMARY KEY", "stem TEXT NOT NULL", "shard TEXT NOT NULL",
    "scene_id TEXT", "step INTEGER", '"group" TEXT', "split TEXT", "apps TEXT",
    "theme TEXT", "resolution TEXT", "profile TEXT", "seed INTEGER",
    "n_elements INTEGER", "n_windows INTEGER", "occluded_ratio REAL",
    "publishable INTEGER", "train_eligible INTEGER", "near_duplicate INTEGER",
    "no_op_frame INTEGER",
)


def elements(count: int, offset: int = 0) -> List[Dict[str, Any]]:
    """A window with widgets inside it, which is the shape of a real capture.

    The frame matters: in the corpus every pixel belongs to some element, so a
    click anywhere hits something. A fixture of scattered widgets on bare
    background does not exercise that and made a sweep click land on nothing.
    """
    kinds = ["Button", "Text", "Checkbox", "Menu", "Image"]
    out = [{
        "uid": "w%04d" % offset,
        "type": "Window",
        "role": "frame",
        "kind": "window",
        "name": "Fixture window",
        "visible_text": "",
        "visible_text_status": "name_only",
        "rect": {"x": 0, "y": 0, "w": 320, "h": 200},
        "visible_fragments": [{"x": 0, "y": 0, "w": 320, "h": 200}],
        "is_occluded": False,
        "occlusion_state": "none",
        "app_name": "mousepad",
        "reading_order_index": 0,
        "_dom_index": 900 + offset,
        "_window_stack_index": 0,
        "source": "app",
    }]
    for index in range(count):
        x, y = 10 + (index % 8) * 30, 10 + (index // 8) * 24
        out.append({
            "uid": "u%04d" % (offset + index),
            "type": kinds[index % len(kinds)],
            "role": "push button",
            "name": "Widget %d" % index,
            "visible_text": "W%d" % index,
            "visible_text_status": "full_visible",
            "rect": {"x": x, "y": y, "w": 24, "h": 18},
            "visible_fragments": [{"x": x, "y": y, "w": 24, "h": 18}],
            "is_occluded": index % 5 == 0,
            "occlusion_state": "partially_occluded" if index % 5 == 0 else "none",
            "app_name": "mousepad",
            "reading_order_index": index,
            "kind": "button",
            "_dom_index": index,
            "_window_stack_index": 0,
            "source": "app",
            "interaction": {"actionable": True},
        })
    return out


def build_corpus(root: Path, captures: int = 9) -> Path:
    """A sharded corpus with an index, shaped like `deskshot_corpus/v1`."""
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    for index in range(captures):
        shard = "shard-%04d" % (index % 3)
        scene = "%016x" % (0xa0000 + index)
        group = "ep" if index % 2 else "st"
        directory = root / "shards" / shard / group / scene
        directory.mkdir(parents=True, exist_ok=True)
        stem = "scene-%s-step%02d" % (scene, index % 3)
        base = directory / stem
        Image.new("RGB", (320, 200), (20 + index, 40, 70)).save(str(base) + ".png")
        payload = elements(12 + index, offset=index * 100)
        Path(str(base) + ".elements.leaf.json").write_text(
            json.dumps(payload), encoding="utf-8")
        Path(str(base) + ".screentag.txt").write_text(
            "<Window><Button>W0</Button></Window>", encoding="utf-8")
        Path(str(base) + ".meta.json").write_text(json.dumps({
            "num_elements_leaf": len(payload), "launched_apps": list(APPS[index % 3]),
            "scene": {"seed": 1000 + index, "theme_preset": THEMES[index % 3]},
        }), encoding="utf-8")
        Path(str(base) + ".verdict.json").write_text(json.dumps({
            "status": "accepted", "failed_checks": [], "quality_ok": True,
        }), encoding="utf-8")
        rows.append({
            "path": "shards/%s/%s/%s/%s" % (shard, group, scene, stem),
            "stem": stem, "shard": shard, "scene_id": scene, "step": index % 3,
            "group": group,
            "split": ["train", "train", "val", "test_app"][index % 4],
            "apps": "|%s|" % "|".join(APPS[index % 3]),
            "theme": THEMES[index % 3], "resolution": RESOLUTIONS[index % 2],
            "profile": "balanced", "seed": 1000 + index,
            "n_elements": 12 + index, "n_windows": 1 + index % 3,
            "occluded_ratio": [0.0, 0.05, 0.2, 0.4, 0.7][index % 5],
            "publishable": 1, "train_eligible": 1,
            "near_duplicate": 0, "no_op_frame": 0,
        })
    (root / "shards" / "shard-0000" / "audit").mkdir(parents=True, exist_ok=True)
    (root / "shards" / "shard-0000" / "audit" / "pixel_audit.json").write_text(
        json.dumps({"captures": 3, "text_elements": 30, "text_phantoms": 1,
                    "phantom_rate": 0.033, "drift_rate": 0.02,
                    "ink_uncovered_mean": 0.21}), encoding="utf-8")

    plan = root / "plan"
    plan.mkdir(exist_ok=True)
    db = sqlite3.connect(str(plan / "index.sqlite"))
    db.execute("CREATE TABLE samples (%s)" % ", ".join(INDEX_COLUMNS))
    db.executemany(
        "INSERT INTO samples VALUES (%s)" % ", ".join("?" * len(INDEX_COLUMNS)),
        [tuple(row[name.split()[0].strip('"')] for name in INDEX_COLUMNS) for row in rows])
    db.commit()
    db.close()
    return root
