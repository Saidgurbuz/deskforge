#!/usr/bin/env python3
"""Generate deterministic wallpaper variants into the extracted tree."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from deskshot.config import ASSETS_DIR, EXTRACTED_DIR
from deskshot.environment.wallpaper_variants import build_wallpaper_variants


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--extracted-dir", default=str(EXTRACTED_DIR))
    parser.add_argument("--assets-dir", default=str(ASSETS_DIR))
    parser.add_argument("--max-sources", type=int, default=100)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    summary = build_wallpaper_variants(
        extracted_dir=Path(args.extracted_dir),
        assets_dir=Path(args.assets_dir),
        max_sources=args.max_sources,
        refresh=args.refresh,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
