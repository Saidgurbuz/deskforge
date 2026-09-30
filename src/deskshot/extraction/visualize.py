"""Simple visualization utility for extraction outputs."""

from __future__ import annotations

import colorsys
from pathlib import Path
from typing import Any, Dict, List, Tuple

from PIL import Image, ImageDraw


Color = Tuple[int, int, int, int]


def get_element_visual_style(elem: Dict[str, Any]) -> Dict[str, Color | int]:
    """Return visualization colors for an element."""
    source = elem.get("source")

    if source == "desktop_chrome":
        outline = (255, 140, 0, 210)
    else:
        outline = (50, 200, 50, 210)

    return {"outline": outline, "label": outline[:3] + (255,), "fill": (0, 0, 0, 0), "width": 2}


def render_elements_visualization(
    img: Image.Image,
    elements: List[Dict[str, Any]],
) -> Image.Image:
    """Render element boxes onto an image and return it."""
    img = img.convert("RGB")
    draw = ImageDraw.Draw(img, "RGBA")

    for elem in elements:
        rect = elem.get("rect", {})
        x = int(rect.get("x", 0))
        y = int(rect.get("y", 0))
        w = int(rect.get("w", 0))
        h = int(rect.get("h", 0))
        if w < 2 or h < 2:
            continue

        style = get_element_visual_style(elem)
        box = [x, y, x + w, y + h]

        draw.rectangle(box, outline=style["outline"], width=int(style["width"]))

        label = elem.get("type") or elem.get("role") or ""
        if label:
            draw.text((x + 2, y + 2), str(label)[:32], fill=style["label"])

    return img


def save_elements_visualization(
    screenshot_path: Path,
    elements: List[Dict[str, Any]],
    out_path: Path,
) -> None:
    """Draw element boxes and save visualization PNG."""
    img = render_elements_visualization_v2(Image.open(screenshot_path), elements)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)


def _occlusion_palette(index: int, *, alpha: int) -> Color:
    hue = ((index * 0.1618) % 1.0)
    sat = 0.65
    val = 0.95
    r, g, b = colorsys.hsv_to_rgb(hue, sat, val)
    return (int(r * 255), int(g * 255), int(b * 255), alpha)


def _alpha_label(index: int) -> str:
    base = ""
    current = index
    while True:
        current, rem = divmod(current, 26)
        base = chr(ord("A") + rem) + base
        if current == 0:
            break
        current -= 1
    return base


