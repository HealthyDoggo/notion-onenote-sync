"""Tests for the SQLite state database."""

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from state_db import SyncStateDB


@pytest.fixture
def db(tmp_path):
    return SyncStateDB(db_path=tmp_path / "test.db")


def test_init_creates_table(db):
    record = db.get_by_notion_id("nonexistent")
    assert record is None


def test_upsert_and_retrieve(db):
    now = datetime.now(timezone.utc)
    db.upsert_page(
        notion_page_id="page-1",
        onenote_page_id="on-1",
        notion_title="Test Page",
        last_notion_edit=now,
        content_hash="abc123",
        last_source="notion",
        sync_status="synced",
    )
    record = db.get_by_notion_id("page-1")
    assert record is not None
    assert record["notion_title"] == "Test Page"
    assert record["onenote_page_id"] == "on-1"
    assert record["last_source"] == "notion"
    assert record["sync_status"] == "synced"
    assert record["content_hash"] == "abc123"


def test_upsert_updates_existing(db):
    db.upsert_page(notion_page_id="page-1", notion_title="V1", sync_status="pending")
    db.upsert_page(notion_page_id="page-1", notion_title="V2", sync_status="synced")
    record = db.get_by_notion_id("page-1")
    assert record["notion_title"] == "V2"
    assert record["sync_status"] == "synced"


def test_get_by_onenote_id(db):
    db.upsert_page(notion_page_id="n1", onenote_page_id="on1", sync_status="synced")
    record = db.get_by_onenote_id("on1")
    assert record is not None
    assert record["notion_page_id"] == "n1"


def test_get_all(db):
    db.upsert_page(notion_page_id="a", sync_status="synced")
    db.upsert_page(notion_page_id="b", sync_status="pending")
    all_pages = db.get_all()
    assert len(all_pages) == 2


def test_get_conflicts(db):
    db.upsert_page(notion_page_id="ok", sync_status="synced")
    db.upsert_page(notion_page_id="bad", sync_status="conflict")
    conflicts = db.get_conflicts()
    assert len(conflicts) == 1
    assert conflicts[0]["notion_page_id"] == "bad"


def test_get_errors(db):
    db.upsert_page(notion_page_id="ok", sync_status="synced")
    db.upsert_page(notion_page_id="err", sync_status="error")
    errors = db.get_errors()
    assert len(errors) == 1
    assert errors[0]["notion_page_id"] == "err"


def test_set_status(db):
    db.upsert_page(notion_page_id="p1", sync_status="synced")
    db.set_status("p1", "error")
    record = db.get_by_notion_id("p1")
    assert record["sync_status"] == "error"


def test_delete_page(db):
    db.upsert_page(notion_page_id="p1", sync_status="synced")
    db.delete_page("p1")
    assert db.get_by_notion_id("p1") is None


def test_count_by_status(db):
    db.upsert_page(notion_page_id="a", sync_status="synced")
    db.upsert_page(notion_page_id="b", sync_status="synced")
    db.upsert_page(notion_page_id="c", sync_status="error")
    counts = db.count_by_status()
    assert counts["synced"] == 2
    assert counts["error"] == 1


def test_get_last_sync_time(db):
    assert db.get_last_sync_time() is None
    db.upsert_page(notion_page_id="p1", sync_status="synced")
    assert db.get_last_sync_time() is not None
