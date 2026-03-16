"""Main sync orchestrator. Runs forward (Notion→OneNote) and reverse (OneNote→Notion) sync."""

import logging
from datetime import datetime, timezone
from typing import Optional

import config
from block_converter import (
    blocks_to_html,
    content_hash_blocks,
    content_hash_html,
    html_to_blocks,
    page_to_html,
)
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

    # ── Forward sync: Notion → OneNote ────────────────────────────────────

    def forward_sync(self, full: bool = False) -> dict:
        """Sync changed Notion pages to OneNote via Power Automate.

        Args:
            full: If True, sync all pages regardless of last sync time.

        Returns:
            Summary dict with counts of created, updated, skipped, and errored pages.
        """
        stats = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}

        since = None
        if not full:
            last_ts = self.db.get_last_sync_time()
            if last_ts:
                since = datetime.fromisoformat(last_ts)

        logger.info("Forward sync started (full=%s, since=%s)", full, since)
        pages = self.notion.query_database(since=since)
        logger.info("Found %d pages to process", len(pages))

        for page in pages:
            notion_id = page["id"]
            title = self.notion.get_page_title(page)
            last_edited = self.notion.get_last_edited_time(page)

            try:
                existing = self.db.get_by_notion_id(notion_id)

                if existing and self._should_skip_forward(existing, last_edited):
                    stats["skipped"] += 1
                    continue

                blocks = self.notion.get_blocks(notion_id)
                new_hash = content_hash_blocks(blocks)

                if existing and existing["content_hash"] == new_hash:
                    logger.debug("Content unchanged for '%s', skipping", title)
                    stats["skipped"] += 1
                    continue

                html = page_to_html(title, blocks)
                onenote_id = existing["onenote_page_id"] if existing else None

                response = self.pa.send_page(title, html, onenote_page_id=onenote_id)
                new_onenote_id = response.get("onenote_page_id") or onenote_id

                self.db.upsert_page(
                    notion_page_id=notion_id,
                    onenote_page_id=new_onenote_id,
                    notion_title=title,
                    last_notion_edit=last_edited,
                    content_hash=new_hash,
                    last_source="notion",
                    sync_status="synced",
                )

                if onenote_id:
                    stats["updated"] += 1
                    logger.info("Updated '%s' in OneNote", title)
                else:
                    stats["created"] += 1
                    logger.info("Created '%s' in OneNote", title)

            except Exception:
                logger.exception("Error syncing page '%s' (%s)", title, notion_id)
                self.db.upsert_page(
                    notion_page_id=notion_id,
                    notion_title=title,
                    last_notion_edit=last_edited,
                    sync_status="error",
                )
                stats["errors"] += 1

        logger.info("Forward sync complete: %s", stats)
        return stats

    def _should_skip_forward(self, record: dict, notion_edit_time: datetime) -> bool:
        """Echo loop prevention: skip if the last modification came from OneNote
        and the content hasn't changed."""
        if record["last_source"] == "onenote" and record["sync_status"] == "synced":
            return True
        if record["sync_status"] == "conflict":
            logger.debug("Skipping conflicted page %s", record["notion_page_id"])
            return True
        return False

    # ── Reverse sync: OneNote → Notion ────────────────────────────────────

    def reverse_sync_page(self, onenote_page_id: str, html_content: str) -> None:
        """Process a single OneNote page update (called from the webhook handler)."""
        logger.info("Reverse sync for OneNote page %s", onenote_page_id)

        existing = self.db.get_by_onenote_id(onenote_page_id)
        if not existing:
            logger.warning("No mapping found for OneNote page %s, ignoring", onenote_page_id)
            return

        notion_id = existing["notion_page_id"]

        if existing["last_source"] == "notion" and existing["sync_status"] == "synced":
            new_hash = content_hash_html(html_content)
            if new_hash == existing["content_hash"]:
                logger.debug("Content unchanged (echo), skipping reverse sync")
                return

        now = datetime.now(timezone.utc)
        if existing["last_notion_edit"]:
            notion_edit = datetime.fromisoformat(existing["last_notion_edit"])
            delta = abs((now - notion_edit).total_seconds())
            if delta < config.SYNC_CONFLICT_WINDOW_SECONDS:
                logger.warning(
                    "Conflict detected for page %s: both sides edited within %ds",
                    notion_id, config.SYNC_CONFLICT_WINDOW_SECONDS,
                )
                self.db.set_status(notion_id, "conflict")
                return

        try:
            blocks = html_to_blocks(html_content)
            new_hash = content_hash_html(html_content)
            self.notion.update_page_blocks(notion_id, blocks)

            self.db.upsert_page(
                notion_page_id=notion_id,
                last_onenote_edit=now,
                content_hash=new_hash,
                last_source="onenote",
                sync_status="synced",
            )
            logger.info("Reverse sync complete for %s", notion_id)

        except Exception:
            logger.exception("Error in reverse sync for %s", notion_id)
            self.db.set_status(notion_id, "error")

    # ── Conflict resolution ───────────────────────────────────────────────

    def resolve_conflict(self, notion_page_id: str, keep: str) -> None:
        """Resolve a conflict by choosing which side wins.

        Args:
            keep: 'notion' or 'onenote'
        """
        record = self.db.get_by_notion_id(notion_page_id)
        if not record:
            raise ValueError(f"No record for page {notion_page_id}")
        if record["sync_status"] != "conflict":
            raise ValueError(f"Page {notion_page_id} is not in conflict state")

        if keep == "notion":
            blocks = self.notion.get_blocks(notion_page_id)
            page = self.notion.get_page(notion_page_id)
            title = self.notion.get_page_title(page)
            html = page_to_html(title, blocks)
            new_hash = content_hash_blocks(blocks)
            self.pa.send_page(title, html, onenote_page_id=record["onenote_page_id"])
            self.db.upsert_page(
                notion_page_id=notion_page_id,
                content_hash=new_hash,
                last_source="notion",
                sync_status="synced",
            )
            logger.info("Conflict resolved for %s: kept Notion version", notion_page_id)

        elif keep == "onenote":
            # For onenote-wins, we'd need to fetch the current OneNote content.
            # This requires a PA flow to retrieve page content on demand (not yet implemented).
            # For now, mark as synced from onenote side so the next reverse webhook updates it.
            self.db.upsert_page(
                notion_page_id=notion_page_id,
                last_source="onenote",
                sync_status="pending",
            )
            logger.info(
                "Conflict resolved for %s: will accept next OneNote version", notion_page_id
            )
        else:
            raise ValueError(f"Invalid keep value: {keep!r}. Must be 'notion' or 'onenote'.")