def _draw_center_label(
    draw: ImageDraw.ImageDraw,
    rect: Dict[str, Any],
    label: str,
    *,
    fill: Color = (255, 255, 255, 220),
    text_fill: Color = (0, 0, 0, 255),
) -> None:
    x = int(rect.get("x", 0))
    y = int(rect.get("y", 0))
    w = int(rect.get("w", 0))
    h = int(rect.get("h", 0))
    if w < 18 or h < 12:
        return
    bbox = draw.textbbox((0, 0), label)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = x + max(2, (w - tw) // 2)
    ty = y + max(2, (h - th) // 2)
    pad = 2
    draw.rounded_rectangle(
        [tx - pad, ty - pad, tx + tw + pad, ty + th + pad],
        radius=3,
        fill=fill,
    )
    draw.text((tx, ty), label, fill=text_fill)


def render_occlusion_visualization(
    img: Image.Image,
    elements: List[Dict[str, Any]],
) -> Image.Image:
    """Render partially occluded elements with full rect + visible fragments."""
    canvas = img.convert("RGBA")
    draw = ImageDraw.Draw(canvas, "RGBA")

    occluded = [
        elem for elem in elements
        if elem.get("is_occluded") and isinstance(elem.get("visible_fragments"), list)
    ]
    occluded.sort(key=lambda elem: int(elem.get("reading_order_index", 10**9)))

    for idx, elem in enumerate(occluded):
        rect = elem.get("rect", {})
        x = int(rect.get("x", 0))
        y = int(rect.get("y", 0))
        w = int(rect.get("w", 0))
        h = int(rect.get("h", 0))
        if w < 2 or h < 2:
            continue

        group_label = _alpha_label(idx)
        base = _occlusion_palette(idx, alpha=26)
        outline = _occlusion_palette(idx, alpha=120)
        draw.rectangle([x, y, x + w, y + h], fill=base, outline=outline, width=2)
        _draw_center_label(
            draw,
            rect,
            group_label,
            fill=(255, 255, 255, 180),
            text_fill=outline[:3] + (255,),
        )

        for frag_idx, fragment in enumerate(elem.get("visible_fragments") or []):
            fx = int(fragment.get("x", 0))
            fy = int(fragment.get("y", 0))
            fw = int(fragment.get("w", 0))
            fh = int(fragment.get("h", 0))
            if fw < 2 or fh < 2:
                continue
            frag_color = _occlusion_palette(idx * 7 + frag_idx + 1, alpha=58)
            frag_outline = _occlusion_palette(idx * 7 + frag_idx + 1, alpha=175)
            draw.rectangle(
                [fx, fy, fx + fw, fy + fh],
                fill=frag_color,
                outline=frag_outline,
                width=2,
            )
            _draw_center_label(
                draw,
                fragment,
                f"{group_label}{frag_idx + 1}",
                fill=(255, 255, 255, 190),
                text_fill=frag_outline[:3] + (255,),
            )

    return canvas.convert("RGB")


def save_occlusion_visualization(
    screenshot_path: Path,
    elements: List[Dict[str, Any]],
    out_path: Path,
) -> None:
    """Draw occluded element overlays and save visualization PNG."""
    img = render_occlusion_visualization(Image.open(screenshot_path), elements)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)


def _element_anchor(elem: Dict[str, Any]) -> Tuple[float, float]:
    rect = elem.get("rect", {})
    x = float(rect.get("x", 0))
    y = float(rect.get("y", 0))
    w = float(rect.get("w", 0))
    h = float(rect.get("h", 0))
    return x + (w / 2.0), y + (h / 2.0)


def _draw_arrow(
    draw: ImageDraw.ImageDraw,
    start: Tuple[float, float],
    end: Tuple[float, float],
    *,
    color: Color = (80, 160, 255, 160),
    width: int = 2,
) -> None:
    sx, sy = start
    ex, ey = end
    draw.line((sx, sy, ex, ey), fill=color, width=width)

    dx = ex - sx
    dy = ey - sy
    length = (dx * dx + dy * dy) ** 0.5
    if length < 6:
        return

    ux = dx / length
    uy = dy / length
    arrow_len = min(10.0, max(6.0, length * 0.08))
    back_x = ex - (ux * arrow_len)
    back_y = ey - (uy * arrow_len)
    perp_x = -uy
    perp_y = ux
    wing = arrow_len * 0.45
    draw.polygon(
        [
            (ex, ey),
            (back_x + perp_x * wing, back_y + perp_y * wing),
            (back_x - perp_x * wing, back_y - perp_y * wing),
        ],
        fill=color,
    )


def render_reading_order_visualization(
    img: Image.Image,
    elements: List[Dict[str, Any]],
) -> Image.Image:
    """Render boxes plus reading-order indices onto an image."""
    img = render_elements_visualization_v2(img, elements)
    draw = ImageDraw.Draw(img, "RGBA")

    ordered = sorted(
        (
            elem for elem in elements
            if isinstance(elem.get("reading_order_index"), int)
        ),
        key=lambda elem: int(elem["reading_order_index"]),
    )

    for prev, curr in zip(ordered, ordered[1:]):
        _draw_arrow(draw, _element_anchor(prev), _element_anchor(curr))

    for elem in ordered:
        rect = elem.get("rect", {})
        x = int(rect.get("x", 0))
        y = int(rect.get("y", 0))
        w = int(rect.get("w", 0))
        h = int(rect.get("h", 0))
        if w < 2 or h < 2:
            continue

        label = str(int(elem["reading_order_index"]))
        bbox = draw.textbbox((x + 3, y + 3), label)
        draw.rectangle(
            [bbox[0] - 2, bbox[1] - 1, bbox[2] + 2, bbox[3] + 1],
            fill=(255, 255, 255, 220),
        )
        draw.text((x + 3, y + 3), label, fill=(0, 0, 0, 255))

    return img


def save_reading_order_visualization(
    screenshot_path: Path,
    elements: List[Dict[str, Any]],
    out_path: Path,
) -> None:
    """Draw reading-order indices and save visualization PNG."""
    img = render_reading_order_visualization(Image.open(screenshot_path), elements)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)


