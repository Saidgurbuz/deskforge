"""SessionWorker: manages one Xvfb session, processes multiple app states."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from deskshot.config import PipelineConfig, WorkItem
from deskshot.environment.session import DesktopSession
from deskshot.extraction.run_extraction import run_chain_extraction, run_single_extraction

logger = logging.getLogger(__name__)


class SessionWorker:
    """Manages a single desktop session and processes work items.

    A work item is either one interaction or one multi-capture chain.
    The worker starts one Xvfb+D-Bus+AT-SPI session and processes
    all work items sequentially within that session.
    """

    def __init__(self, config: PipelineConfig):
        self.config = config
        self.results: List[Dict[str, Any]] = []

    def process_items(
        self,
        items: List[WorkItem],
        output_dir: Optional[Path] = None,
        include_desktop_chrome: bool = False,
        manifest_lookup: Optional[dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Process a list of work items within a single desktop session.

        Args:
            items: List of work items.
            output_dir: Override output directory.
            include_desktop_chrome: If True, include desktop chrome elements.
            manifest_lookup: app_name -> manifest map for multi-window companions.

        Returns:
            List of result dicts (one per successful extraction).
        """
        out = output_dir or self.config.output_dir
        results = []

        with DesktopSession(self.config.session) as session:
            logger.info(f"Session started on display {session.display}")

            for i, item in enumerate(items):
                logger.info(
                    f"Processing item {i+1}/{len(items)}: "
                    f"{item.manifest.app_name}/{item.name}"
                )

                if item.interaction is not None:
                    result = run_single_extraction(
                        item.manifest, item.interaction, self.config, output_dir=out,
                        include_desktop_chrome=include_desktop_chrome,
                        manifest_lookup=manifest_lookup,
                    )
                    item_results = [result] if result else []
                elif item.chain is not None:
                    item_results = run_chain_extraction(
                        item.manifest, item.chain, self.config, output_dir=out,
                        include_desktop_chrome=include_desktop_chrome,
                        manifest_lookup=manifest_lookup,
                    )
                else:
                    item_results = []

                if item_results:
                    results.extend(item_results)
                else:
                    logger.warning(
                        f"Failed: {item.manifest.app_name}/{item.name}"
                    )

        self.results = results
        return results
