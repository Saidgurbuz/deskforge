"""Full extraction pipeline for one app state.

1. Launch app
2. Wait for AT-SPI readiness
3. Execute interaction sequence
4. Walk AT-SPI tree → elements
5. Apply role mapping (done in walker)
6. Capture screenshot
7. Apply filtering (via webshot.filtering)
8. Serialize to ScreenTag (via webshot.utils)
9. Save all artifacts (.png, .elements.json, .meta.json, .screentag.txt)
10. Kill app
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from deskshot.automation.app_launcher import (
    kill_app,
    launch_app,
    wait_for_app_absent_from_atspi,
)
from deskshot.automation.interaction import execute_sequence
from deskshot.config import (
    AppManifest,
    ChainStep,
    InteractionChain,
    InteractionSequence,
    PipelineConfig,
    ensure_webshot_importable,
)
from deskshot.extraction.atspi_walker import walk_application, walk_desktop_chrome
from deskshot.extraction.interaction_state import annotate_interaction_state
from deskshot.extraction.occlusion import (
    apply_occlusion_clipping,
    find_unmanaged_popup_windows,
    get_window_stack,
    shrink_window_elements_to_content,
    strip_shadow_margins,
)
from deskshot.extraction.reading_order import PageSize, assign_reading_order_indices
from deskshot.extraction.screenshot import capture_screenshot
from deskshot.extraction.role_mapping import get_screentag_class
from deskshot.extraction.element_kinds import annotate_kinds
from deskshot.extraction.window_controls import synthesize_window_controls
from deskshot.extraction.blank_widgets import (
    drop_undrawn_windows,
    suppress_blank_widgets,
)
from deskshot.extraction.unpublished_containers import (
    find_unpublished_containers,
)
from deskshot.provenance import stamp as provenance_stamp


def _scene_app_pool():
    from deskshot.generation.scene_composer import DEFAULT_SCENE_APP_POOL

    return list(DEFAULT_SCENE_APP_POOL)
from deskshot.extraction.text_visibility import (
    TEXT_CLIP_MARKER,
    populate_visible_text,
    strip_internal_text_visibility_state,
)
from deskshot.extraction.tree_row_geometry import align_indented_tree_cells
from deskshot.extraction.type_refinement import assign_element_types
from deskshot.extraction.visibility_fragments import (
    annotate_occlusion_state,
    best_fragment,
    build_amodal_elements,
    drop_fully_hidden_elements,
    partition_hidden_elements,
    clip_fragments_to_fragments,
    get_visible_fragments,
    init_visibility_metadata,
    intersect_rect,
    is_valid_rect,
    rect_area,
    set_visible_fragments,
    strip_internal_visibility_state,
    union_rect,
)
from deskshot.extraction.vlm_label_mapping import assign_vlm_labels
from deskshot.extraction.visualize import (
    save_elements_visualization,
    save_reading_order_visualization,
)
from deskshot.environment.backgrounds import stylize_screenshot_background

logger = logging.getLogger(__name__)


def stable_hash(s: str) -> str:
    """SHA256 hash truncated to 16 chars."""
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def _sequence_from_step(chain_name: str, step: ChainStep, step_index: int) -> InteractionSequence:
    """Wrap a chain step as a temporary interaction sequence."""
    step_name = step.name or f"step{step_index:02d}"
    return InteractionSequence(
        name=f"{chain_name}:{step_name}",
        description=step.description,
        actions=list(step.actions),
        companion_apps=list(step.companion_apps),
        actions_after_companions=step.actions_after_companions,
    )


def _launch_companion_apps(
    state_id: str,
    companion_names: List[str],
    *,
    manifest_lookup: Optional[Dict[str, AppManifest]],
    launched_apps: List[str],
    app_targets: List[tuple[str, str]],
    companion_procs: List[Any],
) -> None:
    """Launch companion apps once and extend the active AT-SPI target list."""
    launched_names = set(launched_apps)
    for companion_name in companion_names:
        companion_manifest = (manifest_lookup or {}).get(companion_name)
        if companion_manifest is None:
            logger.warning(f"[{state_id}] Companion manifest not found: {companion_name}")
            continue
        if companion_manifest.app_name in launched_names:
            continue
        logger.info(f"[{state_id}] Launching companion app: {companion_manifest.app_name}")
        cproc = launch_app(companion_manifest)
        companion_procs.append(cproc)
        launched_apps.append(companion_manifest.app_name)
        app_targets.append((companion_manifest.atspi_name, companion_manifest.app_name))
        launched_names.add(companion_manifest.app_name)


def _wait_for_app_targets_to_clear(app_targets: List[tuple[str, str]]) -> None:
    """Wait for launched app AT-SPI targets to disappear before session reuse."""
    seen: set[str] = set()
    for atspi_name, _app_name in reversed(app_targets):
        name = (atspi_name or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        if wait_for_app_absent_from_atspi(name):
            logger.debug("AT-SPI target cleared: %s", name)
        else:
            logger.warning("AT-SPI target still present after cleanup: %s", name)


def _capture_current_state(
    *,
    manifest: AppManifest,
    capture_name: str,
    capture_description: str,
    stem: str,
    config: PipelineConfig,
    out: Path,
    include_desktop_chrome: bool,
    launched_apps: List[str],
    app_targets: List[tuple[str, str]],
    extra_meta: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Walk AT-SPI, postprocess elements, and save one capture snapshot."""
    logger.info(f"[{capture_name}] Walking AT-SPI tree...")
    viewport_w = config.session.display.width
    viewport_h = config.session.display.height
    raw_elements: List[Dict[str, Any]] = []
    atspi_walk_start_monotonic = time.monotonic()
    for atspi_name, app_name in app_targets:
        app_elements = walk_application(
            atspi_name,
            viewport_w=viewport_w,
            viewport_h=viewport_h,
        )
        for elem in app_elements:
            elem["source"] = "app"
            elem["app_name"] = app_name
            elem["_atspi_app_name"] = atspi_name

        if raw_elements:
            _offset_elements(app_elements, len(raw_elements))
        raw_elements.extend(app_elements)

    logger.info(f"[{capture_name}] Found {len(raw_elements)} raw app elements across {len(app_targets)} app(s)")

    chrome_elements: List[Dict[str, Any]] = []
    if include_desktop_chrome:
        chrome_elements = walk_desktop_chrome(
            viewport_w,
            viewport_h,
            launched_apps=launched_apps,
        )
        logger.info(f"[{capture_name}] Found {len(chrome_elements)} desktop chrome elements")
        _offset_elements(chrome_elements, len(raw_elements))
        raw_elements = raw_elements + chrome_elements
    atspi_walk_end_monotonic = time.monotonic()

    if not raw_elements:
        logger.warning(f"[{capture_name}] No elements found, skipping")
        return None

    logger.info(f"[{capture_name}] Capturing screenshot...")
    # The screenshot is written under a hidden partial name and only renamed to
    # `<stem>.png` once every other artifact of this sample is on disk. A run
    # that is killed mid-capture - LSF timeout, node eviction, OOM - used to
    # leave the PNG at its final name with no annotations beside it: 11,835 of
    # them, 2.92% of the corpus, invisible to every audit because the audits
    # iterate over `*.meta.json`. Renaming last makes the PNG a commit marker,
    # so "the screenshot exists" means "the sample is complete" and a consumer
    # can glob for PNGs without picking up torsos.
    screenshot_path = out / f"{stem}.png"
    partial_screenshot_path = out / f".{stem}.png.partial"
    screenshot_start_monotonic = time.monotonic()
    capture_screenshot(output_path=partial_screenshot_path)
    screenshot_end_monotonic = time.monotonic()
    capture_timing = {
        "atspi_walk_start_monotonic": atspi_walk_start_monotonic,
        "atspi_walk_end_monotonic": atspi_walk_end_monotonic,
        "screenshot_start_monotonic": screenshot_start_monotonic,
        "screenshot_end_monotonic": screenshot_end_monotonic,
        "atspi_walk_duration_sec": atspi_walk_end_monotonic - atspi_walk_start_monotonic,
        "screenshot_duration_sec": screenshot_end_monotonic - screenshot_start_monotonic,
        "atspi_to_screenshot_gap_sec": screenshot_start_monotonic - atspi_walk_end_monotonic,
    }

    # The screenshot is the only ground truth for where content is actually
    # drawn, so it is loaded once here and reused by every stage that corrects
    # toolkit-reported geometry against the pixels.
    try:
        import numpy as _np
        from PIL import Image as _Image

        with _Image.open(partial_screenshot_path) as _img:
            text_gray = _np.array(_img.convert("L")).astype(int)
    except Exception:
        logger.warning("Could not load screenshot for geometry validation", exc_info=True)
        text_gray = None

    # Before anything clips, filters or derives from the rects: a tree view's
    # expander column is reported displaced to the right by the row's
    # indentation, which leaves its expander triangles and row icons outside
    # every box. Correcting it here means occlusion, filtering, the leaf export
    # and text geometry all see the corrected rect.
    tree_cell_alignment = align_indented_tree_cells(raw_elements, text_gray)
    if tree_cell_alignment.get("num_rows"):
        logger.info(
            "[%s] Indented tree cell alignment: rows=%d cells=%d apps=%s",
            capture_name,
            tree_cell_alignment["num_rows"],
            tree_cell_alignment["num_cells"],
            tree_cell_alignment["apps"],
        )

    # Sample the X window stack once, right after the screenshot, and reuse it
    # for both popup repair and occlusion: it is the closest reading to what the
    # screenshot shows, and it saves a second xdotool sweep.
    window_stack = get_window_stack()
    # Subtract the invisible CSD shadow margin before anything reads a window
    # rect. The X window and the accessible toplevel report the same padded
    # rect, so both are corrected here and stay consistent with each other.
    window_stack, shadow_corrections = strip_shadow_margins(window_stack)
    window_shadow_meta = shrink_window_elements_to_content(raw_elements, shadow_corrections)
    if window_shadow_meta["num_windows"]:
        logger.info(
            "[%s] Window shadow margins stripped: windows=%d apps=%s",
            capture_name,
            window_shadow_meta["num_windows"],
            window_shadow_meta["by_app"],
        )
    popup_content_rects: List[Dict[str, int]] = [
        dict(popup_window.rect)
        for popup_window in find_unmanaged_popup_windows(
            window_stack, raw_elements, viewport_w * viewport_h
        )
    ]
    popup_coordinate_repairs = _repair_detached_menu_popup_coordinates(
        raw_elements,
        viewport_w=viewport_w,
        viewport_h=viewport_h,
        popup_content_rects=popup_content_rects,
    )
    if popup_coordinate_repairs["num_roots"]:
        logger.info(
            "[%s] Detached menu popup repair: roots=%d elements=%d popup_windows=%d",
            capture_name,
            popup_coordinate_repairs["num_roots"],
            popup_coordinate_repairs["num_elements"],
            len(popup_content_rects),
        )
    raw_elements_full = raw_elements
    type_refinement = assign_element_types(
        raw_elements_full,
        viewport_width=viewport_w,
        viewport_height=viewport_h,
    )
    init_visibility_metadata(raw_elements_full)
    num_elements_before_occlusion = len(raw_elements)
    occlusion_meta: Dict[str, Any] = {
        "enabled": False,
        "num_elements_in": len(raw_elements),
        "num_elements_out": len(raw_elements),
        "num_clipped": 0,
        "num_dropped": 0,
        "window_stack": [],
    }
    if config.occlusion_clipping and raw_elements:
        raw_elements, occlusion_meta = apply_occlusion_clipping(
            raw_elements_full,
            window_stack=window_stack or None,
            allow_partial_clip=config.occlusion_partial_clipping,
            min_visible_ratio=config.occlusion_min_visible_ratio,
        )
        logger.info(
            "[%s] Depth clipping: in=%d out=%d clipped=%d dropped=%d",
            capture_name,
            occlusion_meta.get("num_elements_in", 0),
            occlusion_meta.get("num_elements_out", 0),
            occlusion_meta.get("num_clipped", 0),
            occlusion_meta.get("num_dropped", 0),
        )

    raw_elements, hierarchy_clip_meta = _clip_elements_to_visible_ancestor_chain(raw_elements)
    if hierarchy_clip_meta["enabled"]:
        logger.info(
            "[%s] Hierarchy clipping: in=%d out=%d clipped=%d dropped=%d",
            capture_name,
            hierarchy_clip_meta["num_elements_in"],
            hierarchy_clip_meta["num_elements_out"],
            hierarchy_clip_meta["num_clipped"],
            hierarchy_clip_meta["num_dropped"],
        )

    unfiltered_elements = copy.deepcopy(raw_elements_full)

    raw_elements = _cleanup_elements(raw_elements)
    raw_elements = _suppress_redundant_nested_same_area(raw_elements)
    raw_elements = _suppress_redundant_same_area_clusters(raw_elements)
    raw_elements = _suppress_semantic_atomic_duplicates(raw_elements)

    if not raw_elements:
        logger.warning(f"[{capture_name}] No elements found after postprocessing, skipping")
        return None

    filtered_elements = _apply_filtering(raw_elements, config)
    logger.info(f"[{capture_name}] After filtering: {len(filtered_elements)} elements")

    if (
        config.session.desktop_env == "xfce"
        and os.environ.get("DESKSHOT_LIVE_WALLPAPER_ACTIVE") != "1"
    ):
        try:
            stylize_screenshot_background(
                partial_screenshot_path,
                theme=config.session.theme,
                elements=filtered_elements,
                occlusion_meta=occlusion_meta,
            )
        except Exception:
            logger.warning("Background stylization failed for %s", stem, exc_info=True)

    filtered_elements, hidden_text_suppression = _suppress_blank_stacked_text_alternatives(
        filtered_elements,
        partial_screenshot_path,
    )
    if hidden_text_suppression.get("num_dropped", 0):
        logger.info(
            "[%s] Hidden text suppression: in=%d out=%d dropped=%d groups=%d",
            capture_name,
            hidden_text_suppression["num_elements_in"],
            hidden_text_suppression["num_elements_out"],
            hidden_text_suppression["num_dropped"],
            hidden_text_suppression["num_blank_groups"],
        )

    leaf_elements = _build_leaf_elements(filtered_elements)
    # Before anything is rescued or synthesized on top of the leaf list, take
    # out the boxes that describe a place their own window does not occupy.
    escaped_box_meta = _drop_boxes_outside_their_window(leaf_elements)
    # An element that ships a box must agree that pixels are drawn there.
    fragment_repair_meta = _repair_fragments_against_the_window_stack(leaf_elements)
    if fragment_repair_meta["num_repaired"]:
        logger.info(
            "[%s] Restored visible fragments for %d elements the occlusion "
            "pass had emptied",
            capture_name,
            fragment_repair_meta["num_repaired"],
        )
    if escaped_box_meta["num_dropped"]:
        logger.info(
            "[%s] Dropped %d leaf boxes lying outside their own window",
            capture_name,
            escaped_box_meta["num_dropped"],
        )
    # A container is redundant only while something inside it is annotated.
    # pluma publishes a status bar with no children at all, so dropping it left
    # a 452x36 band of drawn widgets covered by nothing.
    unpublished_meta = _rescue_unpublished_containers(
        filtered_elements, leaf_elements, text_gray
    )
    # After the leaf set is built, so the synthetic boxes are not themselves
    # filtered, deduplicated against their own window, or treated as parents.
    title_bar_meta = synthesize_title_bars(leaf_elements)
    # xfwm4 draws the close/minimise/maximise buttons and publishes nothing to
    # AT-SPI, so they exist only in the pixels of the band synthesized above.
    window_control_meta = synthesize_window_controls(
        leaf_elements, text_gray, screentag_class=get_screentag_class)
    text_geometry_cache: Dict[tuple[str, tuple[int, ...]], Optional[Any]] = {}
    text_offset_cache: Dict[tuple[str, tuple[int, ...]], int] = {}
    text_visibility = {
        "unfiltered": populate_visible_text(
            unfiltered_elements, geometry_cache=text_geometry_cache,
            gray=text_gray, offset_cache=text_offset_cache,
            hierarchy=unfiltered_elements),
        "filtered": populate_visible_text(
            filtered_elements, geometry_cache=text_geometry_cache,
            gray=text_gray, offset_cache=text_offset_cache,
            hierarchy=unfiltered_elements),
        "leaf": populate_visible_text(
            leaf_elements, geometry_cache=text_geometry_cache,
            gray=text_gray, offset_cache=text_offset_cache,
            hierarchy=unfiltered_elements),
    }
    # populate_visible_text runs after filtering and can zero an element's
    # fragments when its text turns out to be entirely hidden, so elements that
    # only become fully covered at this stage were never filtered out and were
    # still serialized as (empty-text) boxes. Ground truth must contain only
    # what is visible, so drop them now. Unfiltered keeps them for debugging.
    # Keep the fully covered elements before dropping them: the modal view is
    # the training target, but the amodal view (what exists, including what is
    # behind something) is what a reveal-planning task needs.
    _, hidden_leaf = partition_hidden_elements(leaf_elements)
    hidden_leaf = [copy.deepcopy(e) for e in hidden_leaf]
    hidden_after_text = {
        "filtered": drop_fully_hidden_elements(filtered_elements),
        "leaf": drop_fully_hidden_elements(leaf_elements),
    }
    # Being mapped is not the same as being drawn. GTK3 paints overlay
    # scrollbars only near the pointer, so a headless capture reports a scroll
    # bar for every scrolled window over pixels that are uniformly background.
    # Those boxes are false positives in the strictest sense, and they are not
    # visible to any check upstream of the screenshot itself.
    # Emitted beside the legacy `type`, never instead of it, so the two
    # vocabularies can be compared on real captures before anything downstream
    # is asked to change.
    element_kinds = {
        "unfiltered": annotate_kinds(unfiltered_elements),
        "filtered": annotate_kinds(filtered_elements),
        "leaf": annotate_kinds(leaf_elements),
    }
    blank_widget_suppression = {
        "filtered": suppress_blank_widgets(filtered_elements, text_gray),
        "leaf": suppress_blank_widgets(leaf_elements, text_gray),
    }
    # A window an app keeps in its tree but never maps draws nothing; neither
    # does anything inside it.
    undrawn_windows = {
        "filtered": drop_undrawn_windows(filtered_elements, text_gray),
        "leaf": drop_undrawn_windows(leaf_elements, text_gray),
    }
    if blank_widget_suppression["leaf"]["num_dropped"]:
        logger.info(
            "[%s] Blank widget suppression: leaf dropped=%d by_role=%s",
            capture_name,
            blank_widget_suppression["leaf"]["num_dropped"],
            blank_widget_suppression["leaf"]["by_role"],
        )

    strip_internal_visibility_state(unfiltered_elements)
    strip_internal_visibility_state(filtered_elements)
    strip_internal_visibility_state(leaf_elements)
    strip_internal_text_visibility_state(unfiltered_elements)
    strip_internal_text_visibility_state(filtered_elements)
    strip_internal_text_visibility_state(leaf_elements)
    page_size = PageSize(width=float(viewport_w), height=float(viewport_h))
    reading_order_backends = {
        "unfiltered": assign_reading_order_indices(unfiltered_elements, page_size),
        "filtered": assign_reading_order_indices(filtered_elements, page_size),
        "leaf": assign_reading_order_indices(leaf_elements, page_size),
    }
    vlm_labeling = {
        "unfiltered": assign_vlm_labels(
            unfiltered_elements,
            viewport_width=viewport_w,
            viewport_height=viewport_h,
        ),
        "filtered": assign_vlm_labels(
            filtered_elements,
            viewport_width=viewport_w,
            viewport_height=viewport_h,
        ),
        "leaf": assign_vlm_labels(
            leaf_elements,
            viewport_width=viewport_w,
            viewport_height=viewport_h,
        ),
    }
    for element_list in (unfiltered_elements, filtered_elements, leaf_elements):
        annotate_occlusion_state(element_list)

    interaction_summary = {
        "unfiltered": annotate_interaction_state(unfiltered_elements),
        "filtered": annotate_interaction_state(filtered_elements),
        "leaf": annotate_interaction_state(leaf_elements),
    }

    screentag_text = _elements_to_screentag(leaf_elements, viewport_w, viewport_h)

    elements_path = out / f"{stem}.elements.json"
    unfiltered_elements_path = out / f"{stem}.elements.unfiltered.json"
    leaf_elements_path = out / f"{stem}.elements.leaf.json"
    amodal_elements_path = out / f"{stem}.elements.amodal.json"
    meta_path = out / f"{stem}.meta.json"
    screentag_path = out / f"{stem}.screentag.txt"
    viz_path = out / f"{stem}.x_viz.png"
    leaf_viz_path = out / f"{stem}.elements.leaf.x_viz.png"
    occlusion_viz_path = out / f"{stem}.occlusion.x_viz.png"
    unfiltered_ro_viz_path = out / f"{stem}.readingorder.unfiltered.x_viz.png"
    filtered_ro_viz_path = out / f"{stem}.readingorder.filtered.x_viz.png"
    leaf_ro_viz_path = out / f"{stem}.readingorder.leaf.x_viz.png"

    # Six debug renders per capture, 5.2 MB of the 8.0 MB a sample occupies.
    # They are derived - `show_flagged.py` and the inspector redraw them from the
    # annotations on demand - so at corpus scale they are 65% of the disk for
    # nothing. Off by default when DESKSHOT_SKIP_VIZ is set; incremental runs
    # leave it unset and keep them.
    write_viz = os.environ.get("DESKSHOT_SKIP_VIZ", "").strip().lower() not in {
        "1", "true", "yes",
    }

    _save_json(unfiltered_elements_path, unfiltered_elements)
    _save_json(elements_path, filtered_elements)
    _save_json(leaf_elements_path, leaf_elements)
    amodal_elements = build_amodal_elements(leaf_elements, hidden_leaf)
    _save_json(amodal_elements_path, amodal_elements)
    try:
        if write_viz:
            save_elements_visualization(partial_screenshot_path, filtered_elements, viz_path)
    except Exception:
        logger.warning("Visualization save failed for %s", stem, exc_info=True)
    try:
        if write_viz:
            save_elements_visualization(partial_screenshot_path, leaf_elements, leaf_viz_path)
    except Exception:
        logger.warning("Leaf visualization save failed for %s", stem, exc_info=True)
    try:
        from deskshot.extraction.visualize import save_occlusion_visualization_v3

        if write_viz:
            save_occlusion_visualization_v3(partial_screenshot_path, leaf_elements, occlusion_viz_path)
    except Exception:
        logger.warning("Occlusion visualization save failed for %s", stem, exc_info=True)
    try:
        if write_viz:
            save_reading_order_visualization(partial_screenshot_path, unfiltered_elements, unfiltered_ro_viz_path)
    except Exception:
        logger.warning("Unfiltered reading-order visualization save failed for %s", stem, exc_info=True)
    try:
        if write_viz:
            save_reading_order_visualization(partial_screenshot_path, filtered_elements, filtered_ro_viz_path)
    except Exception:
        logger.warning("Filtered reading-order visualization save failed for %s", stem, exc_info=True)
    try:
        if write_viz:
            save_reading_order_visualization(partial_screenshot_path, leaf_elements, leaf_ro_viz_path)
    except Exception:
        logger.warning("Leaf reading-order visualization save failed for %s", stem, exc_info=True)

    meta = {
        "app_name": manifest.app_name,
        "binary": manifest.binary,
        "atspi_name": manifest.atspi_name,
        "interaction": capture_name,
        "description": capture_description,
        "viewport": {"width": viewport_w, "height": viewport_h},
        "num_elements_raw": num_elements_before_occlusion,
        "capture_timing": capture_timing,
        "popup_coordinate_repairs": popup_coordinate_repairs,
        "num_elements_after_occlusion": len(raw_elements),
        "num_elements_filtered": len(filtered_elements),
        "num_elements_leaf": len(leaf_elements),
        "num_elements_unfiltered": len(unfiltered_elements),
        "screentag_source": "leaf",
        # What this capture was resolved against. A seed only identifies a scene
        # relative to a build and an app pool; without this, changing the pool
        # silently changes what every seed means.
        "provenance": provenance_stamp(
            # Imported here rather than at module scope: scene_composer imports
            # from this module, so a top-level import is circular.
            app_pool=_scene_app_pool(),
            config={
                "desktop_env": config.session.desktop_env,
                "theme": getattr(config.session.theme, "__dict__", None),
            },
        ),
        "stem": stem,
        "desktop_env": config.session.desktop_env,
        "launched_apps": launched_apps,
        "occlusion": occlusion_meta,
        "window_shadow_margins": window_shadow_meta,
        "hierarchy_visibility": hierarchy_clip_meta,
        "tree_cell_alignment": tree_cell_alignment,
        "reading_order": reading_order_backends,
        "type_refinement": type_refinement,
        "vlm_labeling": vlm_labeling,
        "text_visibility": text_visibility,
        "hidden_text_suppression": hidden_text_suppression,
        "hidden_after_text_visibility": hidden_after_text,
        "unpublished_containers": unpublished_meta,
        # Leaf boxes removed for lying entirely outside their own window.
        "escaped_boxes": escaped_box_meta,
        "fragment_repair": fragment_repair_meta,
        "blank_widget_suppression": blank_widget_suppression,
        "undrawn_windows": undrawn_windows,
        "window_controls": window_control_meta,
        "element_kinds": element_kinds,
        "amodal": {
            "num_visible": len(leaf_elements),
            "num_hidden": len(hidden_leaf),
            "num_total": len(leaf_elements) + len(hidden_leaf),
        },
        "interaction": interaction_summary,
    }
    if include_desktop_chrome:
        meta["num_chrome_elements"] = len(chrome_elements)
        meta["include_desktop_chrome"] = True
    if config.session.desktop_env == "xfce":
        theme = config.session.theme
        meta["theme"] = {
            "gtk_theme": theme.gtk_theme,
            "icon_theme": theme.icon_theme,
            "wm_theme": theme.wm_theme,
            "wallpaper": theme.wallpaper,
            "wallpaper_seed": theme.wallpaper_seed,
            "font": theme.font,
            "desktop_style": theme.desktop_style,
            "panel_variant": theme.panel_variant,
        }
        fixture = config.session.desktop_fixture
        meta["desktop_fixture"] = {
            "enabled": fixture.enabled,
            "seed": fixture.seed,
            "profile": fixture.profile,
            "num_folders": fixture.num_folders,
            "num_files": fixture.num_files,
            "layout_template": fixture.layout_template,
            "content_pack": fixture.content_pack,
        }
    if extra_meta:
        meta.update(extra_meta)

    _save_json(meta_path, meta)
    _write_bytes_atomic(screentag_path, screentag_text.encode("utf-8"))

    # Everything else is committed; publishing the screenshot completes the
    # sample. See the note where it was captured.
    os.replace(partial_screenshot_path, screenshot_path)

    logger.info(f"[{capture_name}] Saved: {stem}")

    return {
        "stem": stem,
        "screenshot": str(screenshot_path),
        "elements": str(elements_path),
        "elements_unfiltered": str(unfiltered_elements_path),
        "elements_leaf": str(leaf_elements_path),
        "elements_amodal": str(amodal_elements_path),
        "meta": str(meta_path),
        "screentag": str(screentag_path),
        "viz": str(viz_path),
        "leaf_viz": str(leaf_viz_path),
        "occlusion_viz": str(occlusion_viz_path),
        "reading_order_unfiltered_viz": str(unfiltered_ro_viz_path),
        "reading_order_filtered_viz": str(filtered_ro_viz_path),
        "reading_order_leaf_viz": str(leaf_ro_viz_path),
        "num_elements": len(filtered_elements),
    }


