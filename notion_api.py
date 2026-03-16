"""Notion API wrapper. Queries database, fetches block trees, creates/updates pages."""

import logging
import time
from datetime import datetime
from typing import Optional

from notion_client import Client
from notion_client.errors import APIResponseError

import config

logger = logging.getLogger(__name__)


class NotionSync:
    def __init__(self, token: Optional[str] = None, database_id: Optional[str] = None):
        self._token = token or config.NOTION_TOKEN
        self._database_id = database_id or config.NOTION_DATABASE_ID
        self._client = Client(auth=self._token)
        self._last_request_time = 0.0

    def _rate_limit(self) -> None:
        """Enforce Notion's 3 req/sec rate limit."""
        min_interval = 1.0 / config.NOTION_API_RATE_LIMIT
        elapsed = time.time() - self._last_request_time
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_request_time = time.time()

    def _api_call(self, fn, *args, **kwargs):
        """Execute an API call with rate limiting and retry on 429."""
        self._rate_limit()
        try:
            return fn(*args, **kwargs)
        except APIResponseError as e:
            if e.status == 429:
                retry_after = float(e.headers.get("Retry-After", "1")) if hasattr(e, "headers") else 1.0
                logger.warning("Rate limited, retrying after %.1fs", retry_after)
                time.sleep(retry_after)
                self._last_request_time = time.time()
                return fn(*args, **kwargs)
            raise

    def query_database(
        self, since: Optional[datetime] = None, page_size: int = 100
    ) -> list[dict]:
        """Query the database, optionally filtering to pages edited since a timestamp."""
        filter_params = {}
        if since:
            filter_params["filter"] = {
                "timestamp": "last_edited_time",
                "last_edited_time": {"after": since.isoformat()},
            }

        all_pages = []
        has_more = True
        next_cursor = None

        while has_more:
            response = self._api_call(
                self._client.databases.query,
                database_id=self._database_id,
                start_cursor=next_cursor,
                page_size=page_size,
                **filter_params,
            )
            all_pages.extend(response["results"])
            has_more = response.get("has_more", False)
            next_cursor = response.get("next_cursor")

        return all_pages

    def get_page(self, page_id: str) -> dict:
        return self._api_call(self._client.pages.retrieve, page_id=page_id)

    def get_page_title(self, page: dict) -> str:
        """Extract the title from a page object."""
        for prop_name, prop_val in page.get("properties", {}).items():
            if prop_val.get("type") == "title":
                parts = prop_val.get("title", [])
                return "".join(p.get("plain_text", "") for p in parts)
        return "Untitled"

    def get_blocks(self, block_id: str) -> list[dict]:
        """Recursively fetch the full block tree for a page."""
        blocks = []
        has_more = True
        next_cursor = None

        while has_more:
            response = self._api_call(
                self._client.blocks.children.list,
                block_id=block_id,
                start_cursor=next_cursor,
                page_size=100,
            )
            for block in response["results"]:
                if block.get("has_children"):
                    block["children"] = self.get_blocks(block["id"])
                blocks.append(block)
            has_more = response.get("has_more", False)
            next_cursor = response.get("next_cursor")

        return blocks

    def create_page(self, title: str, children: list[dict]) -> dict:
        """Create a new page in the sync database."""
        return self._api_call(
            self._client.pages.create,
            parent={"database_id": self._database_id},
            properties={
                "title": {"title": [{"text": {"content": title}}]},
            },
            children=children,
        )

    def update_page_blocks(self, page_id: str, children: list[dict]) -> None:
        """Replace a page's content by archiving existing blocks and appending new ones."""
        existing = self._api_call(
            self._client.blocks.children.list, block_id=page_id, page_size=100
        )
        for block in existing["results"]:
            try:
                self._api_call(
                    self._client.blocks.delete, block_id=block["id"]
                )
            except APIResponseError as e:
                logger.warning("Could not delete block %s: %s", block["id"], e)

        if children:
            # Notion API limits appending to 100 blocks at a time
            for i in range(0, len(children), 100):
                batch = children[i : i + 100]
                self._api_call(
                    self._client.blocks.children.append,
                    block_id=page_id,
                    children=batch,
                )

    def get_last_edited_time(self, page: dict) -> datetime:
        ts = page.get("last_edited_time", "")
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
