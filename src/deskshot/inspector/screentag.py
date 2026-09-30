"""Map the serialized ScreenTag back onto the elements that produced it.

`<stem>.screentag.txt` is one flat string:

    <screentag><window><loc_8><loc_25><loc_306><loc_471>Title
      <select><loc_101>…</select><text_input>…</text_input></window></screentag>

Reading it beside the screenshot means answering "which box is *this* markup?"
in both directions, and the honest way to answer is a character-range index
built on the server, not a re-parse in the browser that hopes the order came out
the same.

Two rules make the index trustworthy rather than plausible:

- **The character ranges are read out of the stored file, never regenerated.**
  `parse_blocks` tokenises the actual bytes the pipeline wrote, so escaping
  (`&amp;`, `&lt;`) and the `<occluded/>` clip marker cannot shift a span: they
  are simply content inside the range they fall in. The tokeniser is safe
  precisely because `escape_screentag_text` guarantees the only `<` and `>` left
  in the file are markup.
- **The element behind a range is identified by what the serializer emitted, not
  by position.** Each block carries the tag name and the four `<loc_>` tokens,
  and `_basic_screentag` derives both deterministically from the element
  (`type`/`tag` lowercased, and `int(x / viewport * 500)` per edge). Matching on
  that pair means a reordering in the serializer shows up as an unmatched block
  instead of silently pointing at the wrong widget. Measured on
  `v235_final3/batch`, exactly 1 of 5,438 elements shares a (tag, loc)
  signature with another; those are resolved in document order and reported.

The inverse of the grid is exported too (`decode_loc`), because the only way to
check the link by eye is to see the rect a block's own `<loc_>` tokens decode to
next to the rect of the element it was mapped to.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: The `<loc_N>` grid. `_basic_screentag` emits `int(x / viewport_w * 500)`, so
#: a token is a truncated 0-500 normalised coordinate and decoding it back can
#: only be accurate to one cell (viewport/500 px, ~6px at 2880 wide).
LOC_GRID = 500

#: The root wrapper. It is not an element and gets no span.
ROOT_TAG = "screentag"

#: Emitted per visible fragment of an occluded element, inside the owner's head.
FRAGMENT_TAG = "fragment"

#: Exactly what `_state_tokens` can emit. Everything else that looks
#: self-closing is *content*: `<occluded/>` is deliberately written inside text
#: to mark where occlusion removed characters, and must stay part of the text.
STATE_TOKENS = frozenset(
    {"checked", "unchecked", "expanded", "collapsed", "selected", "disabled"}
)

#: Tag names are `(type or tag).replace(" ", "_").lower()`, so they are
#: identifier-ish; the extra punctuation is tolerance, not expectation.
_TOKEN = re.compile(r"<(/?)([A-Za-z0-9_.:\-]+)(/?)>")
_LOC = re.compile(r"^loc_(\d+)$")
#: Used as lookahead to tell an element from a labelled token. See `parse_blocks`.
_LOC_AHEAD = re.compile(r"<loc_\d+>")


# --------------------------------------------------------------------------
# the grid


def loc_tokens(rect: Optional[Dict[str, Any]], viewport_w: int, viewport_h: int) -> List[int]:
    """The four `<loc_>` values `_basic_screentag` would emit for this rect.

    Kept identical to the serializer down to the truncation: `int()` rather than
    `round()`, and left/top from the rect origin with right/bottom from origin
    plus size.
    """
    rect = rect or {}
    if not viewport_w or not viewport_h:
        return [0, 0, 0, 0]
    x = float(rect.get("x") or 0)
    y = float(rect.get("y") or 0)
    w = float(rect.get("w") or 0)
    h = float(rect.get("h") or 0)
    return [
        int(x / viewport_w * LOC_GRID),
        int(y / viewport_h * LOC_GRID),
        int((x + w) / viewport_w * LOC_GRID),
        int((y + h) / viewport_h * LOC_GRID),
    ]


def decode_loc(loc: Sequence[int], viewport_w: int, viewport_h: int) -> Dict[str, int]:
    """Turn four `<loc_>` tokens back into a rect in screenshot pixels.

    The inverse of the emission above, and lossy in the same direction: the
    serializer truncated, so a decoded edge is the *start* of the grid cell the
    real edge fell in and can sit up to one cell (viewport/500 px) short of it.
    That is why the UI shows this beside the element's own rect rather than
    instead of it.
    """
    if len(loc) < 4 or not viewport_w or not viewport_h:
        return {"x": 0, "y": 0, "w": 0, "h": 0}
    left = loc[0] * viewport_w / float(LOC_GRID)
    top = loc[1] * viewport_h / float(LOC_GRID)
    right = loc[2] * viewport_w / float(LOC_GRID)
    bottom = loc[3] * viewport_h / float(LOC_GRID)
    return {
        "x": int(round(left)),
        "y": int(round(top)),
        "w": max(0, int(round(right - left))),
        "h": max(0, int(round(bottom - top))),
    }


def element_tag(element: Dict[str, Any]) -> str:
    """The tag name `_basic_screentag` gives this element."""
    name = element.get("type") or element.get("tag") or "unknown"
    return str(name).replace(" ", "_").lower()


def element_signature(
    element: Dict[str, Any], viewport_w: int, viewport_h: int
) -> Tuple[str, Tuple[int, int, int, int]]:
    tokens = loc_tokens(element.get("rect"), viewport_w, viewport_h)
    return element_tag(element), (tokens[0], tokens[1], tokens[2], tokens[3])


# --------------------------------------------------------------------------
# parsing


def parse_blocks(text: str) -> List[Dict[str, Any]]:
    """Every element block in the tag, with the character ranges it occupies.

    Ranges, all offsets into `text`:

    - `start` .. `end` - the whole block including its children and its closing
      tag. This is what "the cursor is inside this element" means, and nesting
      makes the innermost containing block the answer.
    - `head_end` - where the element's own text begins, i.e. after the `<loc_>`
      tokens, any `<fragment>` and any state token.
    - `text_end` - where its own text stops: the first child's `<`, or its
      closing tag. `head_end` .. `text_end` is the element's own text, marker
      and entities included.

    Not every `<tag>` is an element. A window's `<title>Notes</title>` is a
    *labelled token* describing the window, and the serializer may grow more of
    them. They are told apart structurally rather than by name: an element block
    always emits its four `<loc_>` immediately after its open tag, so an open
    tag not followed by one is a label, and is collected into the owning
    element's head instead of becoming a block of its own. Hard-coding the
    names would mean the next token added upstream silently appeared as an
    element that matches nothing.

    Malformed input is survived rather than rejected: a stray closing tag is
    ignored and anything still open at the end is closed at EOF, because a
    truncated capture should still be readable in the inspector.
    """
    blocks: List[Dict[str, Any]] = []
    stack: List[int] = []
    labels: List[Tuple[str, int]] = []
    fragment_depth = 0
    cursor = 0

    def current() -> Optional[Dict[str, Any]]:
        return blocks[stack[-1]] if stack else None

    def begin_content(block: Optional[Dict[str, Any]], at: int) -> None:
        if block is not None and block["head_end"] is None:
            block["head_end"] = at

    def close(block: Dict[str, Any], at: int, end: int) -> None:
        begin_content(block, at)
        if block["text_end"] is None:
            block["text_end"] = at
        block["end"] = end

    for match in _TOKEN.finditer(text):
        gap_start = cursor
        gap = text[gap_start:match.start()]
        cursor = match.end()

        closing, raw, selfclose = match.group(1), match.group(2), match.group(3)
        name = raw.lower()

        if labels:
            # Inside a labelled token: its content is the label's, not the
            # element's, so it must not end the element's head.
            if closing and name == labels[-1][0]:
                label_name, label_start = labels.pop()
                owner = current()
                if owner is not None:
                    owner["labels"].append([label_name, text[label_start:match.start()]])
            elif not closing and not selfclose:
                labels.append((name, match.end()))
            continue

        if gap.strip():
            # Text of the innermost open element. It may be the first thing
            # after the head, which is what ends the head.
            begin_content(current(), gap_start)

        if name == ROOT_TAG:
            continue

        loc = _LOC.match(name)
        if loc is not None and not closing:
            block = current()
            if block is not None:
                value = int(loc.group(1))
                if fragment_depth and block["fragments"]:
                    block["fragments"][-1].append(value)
                elif block["head_end"] is None:
                    block["loc"].append(value)
            continue

        if name == FRAGMENT_TAG:
            block = current()
            if closing:
                fragment_depth = max(0, fragment_depth - 1)
            elif block is not None and block["head_end"] is None:
                fragment_depth += 1
                block["fragments"].append([])
            continue

        if selfclose:
            block = current()
            if block is None:
                continue
            if name in STATE_TOKENS and block["head_end"] is None:
                block["states"].append(name)
            else:
                # `<occluded/>` and anything unrecognised are content.
                begin_content(block, match.start())
            continue

        if not closing:
            if _LOC_AHEAD.match(text, match.end()) is None:
                # A labelled token (`<title>`), not an element.
                if current() is not None:
                    labels.append((name, match.end()))
                continue
            parent = current()
            if parent is not None:
                begin_content(parent, match.start())
                if parent["text_end"] is None:
                    parent["text_end"] = match.start()
            block = {
                "index": len(blocks),
                "tag": name,
                "start": match.start(),
                "open_end": match.end(),
                "head_end": None,
                "text_end": None,
                "end": None,
                "loc": [],
                "fragments": [],
                "states": [],
                "labels": [],
                "depth": len(stack),
                "parent": stack[-1] if stack else -1,
            }
            blocks.append(block)
            stack.append(block["index"])
            continue

        # A closing tag. Pop to the matching open one; ignore it if there is
        # none, rather than unwinding the whole document on one stray token.
        target = None
        for position in range(len(stack) - 1, -1, -1):
            if blocks[stack[position]]["tag"] == name:
                target = position
                break
        if target is None:
            continue
        while len(stack) > target + 1:
            close(blocks[stack.pop()], match.start(), match.start())
        close(blocks[stack.pop()], match.start(), match.end())

    while stack:
        close(blocks[stack.pop()], len(text), len(text))
    return blocks


# --------------------------------------------------------------------------
# the index


def build_index(
    text: str,
    elements: Sequence[Dict[str, Any]],
    viewport_w: int,
    viewport_h: int,
    keys: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Character ranges in `text` paired with the elements that produced them.

    Matching is by (tag name, four `<loc_>` values), consumed in document order.
    A block whose signature no element carries is returned unmatched with
    `element: null` rather than attached to a near neighbour - a wrong link is
    worse than a missing one when the whole point is to check annotations.

    A signature can legitimately be emitted twice: `_basic_screentag` serializes
    a child once per window that lists it, so the second block reuses the same
    element and is flagged `repeat`.
    """
    blocks = parse_blocks(text)
    pending: Dict[Tuple[str, Tuple[int, int, int, int]], List[int]] = {}
    for position, element in enumerate(elements):
        pending.setdefault(element_signature(element, viewport_w, viewport_h), []).append(position)
    remaining = {signature: list(positions) for signature, positions in pending.items()}

    spans: List[Dict[str, Any]] = []
    matched = 0
    repeats = 0
    ambiguous = 0
    used = set()
    for block in blocks:
        loc = list(block["loc"])[:4]
        signature = (block["tag"], tuple(loc + [0] * (4 - len(loc)))) if loc else (block["tag"], (0, 0, 0, 0))
        queue = remaining.get(signature) or []
        position: Optional[int] = None
        repeat = False
        if queue:
            if len(queue) > 1 or len(pending.get(signature) or []) > 1:
                ambiguous += 1
            position = queue.pop(0)
        else:
            # Exhausted: either a repeat emission of an element already bound,
            # or a block no element accounts for.
            candidates = pending.get(signature) or []
            if candidates:
                position = candidates[-1]
                repeat = True
                repeats += 1
        span = {
            "start": block["start"],
            "end": block["end"],
            "head_end": block["head_end"],
            "text_end": block["text_end"],
            "tag": block["tag"],
            "depth": block["depth"],
            "parent": block["parent"],
            "loc": loc,
            "rect": decode_loc(loc, viewport_w, viewport_h) if len(loc) == 4 else None,
            "states": block["states"],
            "labels": block["labels"],
            "fragments": [
                decode_loc(fragment, viewport_w, viewport_h)
                for fragment in block["fragments"]
                if len(fragment) == 4
            ],
            "element": position,
            "key": None,
            "repeat": repeat,
        }
        if position is not None:
            matched += 1
            used.add(position)
            if keys is not None and position < len(keys):
                span["key"] = keys[position]
        spans.append(span)

    return {
        "spans": spans,
        "stats": {
            "blocks": len(blocks),
            "elements": len(elements),
            "matched": matched,
            "unmatched_blocks": len(blocks) - matched,
            "unmatched_elements": len(elements) - len(used),
            "repeats": repeats,
            "ambiguous": ambiguous,
        },
    }


def innermost(spans: Sequence[Dict[str, Any]], offset: int) -> Optional[int]:
    """Index of the deepest span containing `offset`, or None.

    The server does not need this - the browser resolves a caret position
    itself - but the alignment is only worth trusting if it is testable, and
    "offset N lands in the block for element E" is the assertion that says so.
    """
    found = None
    best = -1
    for position, span in enumerate(spans):
        start, end = span.get("start"), span.get("end")
        if start is None or end is None:
            continue
        if start <= offset < end and span.get("depth", 0) >= best:
            found = position
            best = span.get("depth", 0)
    return found
