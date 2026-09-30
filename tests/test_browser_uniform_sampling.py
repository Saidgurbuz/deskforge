"""A batch that exists for page diversity must not inherit a popularity prior.

The default sampler is deliberately head-heavy - most browser scenes should
show sites people actually visit. Measured over the v1 corpus that prior is very
strong: 630 browser scenes drew 50 distinct domains, google.com took 27.5%, and
18 domains covered 80%. Widening the catalog alone would not have moved that,
because the prior, not the list length, is what concentrates it.
"""

from __future__ import annotations

import re
from collections import Counter

from deskshot.browser_catalog import (
    WIDE_CATALOG_PATH,
    load_browser_catalog,
    sample_browser_catalog_state,
)


def _domains(n: int, **kw) -> Counter:
    got = Counter()
    for seed in range(n):
        state = sample_browser_catalog_state(seed, **kw)
        got[re.match(r"catalog:([^:]+):", state).group(1)] += 1
    return got


def _share_for(counts: Counter, fraction: float) -> int:
    total = sum(counts.values())
    cum = n = 0
    for _, c in counts.most_common():
        cum += c
        n += 1
        if cum >= fraction * total:
            return n
    return n


def test_uniform_is_far_more_diverse_than_the_default() -> None:
    """Distinct count is capped by the sample size, so concentration is the
    real measure: how many domains it takes to cover most of the draws."""
    n = 3000
    weighted, uniform = _domains(n), _domains(n, uniform=True)
    assert len(uniform) > 5 * len(weighted), (
        f"uniform {len(uniform)} vs weighted {len(weighted)} distinct domains"
    )
    assert _share_for(uniform, 0.8) > 10 * _share_for(weighted, 0.8), (
        f"80% of draws needs {_share_for(uniform, 0.8)} domains uniform vs "
        f"{_share_for(weighted, 0.8)} weighted"
    )


def test_uniform_has_no_dominant_domain() -> None:
    counts = _domains(3000, uniform=True)
    top_share = counts.most_common(1)[0][1] / sum(counts.values())
    assert top_share < 0.01, f"top domain took {100*top_share:.1f}%"


def test_the_default_sampler_is_unchanged() -> None:
    """The general corpus should keep its popularity prior."""
    counts = _domains(3000)
    assert _share_for(counts, 0.8) < 120, "the default prior got much flatter"
    assert counts.most_common(1)[0][0] in {"google.com", "microsoft.com", "facebook.com"}


def test_uniform_draws_from_the_wide_catalog() -> None:
    wide = {s.domain for s in load_browser_catalog(WIDE_CATALOG_PATH)}
    narrow = {s.domain for s in load_browser_catalog()}
    assert len(wide) > 10 * len(narrow), f"wide {len(wide)} narrow {len(narrow)}"
    drawn = set(_domains(2000, uniform=True))
    assert drawn - narrow, "uniform never left the narrow catalog"


def test_both_modes_stay_deterministic_in_the_seed() -> None:
    for kw in ({}, {"uniform": True}):
        assert sample_browser_catalog_state(1234, **kw) == \
               sample_browser_catalog_state(1234, **kw)


def test_state_ref_shape_is_unchanged() -> None:
    for kw in ({}, {"uniform": True}):
        state = sample_browser_catalog_state(77, **kw)
        assert re.fullmatch(r"catalog:[^:]+:(baseline|scrolled)", state), state


def test_a_missing_wide_catalog_falls_back_rather_than_crashing() -> None:
    from pathlib import Path
    sites = load_browser_catalog(Path("/nonexistent/catalog.csv"))
    assert sites, "fallback produced nothing"
