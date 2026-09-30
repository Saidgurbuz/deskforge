from __future__ import annotations

import pytest

#: Tests that start Xvfb or read the extracted application tree. They need
#: `bash scripts/setup_tools.sh` first and are skipped, not failed, before that.
_NEEDS_EXTRACTED_TOOLS = (
    "tests/test_session.py::TestDesktopSession",
    "tests/test_desktop_diversity.py::test_panel_variant_overrides_style_default",
)


def pytest_collection_modifyitems(config, items):
    from deskshot.config import EXTRACTED_DIR

    if (EXTRACTED_DIR / "usr" / "bin" / "Xvfb").exists():
        return
    skip = pytest.mark.skip(reason="needs the extracted tools; run scripts/setup_tools.sh")
    for item in items:
        if item.nodeid.startswith(_NEEDS_EXTRACTED_TOOLS):
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _isolate_wallpaper_cache(tmp_path_factory, monkeypatch):
    """Keep the wallpaper catalog cache out of the project tools dir.

    build_wallpaper_catalog persists to tools/cache/ so real jobs can share one
    classified pool. Tests classify small fixture pools, so without this they
    write stray catalogs into the repo.
    """
    from deskshot.environment import wallpaper_catalog as wc

    cache = tmp_path_factory.mktemp("wallpaper_cache") / "catalog.json"
    monkeypatch.setenv("DESKSHOT_WALLPAPER_CACHE", str(cache))
    monkeypatch.setattr(wc, "_CATALOG_MEMO", {})
