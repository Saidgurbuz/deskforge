from __future__ import annotations

from deskshot.postprocessing.quality import (
    build_stage_survival_summary,
    run_quality_checks,
)


def _elem(
    *,
    role: str = "push button",
    rect: dict[str, int] | None = None,
    visible_fragments: list[dict[str, int]] | None = None,
    is_occluded: bool = False,
    source: str = "app",
    app_name: str = "demo-app",
    parent_index: int | None = None,
    stack_idx: int | None = 2,
    dom_index: int = 0,
) -> dict[str, object]:
    rect = rect or {"x": 10, "y": 10, "w": 50, "h": 20}
    return {
        "role": role,
        "type": "Window" if role == "frame" else "Button",
        "rect": dict(rect),
        "_visibility_source_rect": dict(rect),
        "visible_fragments": visible_fragments if visible_fragments is not None else [dict(rect)],
        "is_occluded": is_occluded,
        "source": source,
        "app_name": app_name,
        "parent_index": parent_index,
        "children_indices": [],
        "_children_dom_indices": [],
        "_window_stack_index": stack_idx,
        "_dom_index": dom_index,
    }


def test_run_quality_checks_accepts_fragment_consistent_elements() -> None:
    window = _elem(role="frame", rect={"x": 0, "y": 0, "w": 300, "h": 200}, dom_index=0)
    button = _elem(parent_index=0, dom_index=1)
    entry = {
        **_elem(
            role="entry",
            rect={"x": 80, "y": 40, "w": 120, "h": 24},
            dom_index=2,
            parent_index=0,
        ),
        "type": "Text Input",
    }
    heading = {
        **_elem(
            role="heading",
            rect={"x": 40, "y": 80, "w": 160, "h": 30},
            dom_index=3,
            parent_index=0,
        ),
        "type": "Heading",
    }
    checks = run_quality_checks(
        [window, button, entry, heading],
        leaf_elements=[window, button, entry, heading],
        viewport_w=400,
        viewport_h=300,
        min_elements=4,
    )
    assert all(ok for _name, ok, _msg in checks)


def test_run_quality_checks_flags_missing_fragments_and_rect_mismatch() -> None:
    bad_fragments = _elem(visible_fragments=[], dom_index=0)
    bad_rect = _elem(
        rect={"x": 10, "y": 10, "w": 50, "h": 20},
        visible_fragments=[{"x": 10, "y": 10, "w": 40, "h": 20}],
        dom_index=1,
    )
    checks = dict((name, ok) for name, ok, _msg in run_quality_checks([bad_fragments, bad_rect]))
    assert checks["visible_fragments_present"] is False
    assert checks["non_occluded_rect_consistency"] is False


def test_run_quality_checks_flags_bad_leaf_window_ownership() -> None:
    window = _elem(role="frame", rect={"x": 0, "y": 0, "w": 300, "h": 200}, dom_index=0, app_name="good")
    child = _elem(parent_index=0, dom_index=1, app_name="wrong")
    checks = dict(
        (name, ok)
        for name, ok, _msg in run_quality_checks(
            [window, child],
            leaf_elements=[window, child],
            viewport_w=400,
            viewport_h=300,
        )
    )
    assert checks["leaf_window_ownership"] is False


def test_build_stage_survival_summary_counts_and_retention() -> None:
    unfiltered = [_elem(dom_index=0), _elem(dom_index=1), _elem(dom_index=2, source="desktop_chrome", app_name="caja")]
    filtered = [_elem(dom_index=0), _elem(dom_index=2, source="desktop_chrome", app_name="caja")]
    leaf = [_elem(dom_index=0)]

    summary = build_stage_survival_summary(unfiltered, filtered, leaf)

    assert summary["counts"] == {"unfiltered": 3, "filtered": 2, "leaf": 1}
    assert summary["retention"]["filtered_vs_unfiltered"] == 0.6667
    assert summary["retention"]["leaf_vs_filtered"] == 0.5
    assert summary["source_counts"]["unfiltered"]["app"] == 2
    assert summary["source_counts"]["unfiltered"]["desktop_chrome"] == 1
