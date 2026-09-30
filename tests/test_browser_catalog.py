from __future__ import annotations

from pathlib import Path

from deskshot.browser_catalog import (
    BrowserSite,
    browser_catalog_state_family,
    build_browser_catalog_sequence,
    load_browser_catalog,
    parse_browser_catalog_state,
    sample_browser_catalog_state,
)


def test_load_browser_catalog_uses_csv_when_present(tmp_path: Path) -> None:
    path = tmp_path / "catalog.csv"
    path.write_text(
        "rank,domain,url\n1,example.com,https://example.com/\n2,python.org,https://www.python.org/\n",
        encoding="utf-8",
    )

    rows = load_browser_catalog(path)

    assert rows == [
        BrowserSite(domain="example.com", rank=1, url="https://example.com/"),
        BrowserSite(domain="python.org", rank=2, url="https://www.python.org/"),
    ]


def test_parse_browser_catalog_state_and_family() -> None:
    assert parse_browser_catalog_state("catalog:python.org:scrolled") == ("python.org", "scrolled")
    assert browser_catalog_state_family("catalog:python.org:scrolled") == "catalog:python.org"


def test_build_browser_catalog_sequence_scrolled_adds_page_downs() -> None:
    catalog = [BrowserSite(domain="python.org", rank=1, url="https://www.python.org/")]

    seq = build_browser_catalog_sequence(
        "catalog:python.org:scrolled",
        settle_seconds=5.0,
        catalog=catalog,
    )

    assert seq.name == "catalog:python.org:scrolled"
    assert [action.type for action in seq.actions] == [
        "key",
        "type_text",
        "key",
        "wait",
        "key",
        "key",
    ]
    assert seq.actions[1].value == "https://www.python.org/"


def test_sample_browser_catalog_state_returns_catalog_ref() -> None:
    catalog = [
        BrowserSite(domain="python.org", rank=1, url="https://www.python.org/"),
        BrowserSite(domain="docs.python.org", rank=2, url="https://docs.python.org/3/"),
    ]

    ref = sample_browser_catalog_state(123, catalog=catalog)

    assert ref.startswith("catalog:")
    _domain, variant = parse_browser_catalog_state(ref)
    assert variant in {"baseline", "scrolled"}