# --- occlusion-aware rendering ----------------------------------------------
#
# The first occlusion visualization gave every element and every fragment its
# own colour from a rotating palette, plus a letter label. On a dense scene that
# is a few hundred boxes in a few dozen hues, and the one thing it never showed
# was the thing being visualized: which pixels are covered.
#
# Here colour carries a single meaning - occlusion state - and the *covered*
# region is what gets shaded, so the eye lands on what is hidden rather than on
# an arbitrary hue.

#: Green fully visible, amber partly covered, red entirely covered. Three
#: colours, one axis of meaning, and the same order everywhere they appear.
STATE_COLORS: Dict[str, Tuple[int, int, int]] = {
    "none": (46, 184, 92),
    "partial": (240, 158, 30),
    "hidden": (226, 66, 62),
}

#: A side counts as truncated only if the visible region falls short of it by
#: more than this, so a one-pixel rounding difference does not paint an edge.
EDGE_TOLERANCE = 2


def occluded_edges(
    rect: Dict[str, Any],
    fragments: List[Dict[str, Any]],
    *,
    tolerance: int = EDGE_TOLERANCE,
) -> set:
    """Which sides of `rect` the visible fragments do not reach.

    A truncated side is where content has been cut away, so drawing that side in
    a different colour says *where* an element was clipped rather than merely
    that it was. Returns a subset of {"left", "top", "right", "bottom"}.
    """
    x, y = int(rect.get("x", 0)), int(rect.get("y", 0))
    w, h = int(rect.get("w", 0)), int(rect.get("h", 0))
    usable = [f for f in fragments if isinstance(f, dict) and f.get("w") and f.get("h")]
    if not usable:
        return {"left", "top", "right", "bottom"}

    left = min(int(f["x"]) for f in usable)
    top = min(int(f["y"]) for f in usable)
    right = max(int(f["x"]) + int(f["w"]) for f in usable)
    bottom = max(int(f["y"]) + int(f["h"]) for f in usable)

    cut = set()
    if left > x + tolerance:
        cut.add("left")
    if top > y + tolerance:
        cut.add("top")
    if right < x + w - tolerance:
        cut.add("right")
    if bottom < y + h - tolerance:
        cut.add("bottom")
    return cut


def _element_state(elem: Dict[str, Any]) -> str:
    state = str(elem.get("occlusion_state") or "").strip().lower()
    if state in STATE_COLORS:
        return state
    return "partial" if elem.get("is_occluded") else "none"


def _draw_edges(
    draw: "ImageDraw.ImageDraw",
    rect: Dict[str, Any],
    cut: set,
    *,
    intact: Color,
    truncated: Color,
    width: int = 2,
) -> None:
    """Draw a box one side at a time, colouring the truncated sides apart."""
    x, y = int(rect.get("x", 0)), int(rect.get("y", 0))
    w, h = int(rect.get("w", 0)), int(rect.get("h", 0))
    sides = {
        "top": (x, y, x + w, y),
        "bottom": (x, y + h, x + w, y + h),
        "left": (x, y, x, y + h),
        "right": (x + w, y, x + w, y + h),
    }
    for name, line in sides.items():
        draw.line(line, fill=truncated if name in cut else intact, width=width)


