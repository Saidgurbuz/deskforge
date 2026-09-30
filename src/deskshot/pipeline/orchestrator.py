"""Multi-app orchestration: load manifests, generate work items, run worker."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from deskshot.config import (
    AppManifest,
    InteractionChain,
    InteractionSequence,
    PipelineConfig,
    WorkItem,
    CONFIGS_DIR,
)
from deskshot.pipeline.worker import SessionWorker

logger = logging.getLogger(__name__)


def load_manifests(
    app_configs_dir: Optional[Path] = None,
    app_names: Optional[List[str]] = None,
) -> List[AppManifest]:
    """Load app manifests from YAML files.

    Args:
        app_configs_dir: Directory containing app YAML files.
        app_names: If provided, only load these apps (by filename stem).

    Returns:
        List of AppManifest objects.
    """
    config_dir = app_configs_dir or (CONFIGS_DIR / "apps")
    manifests = []

    for yaml_path in sorted(config_dir.glob("*.yaml")):
        if app_names and yaml_path.stem not in app_names:
            continue
        try:
            manifest = AppManifest.from_yaml(yaml_path)
            manifests.append(manifest)
            logger.info(
                "Loaded manifest: %s (%d interactions, %d chains)",
                manifest.app_name,
                len(manifest.interactions),
                len(manifest.chains),
            )
        except Exception:
            logger.exception(f"Failed to load manifest: {yaml_path}")

    return manifests


def generate_work_items(
    manifests: List[AppManifest],
    interaction_names: Optional[List[str]] = None,
) -> List[WorkItem]:
    """Generate (manifest, interaction) work items.

    Args:
        manifests: List of app manifests.
        interaction_names: If provided, only include these interaction names.

    Returns:
        List of WorkItem entries.
    """
    items: List[WorkItem] = []
    for manifest in manifests:
        for interaction in manifest.interactions:
            if interaction_names and interaction.name not in interaction_names:
                continue
            items.append(WorkItem(manifest=manifest, interaction=interaction))
        for chain in manifest.chains:
            if interaction_names and chain.name not in interaction_names:
                continue
            items.append(WorkItem(manifest=manifest, chain=chain))

    logger.info(f"Generated {len(items)} work items from "
                 f"{len(manifests)} apps")
    return items


def run_pipeline(
    config: PipelineConfig,
    app_names: Optional[List[str]] = None,
    interaction_names: Optional[List[str]] = None,
    output_dir: Optional[Path] = None,
    include_desktop_chrome: bool = False,
) -> List[Dict[str, Any]]:
    """Run the full extraction pipeline.

    Args:
        config: Pipeline configuration.
        app_names: If provided, only process these apps.
        interaction_names: If provided, only these interactions.
        output_dir: Override output directory.
        include_desktop_chrome: If True, include desktop chrome elements.

    Returns:
        List of result dicts.
    """
    # Load manifests
    all_manifests = load_manifests(config.apps_config_dir, None)
    if app_names:
        requested = set(app_names)
        primary_manifests = [m for m in all_manifests if m.app_name in requested]
        # Backward compatibility: allow selecting by YAML filename stem.
        if len(primary_manifests) < len(requested):
            by_stem = {
                p.stem: AppManifest.from_yaml(p)
                for p in sorted(config.apps_config_dir.glob("*.yaml"))
            }
            for name in requested:
                if name in by_stem and all(m.app_name != by_stem[name].app_name for m in primary_manifests):
                    primary_manifests.append(by_stem[name])
    else:
        primary_manifests = all_manifests

    if not primary_manifests:
        logger.error("No manifests loaded")
        return []

    # Ensure companion app manifests are available.
    selected_names = {m.app_name for m in primary_manifests}
    needed_companions = {
        cname
        for m in primary_manifests
        for interaction in m.interactions
        for cname in interaction.companion_apps
    } | {
        cname
        for m in primary_manifests
        for chain in m.chains
        for step in chain.steps
        for cname in step.companion_apps
    }
    manifests = list(primary_manifests)
    missing_companions = needed_companions - selected_names
    if missing_companions:
        logger.info(f"Loading companion manifests: {sorted(missing_companions)}")
        for m in all_manifests:
            if m.app_name in missing_companions and m.app_name not in selected_names:
                manifests.append(m)
                selected_names.add(m.app_name)

    # Generate work items
    items = generate_work_items(primary_manifests, interaction_names)
    if not items:
        logger.error("No work items generated")
        return []

    # Run worker
    worker = SessionWorker(config)
    manifest_lookup = {m.app_name: m for m in manifests}
    results = worker.process_items(items, output_dir,
                                   include_desktop_chrome=include_desktop_chrome,
                                   manifest_lookup=manifest_lookup)

    # Summary
    logger.info(
        "Pipeline complete: %d captures across %d work items",
        len(results),
        len(items),
    )

    return results
