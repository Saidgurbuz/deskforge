#!/usr/bin/env python3
"""Fold an exported label file back into its campaign.

    PYTHONPATH=src python scripts/merge_audit_labels.py \
        --campaign audit/pilot40 --labels labels-pilot40-said.jsonl

Refuses rather than guesses. Every row must name this campaign, an item that is
in its queue, and the rubric the campaign is on; a row that fails any of those
is reported and nothing is written, because the failure mode this guards is the
one that silently produces a plausible wrong number - answers attached to a
different capture, or given against different questions, than the ones they
were made against.

Merging is idempotent: rows already in the log, by `event_id`, are skipped. So
re-merging the same export, or merging an export that overlaps an earlier one,
adds nothing and is safe to do whenever a rater sends a new file.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from deskshot.inspector import audit as audit_mod  # noqa: E402

REQUIRED = ("event_id", "campaign", "index", "scope", "rater")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--campaign", required=True, type=Path)
    parser.add_argument("--labels", required=True, type=Path,
                        help="the exported JSONL, from a bundle or another campaign copy")
    parser.add_argument("--rater", default=None,
                        help="override the rater recorded in the file")
    parser.add_argument("--bundle", type=Path, default=None,
                        help="the bundle the answers came from; its item hashes are "
                             "checked against the campaign's")
    parser.add_argument("--allow-rubric-drift", action="store_true",
                        help="merge answers given against a different rubric version "
                             "(they stay tagged with the version they were given against)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    campaign = audit_mod.Campaign(args.campaign)
    if not campaign.exists():
        raise SystemExit("no campaign at %s" % args.campaign)
    expected_rubric = audit_mod.rubric_ref(campaign.rubric)
    queue = campaign.queue
    keys = {str(item.get("observation_key")): position
            for position, item in enumerate(queue)}

    incoming, torn = audit_mod.read_jsonl(args.labels)
    if torn:
        print("note: ignored %d unterminated bytes at the end of %s" % (torn, args.labels))
    if not incoming:
        raise SystemExit("%s has no rows" % args.labels)

    if args.bundle:
        problems = check_bundle(args.bundle, queue)
        if problems:
            for problem in problems[:10]:
                print("  %s" % problem)
            raise SystemExit("%d bundle items do not match the campaign; nothing merged"
                             % len(problems))
        print("bundle item hashes match the campaign")

    errors: List[str] = []
    accepted: List[Dict[str, Any]] = []
    reasons: Counter = Counter()
    existing = {str(row.get("event_id")) for row in campaign.labels() if row.get("event_id")}
    for position, row in enumerate(incoming):
        missing = [field for field in REQUIRED if row.get(field) in (None, "")]
        if missing:
            errors.append("row %d: missing %s" % (position + 1, ", ".join(missing)))
            continue
        if row.get("campaign") != campaign.name:
            errors.append("row %d: campaign %r, expected %r"
                          % (position + 1, row.get("campaign"), campaign.name))
            continue
        index = row.get("index")
        if not isinstance(index, int) or index < 0 or index >= len(queue):
            errors.append("row %d: index %r is not in this queue" % (position + 1, index))
            continue
        key = str(row.get("observation_key") or "")
        if key and keys.get(key) != index:
            errors.append("row %d: %s is item %s here, not %s"
                          % (position + 1, key, keys.get(key), index))
            continue
        rubric = str(row.get("rubric") or "")
        if rubric and rubric != expected_rubric and not args.allow_rubric_drift:
            errors.append("row %d: answered against rubric %s, campaign is on %s"
                          % (position + 1, rubric, expected_rubric))
            continue
        if str(row.get("event_id")) in existing:
            reasons["already present"] += 1
            continue
        merged = dict(row)
        if args.rater:
            merged["rater"] = audit_mod.safe_name(args.rater, "rater")
        merged["merged_at"] = audit_mod.now_stamp()
        merged["merged_from"] = args.labels.name
        accepted.append(merged)
        existing.add(str(row.get("event_id")))

    print("%s: %d rows, %d new, %d already present, %d rejected"
          % (args.labels, len(incoming), len(accepted),
             reasons["already present"], len(errors)))
    if errors:
        for problem in errors[:20]:
            print("  %s" % problem)
        if len(errors) > 20:
            print("  ... and %d more" % (len(errors) - 20))
        raise SystemExit("nothing merged: fix the file or pass --allow-rubric-drift "
                         "if the rubric version is the only difference")
    by_rater = Counter(str(row.get("rater")) for row in accepted)
    by_scope = Counter(str(row.get("scope")) for row in accepted)
    if accepted:
        print("  raters: %s" % ", ".join("%s (%d)" % pair for pair in sorted(by_rater.items())))
        print("  scopes: %s" % ", ".join("%s (%d)" % pair for pair in sorted(by_scope.items())))
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    if not accepted:
        return 0
    audit_mod.append_jsonl(campaign.labels_path, accepted)
    print("appended %d rows to %s" % (len(accepted), campaign.labels_path))
    print("re-run scripts/audit_stats.py to fold them into the statistics")
    return 0


def check_bundle(bundle_root: Path, queue: List[Dict[str, Any]]) -> List[str]:
    """Every item the bundle carries must be the item the campaign drew.

    A bundle is copied to another machine and comes back as a file of answers.
    Comparing the recorded SHA-256 per item is what makes "these answers are
    about these pixels" a checked fact rather than an assumption.
    """
    path = Path(bundle_root)
    manifest_path = path / "bundle.json" if path.is_dir() else path
    try:
        with manifest_path.open("r", encoding="utf-8") as handle:
            bundle = json.load(handle)
    except (OSError, ValueError) as error:
        return ["cannot read %s: %s" % (manifest_path, error)]
    problems: List[str] = []
    for row in bundle.get("items") or []:
        index = row.get("index")
        if not isinstance(index, int) or index >= len(queue):
            problems.append("bundle item %r is not in the queue" % (index,))
            continue
        item = queue[index]
        if row.get("observation_key") != item.get("observation_key"):
            problems.append("item %d: bundle has %s, queue has %s"
                            % (index, row.get("observation_key"), item.get("observation_key")))
            continue
        for field, kind in (("sha256", "leaf"), ("image_sha256", "image")):
            expected = ((item.get("digests") or {}).get(kind) or {}).get("sha256")
            found = row.get(field)
            if found and expected and found != expected:
                problems.append("item %d: %s hash differs from the queue" % (index, kind))
    return problems


if __name__ == "__main__":
    raise SystemExit(main())
