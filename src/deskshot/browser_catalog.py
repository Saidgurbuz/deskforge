"""Weighted public-website catalog for Chromium scene diversity."""

from __future__ import annotations

import csv
import math
import random
from functools import lru_cache
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence

from deskshot.config import ASSETS_DIR, Action, InteractionSequence

CATALOG_PATH = ASSETS_DIR / "browser" / "tranco_safe_top1000.csv"

#: The wide catalog, used when a batch asks for uniform sampling.
#:
#: The default catalog is 1,000 sites but the default *sampler* collapses it:
#: it draws from `PREFERRED_PUBLIC_DOMAINS` 78% of the time and weights the
#: remainder by 1/sqrt(rank). Measured over the v1 corpus that produced **22
#: distinct domains**, with google.com alone at 34% and nine domains covering
#: 80% of every browser scene. Widening the list without changing the sampler
#: would have changed nothing.
WIDE_CATALOG_PATH = ASSETS_DIR / "browser" / "tranco_safe_top20000.csv"
TRANCO_SOURCE_URL = "https://tranco-list.eu/top-1m.csv.zip"


@dataclass(frozen=True)
class BrowserSite:
    domain: str
    rank: int
    url: str

    @property
    def slug(self) -> str:
        return self.domain


_FALLBACK_SITES: Sequence[BrowserSite] = (
    BrowserSite(domain="python.org", rank=1, url="https://www.python.org/"),
    BrowserSite(domain="docs.python.org", rank=2, url="https://docs.python.org/3/"),
    BrowserSite(domain="wikipedia.org", rank=3, url="https://www.wikipedia.org/"),
    BrowserSite(domain="github.com", rank=4, url="https://github.com/"),
    BrowserSite(domain="stackoverflow.com", rank=5, url="https://stackoverflow.com/"),
)

PREFERRED_PUBLIC_DOMAINS = {
    "adobe.com",
    "airbnb.com",
    "amazon.com",
    "apple.com",
    "arxiv.org",
    "bbc.com",
    "behance.net",
    "bing.com",
    "booking.com",
    "canva.com",
    "cnn.com",
    "coursera.org",
    "developer.mozilla.org",
    "docs.python.org",
    "dropbox.com",
    "ebay.com",
    "etsy.com",
    "expedia.com",
    "facebook.com",
    "figma.com",
    "github.com",
    "google.com",
    "imdb.com",
    "instagram.com",
    "khanacademy.org",
    "linkedin.com",
    "medium.com",
    "microsoft.com",
    "mozilla.org",
    "netflix.com",
    "newyorker.com",
    "nike.com",
    "notion.so",
    "nytimes.com",
    "pinterest.com",
    "python.org",
    "quora.com",
    "reddit.com",
    "reuters.com",
    "skype.com",
    "soundcloud.com",
    "spotify.com",
    "stackoverflow.com",
    "target.com",
    "ted.com",
    "theguardian.com",
    "tripadvisor.com",
    "udemy.com",
    "ubuntu.com",
    "vimeo.com",
    "walmart.com",
    "weather.com",
    "whatsapp.com",
    "wikipedia.org",
    "wordpress.com",
    "wordpress.org",
    "x.com",
    "yahoo.com",
    "youtube.com",
}


def browser_catalog_state_family(state_ref: str) -> str:
    """Return a similarity family for catalog-backed states."""
    domain, _variant = parse_browser_catalog_state(state_ref)
    return f"catalog:{domain}"


@lru_cache(maxsize=4)
def _read_catalog(catalog_path: Path) -> tuple:
    """Parse one catalog CSV. Cached: the planner calls this per scene.

    Without the cache the 20,000-row wide catalog is re-parsed for every scene
    composed, which turns catalog sampling from negligible into the dominant
    cost of planning a large batch.
    """
    if not catalog_path.is_file():
        return tuple(_FALLBACK_SITES)
    sites: List[BrowserSite] = []
    with catalog_path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                sites.append(BrowserSite(domain=row["domain"],
                                         rank=int(row["rank"]),
                                         url=row["url"]))
            except (KeyError, TypeError, ValueError):
                continue
    return tuple(sites) or tuple(_FALLBACK_SITES)


def load_browser_catalog(path: Path | None = None) -> List[BrowserSite]:
    """Load the weighted public-site catalog from CSV."""
    catalog_path = path or CATALOG_PATH
    if not catalog_path.is_file() and catalog_path != CATALOG_PATH:
        catalog_path = CATALOG_PATH
    return list(_read_catalog(catalog_path))

def parse_browser_catalog_state(state_ref: str) -> tuple[str, str]:
    """Parse `catalog:<domain>:<variant>` state refs."""
    parts = state_ref.split(":", 2)
    if len(parts) != 3 or parts[0] != "catalog":
        raise ValueError(f"Invalid browser catalog state: {state_ref}")
    domain = parts[1].strip().lower()
    variant = parts[2].strip().lower()
    if not domain or variant not in {"baseline", "scrolled"}:
        raise ValueError(f"Invalid browser catalog state: {state_ref}")
    return domain, variant


def build_browser_catalog_sequence(
    state_ref: str,
    *,
    settle_seconds: float = 7.5,
    catalog: Iterable[BrowserSite] | None = None,
) -> InteractionSequence:
    """Build a Chromium interaction from one catalog-backed state ref."""
    domain, variant = parse_browser_catalog_state(state_ref)
    by_domain = {site.domain: site for site in (catalog or load_browser_catalog())}
    site = by_domain.get(domain)
    if site is None:
        raise ValueError(f"Browser catalog domain not found: {domain}")

    actions = [
        Action(type="key", value="ctrl+l", delay=0.3),
        Action(type="type_text", value=site.url, delay=0.2),
        Action(type="key", value="Return", delay=0.3),
        Action(type="wait", value=f"{settle_seconds:.1f}", delay=0.0),
    ]
    if variant == "scrolled":
        actions.extend(
            [
                Action(type="key", value="Page_Down", delay=0.8),
                Action(type="key", value="Page_Down", delay=1.0),
            ]
        )

    return InteractionSequence(
        name=state_ref,
        description=f"Catalog-backed Chromium state for {site.domain} ({variant})",
        actions=actions,
    )


def sample_browser_catalog_state(
    seed: int,
    *,
    catalog: Iterable[BrowserSite] | None = None,
    uniform: bool = False,
) -> str:
    """Pick one catalog-backed Chromium scene state.

    The default is deliberately head-heavy: most browser scenes should show
    sites a person actually visits, so a popularity prior is right for a general
    corpus.

    `uniform` drops both the preference set and the rank weighting and draws
    flat from the whole catalog. It exists because the prior is *very* strong -
    it turned a 1,000-site list into 22 observed domains - and a batch whose
    purpose is page diversity needs the opposite of a popularity prior.
    """
    sites = list(catalog or load_browser_catalog(
        WIDE_CATALOG_PATH if uniform else None
    ))
    rng = random.Random(seed)
    if uniform:
        chosen = sites[rng.randrange(len(sites))]
    else:
        preferred_sites = [
            site for site in sites if site.domain in PREFERRED_PUBLIC_DOMAINS
        ]
        population = (
            preferred_sites if preferred_sites and rng.random() < 0.78 else sites
        )
        weights = [1.0 / math.sqrt(max(1, site.rank)) for site in population]
        chosen = rng.choices(population, weights=weights, k=1)[0]
    variant = "baseline" if rng.random() < 0.62 else "scrolled"
    return f"catalog:{chosen.slug}:{variant}"
