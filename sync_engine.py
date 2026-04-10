"""One-way sync orchestrator: Notion -> OneNote via Power Automate."""

import logging
from datetime import datetime, timezone
from typing import Optional

from block_converter import content_hash_blocks, page_to_html
from notion_api import NotionSync
from pa_bridge import PAForwardClient
from state_db import SyncStateDB

logger = logging.getLogger(__name__)


class SyncEngine:
    def __init__(
        self,
        db: Optional[SyncStateDB] = None,
        notion: Optional[NotionSync] = None,
        pa_client: Optional[PAForwardClient] = None,
    ):
        self.db = db or SyncStateDB()
        self.notion = notion or NotionSync()
        self.pa = pa_client or PAForwardClient()

    def forward_sync(self, full: bool = False) -> dict:
        """Sync changed Notion pages to OneNote via Power Automate.

        Builds a page tree from the database, maps root pages to OneNote
        sections, and syncs children at the correct page level.

        Args:
            full: If True, sync all pages regardless of last sync time.

        Returns:
            Summary dict with counts.
        """
        stats = {"created": 0, "updated": 0, "skipped": 0, "errors": 0, "sections_created": 0}

        since = None
        if not full:
            last_ts = self.db.get_last_sync_time()
            if last_ts:
                since = datetime.fromisoformat(last_ts)

        logger.info("Forward sync started (full=%s, since=%s)", full, since)
        pages = self.notion.query_database(since=since if not full else None)
        logger.info("Found %d pages to process", len(pages))

        roots = self.notion.build_page_tree(pages)
        logger.info("Built tree: %d root topics", len(roots))

        for root in roots:
            self._sync_tree_node(root, section_name=None, section_id=None, stats=stats)

        logger.info("Forward sync complete: %s", stats)
        return stats

    def _sync_tree_node(
        self,
        node: dict,
        section_name: Optional[str],
        section_id: Optional[str],
        stats: dict,
    ) -> None:
        """Recursively sync a page tree node and its children depth-first."""
        notion_id = node["id"]
        title = self.notion.get_page_title(node)
        last_edited = self.notion.get_last_edited_time(node)
        depth = node.get("_depth", 0)
        parent_id = node.get("_parent_id")
        is_root = depth == 0

        if is_root:
            section_name = title

        try:
            existing = self.db.get_by_notion_id(notion_id)

            if is_root and existing and existing.get("onenote_section_id"):
                section_id = existing["onenote_section_id"]

            blocks = self.notion.get_blocks(notion_id)
            new_hash = content_hash_blocks(blocks)

            if existing and existing["content_hash"] == new_hash:
                logger.debug("Content unchanged for '%s', skipping", title)
                stats["skipped"] += 1
            else:
                html = page_to_html(title, blocks)
                onenote_page_id = existing["onenote_page_id"] if existing else None
                action = "update" if onenote_page_id else "create"

                # Root = section-level entry; children get page_level = depth - 1, capped at 2
                page_level = 0 if is_root else max(0, min(depth - 1, 2))

                response = self.pa.send_page(
                    section_name=section_name,
                    title=title,
                    html_body=html,
                    notion_page_id=notion_id,
                    action=action,
                    onenote_page_id=onenote_page_id,
                    onenote_section_id=section_id,
                    page_level=page_level,
                )

                new_onenote_page_id = response.get("onenote_page_id") or onenote_page_id
                returned_section_id = section_id or response.get("onenote_section_id")

                if is_root and returned_section_id:
                    section_id = returned_section_id
                    if not (existing and existing.get("onenote_section_id")):
                        stats["sections_created"] += 1

                self.db.upsert_page(
                    notion_page_id=notion_id,
                    onenote_page_id=new_onenote_page_id,
                    notion_title=title,
                    last_notion_edit=last_edited,
                    content_hash=new_hash,
                    last_source="notion",
                    sync_status="synced",
                    parent_notion_id=parent_id,
                    onenote_section_id=section_id if is_root else None,
                    page_level=-1 if is_root else page_level,
                )

                if action == "update":
                    stats["updated"] += 1
                    logger.info("Updated '%s' in OneNote", title)
                else:
                    stats["created"] += 1
                    logger.info("Created '%s' in OneNote (section=%s, level=%d)",
                                title, section_name, page_level)

        except Exception:
            logger.exception("Error syncing page '%s' (%s)", title, notion_id)
            self.db.upsert_page(
                notion_page_id=notion_id,
                notion_title=title,
                last_notion_edit=last_edited,
                sync_status="error",
                parent_notion_id=parent_id,
            )
            stats["errors"] += 1

        if is_root and not section_id:
            rec = self.db.get_by_notion_id(notion_id)
            if rec:
                section_id = rec.get("onenote_section_id")

        for child in node.get("_children", []):
            self._sync_tree_node(child, section_name=section_name,
                                 section_id=section_id, stats=stats)