def run_single_extraction(
    manifest: AppManifest,
    interaction: InteractionSequence,
    config: PipelineConfig,
    output_dir: Optional[Path] = None,
    include_desktop_chrome: bool = False,
    manifest_lookup: Optional[Dict[str, AppManifest]] = None,
) -> Optional[Dict[str, Any]]:
    """Run the full extraction pipeline for one app × one interaction.

    Args:
        manifest: App manifest to extract.
        interaction: Interaction sequence to execute.
        config: Pipeline configuration.
        output_dir: Override output directory.
        include_desktop_chrome: If True, also extract desktop chrome elements
            (panels, taskbar, etc.) and merge with app elements.
        manifest_lookup: Optional app_name -> AppManifest map, used for
            interaction.companion_apps multi-window launches.

    Returns:
        Dict with paths to saved artifacts, or None on failure.
    """
    out = output_dir or config.output_dir
    out.mkdir(parents=True, exist_ok=True)

    # Build unique filename stem
    state_id = f"{manifest.app_name}-{interaction.name}"
    hash_str = stable_hash(f"{state_id}-{time.time()}")
    stem = f"{manifest.app_name}-{interaction.name}-{hash_str}"

    proc = None
    companion_procs: list[Any] = []
    app_targets: List[tuple[str, str]] = []
    try:
        logger.info(f"[{state_id}] Launching {manifest.binary}...")
        proc = launch_app(manifest, timeout=config.session.app_launch_timeout)

        if interaction.actions and not interaction.actions_after_companions:
            logger.info(f"[{state_id}] Executing interaction '{interaction.name}' (pre-companion)...")
            execute_sequence(interaction, app_name=manifest.atspi_name)

        launched_apps = [manifest.app_name]
        app_targets = [(manifest.atspi_name, manifest.app_name)]
        _launch_companion_apps(
            state_id,
            interaction.companion_apps,
            manifest_lookup=manifest_lookup,
            launched_apps=launched_apps,
            app_targets=app_targets,
            companion_procs=companion_procs,
        )

        if interaction.actions and interaction.actions_after_companions:
            logger.info(f"[{state_id}] Executing interaction '{interaction.name}' (post-companion)...")
            execute_sequence(interaction, app_name=manifest.atspi_name)

        time.sleep(0.5)
        return _capture_current_state(
            manifest=manifest,
            capture_name=interaction.name,
            capture_description=interaction.description,
            stem=stem,
            config=config,
            out=out,
            include_desktop_chrome=include_desktop_chrome,
            launched_apps=launched_apps,
            app_targets=app_targets,
        )

    except Exception:
        logger.exception(f"[{state_id}] Extraction failed")
        return None

    finally:
        # 10. Kill app
        for cproc in companion_procs:
            kill_app(cproc)
        if proc is not None:
            kill_app(proc)
        _wait_for_app_targets_to_clear(app_targets)


