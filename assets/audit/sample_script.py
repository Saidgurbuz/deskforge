#!/usr/bin/env python3

"""Deterministic editor fixture for Geany qualification."""

from pathlib import Path


def summarize_files(root: Path) -> list[str]:
    names = sorted(p.name for p in root.iterdir())
    return [name for name in names if name.endswith((".py", ".md", ".json"))]


def build_report(project_root: Path) -> str:
    files = summarize_files(project_root)
    joined = ", ".join(files[:6])
    return f"interesting files: {joined}"


if __name__ == "__main__":
    report = build_report(Path.cwd())
    print(report)
