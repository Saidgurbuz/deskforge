#!/usr/bin/env python
"""Browse, inspect and correct capture annotations in a browser.

    PYTHONPATH=src python scripts/inspect_annotations.py

Then, from a laptop:

    ssh -L 8000:localhost:8000 <server>
    open http://localhost:8000

The server binds 127.0.0.1 by default and that is deliberate: this machine is
shared, and `/api/overlay` writes files. `--host` exists for the case where a
tunnel is genuinely impossible, and should be used with that in mind.

Edits never touch `incremental_checks/`, which is regenerated and gitignored.
They are written to `golden/<run>/<stem>.<view>.json`, one small document per
sample, recording the sample's identity, the edits, who made them and a
sha256 stamp of the capture they were made against. That directory is the thing
you commit.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from deskshot.inspector.server import TUNNEL_HINT, build_server, serve  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=PROJECT_ROOT / "incremental_checks",
        help="directory of runs to browse (default: incremental_checks/)",
    )
    parser.add_argument(
        "--golden",
        type=Path,
        default=PROJECT_ROOT / "golden",
        help="where edits are stored, one JSON per sample (default: golden/)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address; keep the loopback default on a shared machine and "
             "reach it with 'ssh -L 8000:localhost:8000 <server>'",
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=None,
        help="a generated corpus root (the directory holding shards/ and plan/). "
             "Adds a lazy browser at /corpus that lists one directory at a time "
             "and renders a capture on demand; nothing is walked and nothing is "
             "written. Build plan/index.sqlite with scripts/build_corpus_index.py "
             "to filter by split, application, theme or occlusion.",
    )
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--author",
        default=None,
        help="stamped into every overlay (default: the current user)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=PROJECT_ROOT / ".deskshot" / "inspector",
        help="run index and generated thumbnails (gitignored)",
    )
    parser.add_argument(
        "--audit",
        type=Path,
        default=None,
        help="directory of human-audit campaigns (see scripts/build_audit_queue.py). "
             "Adds a queue-driven labelling page at /audit that draws boxes in the "
             "browser, autosaves every answer and never writes to the corpus. "
             "Needs --corpus as well, because that is where the captures are.",
    )
    parser.add_argument(
        "--rater",
        default=None,
        help="who is labelling; stamped into every answer (default: --author)",
    )
    parser.add_argument(
        "--audit-read-only",
        action="store_true",
        help="serve /audit for reading without accepting answers",
    )
    parser.add_argument(
        "--cache-max-bytes",
        type=int,
        default=4 << 30,
        help="ceiling on the generated-image cache; oldest evicted first (0 = unbounded)",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="serve the inspector without the write endpoints",
    )
    parser.add_argument("--verbose", action="store_true", help="log every request")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    if not root.is_dir():
        parser.error("--root %s is not a directory" % root)

    corpus_root = args.corpus.resolve() if args.corpus else None
    if corpus_root and not corpus_root.is_dir():
        parser.error("--corpus %s is not a directory" % corpus_root)

    audit_root = args.audit.resolve() if args.audit else None
    if audit_root and not audit_root.is_dir():
        parser.error("--audit %s is not a directory" % audit_root)
    if audit_root and not corpus_root:
        parser.error("--audit needs --corpus: the campaign holds keys, not pixels")

    author = args.author or _default_author()
    server = build_server(
        root=root,
        golden_root=args.golden.resolve(),
        host=args.host,
        port=args.port,
        author=author,
        cache_dir=args.cache_dir,
        project_root=PROJECT_ROOT,
        read_only=args.read_only,
        verbose=args.verbose,
        corpus_root=corpus_root,
        audit_root=audit_root,
        rater=args.rater or author,
        audit_read_only=args.audit_read_only,
        cache_max_bytes=args.cache_max_bytes,
    )
    host, port = server.server_address[0], server.server_address[1]
    print("deskshot inspector")
    print("  runs     %s" % root)
    print("  golden   %s" % args.golden.resolve())
    print("  author   %s" % author)
    if corpus_root:
        index = corpus_root / "plan" / "index.sqlite"
        print("  corpus   %s%s" % (corpus_root, "" if index.is_file()
              else "  (no plan/index.sqlite - browsing only, no filters)"))
    print("  serving  http://%s:%d" % (host, port))
    if audit_root:
        campaigns = sorted(
            entry.name for entry in audit_root.iterdir()
            if entry.is_dir() and (entry / "manifest.json").is_file()
        )
        print("  audit    %s  (%s)" % (
            audit_root, ", ".join(campaigns) if campaigns else "no campaigns yet"))
        print("  rater    %s%s" % (args.rater or author,
                                   "  (read-only)" if args.audit_read_only else ""))
    if corpus_root:
        print("           http://%s:%d/corpus" % (host, port))
    if audit_root:
        print("           http://%s:%d/audit" % (host, port))
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("  WARNING: bound to %s - this machine is shared and "
              "/api/overlay writes files" % args.host)
    print(TUNNEL_HINT.format(port=port))
    sys.stdout.flush()
    serve(server)
    return 0


def _default_author() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - getuser can fail with no passwd entry
        return "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
