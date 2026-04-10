"""Tests for the one-way sync engine (Notion -> OneNote)."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import config
from state_db import SyncStateDB
from sync_engine import SyncEngine

_PARENT_PROP = config.NOTION_PARENT_PROPERTY


def _make_page(page_id, title="Untitled", parent_id=None):
    props = {
        "Name": {"type": "title", "title": [{"plain_text": title}]},
    }
    if parent_id is not None:
        props[_PARENT_PROP] = {"type": "relation", "relation": [{"id": parent_id}]}
    else:
        props[_PARENT_PROP] = {"type": "relation", "relation": []}
    return {
        "id": page_id,
        "properties": props,
        "last_edited_time": datetime.now(timezone.utc).isoformat(),
    }


@pytest.fixture
def db(tmp_path):
    return SyncStateDB(db_path=tmp_path / "test.db")


@pytest.fixture
def mock_notion():
    notion = MagicMock()
    notion.query_database.return_value = []
    notion.get_blocks.return_value = [
        {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Content", "text": {"content": "Content"}}]}}
    ]
    return notion


@pytest.fixture
def mock_pa():
    pa = MagicMock()
    pa.send_page.return_value = {"onenote_page_id": "on-new-123", "onenote_section_id": "sec-new-456"}
    return pa


@pytest.fixture
def engine(db, mock_notion, mock_pa):
    return SyncEngine(db=db, notion=mock_notion, pa_client=mock_pa)


def _setup_notion_for_pages(mock_notion, pages):
    from notion_api import NotionSync
    real = NotionSync.__new__(NotionSync)
    real._token = "fake"
    real._database_id = "fake"
    real._client = MagicMock()
    real._last_request_time = 0.0

    mock_notion.query_database.return_value = pages
    mock_notion.build_page_tree.side_effect = lambda p: real.build_page_tree(p)
    mock_notion.get_parent_id.side_effect = lambda p: real.get_parent_id(p)
    mock_notion.get_page_title.side_effect = lambda p: real.get_page_title(p)
    mock_notion.get_last_edited_time.side_effect = lambda p: real.get_last_edited_time(p)


class TestForwardSync:
    def test_empty_database(self, engine, mock_notion):
        mock_notion.query_database.return_value = []
        mock_notion.build_page_tree.return_value = []
        stats = engine.forward_sync()
        assert stats["created"] == 0
        assert stats["updated"] == 0
        assert stats["skipped"] == 0
        assert stats["errors"] == 0

    def test_creates_root_as_section_and_page(self, engine, mock_notion, mock_pa, db):
        pages = [_make_page("root-1", "Social Influence")]
        _setup_notion_for_pages(mock_notion, pages)

        stats = engine.forward_sync()
        assert stats["created"] == 1
        assert stats["sections_created"] == 1

        mock_pa.send_page.assert_called_once()
        call_kwargs = mock_pa.send_page.call_args
        assert call_kwargs.kwargs["section_name"] == "Social Influence"
        assert call_kwargs.kwargs["action"] == "create"
        assert call_kwargs.kwargs["page_level"] == 0

        record = db.get_by_notion_id("root-1")
        assert record is not None
        assert record["onenote_section_id"] == "sec-new-456"
        assert record["page_level"] == -1

    def test_creates_child_page_with_correct_level(self, engine, mock_notion, mock_pa, db):
        pages = [
            _make_page("root-1", "Social Influence"),
            _make_page("child-1", "Conformity", parent_id="root-1"),
        ]
        _setup_notion_for_pages(mock_notion, pages)

        stats = engine.forward_sync()
        assert stats["created"] == 2
        assert stats["sections_created"] == 1
        assert mock_pa.send_page.call_count == 2

        child_call = mock_pa.send_page.call_args_list[1]
        assert child_call.kwargs["section_name"] == "Social Influence"
        assert child_call.kwargs["onenote_section_id"] == "sec-new-456"
        assert child_call.kwargs["page_level"] == 0

    def test_grandchild_gets_level_1(self, engine, mock_notion, mock_pa, db):
        pages = [
            _make_page("root-1", "Topic"),
            _make_page("child-1", "Subtopic", parent_id="root-1"),
            _make_page("gc-1", "Detail", parent_id="child-1"),
        ]
        _setup_notion_for_pages(mock_notion, pages)

        stats = engine.forward_sync()
        assert stats["created"] == 3

        gc_call = mock_pa.send_page.call_args_list[2]
        assert gc_call.kwargs["page_level"] == 1
        assert gc_call.kwargs["section_name"] == "Topic"

    def test_deep_nesting_caps_at_level_2(self, engine, mock_notion, mock_pa, db):
        pages = [
            _make_page("r", "Root"),
            _make_page("c1", "L1", parent_id="r"),
            _make_page("c2", "L2", parent_id="c1"),
            _make_page("c3", "L3", parent_id="c2"),
            _make_page("c4", "L4", parent_id="c3"),
        ]
        _setup_notion_for_pages(mock_notion, pages)

        stats = engine.forward_sync()
        assert stats["created"] == 5

        c4_call = mock_pa.send_page.call_args_list[4]
        assert c4_call.kwargs["page_level"] == 2

    def test_multiple_roots_create_separate_sections(self, engine, mock_notion, mock_pa, db):
        pages = [
            _make_page("r1", "Social Influence"),
            _make_page("r2", "Memory"),
        ]
        _setup_notion_for_pages(mock_notion, pages)

        stats = engine.forward_sync()
        assert stats["created"] == 2
        assert stats["sections_created"] == 2

    def test_skips_unchanged_content(self, engine, mock_notion, mock_pa, db):
        from block_converter import content_hash_blocks
        blocks = [{"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Same", "text": {"content": "Same"}}]}}]
        existing_hash = content_hash_blocks(blocks)
        db.upsert_page(
            notion_page_id="root-1",
            onenote_page_id="on-1",
            onenote_section_id="sec-1",
            content_hash=existing_hash,
            last_source="notion",
            sync_status="synced",
            page_level=-1,
        )

        pages = [_make_page("root-1", "Topic")]
        _setup_notion_for_pages(mock_notion, pages)
        mock_notion.get_blocks.return_value = blocks

        stats = engine.forward_sync(full=True)
        assert stats["skipped"] == 1
        mock_pa.send_page.assert_not_called()

    def test_handles_api_error(self, engine, mock_notion, mock_pa, db):
        pages = [_make_page("root-1", "Topic")]
        _setup_notion_for_pages(mock_notion, pages)
        mock_notion.get_blocks.side_effect = Exception("API error")

        stats = engine.forward_sync()
        assert stats["errors"] == 1
        record = db.get_by_notion_id("root-1")
        assert record["sync_status"] == "error"

    def test_update_action_for_existing_page(self, engine, mock_notion, mock_pa, db):
        db.upsert_page(
            notion_page_id="root-1",
            onenote_page_id="on-existing",
            onenote_section_id="sec-1",
            content_hash="old-hash",
            last_source="notion",
            sync_status="synced",
            page_level=-1,
        )
        pages = [_make_page("root-1", "Topic")]
        _setup_notion_for_pages(mock_notion, pages)

        stats = engine.forward_sync(full=True)
        assert stats["updated"] == 1

        call_kwargs = mock_pa.send_page.call_args
        assert call_kwargs.kwargs["action"] == "update"
        assert call_kwargs.kwargs["onenote_page_id"] == "on-existing"

    def test_stores_parent_notion_id(self, engine, mock_notion, mock_pa, db):
        pages = [
            _make_page("root-1", "Topic"),
            _make_page("child-1", "Subtopic", parent_id="root-1"),
        ]
        _setup_notion_for_pages(mock_notion, pages)

        engine.forward_sync()
        child_record = db.get_by_notion_id("child-1")
        assert child_record["parent_notion_id"] == "root-1"

    def test_existing_section_id_reused(self, engine, mock_notion, mock_pa, db):
        db.upsert_page(
            notion_page_id="root-1",
            onenote_page_id="on-root",
            onenote_section_id="sec-existing",
            sync_status="synced",
            page_level=-1,
            content_hash="old-hash",
            last_source="notion",
        )
        pages = [
            _make_page("root-1", "Topic"),
            _make_page("child-1", "Subtopic", parent_id="root-1"),
        ]
        _setup_notion_for_pages(mock_notion, pages)

        engine.forward_sync(full=True)

        child_call = mock_pa.send_page.call_args_list[-1]
        assert child_call.kwargs["onenote_section_id"] == "sec-existing"

    def test_notion_page_id_sent_in_payload(self, engine, mock_notion, mock_pa, db):
        pages = [_make_page("root-1", "Topic")]
        _setup_notion_for_pages(mock_notion, pages)

        engine.forward_sync()
        call_kwargs = mock_pa.send_page.call_args
        assert call_kwargs.kwargs["notion_page_id"] == "root-1"
