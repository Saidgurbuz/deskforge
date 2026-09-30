"""Build the shared wallpaper catalog cache.

Classifying the wallpaper pool reads every image, which is the single most
expensive step in session startup. The result is cached under tools/cache/ and
reused by every later session on any node, so this is worth running once after
setup or after the wallpaper pool changes, rather than letting the first scene
of a production run pay for it.
"""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from deskshot.environment.wallpaper_catalog import (  # noqa: E402
    _catalog_cache_path,
    _catalog_fingerprint,
    _scan_wallpaper_paths,
    build_wallpaper_catalog,
)


def main() -> int:
    print(f"host {socket.gethostname()}", flush=True)

    started = time.time()
    paths = _scan_wallpaper_paths()
    print(f"scan {len(paths)} files in {time.time() - started:.1f}s", flush=True)

    fingerprint = _catalog_fingerprint(paths)
    cache_path = _catalog_cache_path(fingerprint)
    print(f"fingerprint {fingerprint[:16]} -> {cache_path}", flush=True)

    started = time.time()
    catalog = build_wallpaper_catalog()
    cold = time.time() - started
    print(f"cold {len(catalog)} entries in {cold:.1f}s", flush=True)

    started = time.time()
    again = build_wallpaper_catalog()
    print(
        f"warm {time.time() - started:.3f}s identical={catalog == again}",
        flush=True,
    )

    if not cache_path.is_file():
        print(f"ERROR cache not written to {cache_path}", flush=True)
        return 1
    print(f"cache {cache_path.stat().st_size} bytes", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
