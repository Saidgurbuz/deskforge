"""Keep a container when the toolkit never published what is inside it.

The leaf export drops structural containers on purpose: a status bar, a tool bar
or a panel is not itself a control, and annotating it as well as its children
would box the same pixels twice. That reasoning holds exactly as long as the
children exist.

Some toolkits publish the container and nothing else. Measured on pluma: a live
walk returns 40 nodes, and its `status bar` comes back with **kids=0** while the
screen plainly draws four widgets inside it - a Markdown menu, a Tab Width menu,
"Ln 1, Col 1" and "INS". The container was then dropped for having no
meaningful text of its own, its absent children could not stand in for it, and a
452x36 band of visibly drawn UI reached ground truth annotated by nothing at
all.

So the rule is completed rather than changed: a container is redundant when
something inside it is annotated, and is the only available annotation when
nothing is. In that case it is kept - but only if it actually draws something,
which is decided from pixels, because a container with no children and no
content is exactly the empty box the leaf rule was written to exclude.

This is toolkit-agnostic by construction: it asks whether *this* container ended
up with annotated descendants, never which app it belongs to, so any widget set
that hides its internals is covered without being named here.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set

#: Never rescued: a window is the area whose contents we are asking about, and
#: annotating the frame itself would cover every hole in the window at once.
WINDOW_ROLES = frozenset({"frame", "window", "dialog", "alert", "file chooser"})


def _rect(elem: Dict[str, Any]):
    r = elem.get("rect")
    if not isinstance(r, dict):
        return None
    try:
        x, y, w, h = int(r["x"]), int(r["y"]), int(r["w"]), int(r["h"])
    except (KeyError, TypeError, ValueError):
        return None
    return (x, y, w, h) if w > 0 and h > 0 else None


def _is_inside(inner, outer, *, slack: int = 2) -> bool:
    ix, iy, iw, ih = inner
    ox, oy, ow, oh = outer
    return (ix >= ox - slack and iy >= oy - slack
            and ix + iw <= ox + ow + slack and iy + ih <= oy + oh + slack)


def find_unpublished_containers(
    filtered_elements: List[Dict[str, Any]],
    leaf_elements: List[Dict[str, Any]],
    gray,
    *,
    is_container,
    draws_content,
) -> List[Dict[str, Any]]:
    """Containers that ended up with no annotated descendant but draw something.

    `is_container` and `draws_content` are injected so this module owns the rule
    and not the definitions - the container roles live with the leaf builder,
    and "draws something" is the same flatness test the blank-widget gate uses.
    """
    if gray is None or not filtered_elements:
        return []

    # Containment, not parent links. The three element lists are indexed
    # separately and the filtered tree arrives with `_children_dom_indices`
    # emptied on exactly these nodes, so a descendant walk finds nothing and
    # every container looks unrepresented - it rescued a pluma split pane that
    # holds the annotated text editor, and a chromium section, boxing pixels
    # that were already covered. Asking "is any annotated element inside this
    # rect" needs no link to be intact.
    leaf_rects = []
    for elem in leaf_elements:
        role = str(elem.get("role") or "").strip().lower()
        if role in WINDOW_ROLES:
            continue
        box = _rect(elem)
        if box is not None:
            leaf_rects.append(box)

    rescued: List[Dict[str, Any]] = []
    for elem in filtered_elements:
        role = str(elem.get("role") or "").strip().lower()
        if role in WINDOW_ROLES or not is_container(role):
            continue
        box = _rect(elem)
        if box is None:
            continue
        if any(_is_inside(leaf, box) for leaf in leaf_rects):
            continue
        if not draws_content(elem):
            continue
        rescued.append(elem)

    # A rescued container may sit inside another rescued one - a panel holding a
    # status bar, both unpublished. Keep only the innermost so the same pixels
    # are not boxed at two depths.
    boxes = [(_rect(e), e) for e in rescued]
    innermost = [
        elem for box, elem in boxes
        if not any(other is not elem and _is_inside(other_box, box)
                   for other_box, other in boxes)
    ]
    return innermost
