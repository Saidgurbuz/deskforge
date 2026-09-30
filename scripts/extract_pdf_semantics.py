#!/usr/bin/env python3
"""Extract coarse semantic blocks from a PDF using PyMuPDF."""

from __future__ import annotations

import argparse
from pathlib import Path

from deskshot.extraction.pdf_semantics import extract_generic_pdf_semantics


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    import json

    rows = extract_generic_pdf_semantics(args.pdf)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
