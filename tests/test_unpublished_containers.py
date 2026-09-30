"""A container is redundant only while something inside it is annotated.

The leaf export drops structural containers on purpose - a status bar is not
itself a control, and boxing it as well as its children would cover the same
pixels twice. That holds until a toolkit publishes the container and nothing
else. A live walk of pluma returns 40 nodes and its `status bar` comes back with
kids=0, while the screen draws a Markdown menu, a Tab Width menu, "Ln 1, Col 1"
and "INS" inside it. The container was dropped for lacking text of its own, its
absent children could not stand in for it, and a 452x36 band of drawn UI was
annotated by nothing.
"""

import numpy as np

from deskshot.extraction.unpublished_containers import find_unpublished_containers


def _gray(size=(200, 200)):
    return np.full(size, 240, dtype=np.int32)


def _with_marks(gray):
    """Widget-like marks - a uniformly filled band is *not* content."""
    for x0 in (30, 60, 100):
        gray[155:170, x0:x0 + 10] = 0
    return gray


CONTAINERS = {"status bar", "panel", "tool bar"}


def _is_container(role):
    return role in CONTAINERS


def _run(filtered, leaf, gray):
    from deskshot.extraction.blank_widgets import draws_content

    return find_unpublished_containers(
        filtered, leaf, gray,
        is_container=_is_container,
        draws_content=lambda e: draws_content(e, gray),
    )


def _node(index, role, rect, children=()):
    x, y, w, h = rect
    return {"_dom_index": index, "role": role, "app_name": "pluma",
            "rect": {"x": x, "y": y, "w": w, "h": h},
            "_children_dom_indices": list(children)}


def test_a_container_whose_children_were_never_published_is_kept():
    gray = _with_marks(_gray())
    bar = _node(1, "status bar", (20, 150, 160, 30))
    assert [e["_dom_index"] for e in _run([bar], [], gray)] == [1]


def test_a_container_with_an_annotated_child_is_not_kept():
    """The normal case: the children carry the annotation, so the box would
    cover the same pixels twice."""
    gray = _with_marks(_gray())
    bar = _node(1, "status bar", (20, 150, 160, 30), children=[9])
    leaf = [{"_source_dom_index": 9, "role": "label",
             "rect": {"x": 30, "y": 155, "w": 10, "h": 15}}]
    assert _run([bar], leaf, gray) == []


def test_an_empty_container_is_not_kept():
    """No children and nothing drawn is exactly the empty box the leaf rule
    exists to exclude."""
    gray = _gray()
    bar = _node(1, "status bar", (20, 150, 160, 30))
    assert _run([bar], [], gray) == []


def test_a_uniformly_filled_container_is_not_content():
    """Painted one colour is still nothing drawn - the same flatness test the
    blank-widget gate uses, which a fill would otherwise defeat."""
    gray = _gray()
    gray[150:180, 20:180] = 0
    bar = _node(1, "status bar", (20, 150, 160, 30))
    assert _run([bar], [], gray) == []


def test_a_window_is_never_rescued():
    """Rescuing a frame would paper over every hole in the window at once."""
    gray = _with_marks(_gray())
    frame = _node(1, "frame", (0, 0, 200, 200))
    assert _run([frame], [], gray) == []


def test_only_the_innermost_unpublished_container_is_kept():
    """A panel holding a status bar, both unpublished: boxing both would
    annotate the same pixels at two depths."""
    gray = _with_marks(_gray())
    panel = _node(1, "panel", (10, 140, 180, 50), children=[2])
    bar = _node(2, "status bar", (20, 150, 160, 30))
    assert [e["_dom_index"] for e in _run([panel, bar], [], gray)] == [2]


def test_a_deep_annotated_descendant_still_counts():
    """The child need not be direct - anything annotated below it means the
    container is already represented."""
    gray = _with_marks(_gray())
    panel = _node(1, "panel", (10, 140, 180, 50), children=[2])
    bar = _node(2, "status bar", (20, 150, 160, 30), children=[9])
    leaf = [{"_source_dom_index": 9, "role": "label",
             "rect": {"x": 30, "y": 155, "w": 10, "h": 15}}]
    assert _run([panel, bar], leaf, gray) == []


def test_no_screenshot_means_no_rescue():
    bar = _node(1, "status bar", (20, 150, 160, 30))
    assert find_unpublished_containers(
        [bar], [], None, is_container=_is_container, draws_content=lambda e: True
    ) == []


def test_a_rescued_container_gets_an_index_that_is_free_in_the_leaf_export():
    """The leaf export is re-indexed from zero, so a container copied out of the
    filtered tree arrives with an index that already belongs to a leaf.
    `_basic_screentag` keys elements by `_dom_index`, so the clone silently
    replaced a real element in the markup - 29 leaf elements in one 21-capture
    batch never reached the ScreenTag.
    """
    from deskshot.extraction.run_extraction import _rescue_unpublished_containers

    gray = _with_marks(_gray())
    filtered = [_node(1, "status bar", (20, 150, 160, 30))]
    leaf = [{"_dom_index": 0, "role": "label", "rect": {"x": 0, "y": 0, "w": 5, "h": 5}},
            {"_dom_index": 1, "role": "label", "rect": {"x": 6, "y": 0, "w": 5, "h": 5}}]

    _rescue_unpublished_containers(filtered, leaf, gray)

    indices = [e["_dom_index"] for e in leaf]
    assert len(indices) == len(set(indices)), indices
    assert leaf[-1]["_source_dom_index"] == 1