def render_elements_visualization_v2(
    img: Image.Image,
    elements: List[Dict[str, Any]],
    *,
    show_labels: bool = True,
) -> Image.Image:
    """Element boxes whose edges say where the element was cut.

    A fully visible element is drawn in green on all four sides. A partly
    covered one keeps green on the sides that are intact and switches to amber
    on the sides the visible region does not reach - so a box that is clipped on
    its right, which used to look identical to one that is not, now reads as
    clipped at a glance and says on which side.
    """
    img = img.convert("RGB")
    draw = ImageDraw.Draw(img, "RGBA")

    for elem in elements:
        rect = elem.get("rect", {})
        if int(rect.get("w", 0)) < 2 or int(rect.get("h", 0)) < 2:
            continue

        state = _element_state(elem)
        if elem.get("source") == "desktop_chrome" and state == "none":
            base = (120, 130, 240)          # chrome stays distinguishable
        else:
            base = STATE_COLORS[state]
        cut = occluded_edges(rect, elem.get("visible_fragments") or []) if state != "none" else set()

        _draw_edges(
            draw, rect, cut,
            intact=base + (215,),
            truncated=STATE_COLORS["partial"] + (255,),
        )
        if show_labels:
            label = elem.get("type") or elem.get("role") or ""
            if label:
                draw.text(
                    (int(rect.get("x", 0)) + 2, int(rect.get("y", 0)) + 2),
                    str(label)[:32], fill=base + (255,),
                )
    return img


def render_occlusion_visualization_v2(
    img: Image.Image,
    elements: List[Dict[str, Any]],
) -> Image.Image:
    """Shade what is covered, outline what is visible, and count both.

    The covered region is `rect` minus the visible fragments, drawn as a red
    wash; the visible fragments are outlined in green. Nothing is labelled per
    element, because on a dense scene those labels are the noise that made the
    previous version unreadable - the legend carries the meaning instead.
    """
    canvas = img.convert("RGBA")
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")

    counts = {"none": 0, "partial": 0, "hidden": 0}
    for elem in elements:
        rect = elem.get("rect", {})
        w, h = int(rect.get("w", 0)), int(rect.get("h", 0))
        if w < 2 or h < 2:
            continue
        state = _element_state(elem)
        counts[state] += 1
        if state == "none":
            continue

        x, y = int(rect.get("x", 0)), int(rect.get("y", 0))
        fragments = [
            f for f in (elem.get("visible_fragments") or [])
            if isinstance(f, dict) and int(f.get("w", 0)) > 0 and int(f.get("h", 0)) > 0
        ]
        # The whole box washed red, then the visible parts cut back out of the
        # wash - so what remains tinted is exactly what is hidden.
        draw.rectangle([x, y, x + w, y + h], fill=STATE_COLORS["hidden"] + (58,))
        for frag in fragments:
            fx, fy = int(frag["x"]), int(frag["y"])
            fw, fh = int(frag["w"]), int(frag["h"])
            draw.rectangle([fx, fy, fx + fw, fy + fh], fill=(0, 0, 0, 0))
            draw.rectangle(
                [fx, fy, fx + fw, fy + fh],
                outline=STATE_COLORS["none"] + (200,), width=2,
            )
        _draw_edges(
            draw, rect, occluded_edges(rect, fragments),
            intact=STATE_COLORS["partial"] + (150,),
            truncated=STATE_COLORS["hidden"] + (235,),
        )

    canvas = Image.alpha_composite(canvas, overlay)
    _draw_legend(canvas, counts)
    return canvas.convert("RGB")


def _draw_legend(canvas: Image.Image, counts: Dict[str, int]) -> None:
    """A fixed key in the corner, so the colours do not have to be guessed."""
    draw = ImageDraw.Draw(canvas, "RGBA")
    rows = [
        ("visible", "none"),
        ("partly covered", "partial"),
        ("fully covered", "hidden"),
    ]
    pad, box, line_h = 10, 12, 18
    width = 190
    height = pad * 2 + line_h * len(rows)
    x0, y0 = 12, 12
    draw.rectangle([x0, y0, x0 + width, y0 + height], fill=(18, 18, 22, 205))
    for index, (label, key) in enumerate(rows):
        ty = y0 + pad + index * line_h
        draw.rectangle(
            [x0 + pad, ty, x0 + pad + box, ty + box],
            fill=STATE_COLORS[key] + (235,),
        )
        draw.text(
            (x0 + pad + box + 8, ty),
            f"{label}: {counts.get(key, 0)}",
            fill=(238, 238, 242, 255),
        )


