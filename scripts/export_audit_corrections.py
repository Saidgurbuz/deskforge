#!/usr/bin/env python3
"""Write out the annotations a human corrected, without touching the corpus.

    PYTHONPATH=src python scripts/export_audit_corrections.py \
        --campaign audit/pilot40 --output audit/pilot40/corrected

The audit records two different things about an item: a *judgement* (which is
what the statistics are made of) and, optionally, a *correction* - a rect moved,
a class changed, a missing element added. The judgements are the audit. The
corrections are the beginning of a hand-verified subset, and this script is what
turns them into element lists that something else can consume.

It reads the capture from the corpus and writes a new file elsewhere. The corpus
is opened read-only and nothing is ever written under it: a frozen release stays
frozen, and a corrected copy that lives beside the audit can be reviewed,
regenerated or thrown away without the original being at risk.

Each output carries the SHA-256 of the leaf file it was derived from, so a
corrected list can always be told apart from - and re-checked against - the
released one.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from deskshot.inspector import audit as audit_mod  # noqa: E402
from deskshot.inspector import overlay as overlay_mod  # noqa: E402
from deskshot.inspector import samples as samples_mod  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--campaign", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=None,
                        help="directory for the corrected element lists "
                             "(default: <campaign>/corrected)")
    parser.add_argument("--corpus", type=Path, default=None,
                        help="override the corpus recorded in the manifest")
    parser.add_argument("--jsonl", action="store_true",
                        help="also write one corrected.jsonl with every list inline")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    campaign = audit_mod.Campaign(args.campaign)
    if not campaign.exists():
        raise SystemExit("no campaign at %s" % args.campaign)
    corpus = (args.corpus or Path(campaign.manifest["corpus"])).resolve()
    output = (args.output or (campaign.root / "corrected")).resolve()
    by_key = {str(item.get("observation_key")): item for item in campaign.queue}

    documents = list(overlay_mod.iter_overlays(campaign.corrections_root))
    if not documents:
        print("no corrections in %s; nothing to export" % campaign.corrections_root)
        return 0

    written = 0
    stale = 0
    rows: List[Dict[str, Any]] = []
    for path, document in documents:
        key = str((document.get("sample") or {}).get("stem") or "")
        item = by_key.get(key)
        if item is None:
            print("  %s: %s is not in this campaign's queue" % (path.name, key))
            continue
        leaf = corpus / (str(item["source_path"]) + samples_mod.VIEWS["leaf"])
        if not leaf.is_file():
            print("  %s: no element list at %s" % (path.name, leaf))
            continue
        elements = samples_mod.load_elements(leaf)
        recorded = (document.get("source") or {}).get("sha256")
        current = ((item.get("digests") or {}).get("leaf") or {}).get("sha256")
        if recorded and current and recorded != current:
            # The capture changed under the correction. Refusing is the point:
            # re-attaching an edit to different pixels is how a hand-verified
            # set quietly stops being verified.
            print("  %s: the element list changed since the correction was made; skipped" % key)
            stale += 1
            continue
        merged, applied = overlay_mod.apply(elements, document)
        payload = {
            "schema": "deskshot.audit.corrected/1",
            "observation_key": key,
            "source": {
                "path": str(item["source_path"]) + samples_mod.VIEWS["leaf"],
                "sha256": recorded or current,
            },
            "corrected_by": document.get("updated_by") or document.get("created_by"),
            "corrected_at": document.get("updated_at") or document.get("created_at"),
            "mark": document.get("mark"),
            "counts": {
                "elements_before": len(elements),
                "elements_after": len(merged),
                "modified": len((document.get("edits") or {}).get("modified") or {}),
                "deleted": len((document.get("edits") or {}).get("deleted") or []),
                "added": len((document.get("edits") or {}).get("added") or []),
                "unmatched": len(applied.get("unmatched") or []),
            },
            "elements": merged,
        }
        rows.append(payload)
        if not args.dry_run:
            output.mkdir(parents=True, exist_ok=True)
            target = output / ("%s.leaf.json" % key)
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                              encoding="utf-8")
        written += 1

    print("%d corrections, %d exported, %d skipped as stale"
          % (len(documents), written, stale))
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    if args.jsonl and rows:
        audit_mod.append_jsonl(output / "corrected.jsonl", rows)
        print("wrote %s" % (output / "corrected.jsonl"))
    if written:
        print("wrote %d corrected element lists to %s" % (written, output))
        print("the corpus was not modified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