def run_chain_extraction(
    manifest: AppManifest,
    chain: InteractionChain,
    config: PipelineConfig,
    output_dir: Optional[Path] = None,
    include_desktop_chrome: bool = False,
    manifest_lookup: Optional[Dict[str, AppManifest]] = None,
) -> List[Dict[str, Any]]:
    """Run a multi-capture interaction chain in one long-lived app session."""
    out = output_dir or config.output_dir
    out.mkdir(parents=True, exist_ok=True)

    state_id = f"{manifest.app_name}-{chain.name}"
    chain_hash = stable_hash(f"{state_id}-{time.time()}")
    stem_root = f"{manifest.app_name}-{chain.name}-{chain_hash}"

    results: List[Dict[str, Any]] = []
    proc = None
    companion_procs: list[Any] = []
    app_targets: List[tuple[str, str]] = []
    try:
        logger.info(f"[{state_id}] Launching {manifest.binary}...")
        proc = launch_app(manifest, timeout=config.session.app_launch_timeout)

        launched_apps = [manifest.app_name]
        app_targets = [(manifest.atspi_name, manifest.app_name)]

        for step_index, step in enumerate(chain.steps, start=1):
            step_seq = _sequence_from_step(chain.name, step, step_index)
            step_label = step.name or f"step{step_index:02d}"

            if step.actions and not step.actions_after_companions:
                logger.info(f"[{state_id}] Executing chain step '{step_label}' (pre-companion)...")
                execute_sequence(step_seq, app_name=manifest.atspi_name)

            _launch_companion_apps(
                f"{state_id}:{step_label}",
                step.companion_apps,
                manifest_lookup=manifest_lookup,
                launched_apps=launched_apps,
                app_targets=app_targets,
                companion_procs=companion_procs,
            )

            if step.actions and step.actions_after_companions:
                logger.info(f"[{state_id}] Executing chain step '{step_label}' (post-companion)...")
                execute_sequence(step_seq, app_name=manifest.atspi_name)

            if not step.capture:
                continue

            time.sleep(0.5)
            stem = f"{stem_root}-step{step_index:02d}"
            result = _capture_current_state(
                manifest=manifest,
                capture_name=chain.name,
                capture_description=chain.description or step.description,
                stem=stem,
                config=config,
                out=out,
                include_desktop_chrome=include_desktop_chrome,
                launched_apps=launched_apps,
                app_targets=app_targets,
                extra_meta={
                    "chain_name": chain.name,
                    "chain_description": chain.description,
                    "chain_step_index": step_index,
                    "chain_step_name": step_label,
                    "chain_step_description": step.description,
                },
            )
            if result is not None:
                results.append(result)

        return results

    except Exception:
        logger.exception(f"[{state_id}] Chain extraction failed")
        return []

    finally:
        for cproc in companion_procs:
            kill_app(cproc)
        if proc is not None:
            kill_app(proc)
        _wait_for_app_targets_to_clear(app_targets)


def _offset_elements(elements: List[Dict[str, Any]], offset: int) -> None:
    """Offset dom-index based hierarchy fields for a merged element list."""
    if offset <= 0:
        return
    for elem in elements:
        elem["_dom_index"] += offset
        if elem["_parent_dom_index"] is not None:
            elem["_parent_dom_index"] += offset
        elem["parent_index"] = elem["_parent_dom_index"]
        elem["_children_dom_indices"] = [c + offset for c in elem["_children_dom_indices"]]
        elem["children_indices"] = list(elem["_children_dom_indices"])


MENU_POPUP_CHILD_ROLES = {"menu", "popup menu", "menu item", "check menu item", "radio menu item", "separator"}


def _rect_center(rect: Dict[str, int]) -> tuple[float, float]:
    return rect.get("x", 0) + rect.get("w", 0) / 2, rect.get("y", 0) + rect.get("h", 0) / 2


def _rect_contains_point(rect: Dict[str, int], x: float, y: float) -> bool:
    return rect.get("x", 0) <= x <= rect.get("x", 0) + rect.get("w", 0) and rect.get("y", 0) <= y <= rect.get("y", 0) + rect.get("h", 0)


def _ancestor_chain(dom_index: int, by_idx: Dict[int, Dict[str, Any]]) -> List[int]:
    chain: List[int] = []
    current = by_idx.get(dom_index, {}).get("_parent_dom_index")
    seen: set[int] = set()
    while isinstance(current, int) and current in by_idx and current not in seen:
        seen.add(current)
        chain.append(current)
        current = by_idx[current].get("_parent_dom_index")
    return chain