def save_occlusion_visualization_v2(
    screenshot_path: Path,
    elements: List[Dict[str, Any]],
    out_path: Path,
) -> None:
    img = render_occlusion_visualization_v2(Image.open(screenshot_path), elements)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)


# --- occlusion, third attempt -----------------------------------------------
#
# The second version drew window-level and element-level occlusion identically:
# a flat red wash over both. Two problems followed. A translucent red over a
# blue window reads as "this window is purple" rather than "this region is
# hidden", and the one fact a reader wants first - which part of this window is
# behind another window - was drawn in the same ink as twenty-six clipped
# labels, so it disappeared into them.
#
# So the two levels are now drawn differently and in order: windows first, as a
# hatched mask that cannot be mistaken for the app's own colour, then elements
# as outlines only.

WINDOW_ROLES = {"frame", "window", "dialog", "alert"}


def subtract_rects(
    rect: Dict[str, Any],
    holes: List[Dict[str, Any]],
) -> List[Tuple[int, int, int, int]]:
    """`rect` minus `holes`, as a list of disjoint boxes.

    Used to get the *covered* region from an element's rect and its visible
    fragments, so the overlay marks only what is actually hidden. Drawing the
    whole rect and relying on the fragments to be painted back over it is what
    made the previous version tint visible content.
    """
    x0, y0 = int(rect.get("x", 0)), int(rect.get("y", 0))
    x1, y1 = x0 + int(rect.get("w", 0)), y0 + int(rect.get("h", 0))
    if x1 <= x0 or y1 <= y0:
        return []

    pieces = [(x0, y0, x1, y1)]
    for hole in holes:
        hx0, hy0 = int(hole.get("x", 0)), int(hole.get("y", 0))
        hx1, hy1 = hx0 + int(hole.get("w", 0)), hy0 + int(hole.get("h", 0))
        nxt: List[Tuple[int, int, int, int]] = []
        for px0, py0, px1, py1 in pieces:
            if hx1 <= px0 or hx0 >= px1 or hy1 <= py0 or hy0 >= py1:
                nxt.append((px0, py0, px1, py1))
                continue
            if py0 < hy0:                      # band above the hole
                nxt.append((px0, py0, px1, min(py1, hy0)))
            if py1 > hy1:                      # band below
                nxt.append((px0, max(py0, hy1), px1, py1))
            mid0, mid1 = max(py0, hy0), min(py1, hy1)
            if mid1 > mid0:
                if px0 < hx0:                  # strip left of the hole
                    nxt.append((px0, mid0, min(px1, hx0), mid1))
                if px1 > hx1:                  # strip right
                    nxt.append((max(px0, hx1), mid0, px1, mid1))
        pieces = [p for p in nxt if p[2] > p[0] and p[3] > p[1]]
        if not pieces:
            break
    return pieces


def _hatch(
    draw: "ImageDraw.ImageDraw",
    box: Tuple[int, int, int, int],
    color: Color,
    *,
    spacing: int = 9,
    width: int = 2,
) -> None:
    """Diagonal stripes inside `box`.

    Stripes rather than a flat wash because a translucent fill over a coloured
    window just shifts its hue, which reads as a different app rather than as a
    masked region. Hatching is unmistakably an overlay, and the screenshot stays
    legible between the lines.
    """
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        return
    old = draw.im.mode if hasattr(draw, "im") else None  # noqa: F841 (documentational)
    for offset in range(0, (x1 - x0) + (y1 - y0), spacing):
        sx, sy = x0 + offset, y0
        ex, ey = x0, y0 + offset
        if sx > x1:
            sy = y0 + (sx - x1)
            sx = x1
        if ey > y1:
            ex = x0 + (ey - y1)
            ey = y1
        if sy > y1 or ex > x1:
            continue
        draw.line((sx, sy, ex, ey), fill=color, width=width)


