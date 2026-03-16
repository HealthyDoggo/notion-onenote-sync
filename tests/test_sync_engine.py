"""Tests for the sync engine."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from state_db import SyncStateDB
from sync_engine import SyncEngine  # noqa: E402


@pytest.fixture
def db(tmp_path):
    return SyncStateDB(db_path=tmp_path / "test.db")


@pytest.fixture
def mock_notion():
    notion = MagicMock()
    notion.query_database.return_value = []
    return notion


@pytest.fixture
def mock_pa():
    pa = MagicMock()
    pa.send_page.return_value = {"onenote_page_id": "on-new-123"}
    return pa


@pytest.fixture
def engine(db, mock_notion, mock_pa):
    return SyncEngine(db=db, notion=mock_notion, pa_client=mock_pa)


class TestForwardSync:
    def test_empty_database(self, engine, mock_notion):
        mock_notion.query_database.return_value = []
        stats = engine.forward_sync()
        assert stats["created"] == 0
        assert stats["updated"] == 0
        assert stats["skipped"] == 0
        assert stats["errors"] == 0

    def test_creates_new_page(self, engine, mock_notion, mock_pa, db):
        now = datetime.now(timezone.utc)
        page = {
            "id": "page-1",
            "properties": {"Name": {"type": "title", "title": [{"plain_text": "Test"}]}},
            "last_edited_time": now.isoformat(),
        }
        mock_notion.query_database.return_value = [page]
        mock_notion.get_page_title.return_value = "Test"
        mock_notion.get_last_edited_time.return_value = now
        mock_notion.get_blocks.return_value = [
            {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Content", "text": {"content": "Content"}}]}}
        ]

        stats = engine.forward_sync()
        assert stats["created"] == 1
        mock_pa.send_page.assert_called_once()

        record = db.get_by_notion_id("page-1")
        assert record is not None
        assert record["onenote_page_id"] == "on-new-123"
        assert record["sync_status"] == "synced"

    def test_skips_unchanged_content(self, engine, mock_notion, mock_pa, db):
        now = datetime.now(timezone.utc)

        # Pre-seed the DB with a synced page
        from block_converter import content_hash_blocks
        blocks = [{"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Same", "text": {"content": "Same"}}]}}]
        existing_hash = content_hash_blocks(blocks)
        db.upsert_page(
            notion_page_id="page-1",
            onenote_page_id="on-1",
            content_hash=existing_hash,
            last_source="notion",
            sync_status="synced",
        )

        page = {
            "id": "page-1",
            "properties": {"Name": {"type": "title", "title": [{"plain_text": "Test"}]}},
            "last_edited_time": now.isoformat(),
        }
        mock_notion.query_database.return_value = [page]
        mock_notion.get_page_title.return_value = "Test"
        mock_notion.get_last_edited_time.return_value = now
        mock_notion.get_blocks.return_value = blocks

        stats = engine.forward_sync(full=True)
        assert stats["skipped"] == 1
        mock_pa.send_page.assert_not_called()

    def test_skips_onenote_sourced_page(self, engine, mock_notion, db):
        now = datetime.now(timezone.utc)
        db.upsert_page(
            notion_page_id="page-1",
            onenote_page_id="on-1",
            last_source="onenote",
            sync_status="synced",
        )
        page = {
            "id": "page-1",
            "properties": {"Name": {"type": "title", "title": [{"plain_text": "Test"}]}},
            "last_edited_time": now.isoformat(),
        }
        mock_notion.query_database.return_value = [page]
        mock_notion.get_page_title.return_value = "Test"
        mock_notion.get_last_edited_time.return_value = now

        stats = engine.forward_sync(full=True)
        assert stats["skipped"] == 1

    def test_handles_api_error(self, engine, mock_notion, mock_pa, db):
        now = datetime.now(timezone.utc)
        page = {
            "id": "page-1",
            "properties": {"Name": {"type": "title", "title": [{"plain_text": "Test"}]}},
            "last_edited_time": now.isoformat(),
        }
        mock_notion.query_database.return_value = [page]
        mock_notion.get_page_title.return_value = "Test"
        mock_notion.get_last_edited_time.return_value = now
        mock_notion.get_blocks.side_effect = Exception("API error")

        stats = engine.forward_sync()
        assert stats["errors"] == 1
        record = db.get_by_notion_id("page-1")
        assert record["sync_status"] == "error"


class TestReverseSync:
    def test_ignores_unknown_onenote_page(self, engine):
        engine.reverse_sync_page("unknown-id", "<p>Content</p>")
        # Should not raise

    def test_processes_known_page(self, engine, mock_notion, db):
        db.upsert_page(
            notion_page_id="n1",
            onenote_page_id="on1",
            last_source="notion",
            sync_status="synced",
            content_hash="old-hash",
        )
        engine.reverse_sync_page("on1", "<p>Updated content</p>")
        mock_notion.update_page_blocks.assert_called_once()
        record = db.get_by_notion_id("n1")
        assert record["last_source"] == "onenote"
        assert record["sync_status"] == "synced"


class TestConflictResolution:
    def test_resolve_keep_notion(self, engine, mock_notion, mock_pa, db):
        now = datetime.now(timezone.utc)
        db.upsert_page(
            notion_page_id="p1",
            onenote_page_id="on1",
            sync_status="conflict",
        )
        mock_notion.get_blocks.return_value = [
            {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "V1", "text": {"content": "V1"}}]}}
        ]
        mock_notion.get_page.return_value = {
            "id": "p1",
            "properties": {"Name": {"type": "title", "title": [{"plain_text": "Page"}]}},
        }
        mock_notion.get_page_title.return_value = "Page"

        engine.resolve_conflict("p1", keep="notion")
        mock_pa.send_page.assert_called_once()
        record = db.get_by_notion_id("p1")
        assert record["sync_status"] == "synced"

    def test_resolve_keep_onenote(self, engine, db):
        db.upsert_page(notion_page_id="p1", onenote_page_id="on1", sync_status="conflict")
        engine.resolve_conflict("p1", keep="onenote")
        record = db.get_by_notion_id("p1")
        assert record["sync_status"] == "pending"

    def test_resolve_nonexistent_page(self, engine):
        with pytest.raises(ValueError):
            engine.resolve_conflict("nonexistent", keep="notion")

    def test_resolve_non_conflict_page(self, engine, db):
        db.upsert_page(notion_page_id="p1", sync_status="synced")
        with pytest.raises(ValueError):
            engine.resolve_conflict("p1", keep="notion")