def _nearest_owner_frame(dom_index: int, by_idx: Dict[int, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for ancestor in _ancestor_chain(dom_index, by_idx):
        role = (by_idx[ancestor].get("role") or "").strip().lower()
        if _is_window_like_role(role):
            return by_idx[ancestor]
    return None


def _children_by_parent(elements: List[Dict[str, Any]]) -> Dict[int, List[Dict[str, Any]]]:
    children: Dict[int, List[Dict[str, Any]]] = {}
    for elem in elements:
        parent = elem.get("_parent_dom_index")
        if isinstance(parent, int):
            children.setdefault(parent, []).append(elem)
    return children


def _collect_menu_popup_descendants(
    root: Dict[str, Any],
    children_by_parent: Dict[int, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    stack = list(children_by_parent.get(int(root["_dom_index"]), []))
    seen: set[int] = set()
    while stack:
        elem = stack.pop()
        object_id = id(elem)
        if object_id in seen:
            continue
        seen.add(object_id)
        role = (elem.get("role") or "").strip().lower()
        if role not in MENU_POPUP_CHILD_ROLES:
            continue
        out.append(elem)
        stack.extend(children_by_parent.get(int(elem.get("_dom_index", -1)), []))
    return out


def _union_element_rects(elements: List[Dict[str, Any]]) -> Optional[Dict[str, int]]:
    return union_rect([e["rect"] for e in elements if is_valid_rect(e.get("rect"))])


#: Slack allowed between a popup window's drawn area and the union of the menu
#: items inside it - the menu's own border and padding. Measured 6px per side on
#: the xarchiver Archive menu; 48 leaves room for heavier themes without letting
#: an unrelated window match.
MAX_POPUP_WINDOW_PADDING = 48


def _match_popup_window_rect(
    popup_union: Dict[str, int],
    popup_content_rects: List[Dict[str, int]],
) -> Optional[Dict[str, int]]:
    """Find the drawn popup window that holds this set of menu items.

    A menu popup is its own X window, so its true position is a measurement
    rather than a guess. Match by size: the window's drawn area is the menu item
    union plus the menu's border and padding, which is a tight and distinctive
    fit.
    """
    best: Optional[Dict[str, int]] = None
    best_slack: Optional[int] = None
    for content in popup_content_rects:
        slack_w = content["w"] - popup_union["w"]
        slack_h = content["h"] - popup_union["h"]
        if slack_w < 0 or slack_h < 0:
            continue
        if slack_w > MAX_POPUP_WINDOW_PADDING or slack_h > MAX_POPUP_WINDOW_PADDING:
            continue
        slack = slack_w + slack_h
        if best_slack is None or slack < best_slack:
            best, best_slack = content, slack
    return best


def _repair_detached_menu_popup_coordinates(
    elements: List[Dict[str, Any]],
    *,
    viewport_w: int,
    viewport_h: int,
    popup_content_rects: Optional[List[Dict[str, int]]] = None,
) -> Dict[str, Any]:
    """Put menu popup children where the popup is actually drawn.

    Two different toolkit behaviours land here, and they need opposite fixes:

    - Mousepad reports its open menu's items in *owner-local* coordinates
      (x=8, y=86), so they have to be re-anchored under the menu bar item.
    - xarchiver reports its open menu in correct screen coordinates, but the
      menu is drawn nowhere near the frame, because the scene moves the window
      after the menu opens and an override-redirect popup does not move with it.
      Measured: popup drawn at (661,373), frame at (1093,536).

    Both look identical from the element tree alone - a menu popup outside its
    owner frame - and the previous anchor-only repair "fixed" the second case by
    dragging a correctly placed popup into the frame, where it covered the
    toolbar. Ten menu items were annotated 211px below where they were drawn,
    the popup region had no annotations at all, and four xarchiver toolbar
    buttons underneath were dropped as occluded.

    So prefer the measurement: if an unmanaged popup X window fits the menu,
    centre the items in its drawn area. Fall back to the menu bar anchor only
    when no such window is available.
    """
    by_idx = {int(e["_dom_index"]): e for e in elements if "_dom_index" in e}
    children_by_parent = _children_by_parent(elements)
    repaired_roots = 0
    repaired_elements = 0
    popup_content_rects = popup_content_rects or []

    for elem in elements:
        role = (elem.get("role") or "").strip().lower()
        if role != "menu":
            continue
        dom_index = int(elem.get("_dom_index", -1))
        parent = by_idx.get(elem.get("_parent_dom_index"))
        parent_role = (parent.get("role") or "").strip().lower() if parent else ""
        if parent_role != "menu bar":
            continue

        owner = _nearest_owner_frame(dom_index, by_idx)
        if owner is None:
            continue
        owner_rect = owner.get("rect", {})
        anchor_rect = elem.get("rect", {})
        if not is_valid_rect(owner_rect) or not is_valid_rect(anchor_rect):
            continue

        child_elems = _collect_menu_popup_descendants(elem, children_by_parent)
        if not child_elems:
            continue
        popup_union = _union_element_rects(child_elems)
        if popup_union is None:
            continue

        popup_cx, popup_cy = _rect_center(popup_union)
        if _rect_contains_point(owner_rect, popup_cx, popup_cy):
            continue

        drawn = _match_popup_window_rect(popup_union, popup_content_rects)
        if drawn is not None:
            # Centre the items in the drawn area: a menu's border and padding
            # are symmetric, so this recovers the exact offset.
            candidate = {
                "x": drawn["x"] + (drawn["w"] - popup_union["w"]) // 2,
                "y": drawn["y"] + (drawn["h"] - popup_union["h"]) // 2,
                "w": popup_union["w"],
                "h": popup_union["h"],
            }
            source = "popup_window"
        else:
            candidate = {
                "x": anchor_rect["x"],
                "y": anchor_rect["y"] + anchor_rect["h"],
                "w": popup_union["w"],
                "h": popup_union["h"],
            }
            candidate_cx, candidate_cy = _rect_center(candidate)
            if not _rect_contains_point(owner_rect, candidate_cx, candidate_cy):
                continue
            source = "menu_bar_anchor"
        if candidate["x"] >= viewport_w or candidate["y"] >= viewport_h:
            continue

        dx = candidate["x"] - popup_union["x"]
        dy = candidate["y"] - popup_union["y"]
        if dx == 0 and dy == 0:
            continue
        for child in child_elems:
            rect = child.get("rect", {})
            if not is_valid_rect(rect):
                continue
            rect["x"] += dx
            rect["y"] += dy
            child["_coords_repaired_from_detached_menu_popup"] = True
            child["_detached_menu_popup_repair_source"] = source
            repaired_elements += 1
        repaired_roots += 1

    return {"num_roots": repaired_roots, "num_elements": repaired_elements}


STACKED_TEXT_ROLES = {"paragraph", "static", "text", "label"}
STACKED_TEXT_ACTIONABLE_ROLES = {
    "push button",
    "toggle button",
    "check box",
    "check menu item",
    "radio button",
    "radio menu item",
    "menu item",
    "page tab",
    "link",
    "combo box",
    "entry",
    "editbar",
    "password text",
    "spin button",
    "switch",
    "slider",
    "scroll bar",
    "menu",
    "popup menu",
}


def _suppress_blank_stacked_text_alternatives(
    elements: List[Dict[str, Any]],
    screenshot_path: Path,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if not elements:
        return elements, _blank_stacked_text_suppression_meta(0)

    groups = _stacked_text_alternative_groups(elements)
    if not groups:
        return elements, _blank_stacked_text_suppression_meta(len(elements))

    try:
        from PIL import Image

        with Image.open(screenshot_path) as img:
            gray = img.convert("L")
            to_drop = _blank_stacked_text_group_drops(groups, gray)
    except Exception:
        logger.warning("Hidden text suppression skipped for %s", screenshot_path, exc_info=True)
        return elements, {
            **_blank_stacked_text_suppression_meta(len(elements)),
            "enabled": False,
            "error": "screenshot_read_failed",
        }

    if not to_drop:
        return elements, {
            **_blank_stacked_text_suppression_meta(len(elements)),
            "num_candidate_groups": len(groups),
        }

    out = _reindex_hierarchy([
        elem for elem in elements
        if int(elem.get("_dom_index", -1)) not in to_drop
    ])
    return out, {
        "enabled": True,
        "num_elements_in": len(elements),
        "num_elements_out": len(out),
        "num_candidate_groups": len(groups),
        "num_blank_groups": sum(1 for group in groups if any(int(e.get("_dom_index", -1)) in to_drop for e in group)),
        "num_dropped": len(to_drop),
    }


def _blank_stacked_text_suppression_meta(num_elements: int) -> Dict[str, Any]:
    return {
        "enabled": True,
        "num_elements_in": num_elements,
        "num_elements_out": num_elements,
        "num_candidate_groups": 0,
        "num_blank_groups": 0,
        "num_dropped": 0,
    }


def _blank_stacked_text_group_drops(groups: List[List[Dict[str, Any]]], gray: Any) -> set[int]:
    to_drop: set[int] = set()
    width, height = gray.size
    bounds = {"x": 0, "y": 0, "w": width, "h": height}
    for group in groups:
        blank_members = []
        for elem in group:
            rect = intersect_rect(elem.get("rect", {}), bounds)
            if rect is None or _text_rect_has_visible_ink(gray, rect):
                continue
            blank_members.append(elem)
        if len(blank_members) >= max(2, len(group) - 1):
            to_drop.update(int(elem["_dom_index"]) for elem in blank_members if "_dom_index" in elem)
    return to_drop


def _text_rect_has_visible_ink(gray: Any, rect: Dict[str, int]) -> bool:
    pad = 1
    left = max(0, int(rect["x"]) + pad)
    top = max(0, int(rect["y"]) + pad)
    right = min(gray.size[0], int(rect["x"]) + int(rect["w"]) - pad)
    bottom = min(gray.size[1], int(rect["y"]) + int(rect["h"]) - pad)
    if right <= left or bottom <= top:
        left = max(0, int(rect["x"]))
        top = max(0, int(rect["y"]))
        right = min(gray.size[0], int(rect["x"]) + int(rect["w"]))
        bottom = min(gray.size[1], int(rect["y"]) + int(rect["h"]))
    if right <= left or bottom <= top:
        return False

    pixels = list(gray.crop((left, top, right, bottom)).getdata())
    if not pixels:
        return False
    min_px = min(pixels)
    max_px = max(pixels)
    if max_px - min_px >= 24:
        return True

    ordered = sorted(pixels)
    n = len(ordered)
    median = ordered[n // 2]
    dark_ink = sum(1 for px in pixels if median - px >= 18) / n
    bright_ink = sum(1 for px in pixels if px - median >= 18) / n
    if dark_ink >= 0.012 or bright_ink >= 0.012:
        return True

    sample_stride = max(1, int(n ** 0.5) // 64)
    sampled = pixels[::sample_stride]
    if len(sampled) >= 2:
        transitions = sum(1 for a, b in zip(sampled, sampled[1:]) if abs(a - b) >= 12) / (len(sampled) - 1)
        if transitions >= 0.02:
            return True
    return False


def _stacked_text_alternative_groups(elements: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    candidates = [elem for elem in elements if _is_stacked_text_candidate(elem)]
    groups: List[List[Dict[str, Any]]] = []
    used: set[int] = set()
    for i, elem in enumerate(candidates):
        if i in used:
            continue
        group = [elem]
        used.add(i)
        for j in range(i + 1, len(candidates)):
            if j in used:
                continue
            other = candidates[j]
            if _same_stacked_text_cluster(elem, other):
                group.append(other)
                used.add(j)
        distinct_texts = {_text_norm(e.get("inner_text", "")).lower() for e in group}
        if len(group) >= 3 and len(distinct_texts) >= 2:
            groups.append(group)
    return groups


def _is_stacked_text_candidate(elem: Dict[str, Any]) -> bool:
    role = (elem.get("role") or "").strip().lower()
    elem_type = (elem.get("type") or "").strip().lower()
    text = _text_norm(elem.get("inner_text", ""))
    if not text or _is_generic_text(text):
        return False
    if role in STACKED_TEXT_ACTIONABLE_ROLES:
        return False
    if elem_type in {"button", "link", "text input", "checkbox", "radio button", "tab", "menu item"}:
        return False
    if role not in STACKED_TEXT_ROLES and elem_type != "text":
        return False
    if not is_valid_rect(elem.get("rect"), min_visible_size=6):
        return False
    return True


def _same_stacked_text_cluster(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    if a.get("source") != b.get("source") or a.get("app_name") != b.get("app_name"):
        return False
    ar = a.get("rect", {})
    br = b.get("rect", {})
    if abs(int(ar.get("x", 0)) - int(br.get("x", 0))) > 8:
        return False
    if abs(int(ar.get("y", 0)) - int(br.get("y", 0))) > 8:
        return False
    if abs(int(ar.get("h", 0)) - int(br.get("h", 0))) > 8:
        return False
    return _rect_containment_ratio(ar, br) >= 0.70 or _rect_containment_ratio(br, ar) >= 0.70


def _cleanup_elements(elements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove obvious duplicate/ghost boxes in fallback mode."""
    cleaned = []
    for e in elements:
        # Ghost top-panel action placeholders (empty, tiny, often not rendered).
        if (
            e.get("source") == "desktop_chrome"
            and e.get("role") == "push button"
            and not (e.get("inner_text") or "").strip()
            and e.get("rect", {}).get("y", 9999) <= 2
            and e.get("rect", {}).get("w", 0) <= 32
            and e.get("rect", {}).get("h", 0) <= 32
        ):
            continue
        cleaned.append(e)

    deduped: List[Dict[str, Any]] = []
    for e in cleaned:
        if _is_duplicate_of_existing(e, deduped):
            continue
        deduped.append(e)
    return _reindex_hierarchy(deduped)


def _is_duplicate_of_existing(elem: Dict[str, Any], kept: List[Dict[str, Any]]) -> bool:
    rect = elem.get("rect", {})
    area = max(1, rect.get("w", 0) * rect.get("h", 0))
    for k in kept:
        if _is_same_role_partition_subcell(elem, k):
            continue
        if elem.get("source") != k.get("source"):
            continue
        if elem.get("app_name") != k.get("app_name"):
            continue
        if elem.get("type") != k.get("type"):
            continue
        ri = _rect_iou(rect, k.get("rect", {}))
        if ri < 0.92:
            continue
        ka = max(1, k.get("rect", {}).get("w", 0) * k.get("rect", {}).get("h", 0))
        ratio = area / ka
        if ratio < 0.85 or ratio > 1.15:
            continue
        ta = (elem.get("inner_text") or "").strip()
        tb = (k.get("inner_text") or "").strip()
        if ta and tb and ta != tb:
            continue
        return True
    return False


def _rect_iou(a: Dict[str, int], b: Dict[str, int]) -> float:
    ax1, ay1 = a.get("x", 0), a.get("y", 0)
    ax2, ay2 = ax1 + a.get("w", 0), ay1 + a.get("h", 0)
    bx1, by1 = b.get("x", 0), b.get("y", 0)
    bx2, by2 = bx1 + b.get("w", 0), by1 + b.get("h", 0)
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter == 0:
        return 0.0
    aa = max(1, (ax2 - ax1) * (ay2 - ay1))
    ba = max(1, (bx2 - bx1) * (by2 - by1))
    return inter / max(1, aa + ba - inter)


def _intersect_rects(a: Dict[str, int], b: Dict[str, int]) -> Optional[Dict[str, int]]:
    x1 = max(a.get("x", 0), b.get("x", 0))
    y1 = max(a.get("y", 0), b.get("y", 0))
    x2 = min(a.get("x", 0) + a.get("w", 0), b.get("x", 0) + b.get("w", 0))
    y2 = min(a.get("y", 0) + a.get("h", 0), b.get("y", 0) + b.get("h", 0))
    if x2 <= x1 or y2 <= y1:
        return None
    return {"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1}


def _reindex_hierarchy(elements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rebuild _dom_index and parent/child indices after element drops."""
    old_parent_lookup = {
        int(e["_dom_index"]): e.get("_parent_dom_index")
        for e in elements
        if "_dom_index" in e
    }
    old_to_new = {int(e["_dom_index"]): i for i, e in enumerate(elements)}

    for i, elem in enumerate(elements):
        old_parent = elem.get("_parent_dom_index")
        while old_parent is not None and old_parent not in old_to_new:
            old_parent = old_parent_lookup.get(old_parent)
        new_parent = old_to_new.get(old_parent) if old_parent is not None else None

        elem["_dom_index"] = i
        elem["_parent_dom_index"] = new_parent
        elem["parent_index"] = new_parent
        elem["_children_dom_indices"] = []
        elem["children_indices"] = []

    for child in elements:
        p = child.get("_parent_dom_index")
        if p is not None and 0 <= p < len(elements):
            elements[p]["_children_dom_indices"].append(child["_dom_index"])

    for elem in elements:
        elem["children_indices"] = list(elem["_children_dom_indices"])

    return elements


def _clip_elements_to_visible_ancestor_chain(
    elements: List[Dict[str, Any]],
    *,
    min_visible_size: int = 2,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Clip each element to the intersection of its visible ancestor chain.

    AT-SPI sometimes reports descendant bounds in document coordinates rather
    than the currently visible viewport. This pass constrains descendants to
    their ancestor-visible region so offscreen or partially visible content
    matches what is actually rendered in the screenshot.
    """
    if not elements:
        return elements, {
            "enabled": False,
            "num_elements_in": 0,
            "num_elements_out": 0,
            "num_clipped": 0,
            "num_dropped": 0,
        }

    by_idx = {int(e["_dom_index"]): e for e in elements if "_dom_index" in e}
    parent_lookup = {
        int(e["_dom_index"]): e.get("_parent_dom_index")
        for e in elements
        if "_dom_index" in e
    }

    def _has_plausible_ancestor_overlap(
        child_fragments: List[Dict[str, int]],
        parent_fragments: List[Dict[str, int]],
    ) -> bool:
        """Return True only when the supposed ancestor is spatially compatible.

        AT-SPI occasionally attaches visible descendants to unrelated
        container/landmark nodes elsewhere in the document tree. Those bogus
        ancestors should not zero out otherwise visible nodes.
        """
        if not child_fragments or not parent_fragments:
            return False
        for child in child_fragments:
            cx = child["x"] + child["w"] / 2
            cy = child["y"] + child["h"] / 2
            for parent in parent_fragments:
                if (
                    parent["x"] <= cx <= parent["x"] + parent["w"]
                    and parent["y"] <= cy <= parent["y"] + parent["h"]
                ):
                    return True
                inter = intersect_rect(child, parent)
                if inter is None:
                    continue
                if rect_area(inter) > 0:
                    return True
        return False

    def _visible_fragments_for(dom_index: int) -> List[Dict[str, int]]:
        elem = by_idx[dom_index]
        rect = dict(elem.get("rect", {}))
        remaining = get_visible_fragments(elem)
        if not remaining and rect.get("w", 0) >= min_visible_size and rect.get("h", 0) >= min_visible_size:
            remaining = [rect]
        elem_stack_idx = elem.get("_window_stack_index")
        if not remaining:
            return []

        parent_idx = parent_lookup.get(dom_index)
        visited: set[int] = set()
        while parent_idx is not None and parent_idx in by_idx and parent_idx not in visited:
            visited.add(parent_idx)
            parent = by_idx[parent_idx]
            parent_role = (parent.get("role") or "").strip().lower()
            parent_stack_idx = parent.get("_window_stack_index")
            if (
                _is_window_like_role(parent_role)
                and isinstance(elem_stack_idx, int)
                and isinstance(parent_stack_idx, int)
                and elem_stack_idx > parent_stack_idx
            ):
                # Some toolkit popup menus remain nested under the main app
                # frame in AT-SPI even though they render in a separate
                # transient X window above it. Once occlusion has assigned the
                # descendant to a higher real window stack, do not clamp it
                # back into the lower ancestor frame.
                break
            if parent_role in SOFT_ANCESTOR_CLIP_ROLES:
                parent_idx = parent_lookup.get(parent_idx)
                continue
            parent_fragments = get_visible_fragments(parent)
            if not parent_fragments:
                parent_rect = dict(parent.get("rect", {}))
                if parent_rect.get("w", 0) >= min_visible_size and parent_rect.get("h", 0) >= min_visible_size:
                    parent_fragments = [parent_rect]
            if parent_fragments:
                if (
                    not _has_plausible_ancestor_overlap(remaining, parent_fragments)
                    and parent_role in NON_AUTHORITATIVE_HARD_ANCESTOR_ROLES
                ):
                    parent_idx = parent_lookup.get(parent_idx)
                    continue
                clipped = clip_fragments_to_fragments(
                    remaining,
                    parent_fragments,
                    min_visible_size=min_visible_size,
                )
                if not clipped:
                    if (
                        elem.get("source") == "desktop_chrome"
                        and parent.get("source") == "desktop_chrome"
                        and elem_stack_idx == parent_stack_idx
                    ):
                        parent_idx = parent_lookup.get(parent_idx)
                        continue
                    if parent_role in HARD_ANCESTOR_CLIP_ROLES:
                        return []
                else:
                    remaining = clipped
            parent_idx = parent_lookup.get(parent_idx)
        return remaining

    clipped = 0
    dropped = 0
    out: List[Dict[str, Any]] = []
    for elem in elements:
        dom_index = int(elem["_dom_index"])
        original_rect = dict(elem.get("rect", {}))
        original_fragments = get_visible_fragments(elem)
        clipped_fragments = _visible_fragments_for(dom_index)
        if not clipped_fragments:
            set_visible_fragments(elem, [], min_visible_size=min_visible_size)
            dropped += 1
            continue
        set_visible_fragments(elem, clipped_fragments, min_visible_size=min_visible_size)
        best_visible = best_fragment(clipped_fragments)
        rect_changed = (
            len(clipped_fragments) == 1
            and best_visible is not None
            and best_visible != original_rect
        )
        if clipped_fragments != original_fragments or rect_changed:
            if rect_changed and best_visible is not None:
                elem["rect"] = best_visible
            elem["_hierarchy_visibility_clipped"] = True
            elem["_hierarchy_visibility_original_rect"] = original_rect
            clipped += 1
        out.append(elem)

    out = _reindex_hierarchy(out)
    return out, {
        "enabled": True,
        "num_elements_in": len(elements),
        "num_elements_out": len(out),
        "num_clipped": clipped,
        "num_dropped": dropped,
    }


ROLE_PRIORITY = {
    "document web": 95,
    "document frame": 90,
    "entry": 85,
    "editbar": 85,
    "push button": 80,
    "toggle button": 80,
    "combo box": 70,
    "page tab": 70,
    "text": 68,
    "frame": 60,
    "desktop frame": 60,
    "page tab list": 22,
    "scroll pane": 25,
    "internal frame": 20,
    "section": 15,
    "panel": 12,
    "filler": 10,
    "viewport": 10,
    "layered pane": 10,
    "root pane": 10,
}

CONTAINER_ROLES = {
    "page tab list",
    "scroll pane",
    "internal frame",
    "section",
    "panel",
    "filler",
    "viewport",
    "layered pane",
    "root pane",
}

GENERIC_TEXTS = {
    "",
    "content view",
    "desktop",
}
TEXT_PLACEHOLDER_CHARS = str.maketrans("", "", "\u200b\u200c\u200d\u2060\ufeff\ufffc")
DECORATIVE_CHILD_ROLES = {
    "static",
    "text",
    "label",
    "image",
    "icon",
}
ATOMIC_LEAF_ROLES = {
    "push button",
    "toggle button",
    "check box",
    "check menu item",
    "radio button",
    "radio menu item",
    "menu item",
    "page tab",
    "link",
    "combo box",
    "entry",
    "editbar",
    "password text",
    "spin button",
    "switch",
    "slider",
    "scroll bar",
    "heading",
    "list item",
    "table cell",
    "tree item",
}
#: Text-entry widget classes. `assign_element_types` derives both from the same
#: branches - editable state, or an editable-text interface - and only splits
#: them on whether the widget carries a search signal, so anything that treats a
#: text field as an atomic control must treat a search field the same way.
#: Measured on Nautilus's header bar: its search entry (AT-SPI role `text`,
#: 198x46) holds one `edit-find-symbolic` icon child of 16x16. Typed
#: `Text Input` the entry survives into the leaf set and the icon is suppressed;
#: typed `Search Field` the entry was dropped and the 16x16 icon was all that
#: remained of a 198x46 widget.
TEXT_ENTRY_TYPES = {"text input", "search field"}
WINDOW_LIKE_ROLES = {
    "frame",
    "desktop frame",
    "window",
    "dialog",
    "alert",
    "file chooser",
}
SOFT_ANCESTOR_CLIP_ROLES = {
    "menu",
    "menu item",
    "menu bar",
    "popup menu",
    "split pane",
}
HARD_ANCESTOR_CLIP_ROLES = WINDOW_LIKE_ROLES | {
    "document web",
    "document frame",
    "panel",
    "menu bar",
    "tool bar",
    "toolbar",
    "status bar",
    "landmark",
    "list",
    "table",
    "tree",
    "scroll pane",
    "internal frame",
    "viewport",
    "layered pane",
    "root pane",
    "page tab list",
    "filler",
}
NON_AUTHORITATIVE_HARD_ANCESTOR_ROLES = {
    "landmark",
    "list",
    "table",
    "tree",
    "internal frame",
    "filler",
}
STRUCTURAL_CONTAINER_ROLES = {
    "document web",
    "document frame",
    "panel",
    "menu bar",
    "tool bar",
    "toolbar",
    "status bar",
    "landmark",
    "section",
    "list",
    "table",
    "tree",
    "split pane",
    "scroll pane",
    "internal frame",
    "viewport",
    "layered pane",
    "root pane",
    "page tab list",
    "filler",
}
def _role_priority(role: str) -> int:
    return ROLE_PRIORITY.get((role or "").strip().lower(), 50)


def _text_norm(v: str) -> str:
    cleaned = (v or "").translate(TEXT_PLACEHOLDER_CHARS)
    return " ".join(cleaned.strip().split())


def _is_container_role(role: str) -> bool:
    return (role or "").strip().lower() in CONTAINER_ROLES


def _is_generic_text(text: str) -> bool:
    return _text_norm(text).lower() in GENERIC_TEXTS


def _is_iconic_text(text: str) -> bool:
    normalized = _text_norm(text)
    if not normalized or len(normalized) > 4:
        return False
    return not any(ch.isalnum() for ch in normalized)


def _looks_like_icon_asset_name(text: str) -> bool:
    normalized = _text_norm(text).lower()
    if not normalized:
        return True
    if normalized.endswith("-symbolic"):
        return True
    return any(token in normalized for token in {"symbolic", "dialog-", "folder-", "emblem-", "gtk-"})


def _is_atomic_leaf_role(role: str, elem_type: str = "") -> bool:
    if (elem_type or "").strip().lower() in TEXT_ENTRY_TYPES:
        return True
    return (role or "").strip().lower() in ATOMIC_LEAF_ROLES


def _is_decorative_child(elem: Dict[str, Any]) -> bool:
    role = (elem.get("role") or "").strip().lower()
    elem_type = (elem.get("type") or "").strip().lower()
    return (
        role in DECORATIVE_CHILD_ROLES
        or elem_type == "text"
    )


def _is_window_like_role(role: str) -> bool:
    return (role or "").strip().lower() in WINDOW_LIKE_ROLES


def _is_protected_top_level_app_window(
    elem: Dict[str, Any],
    by_idx: Dict[int, Dict[str, Any]] | None = None,
) -> bool:
    """Return True for genuine top-level app windows we should preserve.

    These nodes are important to keep because they anchor app ownership for
    flat exports and scene understanding. We avoid preserving generic empty
    root frames by requiring either meaningful title text or explicit window
    control children such as Minimize/Maximize/Close.
    """
    role = (elem.get("role") or "").strip().lower()
    source = (elem.get("source") or "").strip().lower()
    app_name = (elem.get("app_name") or "").strip().lower()
    text = _text_norm(elem.get("inner_text", ""))
    if not _is_window_like_role(role):
        return False
    if source != "app":
        return False
    if app_name in {"desktop", "top panel", "xfwm4", ""}:
        return False
    if elem.get("parent_index") is not None:
        return False
    if elem.get("_parent_dom_index") is not None:
        return False
    if elem.get("_source_parent_dom_index") is not None:
        return False
    if text and not _is_generic_text(text):
        return True
    # A dialog is never the "generic empty root frame" this guard was written
    # for. Those are the invisible top-level frames a toolkit keeps around; an
    # alert, dialog or file chooser only exists while it is on screen. Requiring
    # a title dropped a transmission licence dialog - 592x167, unoccluded,
    # holding "Transmission is a file..." with Cancel and I Agree - from the
    # export entirely, so a whole visible window went unannotated.
    if role in {"alert", "dialog", "file chooser"}:
        return True
    if by_idx is None:
        return False
    for cidx in elem.get("_children_dom_indices") or elem.get("children_indices") or []:
        child = by_idx.get(int(cidx))
        if child is None:
            continue
        child_text = _text_norm(child.get("inner_text", "")).lower()
        if child_text in {"minimize", "maximize", "close"}:
            return True
    return False


def _rect_containment_ratio(inner: Dict[str, int], outer: Dict[str, int]) -> float:
    inter = _intersect_rects(inner, outer)
    if inter is None:
        return 0.0
    inner_area = max(1, inner.get("w", 0) * inner.get("h", 0))
    inter_area = max(0, inter.get("w", 0) * inter.get("h", 0))
    return inter_area / inner_area


def _semantic_score(elem: Dict[str, Any]) -> int:
    role = (elem.get("role") or "").strip().lower()
    elem_type = (elem.get("type") or "").strip().lower()
    text = _text_norm(elem.get("inner_text", "")).lower()
    score = _role_priority(role)
    if text and text not in GENERIC_TEXTS:
        score += 200
    elif text:
        score += 20
    if role in {"document web", "document frame", "entry", "editbar", "push button", "toggle button"}:
        score += 40
    if elem_type == "text input":
        score += 40
    return score


def _texts_compatible(a_text: str, b_text: str) -> bool:
    a = _text_norm(a_text).lower()
    b = _text_norm(b_text).lower()
    if a == b:
        return True
    if not a or not b:
        return True
    if a in GENERIC_TEXTS and b in GENERIC_TEXTS:
        return True
    return False


def _same_area(a: Dict[str, int], b: Dict[str, int]) -> bool:
    ia = _rect_iou(a, b)
    if ia < 0.992:
        return False
    aa = max(1, a.get("w", 0) * a.get("h", 0))
    ba = max(1, b.get("w", 0) * b.get("h", 0))
    ratio = aa / ba
    return 0.96 <= ratio <= 1.04


def _same_area_or_contained(a: Dict[str, int], b: Dict[str, int]) -> bool:
    return _same_area(a, b) or _rect_containment_ratio(a, b) >= 0.98 or _rect_containment_ratio(b, a) >= 0.98


def _is_same_role_partition_subcell(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Return True for row/item subcells that should survive semantic dedupe.

    Some widgets (for example Thunar sidebar rows) expose one parent row cell
    and several same-role child cells for spacer, icon, and label. The icon and
    label occupy distinct visible regions and should not be collapsed into the
    broader row container.
    """
    a_role = (a.get("role") or "").strip().lower()
    b_role = (b.get("role") or "").strip().lower()
    if a_role != b_role or a_role not in {"table cell", "list item", "tree item"}:
        return False

    a_rect = a.get("rect", {})
    b_rect = b.get("rect", {})
    if _rect_containment_ratio(a_rect, b_rect) < 0.98 and _rect_containment_ratio(b_rect, a_rect) < 0.98:
        return False

    a_area = max(1, a_rect.get("w", 0) * a_rect.get("h", 0))
    b_area = max(1, b_rect.get("w", 0) * b_rect.get("h", 0))
    smaller = a if a_area <= b_area else b
    smaller_rect = smaller.get("rect", {})
    smaller_text = _text_norm(smaller.get("inner_text", ""))
    sw = max(0, smaller_rect.get("w", 0))
    sh = max(0, smaller_rect.get("h", 0))

    if smaller_text and not _is_generic_text(smaller_text):
        return True

    # Preserve icon-like subcells (roughly square, meaningful visible area).
    if min(sw, sh) >= 16 and max(sw, sh) <= 48 and abs(sw - sh) <= 12:
        return True

    return False


def _is_partition_icon_child(parent: Dict[str, Any], child: Dict[str, Any]) -> bool:
    """Return True for distinct icon/image subcells inside row-style containers.

    Some apps expose rows as a broad list item plus separate icon and text cells.
    An unlabeled icon is still meaningful visible coverage there and should not be
    dropped as decorative just because the parent row is actionable.
    """
    parent_role = (parent.get("role") or "").strip().lower()
    child_role = (child.get("role") or "").strip().lower()
    if parent_role not in {"list item", "table cell", "tree item"}:
        return False
    if child_role not in {"icon", "image"}:
        return False

    parent_rect = parent.get("rect", {})
    child_rect = child.get("rect", {})
    if _rect_containment_ratio(child_rect, parent_rect) < 0.95:
        return False

    cw = max(0, child_rect.get("w", 0))
    ch = max(0, child_rect.get("h", 0))
    if min(cw, ch) < 16 or max(cw, ch) > 96 or abs(cw - ch) > 24:
        return False

    child_area = max(1, cw * ch)
    parent_area = max(1, parent_rect.get("w", 0) * parent_rect.get("h", 0))
    if child_area / parent_area > 0.30:
        return False

    label_hint = _text_norm(parent.get("inner_text", ""))
    if label_hint and not _is_generic_text(label_hint):
        return True

    sibling_text = child.get("_row_partition_has_text_sibling")
    return bool(sibling_text)


def _is_generic_wrapper_text(text: str) -> bool:
    normalized = _text_norm(text)
    return (
        not normalized
        or _is_generic_text(normalized)
        or _is_iconic_text(normalized)
    )


def _semantic_duplicate_loser(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[int]:
    if a.get("source") != b.get("source"):
        return None
    if a.get("app_name") != b.get("app_name"):
        return None
    if not _same_area_or_contained(a.get("rect", {}), b.get("rect", {})):
        return None

    a_role = (a.get("role") or "").strip().lower()
    b_role = (b.get("role") or "").strip().lower()
    a_type = (a.get("type") or "").strip().lower()
    b_type = (b.get("type") or "").strip().lower()
    a_text = _text_norm(a.get("inner_text", ""))
    b_text = _text_norm(b.get("inner_text", ""))

    a_atomic = _is_atomic_leaf_role(a_role, a_type)
    b_atomic = _is_atomic_leaf_role(b_role, b_type)
    a_decorative = _is_decorative_child(a)
    b_decorative = _is_decorative_child(b)

    if _is_same_role_partition_subcell(a, b):
        a_rect = a.get("rect", {})
        b_rect = b.get("rect", {})
        a_area = max(1, a_rect.get("w", 0) * a_rect.get("h", 0))
        b_area = max(1, b_rect.get("w", 0) * b_rect.get("h", 0))
        smaller = a if a_area <= b_area else b
        smaller_rect = smaller.get("rect", {})
        smaller_text = _text_norm(smaller.get("inner_text", ""))
        if not smaller_text and smaller_rect.get("w", 0) <= 14:
            return int(smaller["_dom_index"])
        return None

    row_wrapper_roles = {"list item", "table cell", "tree item"}
    actionable_roles = {
        "push button",
        "toggle button",
        "check box",
        "check menu item",
        "radio button",
        "radio menu item",
        "menu item",
        "page tab",
        "link",
        "combo box",
        "entry",
        "editbar",
        "password text",
        "spin button",
        "switch",
    }
    near_same_wrapper_area = _same_area_or_contained(a.get("rect", {}), b.get("rect", {}))
    if a_role in row_wrapper_roles and b_role in actionable_roles:
        if near_same_wrapper_area and _is_generic_wrapper_text(a_text):
            if b_text and not _is_generic_wrapper_text(b_text):
                return int(a["_dom_index"])
    if b_role in row_wrapper_roles and a_role in actionable_roles:
        if near_same_wrapper_area and _is_generic_wrapper_text(b_text):
            if a_text and not _is_generic_wrapper_text(a_text):
                return int(b["_dom_index"])

    if a_atomic and b_decorative and _is_partition_icon_child(a, b):
        return None
    if b_atomic and a_decorative and _is_partition_icon_child(b, a):
        return None

    if a_atomic and b_decorative:
        if (
            (a_text and b_text and a_text.lower() == b_text.lower())
            or not b_text
            or _is_iconic_text(b_text)
        ):
            return int(b["_dom_index"])
    if b_atomic and a_decorative:
        if (
            (a_text and b_text and a_text.lower() == b_text.lower())
            or not a_text
            or _is_iconic_text(a_text)
        ):
            return int(a["_dom_index"])

    if a_role == "heading" and b_decorative and a_text and a_text.lower() == b_text.lower():
        return int(b["_dom_index"])
    if b_role == "heading" and a_decorative and a_text.lower() == b_text.lower():
        return int(a["_dom_index"])

    return None


def _suppress_redundant_nested_same_area(elements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse parent/child same-area stacks by keeping more semantic node."""
    current = elements
    for _ in range(4):  # converge quickly
        by_idx = {int(e["_dom_index"]): e for e in current if "_dom_index" in e}
        to_drop: set[int] = set()

        for child in current:
            cidx = int(child["_dom_index"])
            pidx = child.get("_parent_dom_index")
            if pidx is None or pidx not in by_idx:
                continue
            parent = by_idx[pidx]
            if child.get("source") != parent.get("source"):
                continue
            if child.get("app_name") != parent.get("app_name"):
                continue
            if not _same_area(child.get("rect", {}), parent.get("rect", {})):
                continue

            parent_protected = _is_protected_top_level_app_window(parent, by_idx)
            child_protected = _is_protected_top_level_app_window(child, by_idx)
            if parent_protected and not child_protected:
                to_drop.add(cidx)
                continue
            if child_protected and not parent_protected:
                to_drop.add(pidx)
                continue

            ct = _text_norm(child.get("inner_text", ""))
            pt = _text_norm(parent.get("inner_text", ""))
            if ct and pt and ct != pt:
                continue

            cp = _role_priority(child.get("role", ""))
            pp = _role_priority(parent.get("role", ""))
            child_empty = not ct
            parent_empty = not pt
            parent_role = (parent.get("role") or "").strip().lower()
            child_role = (child.get("role") or "").strip().lower()
            child_type = (child.get("type") or "").strip().lower()
            parent_children = parent.get("_children_dom_indices") or parent.get("children_indices") or []
            child_children = child.get("_children_dom_indices") or child.get("children_indices") or []
            child_is_leaf_content = (
                not child_children
                and (
                    child_type == "text input"
                    or child_role in {"entry", "editbar", "text"}
                )
            )
            parent_is_soft_container = (
                _is_container_role(parent_role)
                or parent_role in {"page tab list", "menu bar"}
                or bool(parent_children)
            )

            if child_is_leaf_content and parent_is_soft_container:
                to_drop.add(pidx)
                continue

            # Prefer dropping low-priority empty container nodes.
            if pp < cp and parent_empty and parent_role not in {"frame", "desktop frame"}:
                to_drop.add(pidx)
            elif cp < pp and child_empty:
                to_drop.add(cidx)
            elif cp == pp and child_empty:
                to_drop.add(cidx)

        if not to_drop:
            break
        current = _reindex_hierarchy([e for e in current if int(e["_dom_index"]) not in to_drop])

    return current


def _suppress_redundant_same_area_clusters(elements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse same-area same-app clusters when lower node is a generic container."""
    current = elements
    for _ in range(3):
        by_idx = {int(e["_dom_index"]): e for e in current if "_dom_index" in e}
        to_drop: set[int] = set()
        n = len(current)
        for i in range(n):
            a = current[i]
            a_idx = int(a["_dom_index"])
            if a_idx in to_drop:
                continue
            for j in range(i + 1, n):
                b = current[j]
                b_idx = int(b["_dom_index"])
                if b_idx in to_drop:
                    continue

                if a.get("source") != b.get("source"):
                    continue
                if a.get("app_name") != b.get("app_name"):
                    continue
                if not _same_area(a.get("rect", {}), b.get("rect", {})):
                    continue
                if not _texts_compatible(a.get("inner_text", ""), b.get("inner_text", "")):
                    continue

                a_protected = _is_protected_top_level_app_window(a, by_idx)
                b_protected = _is_protected_top_level_app_window(b, by_idx)
                if a_protected and not b_protected:
                    to_drop.add(b_idx)
                    continue
                if b_protected and not a_protected:
                    to_drop.add(a_idx)
                    continue

                a_role = (a.get("role") or "").strip().lower()
                b_role = (b.get("role") or "").strip().lower()
                a_text = _text_norm(a.get("inner_text", ""))
                b_text = _text_norm(b.get("inner_text", ""))
                a_soft = _is_container_role(a_role) or _is_generic_text(a_text)
                b_soft = _is_container_role(b_role) or _is_generic_text(b_text)
                if not (a_soft or b_soft):
                    continue

                a_score = _semantic_score(a)
                b_score = _semantic_score(b)
                if a_score > b_score:
                    loser = b_idx
                elif b_score > a_score:
                    loser = a_idx
                else:
                    loser = b_idx
                to_drop.add(loser)

        if not to_drop:
            break
        current = _reindex_hierarchy([e for e in current if int(e["_dom_index"]) not in to_drop])

    return current


def _suppress_semantic_atomic_duplicates(elements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop decorative child/sibling nodes when a stronger atomic node already captures them."""
    current = elements
    for _ in range(3):
        by_idx = {int(e["_dom_index"]): e for e in current if "_dom_index" in e}
        to_drop: set[int] = set()

        for parent in current:
            children = [
                by_idx[int(cidx)]
                for cidx in parent.get("_children_dom_indices") or parent.get("children_indices") or []
                if int(cidx) in by_idx
            ]
            has_meaningful = any(
                _text_norm(child.get("inner_text", ""))
                and not _is_generic_text(child.get("inner_text", ""))
                and child is not None
                for child in children
            )
            for child in children:
                child["_row_partition_has_text_sibling"] = has_meaningful

        for elem in current:
            eidx = int(elem["_dom_index"])
            pidx = elem.get("_parent_dom_index")
            if pidx is not None and pidx in by_idx:
                if _is_protected_top_level_app_window(by_idx[pidx], by_idx):
                    continue
                loser = _semantic_duplicate_loser(by_idx[pidx], elem)
                if loser is not None:
                    to_drop.add(loser)

        n = len(current)
        for i in range(n):
            a = current[i]
            a_idx = int(a["_dom_index"])
            if a_idx in to_drop:
                continue
            for j in range(i + 1, n):
                b = current[j]
                b_idx = int(b["_dom_index"])
                if b_idx in to_drop:
                    continue
                if _is_protected_top_level_app_window(a, by_idx) or _is_protected_top_level_app_window(b, by_idx):
                    continue
                loser = _semantic_duplicate_loser(a, b)
                if loser is not None:
                    to_drop.add(loser)

        if not to_drop:
            for elem in current:
                elem.pop("_row_partition_has_text_sibling", None)
            break
        current = _reindex_hierarchy([e for e in current if int(e["_dom_index"]) not in to_drop])
        for elem in current:
            elem.pop("_row_partition_has_text_sibling", None)

    return current



def _rescue_unpublished_containers(filtered_elements, leaf_elements, gray):
    """Re-add containers whose children the toolkit never published."""
    from deskshot.extraction.blank_widgets import draws_content as _draws_content

    if gray is None:
        return {"num_rescued": 0, "by_role": {}}

    def draws_content(elem):
        return _draws_content(elem, gray)

    def is_container(role):
        return role in STRUCTURAL_CONTAINER_ROLES or _is_container_role(role)

    rescued = find_unpublished_containers(
        filtered_elements, leaf_elements, gray,
        is_container=is_container, draws_content=draws_content,
    )
    # Fresh indices. The leaf export is re-indexed from zero, so a container
    # copied out of the filtered tree brings an index that already belongs to a
    # leaf - and `_basic_screentag` keys elements by `_dom_index`, so the clone
    # silently replaced a real element in the markup. Measured: 29 leaf elements
    # in one 21-capture batch never reached the ScreenTag this way.
    indices = [
        int(e["_dom_index"]) for e in leaf_elements
        if isinstance(e.get("_dom_index"), int)
    ]
    next_index = (max(indices) + 1) if indices else 0

    by_role = {}
    for elem in rescued:
        clone = copy.deepcopy(elem)
        clone["_source_dom_index"] = elem.get("_dom_index")
        clone["_dom_index"] = next_index
        clone["_parent_dom_index"] = None
        clone["parent_index"] = None
        next_index += 1
        attrs = dict(clone.get("attrs") or {})
        attrs["rescued"] = "unpublished_container"
        clone["attrs"] = attrs
        clone["_children_dom_indices"] = []
        clone["children_indices"] = []
        leaf_elements.append(clone)
        role = str(elem.get("role") or "")
        by_role[role] = by_role.get(role, 0) + 1
    return {"num_rescued": len(rescued), "by_role": by_role}


WINDOW_LIKE_ROLES_FOR_ESCAPE = frozenset(
    {"frame", "window", "dialog", "alert", "desktop frame"}
)


def _drop_boxes_outside_their_window(
    elements: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Remove leaf boxes that sit entirely outside the window they belong to.

    VS Code (Electron) publishes `section` nodes whose AT-SPI geometry is
    stale: measured over 26,056 leaves, nine of them landed on bare wallpaper
    below their own window, and two were outside the viewport altogether. Each
    one is an annotation over pixels that show nothing, which is precisely the
    false positive the quality bar forbids.

    A popup - a menu, a combo dropdown, a tooltip - legitimately extends past
    its parent window, and rejecting those would be much worse than the bug
    being fixed: 2.64% of leaves are outside their window and almost all are
    real, visible menu items. The discriminator is the window stack. A popup is
    its own X window and gets its own, higher `_window_stack_index`; a widget
    claiming its parent window's stack index while lying entirely outside that
    window's rectangle is describing something geometrically impossible.

    Only that impossible case is dropped, and only when the box does not
    overlap its window at all - a partial overlap is ordinary clipping.
    """
    if not elements:
        return {"num_dropped": 0, "dropped": []}

    by_dom = {
        int(e["_dom_index"]): e for e in elements if isinstance(e.get("_dom_index"), int)
    }
    dropped: List[Dict[str, Any]] = []
    for elem in elements:
        if elem.get("source") != "app":
            continue
        if (elem.get("role") or "").strip().lower() in WINDOW_LIKE_ROLES_FOR_ESCAPE:
            continue
        owner = by_dom.get(elem.get("_window_owner_dom_index"))
        if owner is None:
            continue
        if (owner.get("role") or "").strip().lower() not in WINDOW_LIKE_ROLES_FOR_ESCAPE:
            continue
        elem_stack = elem.get("_window_stack_index")
        owner_stack = owner.get("_window_stack_index")
        if not (isinstance(elem_stack, int) and isinstance(owner_stack, int)):
            continue
        if elem_stack != owner_stack:
            continue  # a popup of its own: legitimately outside
        rect = elem.get("rect") or {}
        wrect = owner.get("rect") or {}
        try:
            x, y = int(rect["x"]), int(rect["y"])
            w, h = int(rect["w"]), int(rect["h"])
            wx, wy = int(wrect["x"]), int(wrect["y"])
            ww, wh = int(wrect["w"]), int(wrect["h"])
        except (KeyError, TypeError, ValueError):
            continue
        if w <= 0 or h <= 0:
            continue
        overlap_w = min(x + w, wx + ww) - max(x, wx)
        overlap_h = min(y + h, wy + wh) - max(y, wy)
        if overlap_w > 0 and overlap_h > 0:
            continue  # clipped, not escaped
        dropped.append(elem)

    if dropped:
        drop_ids = {id(e) for e in dropped}
        elements[:] = [e for e in elements if id(e) not in drop_ids]

    return {
        "num_dropped": len(dropped),
        "dropped": [
            {
                "type": e.get("type"),
                "role": e.get("role"),
                "app_name": e.get("app_name"),
                "rect": e.get("rect"),
            }
            for e in dropped[:20]
        ],
    }


def _repair_fragments_against_the_window_stack(
    elements: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Give a visible element back the fragment the occlusion pass lost.

    An element that ships a box is claiming pixels are drawn there, so its
    `visible_fragments` must say the same. Ten leaf elements in 20,206 disagreed:
    a valid published rect and an empty fragment list, which
    `annotate_occlusion_state` then stamped `hidden`. Because
    `check_visible_fragments_present` is all-or-nothing over a whole capture,
    one such element rejected the entire sample - 4.1% of captures, median
    affected share 0.53% of their elements.

    They are not hidden. Every one was VS Code's "install the recommended
    'Python' extension" notification, and looking at the pixels showed it plainly
    on top, its left edge clipped by a homebank window and the rest fully drawn.
    The occlusion pass had already clipped `rect` to that visible remainder and
    then, working from a stale `_visibility_source_rect`, threw the fragments
    away. Dropping these elements instead would have deleted a correct
    annotation of something a human can see.

    The repair recomputes visibility the direct way - the published rect minus
    every window above it in the stack - and only restores a fragment when
    something is genuinely left over. An element that really is behind another
    window keeps its empty list and stays hidden.

    **This is a guard, not the cure.** The fragments were not lost here at all:
    `populate_visible_text` clears an element's geometry when none of its glyphs
    survive the pixel test, and VS Code gives those dialogs an `inner_text` of
    U+FFFC, which has no glyphs to find. That is fixed at its source in
    `_preserve_widget_geometry_for_visible_text`, and with it in place this
    function has not fired on a single real capture. It runs before the text
    pass, so it can only catch the occlusion-side version of the same
    inconsistency - which is worth keeping cheap and tested ahead of a
    600,000-sample run, but should not be mistaken for the thing that fixed it.
    """
    from deskshot.extraction.occlusion import subtract_rect

    if not elements:
        return {"num_repaired": 0, "repaired": []}

    windows = [
        e for e in elements
        if (e.get("role") or "").strip().lower() in WINDOW_LIKE_ROLES_FOR_ESCAPE
        and isinstance(e.get("_window_stack_index"), int)
        and _is_positive_rect(e.get("rect"))
    ]

    repaired: List[Dict[str, Any]] = []
    for elem in elements:
        fragments = elem.get("visible_fragments")
        if isinstance(fragments, list) and fragments:
            continue
        rect = elem.get("rect")
        if not _is_positive_rect(rect):
            continue
        stack = elem.get("_window_stack_index")
        if not isinstance(stack, int):
            continue
        remaining = [dict(rect)]
        for window in windows:
            if window is elem or window.get("_window_stack_index") <= stack:
                continue
            nxt: List[Dict[str, int]] = []
            for piece in remaining:
                nxt.extend(subtract_rect(piece, window["rect"]))
            remaining = [r for r in nxt if r["w"] > 0 and r["h"] > 0]
            if not remaining:
                break
        if not remaining:
            continue  # genuinely behind something: leave it hidden
        elem["visible_fragments"] = remaining
        elem["is_occluded"] = not (len(remaining) == 1 and remaining[0] == rect)
        elem["occlusion_state"] = "partial" if elem["is_occluded"] else "none"
        repaired.append(elem)

    return {
        "num_repaired": len(repaired),
        "repaired": [
            {"type": e.get("type"), "role": e.get("role"),
             "app_name": e.get("app_name"), "rect": e.get("rect")}
            for e in repaired[:20]
        ],
    }


def _is_positive_rect(rect: Any) -> bool:
    try:
        return int(rect["w"]) > 0 and int(rect["h"]) > 0
    except (KeyError, TypeError, ValueError):
        return False


def _build_leaf_elements(elements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build a flat, coverage-oriented leaf export from the filtered hierarchy.

    The goal is to keep atomic controls such as buttons, tabs, links, and text
    inputs even when they own decorative text/icon children, while excluding
    broad window/container nodes from the leaf-only training view.
    """
    if not elements:
        return []

    by_idx = {int(e["_dom_index"]): e for e in elements if "_dom_index" in e}
    parent_by_idx = {
        int(e["_dom_index"]): e.get("_parent_dom_index")
        for e in elements
        if "_dom_index" in e
    }
    children_map = {
        int(e["_dom_index"]): list(e.get("_children_dom_indices") or e.get("children_indices") or [])
        for e in elements
        if "_dom_index" in e
    }

    for parent in elements:
        children = [
            by_idx[int(cidx)]
            for cidx in parent.get("_children_dom_indices") or parent.get("children_indices") or []
            if int(cidx) in by_idx
        ]
        has_meaningful = any(
            _text_norm(child.get("inner_text", ""))
            and not _is_generic_text(child.get("inner_text", ""))
            for child in children
        )
        for child in children:
            child["_row_partition_has_text_sibling"] = has_meaningful

    suppressed_children: set[int] = set()
    promoted_parents: set[int] = set()
    for parent in elements:
        pidx = int(parent["_dom_index"])
        if not _is_atomic_leaf_role(parent.get("role", ""), parent.get("type", "")):
            continue
        for cidx in children_map.get(pidx, []):
            child = by_idx.get(cidx)
            if child is None:
                continue
            loser = _semantic_duplicate_loser(parent, child)
            if loser == cidx or _should_leaf_suppress_atomic_child(parent, child):
                suppressed_children.add(cidx)
                promoted_parents.add(pidx)

    keep: List[Dict[str, Any]] = []
    for elem in elements:
        idx = int(elem["_dom_index"])
        if idx in suppressed_children:
            continue

        surviving_children = [
            cidx for cidx in children_map.get(idx, [])
            if cidx not in suppressed_children and cidx in by_idx
        ]
        if _keep_leaf_window_candidate(elem, by_idx):
            keep.append(copy.deepcopy(elem))
            continue
        if idx in promoted_parents:
            keep.append(copy.deepcopy(elem))
            continue
        if surviving_children:
            if _keep_atomic_parent_with_surviving_children(
                elem,
                surviving_children=surviving_children,
                by_idx=by_idx,
            ):
                keep.append(copy.deepcopy(elem))
                continue
            continue
        if not _keep_leaf_candidate(
            elem,
            by_idx=by_idx,
            parent_by_idx=parent_by_idx,
            children_map=children_map,
        ):
            continue
        keep.append(copy.deepcopy(elem))

    flattened: List[Dict[str, Any]] = []
    keep_local_by_source_dom: Dict[int, int] = {}
    leaf_window_source_doms: set[int] = set()
    for new_idx, elem in enumerate(keep):
        source_dom = int(elem.get("_dom_index"))
        elem["_source_dom_index"] = source_dom
        elem["_source_parent_dom_index"] = elem.get("_parent_dom_index")
        if _keep_leaf_window_candidate(elem, by_idx):
            leaf_window_source_doms.add(source_dom)
        keep_local_by_source_dom[source_dom] = new_idx
        elem["_dom_index"] = new_idx
        elem["_parent_dom_index"] = None
        elem["parent_index"] = None
        elem["_children_dom_indices"] = []
        elem["children_indices"] = []
        flattened.append(elem)

    for elem in elements:
        elem.pop("_row_partition_has_text_sibling", None)

    for elem in flattened:
        source_dom = elem.get("_source_dom_index")
        if not isinstance(source_dom, int) or source_dom in leaf_window_source_doms:
            continue
        owner_source_dom = _find_leaf_window_owner_by_window_hints(
            elem,
            flattened=flattened,
            keep_local_by_source_dom=keep_local_by_source_dom,
            leaf_window_source_doms=leaf_window_source_doms,
        )
        if _popup_stack_mismatches_leaf_owner(
            elem,
            owner_source_dom,
            flattened=flattened,
            keep_local_by_source_dom=keep_local_by_source_dom,
        ):
            owner_source_dom = None
        if owner_source_dom is None:
            owner_source_dom = _find_leaf_window_owner(
                source_dom,
                parent_by_idx=parent_by_idx,
                leaf_window_source_doms=leaf_window_source_doms,
            )
            if _popup_stack_mismatches_leaf_owner(
                elem,
                owner_source_dom,
                flattened=flattened,
                keep_local_by_source_dom=keep_local_by_source_dom,
            ):
                owner_source_dom = None
        if owner_source_dom is None:
            owner_source_dom = _find_leaf_window_owner_by_app_window(
                elem,
                flattened=flattened,
                keep_local_by_source_dom=keep_local_by_source_dom,
                leaf_window_source_doms=leaf_window_source_doms,
            )
            if _popup_stack_mismatches_leaf_owner(
                elem,
                owner_source_dom,
                flattened=flattened,
                keep_local_by_source_dom=keep_local_by_source_dom,
            ):
                owner_source_dom = None
        if owner_source_dom is None:
            continue
        owner_local_idx = keep_local_by_source_dom.get(owner_source_dom)
        if owner_local_idx is None:
            continue
        local_idx = int(elem["_dom_index"])
        elem["_parent_dom_index"] = owner_local_idx
        elem["parent_index"] = owner_local_idx
        flattened[owner_local_idx]["_children_dom_indices"].append(local_idx)
        flattened[owner_local_idx]["children_indices"].append(local_idx)

    return flattened


def _keep_leaf_candidate(
    elem: Dict[str, Any],
    *,
    by_idx: Dict[int, Dict[str, Any]] | None = None,
    parent_by_idx: Dict[int, Any] | None = None,
    children_map: Dict[int, List[int]] | None = None,
) -> bool:
    role = (elem.get("role") or "").strip().lower()
    elem_type = (elem.get("type") or "").strip().lower()
    text = _text_norm(elem.get("inner_text", ""))

    if _is_window_like_role(role):
        return False
    if _is_atomic_leaf_role(role, elem_type):
        return True
    if (
        by_idx is not None
        and parent_by_idx is not None
        and children_map is not None
        and _is_partition_icon_leaf_candidate(
            elem,
            by_idx=by_idx,
            parent_by_idx=parent_by_idx,
            children_map=children_map,
        )
    ):
        return True
    if _is_decorative_child(elem):
        if _is_standalone_graphic(elem, by_idx=by_idx, parent_by_idx=parent_by_idx):
            return True
        return bool(text)
    if role in STRUCTURAL_CONTAINER_ROLES or _is_container_role(role):
        return bool(text) and not _is_generic_text(text)
    return True


def _is_standalone_graphic(
    elem: Dict[str, Any],
    *,
    by_idx: Dict[int, Dict[str, Any]] | None,
    parent_by_idx: Dict[int, Any] | None,
) -> bool:
    """A nameless icon that hangs off a container, so nothing else annotates it.

    An icon inside a button or a table cell is decoration: the control around it
    is annotated and its box already covers the graphic. An icon whose parent is
    a window or a plain container has no such owner - drop it and that part of
    the screen carries no annotation at all.

    Measured on the 5-app scene: xarchiver draws a 16x20 "no entry" status
    graphic at (1225,1338), reported by AT-SPI as a nameless `icon` child of the
    frame. It reached the filtered export and was dropped from the leaf export,
    and was the only widget-level gap left in that capture after the tree-cell
    and border work.
    """
    role = (elem.get("role") or "").strip().lower()
    if role not in {"icon", "image"}:
        return False
    if _text_norm(elem.get("inner_text", "")):
        # A named icon is already kept by the caller's text rule.
        return False
    if by_idx is None or parent_by_idx is None:
        return False
    idx = elem.get("_dom_index")
    if not isinstance(idx, int):
        return False
    parent_idx = parent_by_idx.get(idx)
    if not isinstance(parent_idx, int):
        return False
    parent = by_idx.get(parent_idx)
    if parent is None:
        return False
    parent_role = (parent.get("role") or "").strip().lower()
    parent_type = (parent.get("type") or "").strip().lower()
    if _is_atomic_leaf_role(parent_role, parent_type):
        return False
    if _is_decorative_child(parent):
        return False
    return (
        _is_window_like_role(parent_role)
        or parent_role in STRUCTURAL_CONTAINER_ROLES
        or _is_container_role(parent_role)
    )


def _keep_atomic_parent_with_surviving_children(
    elem: Dict[str, Any],
    *,
    surviving_children: List[int],
    by_idx: Dict[int, Dict[str, Any]],
) -> bool:
    """Keep certain atomic parents even when they own meaningful child controls.

    Browser tabs are the main case: the tab body is a click target and its
    close button is another click target. Leaf exports should keep both rather
    than dropping the tab just because the close button survives.
    """
    role = (elem.get("role") or "").strip().lower()
    if not surviving_children:
        return False

    if role == "menu":
        # `or -1`, not a `.get` default: a key that is *present and None* skips
        # the default entirely, and `_parent_dom_index` is explicitly None for a
        # root element. `int(None)` then raised, which failed the capture and -
        # before episodes learned to survive a failed action - ended the whole
        # trajectory. This was the single largest source of step failures.
        parent = by_idx.get(int(elem.get("_parent_dom_index") or -1))
        parent_role = (parent.get("role") or "").strip().lower() if parent else ""
        if parent_role != "menu bar":
            return False
        for cidx in surviving_children:
            child = by_idx.get(int(cidx))
            if child is None:
                continue
            child_role = (child.get("role") or "").strip().lower()
            if child_role not in {"menu", "popup menu", "menu item", "check menu item", "radio menu item", "separator"}:
                return False
        return True

    if role != "page tab":
        return False

    for cidx in surviving_children:
        child = by_idx.get(int(cidx))
        if child is None:
            continue
        child_role = (child.get("role") or "").strip().lower()
        child_type = (child.get("type") or "").strip().lower()
        if child_role in {"push button", "icon", "image", "label", "static", "text"}:
            continue
        if child_type == "text":
            continue
        return False
    return True


def _should_leaf_suppress_atomic_child(
    parent: Dict[str, Any],
    child: Dict[str, Any],
) -> bool:
    parent_role = (parent.get("role") or "").strip().lower()
    parent_type = (parent.get("type") or "").strip().lower()
    if not _is_atomic_leaf_role(parent_role, parent_type):
        return False

    child_role = (child.get("role") or "").strip().lower()
    child_type = (child.get("type") or "").strip().lower()
    child_text = _text_norm(child.get("inner_text", ""))

    if _is_same_role_partition_subcell(parent, child):
        return False
    if _is_partition_icon_child(parent, child):
        return False

    if child_role in {"icon", "image"}:
        return _looks_like_icon_asset_name(child_text)

    if child_type == "text":
        if not child_text:
            return True
        parent_text = _text_norm(parent.get("inner_text", ""))
        if parent_text and child_text.lower() == parent_text.lower():
            return True

    if _is_decorative_child(child) and (not child_text or _is_iconic_text(child_text)):
        return True

    return False


def _is_partition_icon_leaf_candidate(
    elem: Dict[str, Any],
    *,
    by_idx: Dict[int, Dict[str, Any]],
    parent_by_idx: Dict[int, Any],
    children_map: Dict[int, List[int]],
) -> bool:
    role = (elem.get("role") or "").strip().lower()
    if role not in {"icon", "image"}:
        return False
    source_dom = elem.get("_dom_index")
    if not isinstance(source_dom, int):
        return False
    parent_idx = parent_by_idx.get(source_dom)
    if not isinstance(parent_idx, int):
        return False
    parent = by_idx.get(parent_idx)
    if parent is None:
        return False
    if not _is_partition_icon_child(parent, elem):
        return False

    parent_text = _text_norm(parent.get("inner_text", ""))
    if parent_text and not _is_generic_text(parent_text):
        return True

    for sibling_idx in children_map.get(parent_idx, []):
        if sibling_idx == source_dom:
            continue
        sibling = by_idx.get(int(sibling_idx))
        if sibling is None:
            continue
        sibling_text = _text_norm(sibling.get("inner_text", ""))
        if sibling_text and not _is_generic_text(sibling_text):
            return True
    return False


def _keep_leaf_window_candidate(
    elem: Dict[str, Any],
    by_idx: Dict[int, Dict[str, Any]] | None = None,
) -> bool:
    """Keep only real top-level app windows in flat leaf exports."""
    return _is_protected_top_level_app_window(elem, by_idx)


def _find_leaf_window_owner(
    source_dom: int,
    *,
    parent_by_idx: Dict[int, Any],
    leaf_window_source_doms: set[int],
) -> Optional[int]:
    """Find the nearest preserved top-level app window ancestor."""
    seen: set[int] = set()
    parent = parent_by_idx.get(source_dom)
    while isinstance(parent, int) and parent not in seen:
        if parent in leaf_window_source_doms:
            return parent
        seen.add(parent)
        parent = parent_by_idx.get(parent)
    return None


def _find_leaf_window_owner_by_app_window(
    elem: Dict[str, Any],
    *,
    flattened: List[Dict[str, Any]],
    keep_local_by_source_dom: Dict[int, int],
    leaf_window_source_doms: set[int],
) -> Optional[int]:
    """Fallback leaf-to-window ownership by same app + geometric containment."""
    app_name = (elem.get("app_name") or "").strip().lower()
    source = (elem.get("source") or "").strip().lower()
    rect = elem.get("rect", {})
    if source != "app" or not app_name:
        return None

    best_owner_source: Optional[int] = None
    best_area: Optional[int] = None
    for source_dom in leaf_window_source_doms:
        local_idx = keep_local_by_source_dom.get(source_dom)
        if local_idx is None or not (0 <= local_idx < len(flattened)):
            continue
        window_elem = flattened[local_idx]
        if (window_elem.get("app_name") or "").strip().lower() != app_name:
            continue
        if _rect_containment_ratio(rect, window_elem.get("rect", {})) < 0.98:
            continue
        window_rect = window_elem.get("rect", {})
        area = int(window_rect.get("w", 0)) * int(window_rect.get("h", 0))
        if best_area is None or area < best_area:
            best_area = area
            best_owner_source = source_dom

    return best_owner_source


def _popup_stack_mismatches_leaf_owner(
    elem: Dict[str, Any],
    owner_source_dom: Optional[int],
    *,
    flattened: List[Dict[str, Any]],
    keep_local_by_source_dom: Dict[int, int],
) -> bool:
    if owner_source_dom is None:
        return False
    role = (elem.get("role") or "").strip().lower()
    if role not in {"menu", "popup menu", "menu item", "check menu item", "radio menu item", "separator"}:
        return False
    elem_stack = elem.get("_window_stack_index")
    if not isinstance(elem_stack, int):
        return False
    owner_local_idx = keep_local_by_source_dom.get(owner_source_dom)
    if owner_local_idx is None or not (0 <= owner_local_idx < len(flattened)):
        return False
    owner_stack = flattened[owner_local_idx].get("_window_stack_index")
    return isinstance(owner_stack, int) and owner_stack != elem_stack


def _find_leaf_window_owner_by_window_hints(
    elem: Dict[str, Any],
    *,
    flattened: List[Dict[str, Any]],
    keep_local_by_source_dom: Dict[int, int],
    leaf_window_source_doms: set[int],
) -> Optional[int]:
    """Prefer occlusion-derived window hints over loose same-app containment.

    Overlapping windows from the same app are common for dialogs/file choosers.
    In those cases, `_window_stack_index` and `_window_owner_dom_index` are a
    much stronger ownership signal than geometric containment alone.
    """
    app_name = (elem.get("app_name") or "").strip().lower()
    source = (elem.get("source") or "").strip().lower()
    if source != "app" or not app_name:
        return None

    owner_dom_hint = elem.get("_window_owner_dom_index")
    stack_hint = elem.get("_window_stack_index")

    owner_dom_matches: list[int] = []
    stack_matches: list[int] = []
    for source_dom in leaf_window_source_doms:
        local_idx = keep_local_by_source_dom.get(source_dom)
        if local_idx is None or not (0 <= local_idx < len(flattened)):
            continue
        window_elem = flattened[local_idx]
        if (window_elem.get("app_name") or "").strip().lower() != app_name:
            continue

        if isinstance(owner_dom_hint, int):
            if window_elem.get("_window_owner_dom_index") == owner_dom_hint:
                owner_dom_matches.append(source_dom)
                continue
            if window_elem.get("_source_dom_index") == owner_dom_hint:
                owner_dom_matches.append(source_dom)
                continue
        if isinstance(stack_hint, int) and window_elem.get("_window_stack_index") == stack_hint:
            stack_matches.append(source_dom)

    if len(owner_dom_matches) == 1:
        return owner_dom_matches[0]
    if len(stack_matches) == 1:
        return stack_matches[0]

    if owner_dom_matches:
        return _select_best_leaf_window_by_geometry(
            elem,
            owner_dom_matches,
            flattened=flattened,
            keep_local_by_source_dom=keep_local_by_source_dom,
        )
    if stack_matches:
        return _select_best_leaf_window_by_geometry(
            elem,
            stack_matches,
            flattened=flattened,
            keep_local_by_source_dom=keep_local_by_source_dom,
        )
    return None


def _select_best_leaf_window_by_geometry(
    elem: Dict[str, Any],
    candidate_source_doms: List[int],
    *,
    flattened: List[Dict[str, Any]],
    keep_local_by_source_dom: Dict[int, int],
) -> Optional[int]:
    rect = elem.get("rect", {})
    best_source: Optional[int] = None
    best_score: Optional[tuple[float, int]] = None
    for source_dom in candidate_source_doms:
        local_idx = keep_local_by_source_dom.get(source_dom)
        if local_idx is None or not (0 <= local_idx < len(flattened)):
            continue
        window_elem = flattened[local_idx]
        window_rect = window_elem.get("rect", {})
        contain = _rect_containment_ratio(rect, window_rect)
        area = int(window_rect.get("w", 0)) * int(window_rect.get("h", 0))
        score = (contain, -area)
        if best_score is None or score > best_score:
            best_score = score
            best_source = source_dom
    return best_source


def _apply_filtering(
    elements: List[Dict[str, Any]],
    config: PipelineConfig,
) -> List[Dict[str, Any]]:
    """Apply webshot filtering if available, otherwise return as-is."""
    try:
        ensure_webshot_importable()
        from webshot.filtering import filter_elements

        return filter_elements(
            elements,
            viewport_w=config.session.display.width,
            viewport_h=config.session.display.height,
            iou_threshold=config.iou_threshold,
            containment_threshold=config.containment_threshold,
            min_box_size=config.min_box_size,
            max_box_size=config.max_box_size,
        )
    except ImportError:
        return elements


#: Widest a title bar can plausibly be. Beyond this the top band is a header
#: bar with its own widgets, not a title strip, and inventing one box over it
#: would cover content that is already annotated.
MAX_TITLE_BAR_HEIGHT = 48

#: A band already this well covered by real elements needs no synthetic box.
TITLE_BAR_COVERED_RATIO = 0.5

#: Titles that carry no information. GTK reports an object-replacement char for
#: windows whose title is drawn as an icon.
_EMPTY_TITLES = {"", "none", "\ufffc"}


def synthesize_title_bars(elements: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Add an element for each window's title bar.

    A window's title is visible text at a specific place on screen, and nothing
    covered it: the frame element carries the title as *text*, but its rect is
    the whole window, so no box sits where the title is drawn. Measured across a
    12-scene batch, 42% of all flagged uncovered ink was in the title-bar band.

    The band's height is taken from the window's own contents - the gap between
    the frame's top and its topmost child - rather than a fixed guess, because
    it varies with theme and with whether the app draws its own decoration. A
    window whose top band is already covered by real widgets (a GTK header bar
    with tabs and buttons) is left alone.
    """
    added: List[Dict[str, Any]] = []
    # Synthetic elements join an already-indexed list, so they need indices of
    # their own: downstream stages key on `_dom_index` and raise without it.
    next_index = max(
        (int(e["_dom_index"]) for e in elements if isinstance(e.get("_dom_index"), int)),
        default=-1,
    ) + 1
    windows = [
        e for e in elements
        if _is_window_like_role(e.get("role", "")) and isinstance(e.get("rect"), dict)
    ]
    for window in windows:
        rect = window["rect"]
        wx, wy = int(rect.get("x", 0)), int(rect.get("y", 0))
        ww, wh = int(rect.get("w", 0)), int(rect.get("h", 0))
        if ww < 40 or wh < 40:
            continue
        title = str(
            window.get("visible_text") or window.get("inner_text") or window.get("name") or ""
        ).strip()
        if title.lower() in _EMPTY_TITLES:
            continue

        inside = [
            e for e in elements
            if e is not window
            and isinstance(e.get("rect"), dict)
            and e.get("app_name") == window.get("app_name")
            and int(e["rect"].get("w", 0)) > 0
            and wy <= int(e["rect"].get("y", 0)) < wy + wh
            and wx - 2 <= int(e["rect"].get("x", 0)) < wx + ww
        ]
        if not inside:
            continue
        band_h = min(int(min(e["rect"]["y"] for e in inside)) - wy, MAX_TITLE_BAR_HEIGHT)
        if band_h < 8:
            # The first widget starts at the very top, so this window draws its
            # own header bar and there is no title strip to add.
            continue

        band = {"x": wx, "y": wy, "w": ww, "h": band_h}
        overlap = sum(
            _rect_overlap_area(band, e["rect"]) for e in inside
            if int(e["rect"].get("y", 0)) < wy + band_h
        )
        if overlap >= TITLE_BAR_COVERED_RATIO * ww * band_h:
            continue

        added.append({
            "tag": "title bar",
            "role": "title bar",
            "name": title,
            "attrs": {"synthesized": "title_bar"},
            "rect": band,
            "z": window.get("z", 0),
            "position": "absolute",
            "inner_text": title,
            "visible_text": title,
            "visible_text_status": "full_visible",
            "type": get_screentag_class("title bar") or "Heading",
            "vlm_label": "Heading",
            "frame_index": window.get("frame_index", 0),
            "source": window.get("source", "app"),
            "app_name": window.get("app_name"),
            "occlusion_state": "none",
            "visible_fragments": [dict(band)],
            "is_occluded": False,
            "_window_stack_index": window.get("_window_stack_index"),
            "_parent_dom_index": window.get("_dom_index"),
            "_dom_index": next_index,
            "_children_dom_indices": [],
            "_depth": int(window.get("_depth", 0)) + 1,
            "_atspi_path": [],
            "parent_index": None,
            "children_indices": [],
            "id": None,
            "classes": None,
            "reading_order_index": window.get("reading_order_index"),
        })
        next_index += 1

    elements.extend(added)
    return {"num_title_bars": len(added)}


def _rect_overlap_area(a: Dict[str, Any], b: Dict[str, Any]) -> int:
    x0 = max(int(a["x"]), int(b["x"]))
    y0 = max(int(a["y"]), int(b["y"]))
    x1 = min(int(a["x"]) + int(a["w"]), int(b["x"]) + int(b["w"]))
    y1 = min(int(a["y"]) + int(a["h"]), int(b["y"]) + int(b["h"]))
    return max(0, x1 - x0) * max(0, y1 - y0)


def _elements_to_screentag(
    elements: List[Dict[str, Any]],
    viewport_w: int,
    viewport_h: int,
) -> str:
    """Serialize elements to ScreenTag format."""
    return _basic_screentag(elements, viewport_w, viewport_h)


def escape_screentag_text(text: str) -> str:
    """Escape text so its content cannot be read as ScreenTag markup.

    Measured on a Bluefish window showing an HTML file: the editor's contents
    were serialized raw, so the document's own `<html>`, `<head>` and `<title>`
    appeared in the ScreenTag as though they were elements - eleven phantom tags
    in one capture. Any app that displays markup or code does this: editors,
    terminals, a browser viewing source.

    `&` is escaped first, or escaping `<` would corrupt an existing entity.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _serialized_text(elem: Dict[str, Any]) -> str:
    """Text for ScreenTag, marking where occlusion removed characters.

    A clipped string is otherwise indistinguishable from complete content, which
    teaches a model that the truncated form is the whole value.
    """
    # `None` is a decision, not missing data.
    #
    # Every withheld status sets `visible_text` to None, and falling through to
    # `inner_text` put the string back into the markup - so a Go To dialog that
    # is not drawn at all (292x160 of uniform white, every child marked
    # `no_ink`) still serialized "Column number:" and "Line number:", and the
    # whole name/glyph separation was undone at the last step. Only an element
    # that never had the key answered falls back.
    if "visible_text" in elem and elem["visible_text"] is None:
        return ""
    if elem.get("visible_text_status") == "unsupported_partial":
        return ""
    marked = elem.get("visible_text_marked")
    if isinstance(marked, str) and marked:
        # Escape around the occlusion marker, never through it: the marker is
        # markup we are deliberately emitting, the text around it is not.
        return TEXT_CLIP_MARKER.join(
            escape_screentag_text(part) for part in marked.split(TEXT_CLIP_MARKER)
        )
    text = elem.get("visible_text")
    if text is None:
        text = elem.get("inner_text") or ""
    return escape_screentag_text(text)


#: Roles whose checked state is part of what the screen shows. Gating on role
#: rather than on the `checkable` flag is deliberate: measured on this stack, a
#: GTK dialog's check boxes report `checkable=False` while carrying a real
#: `checked` value, so trusting the flag would drop the state of every control
#: that has one.
CHECKABLE_ROLES = {
    "check box",
    "radio button",
    "toggle button",
    "check menu item",
    "radio menu item",
}


def _state_tokens(elem: Dict[str, Any]) -> str:
    """Widget state that a screenshot shows but geometry and text do not.

    A ticked box and an unticked one occupy the same rectangle and carry the same
    label, so without these tokens the two serialize identically - the
    representation cannot express the difference the screen is displaying, and a
    model trained on it cannot be asked to read a settings panel.

    Two conventions, chosen by whether absence is informative. Checked state is
    emitted both ways, because "unchecked" is a fact about the screen rather than
    missing data. Disabled, expanded and selected are emitted only when true,
    since almost everything is enabled, unexpanded and unselected, and spending a
    token on each would triple the length of a typical tag for no information.
    """
    interaction = elem.get("interaction")
    if not isinstance(interaction, dict):
        return ""
    role = str(elem.get("role") or "").strip().lower()
    tokens: List[str] = []

    if role in CHECKABLE_ROLES or interaction.get("checkable"):
        tokens.append("<checked/>" if interaction.get("checked") else "<unchecked/>")
    if interaction.get("expandable"):
        tokens.append("<expanded/>" if interaction.get("expanded") else "<collapsed/>")
    if interaction.get("selected"):
        tokens.append("<selected/>")
    if interaction.get("enabled") is False:
        tokens.append("<disabled/>")
    return "".join(tokens)


def _basic_screentag(
    elements: List[Dict[str, Any]],
    viewport_w: int,
    viewport_h: int,
) -> str:
    """Serialize ScreenTag using window-nested leaf ownership when available."""

    def _tag_name(elem: Dict[str, Any]) -> str:
        """`Button` and `File Icon` become `<Button>` and `<File_Icon>`.

        The case is not cosmetic. The project's patched tokenizers carry 167
        element tags as single tokens and every one of them is capitalised -
        `<Button>`, `<File_Icon>`, `<Toggles>`. Lowercasing here meant the
        corpus emitted `<button>` (3 tokens) where the vocabulary held
        `<Button>` (1), twice per element, ~135 elements per capture.
        All 27 types the corpus produces have a capitalised single token; only
        three exist in lowercase. Measured over 150 captures, dropping the
        lowercase recovers **21%** of the sequence - 1,801 tokens to 1,420.
        """
        return (elem.get("type") or elem.get("tag", "unknown")).replace(" ", "_")

    def _loc_tokens(rect: Dict[str, Any]) -> str:
        l = int(rect.get("x", 0) / viewport_w * 500)
        t = int(rect.get("y", 0) / viewport_h * 500)
        r = int((rect.get("x", 0) + rect.get("w", 0)) / viewport_w * 500)
        b = int((rect.get("y", 0) + rect.get("h", 0)) / viewport_h * 500)
        return f"<loc_{l}><loc_{t}><loc_{r}><loc_{b}>"

    def _reading_order(elem: Dict[str, Any]) -> tuple[int, int]:
        return (int(elem.get("reading_order_index", 10**9)), int(elem.get("_dom_index", 10**9)))

    def _same_rect(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
        return (
            int(a.get("x", 0) or 0) == int(b.get("x", 0) or 0)
            and int(a.get("y", 0) or 0) == int(b.get("y", 0) or 0)
            and int(a.get("w", 0) or 0) == int(b.get("w", 0) or 0)
            and int(a.get("h", 0) or 0) == int(b.get("h", 0) or 0)
        )

    def _meaningful_fragments(elem: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not elem.get("is_occluded"):
            return []
        rect = elem.get("rect", {}) or {}
        fragments = [
            fragment
            for fragment in (elem.get("visible_fragments") or [])
            if isinstance(fragment, dict)
        ]
        if len(fragments) == 1 and _same_rect(rect, fragments[0]):
            return []
        return fragments

    def _window_title(elem: Dict[str, Any]) -> str:
        """A window's own title, as a labelled token rather than as its text.

        Windows serialized with nothing but coordinates, because a frame's title
        is drawn in its title bar and not across the window, so it is correctly
        withheld from `visible_text`. That left every `<window>` in the markup
        indistinguishable from every other, and a reader could not tell which
        application a block belonged to.

        It goes in its own `<title>` token, not as the tag's text: the tag's
        text means "these glyphs are drawn inside this box", and overloading it
        here would put the same string in two places (the title bar element
        already carries it, correctly) and break that meaning.
        """
        if not _is_window_like_role(elem.get("role", "")):
            return ""
        title = _text_norm(elem.get("name") or "")
        if not title or _is_generic_text(title):
            return ""
        return f"<title>{escape_screentag_text(title)}</title>"

    def _serialize_elem(elem: Dict[str, Any]) -> str:
        tag = _tag_name(elem)
        rect = elem.get("rect", {})
        text = _serialized_text(elem)
        fragments = _meaningful_fragments(elem)
        fragment_text = "".join(
            f"<fragment>{_loc_tokens(fragment)}</fragment>"
            for fragment in fragments
        )
        return f"<{tag}>{_loc_tokens(rect)}{fragment_text}{_state_tokens(elem)}{text}</{tag}>"

    by_idx = {int(e["_dom_index"]): e for e in elements if isinstance(e.get("_dom_index"), int)}
    window_nodes = [
        e for e in elements
        if _is_window_like_role(e.get("role", ""))
    ]
    window_ids = {int(e["_dom_index"]) for e in window_nodes if isinstance(e.get("_dom_index"), int)}
    child_ids: set[int] = set()
    for window in window_nodes:
        for cidx in window.get("children_indices") or window.get("_children_dom_indices") or []:
            if isinstance(cidx, int) and cidx in by_idx:
                child_ids.add(cidx)

    parts = ["<screentag>"]

    for window in sorted(window_nodes, key=_reading_order):
        tag = _tag_name(window)
        rect = window.get("rect", {})
        text = _serialized_text(window)
        fragment_text = "".join(
            f"<fragment>{_loc_tokens(fragment)}</fragment>"
            for fragment in _meaningful_fragments(window)
        )
        parts.append(
            f"<{tag}>{_loc_tokens(rect)}{fragment_text}{_state_tokens(window)}"
            f"{_window_title(window)}{text}"
        )
        child_indices = [
            cidx for cidx in (window.get("children_indices") or window.get("_children_dom_indices") or [])
            if isinstance(cidx, int) and cidx in by_idx and cidx not in window_ids
        ]
        children = sorted((by_idx[cidx] for cidx in child_indices), key=_reading_order)
        for child in children:
            parts.append(_serialize_elem(child))
        parts.append(f"</{tag}>")

    top_level_nonwindow = [
        e for e in elements
        if int(e.get("_dom_index", -1)) not in child_ids
        and int(e.get("_dom_index", -1)) not in window_ids
    ]
    for elem in sorted(top_level_nonwindow, key=_reading_order):
        parts.append(_serialize_elem(elem))

    parts.append("</screentag>")
    return "".join(parts)


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    """Write via a temporary file and rename, so a kill leaves no half-file.

    A SIGKILL landing mid-write left a **0-byte `meta.json`** in a real run, and
    that is worse than leaving nothing: `--resume` decides a scene is finished by
    the presence of its meta file, so the scene would be skipped forever and an
    incomplete sample would stay in the corpus. Any consumer reading it also
    crashes. Rename within a directory is atomic, so a reader sees either the
    old file or the whole new one.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(payload)
    tmp.replace(path)


def _save_json(path: Path, data: Any) -> None:
    """Save JSON data to file.

    Compact by default. Two-space indentation cost a third of the bytes in every
    element file - measured at 1.44 MB versus 0.96 MB for one capture's
    unfiltered elements - which over a million samples is most of a terabyte
    spent on whitespace. Nothing reads these by eye; `scripts/inspect_annotations.py`
    and `jq` both format on demand. Set DESKSHOT_PRETTY_JSON=1 to get the
    indented form back while debugging.
    """
    pretty = os.environ.get("DESKSHOT_PRETTY_JSON", "").strip().lower() in {"1", "true", "yes"}
    try:
        import orjson

        payload = orjson.dumps(data, option=orjson.OPT_INDENT_2) if pretty else orjson.dumps(data)
    except ImportError:
        text = (
            json.dumps(data, indent=2, ensure_ascii=False)
            if pretty
            else json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        )
        payload = text.encode("utf-8")
    _write_bytes_atomic(path, payload)
