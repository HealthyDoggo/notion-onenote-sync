"""One-way sync orchestrator: Notion -> OneNote via Power Automate.

For updates, uses a two-step flow:
  1. Read current OneNote page HTML (to capture teacher feedback)
  2. Merge fresh Notion HTML with preserved teacher content
  3. Replace the page (delete old + create new with merged HTML)
"""

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

import config
from block_converter import content_hash_blocks, page_to_html
from html_merge import extract_red_items, merge_harvested, merge_html
from notion_api import NotionSync
from pa_bridge import PAForwardClient, RateLimitedError, RateLimitGate
from state_db import SyncStateDB

logger = logging.getLogger(__name__)


class SyncEngine:
    def __init__(
        self,
        db: Optional[SyncStateDB] = None,
        notion: Optional[NotionSync] = None,
        pa_client: Optional[PAForwardClient] = None,
        gate: Optional[RateLimitGate] = None,
    ):
        self.db = db or SyncStateDB()
        self.notion = notion or NotionSync()
        self.gate = gate or RateLimitGate()
        self.pa = pa_client or PAForwardClient(gate=self.gate)
        self._section_cache: dict[tuple[str, ...], str] = {}
        self._section_cache_lock = threading.Lock()
        self._harvested_red: dict[str, list[dict]] = {}
        self._progress_callback: Optional[callable] = None

    def harvest_teacher_notes(
        self,
        max_workers: int = 4,
        progress_callback: Optional[callable] = None,
    ) -> dict[str, list[dict]]:
        """Read all tracked OneNote pages and extract red teacher text.

        Returns a dict mapping notion_page_id -> list of red items (each with
        html, prev_text, next_text, position keys).
        """
        all_pages = self.db.get_all_with_onenote_ids()
        pages = [p for p in all_pages if p.get("onenote_section_id")]
        logger.info("Harvesting teacher notes from %d pages", len(pages))

        skipped = len(all_pages) - len(pages)
        if skipped:
            logger.warning("Skipped %d page(s) with no section ID", skipped)

        harvested = {}
        lock = threading.Lock()

        def _harvest_one(page: dict) -> None:
            notion_id = page["notion_page_id"]
            onenote_page_id = page["onenote_page_id"]
            section_id = page["onenote_section_id"]
            title = page.get("notion_title", notion_id)

            try:
                self.gate.wait()
                time.sleep(config.PA_CALL_DELAY)
                resp = self.pa.read_page(onenote_page_id, onenote_section_id=section_id)
                current_html = resp.get("current_html", "")
                if not current_html:
                    logger.debug("Empty HTML for '%s', skipping", title)
                    return

                red_items = extract_red_items(current_html)
                if red_items:
                    with lock:
                        harvested[notion_id] = red_items
                    logger.info(
                        "Harvested %d red item(s) from '%s'",
                        len(red_items), title,
                    )
            except Exception:
                logger.warning(
                    "Could not read '%s' for harvest, skipping",
                    title, exc_info=True,
                )
            finally:
                if progress_callback:
                    progress_callback()

        workers = min(len(pages), max_workers) or 1
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pool.map(_harvest_one, pages)

        logger.info(
            "Harvest complete: %d pages with teacher notes",
            len(harvested),
        )
        return harvested

    @staticmethod
    def _count_tree_nodes(roots: list[dict]) -> int:
        count = 0
        stack = list(roots)
        while stack:
            node = stack.pop()
            count += 1
            stack.extend(node.get("_children", []))
        return count

    def fetch_pages(self, full: bool = False) -> tuple:
        """Fetch pages from Notion and build the tree.

        Returns (roots, total_count) so callers can set up progress bars
        before calling sync_fetched().
        """
        since = None
        if not full:
            last_ts = self.db.get_last_sync_time()
            if last_ts:
                since = datetime.fromisoformat(last_ts)

        pages = self.notion.query_database(since=since if not full else None)
        logger.info("Found %d pages to process", len(pages))

        roots = self.notion.build_page_tree(pages)
        total = self._count_tree_nodes(roots)
        logger.info("Built tree: %d root topics, %d total nodes", len(roots), total)
        return roots, total

    def sync_fetched(
        self,
        roots: list[dict],
        full: bool = False,
        progress_callback: Optional[callable] = None,
    ) -> dict:
        """Sync pre-fetched page trees to OneNote.

        Use fetch_pages() first to get roots and total count for progress bars.
        """
        self._progress_callback = progress_callback

        stats = {
            "created": 0, "updated": 0, "skipped": 0,
            "errors": 0, "sections_created": 0,
        }

        logger.info("Forward sync started (full=%s)", full)

        def _sync_root(root):
            root_stats = {
                "created": 0, "updated": 0, "skipped": 0,
                "errors": 0, "sections_created": 0,
            }
            self._sync_tree_node(
                root,
                section_name=None,
                section_id=None,
                stats=root_stats,
                parent_context_path=None,
                full_sync=full,
            )
            return root_stats

        max_workers = min(len(roots), 4) or 1
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_sync_root, root): root for root in roots}
            for future in as_completed(futures):
                root = futures[future]
                try:
                    root_stats = future.result()
                    for key in stats:
                        stats[key] += root_stats[key]
                except Exception:
                    title = self.notion.get_page_title(root)
                    logger.exception("Error syncing root '%s'", title)
                    stats["errors"] += 1

        self._progress_callback = None
        logger.info("Forward sync complete: %s", stats)
        return stats

    def forward_sync(
        self,
        full: bool = False,
        progress_callback: Optional[callable] = None,
    ) -> dict:
        """Fetch pages and sync in one call."""
        roots, _total = self.fetch_pages(full=full)
        return self.sync_fetched(roots, full=full, progress_callback=progress_callback)

    def _recover_path_from_parent_chain(self, parent_id: str) -> Optional[list[str]]:
        """Walk up the parent chain in the DB to reconstruct section_group_path."""
        path_parts = []
        current_id = parent_id
        for _ in range(10):
            rec = self.db.get_by_notion_id(current_id)
            if not rec:
                return None
            if rec.get("sync_section_group_path"):
                stored = json.loads(rec["sync_section_group_path"])
                return stored + path_parts
            path_parts.insert(0, rec.get("notion_title", ""))
            if not rec.get("parent_notion_id"):
                return path_parts
            current_id = rec["parent_notion_id"]
        return None

    def _sync_tree_node(
        self,
        node: dict,
        section_name: Optional[str],
        section_id: Optional[str],
        stats: dict,
        parent_context_path: Optional[list[str]] = None,
        use_flat_section: bool = False,
        full_sync: bool = False,
    ) -> None:
        """Recursively sync a page tree node and its children depth-first."""
        notion_id = node["id"]
        title = self.notion.get_page_title(node)
        last_edited = self.notion.get_last_edited_time(node)
        depth = node.get("_depth", 0)
        parent_id = node.get("_parent_id")
        is_root = depth == 0

        current_mode: Optional[str] = None
        orphan_recovered = False

        # Orphan root: page has a parent in Notion but the parent wasn't
        # fetched in this incremental sync, so it landed at tree depth 0.
        # Recover stored section routing instead of computing from the
        # incomplete tree (which would use the page's own title as the path).
        if is_root and parent_id is not None:
            existing_rec = self.db.get_by_notion_id(notion_id)
            if existing_rec and existing_rec.get("sync_section_group_path"):
                section_group_path = json.loads(existing_rec["sync_section_group_path"])
                section_name = existing_rec.get("sync_section_name") or title
                use_flat_section = bool(existing_rec.get("sync_use_flat_section", 0))
                section_id = existing_rec.get("onenote_section_id") or section_id
                child_parent_context = section_group_path
                current_mode = existing_rec.get("section_mode")
                orphan_recovered = True
                logger.debug(
                    "Recovered stored path for orphan '%s': path=%s, section=%s",
                    title, section_group_path, section_name,
                )
            elif existing_rec:
                recovered_path = self._recover_path_from_parent_chain(parent_id)
                if recovered_path is not None:
                    section_group_path = recovered_path
                    section_name = recovered_path[0] if recovered_path else title
                    use_flat_section = bool(existing_rec.get("sync_use_flat_section", 0))
                    section_id = existing_rec.get("onenote_section_id") or section_id
                    child_parent_context = section_group_path
                    current_mode = existing_rec.get("section_mode")
                    orphan_recovered = True
                    logger.debug(
                        "Recovered path from parent chain for orphan '%s': path=%s",
                        title, section_group_path,
                    )

        if is_root and not orphan_recovered:
            section_name = title
            has_subfolder = any(
                child.get("_children") for child in node.get("_children", [])
            )
            existing_for_mode = self.db.get_by_notion_id(notion_id)
            prev_mode = (existing_for_mode or {}).get("section_mode")

            if not full_sync and prev_mode:
                use_flat_section = prev_mode == "flat"
                current_mode = prev_mode
            else:
                use_flat_section = not has_subfolder
                current_mode = "flat" if use_flat_section else "grouped"

            if existing_for_mode and prev_mode and prev_mode != current_mode:
                logger.warning(
                    "Root '%s' section mode changed %s → %s. "
                    "Clearing sync state so all pages are recreated. "
                    "The old OneNote section will be orphaned — clean it up manually. "
                    "Run a full sync (--full) to ensure all children are reprocessed.",
                    title, prev_mode, current_mode,
                )
                self.db.reset_subtree(notion_id)
                section_id = None

        if is_root and not orphan_recovered:
            section_group_path = [title]
            child_parent_context = [title]
        elif not is_root:
            pc = list(parent_context_path or [])
            if node.get("_children"):
                section_group_path = pc + [title]
                child_parent_context = section_group_path
            else:
                section_group_path = pc[:-1] if pc else []
                child_parent_context = pc

        has_children = bool(node.get("_children"))

        try:
            existing = self.db.get_by_notion_id(notion_id)

            if existing and existing.get("onenote_section_id"):
                section_id = existing["onenote_section_id"]

            if has_children:
                logger.debug(
                    "Skipping content sync for parent page '%s' (folder-only node)",
                    title,
                )
                if is_root:
                    self.db.upsert_page(
                        notion_page_id=notion_id,
                        notion_title=title,
                        last_notion_edit=last_edited,
                        last_source="notion",
                        sync_status="synced",
                        parent_notion_id=parent_id,
                        onenote_section_id=section_id,
                        section_mode=current_mode,
                        sync_section_name=section_name,
                        sync_section_group_path=json.dumps(section_group_path),
                        sync_use_flat_section=use_flat_section,
                    )
                stats["skipped"] += 1
            else:
                blocks = self.notion.get_blocks(notion_id)
                new_hash = content_hash_blocks(blocks)

                if existing and existing["content_hash"] == new_hash:
                    logger.debug("Content unchanged for '%s', skipping", title)
                    if not existing.get("sync_section_group_path"):
                        self.db.upsert_page(
                            notion_page_id=notion_id,
                            sync_section_group_path=json.dumps(section_group_path),
                            sync_section_name=section_name,
                            sync_use_flat_section=use_flat_section,
                        )
                    stats["skipped"] += 1
                else:
                    html = page_to_html(title, blocks)
                    onenote_page_id = existing["onenote_page_id"] if existing else None
                    is_update = bool(onenote_page_id)

                    cache_key = tuple(section_group_path) + (section_name,)
                    if not section_id:
                        with self._section_cache_lock:
                            section_id = self._section_cache.get(cache_key)

                    response = self._send_with_rate_limit_retry(
                        is_update=is_update,
                        onenote_page_id=onenote_page_id,
                        section_name=section_name,
                        title=title,
                        html=html,
                        notion_id=notion_id,
                        section_id=section_id,
                        section_group_path=section_group_path,
                        use_flat_section=use_flat_section,
                    )

                    action = "replace" if is_update else "create"
                    new_onenote_page_id = response.get("onenote_page_id") or onenote_page_id
                    returned_section_id = response.get("onenote_section_id") or section_id

                    self.db.log_pa_run(
                        notion_page_id=notion_id,
                        action=action,
                        page_title=title,
                        section_name=section_name,
                        section_group_path=json.dumps(section_group_path),
                        run_url=response.get("run_url"),
                    )

                    if returned_section_id:
                        section_id = returned_section_id
                        with self._section_cache_lock:
                            self._section_cache[cache_key] = returned_section_id
                        if is_root and not (existing and existing.get("onenote_section_id")):
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
                        onenote_section_id=section_id,
                        section_mode=current_mode if is_root else None,
                        sync_section_name=section_name,
                        sync_section_group_path=json.dumps(section_group_path),
                        sync_use_flat_section=use_flat_section,
                    )

                    if is_update:
                        stats["updated"] += 1
                        logger.info("Updated '%s' in OneNote (merged)", title)
                    else:
                        stats["created"] += 1
                        logger.info("Created '%s' in OneNote (section=%s, flat=%s)",
                                    title, section_name, use_flat_section)

        except Exception as exc:
            logger.exception("Error syncing page '%s' (%s)", title, notion_id)
            self.db.log_pa_run(
                notion_page_id=notion_id,
                action="replace" if (existing and existing.get("onenote_page_id")) else "create",
                page_title=title,
                section_name=section_name,
                section_group_path=json.dumps(section_group_path),
                status="error",
                error_message=str(exc),
            )
            self.db.upsert_page(
                notion_page_id=notion_id,
                notion_title=title,
                last_notion_edit=last_edited,
                content_hash=None,
                sync_status="error",
                parent_notion_id=parent_id,
            )
            stats["errors"] += 1

        if self._progress_callback:
            self._progress_callback()

        if is_root and not section_id:
            rec = self.db.get_by_notion_id(notion_id)
            if rec:
                section_id = rec.get("onenote_section_id")

        child_section_name = title if has_children else section_name
        child_use_flat = use_flat_section
        if has_children and not is_root:
            has_grandchild_folders = any(
                child.get("_children") for child in node.get("_children", [])
            )
            if not has_grandchild_folders:
                child_use_flat = True

        for child in node.get("_children", []):
            self._sync_tree_node(
                child,
                section_name=child_section_name,
                section_id=section_id,
                stats=stats,
                parent_context_path=child_parent_context,
                use_flat_section=child_use_flat,
                full_sync=full_sync,
            )

    def _send_with_rate_limit_retry(
        self,
        *,
        is_update: bool,
        onenote_page_id: Optional[str],
        section_name: str,
        title: str,
        html: str,
        notion_id: str,
        section_id: Optional[str],
        section_group_path: list[str],
        use_flat_section: bool,
    ) -> dict:
        """Send a create or replace to PA, retrying the full operation on 429.

        For updates the entire read-merge-replace sequence is retried so
        the merge uses fresh OneNote HTML after the backoff.
        """
        for attempt in range(config.PA_RATE_LIMIT_RETRIES):
            try:
                if is_update:
                    merged_html = self._read_and_merge(
                        onenote_page_id, html, section_id,
                    )
                    return self.pa.send_page(
                        section_name=section_name,
                        title=title,
                        html_body=merged_html,
                        notion_page_id=notion_id,
                        action="replace",
                        onenote_page_id=onenote_page_id,
                        onenote_section_id=section_id,
                        section_group_path=section_group_path,
                        use_flat_section=use_flat_section,
                    )
                else:
                    create_html = html
                    harvested = self._harvested_red.get(notion_id)
                    if harvested:
                        create_html = merge_harvested(html, harvested)
                        logger.info(
                            "Applied %d harvested red item(s) to '%s'",
                            len(harvested), title,
                        )
                    return self.pa.send_page(
                        section_name=section_name,
                        title=title,
                        html_body=create_html,
                        notion_page_id=notion_id,
                        action="create",
                        onenote_section_id=section_id,
                        section_group_path=section_group_path,
                        use_flat_section=use_flat_section,
                    )
            except RateLimitedError as e:
                logger.warning(
                    "Rate-limited sending '%s' (attempt %d/%d): %s",
                    title, attempt + 1, config.PA_RATE_LIMIT_RETRIES, e,
                )
                self.gate.wait()
                if attempt == config.PA_RATE_LIMIT_RETRIES - 1:
                    raise
        raise RuntimeError("unreachable")

    def _read_and_merge(self, onenote_page_id: str, fresh_html: str, section_id: Optional[str] = None) -> str:
        """Read current OneNote page and merge with fresh Notion HTML."""
        if not section_id:
            logger.warning(
                "No section ID for page %s, skipping merge — using fresh HTML",
                onenote_page_id,
            )
            return fresh_html
        try:
            read_response = self.pa.read_page(onenote_page_id, onenote_section_id=section_id)
            current_html = read_response.get("current_html", "")
            if current_html:
                merged = merge_html(fresh_html, current_html)
                logger.info("Merged teacher feedback into updated page")
                return merged
        except Exception:
            logger.warning(
                "Could not read OneNote page %s for merge, using fresh HTML",
                onenote_page_id, exc_info=True,
            )
        return fresh_html