def render_occlusion_visualization_v3(
    img: Image.Image,
    elements: List[Dict[str, Any]],
) -> Image.Image:
    """Windows as hatched masks, elements as outlines, in that order.

    Reading order is deliberate: the hatched regions answer "which part of this
    window is behind another", which is the question a person asks first, and
    the element outlines then say which individual widgets were clipped without
    competing for the same ink.
    """
    canvas = img.convert("RGBA")
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")

    counts = {"none": 0, "partial": 0, "hidden": 0}
    windows: List[Dict[str, Any]] = []
    widgets: List[Dict[str, Any]] = []
    for elem in elements:
        rect = elem.get("rect", {})
        if int(rect.get("w", 0)) < 2 or int(rect.get("h", 0)) < 2:
            continue
        counts[_element_state(elem)] += 1
        role = str(elem.get("role") or "").strip().lower()
        (windows if role in WINDOW_ROLES else widgets).append(elem)

    # 1. Window-level: hatch what another window is covering.
    for win in windows:
        if _element_state(win) == "none":
            continue
        covered = subtract_rects(win.get("rect", {}), win.get("visible_fragments") or [])
        for box in covered:
            draw.rectangle(box, fill=STATE_COLORS["hidden"] + (46,))
            _hatch(draw, box, STATE_COLORS["hidden"] + (190,))
            draw.rectangle(box, outline=STATE_COLORS["hidden"] + (225,), width=2)

    # 2. Window outlines, so each app's extent is legible under the hatching.
    for win in windows:
        rect = win.get("rect", {})
        state = _element_state(win)
        draw.rectangle(
            [int(rect["x"]), int(rect["y"]),
             int(rect["x"]) + int(rect["w"]), int(rect["y"]) + int(rect["h"])],
            outline=STATE_COLORS[state] + (170,), width=3,
        )

    # 3. Element-level: outlines only. No fill, so widget detail never competes
    #    with the window-level story.
    for elem in widgets:
        state = _element_state(elem)
        if state == "none":
            continue
        rect = elem.get("rect", {})
        fragments = elem.get("visible_fragments") or []
        _draw_edges(
            draw, rect, occluded_edges(rect, fragments),
            intact=STATE_COLORS["partial"] + (185,),
            truncated=STATE_COLORS["hidden"] + (240,),
            width=2,
        )

    canvas = Image.alpha_composite(canvas, overlay)
    _draw_legend_v3(canvas, counts, len(windows))
    return canvas.convert("RGB")


def _draw_legend_v3(canvas: Image.Image, counts: Dict[str, int], num_windows: int) -> None:
    draw = ImageDraw.Draw(canvas, "RGBA")
    pad, swatch, line_h = 10, 14, 19
    rows = [
        ("hatched = hidden by another window", STATE_COLORS["hidden"]),
        (f"partly covered elements: {counts.get('partial', 0)}", STATE_COLORS["partial"]),
        (f"fully visible elements: {counts.get('none', 0)}", STATE_COLORS["none"]),
        (f"windows: {num_windows}", (150, 160, 200)),
    ]
    width, height = 290, pad * 2 + line_h * len(rows)
    x0, y0 = 12, 12
    draw.rectangle([x0, y0, x0 + width, y0 + height], fill=(16, 16, 20, 214))
    for index, (label, color) in enumerate(rows):
        ty = y0 + pad + index * line_h
        draw.rectangle([x0 + pad, ty, x0 + pad + swatch, ty + swatch], fill=color + (240,))
        draw.text((x0 + pad + swatch + 8, ty), label, fill=(238, 238, 242, 255))


def save_occlusion_visualization_v3(
    screenshot_path: Path,
    elements: List[Dict[str, Any]],
    out_path: Path,
) -> None:
    img = render_occlusion_visualization_v3(Image.open(screenshot_path), elements)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
