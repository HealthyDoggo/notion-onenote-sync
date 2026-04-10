"""Tests for the SQLite state database."""

from datetime import datetime, timezone

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


# ── Hierarchy column tests ────────────────────────────────────────────────

def test_upsert_with_hierarchy_fields(db):
    db.upsert_page(
        notion_page_id="child-1",
        notion_title="Conformity",
        sync_status="synced",
        parent_notion_id="root-1",
        onenote_section_id=None,
        page_level=0,
    )
    record = db.get_by_notion_id("child-1")
    assert record["parent_notion_id"] == "root-1"
    assert record["page_level"] == 0


def test_upsert_root_with_section_id(db):
    db.upsert_page(
        notion_page_id="root-1",
        notion_title="Social Influence",
        sync_status="synced",
        onenote_section_id="sec-abc",
        page_level=-1,
    )
    record = db.get_by_notion_id("root-1")
    assert record["onenote_section_id"] == "sec-abc"
    assert record["page_level"] == -1


def test_get_children(db):
    db.upsert_page(notion_page_id="root", notion_title="Topic", sync_status="synced")
    db.upsert_page(notion_page_id="c1", notion_title="A Child", sync_status="synced", parent_notion_id="root")
    db.upsert_page(notion_page_id="c2", notion_title="B Child", sync_status="synced", parent_notion_id="root")
    db.upsert_page(notion_page_id="other", notion_title="Unrelated", sync_status="synced", parent_notion_id="other-root")

    children = db.get_children("root")
    assert len(children) == 2
    assert children[0]["notion_page_id"] == "c1"
    assert children[1]["notion_page_id"] == "c2"


def test_get_children_empty(db):
    db.upsert_page(notion_page_id="root", notion_title="Topic", sync_status="synced")
    assert db.get_children("root") == []


def test_get_section_id_for_page_direct(db):
    db.upsert_page(notion_page_id="root", onenote_section_id="sec-1", sync_status="synced")
    assert db.get_section_id_for_page("root") == "sec-1"


def test_get_section_id_for_page_via_parent(db):
    db.upsert_page(notion_page_id="root", onenote_section_id="sec-1", sync_status="synced")
    db.upsert_page(notion_page_id="child", parent_notion_id="root", sync_status="synced")
    db.upsert_page(notion_page_id="grandchild", parent_notion_id="child", sync_status="synced")
    assert db.get_section_id_for_page("grandchild") == "sec-1"


def test_get_section_id_for_page_not_found(db):
    db.upsert_page(notion_page_id="orphan", sync_status="synced")
    assert db.get_section_id_for_page("orphan") is None


# ── Migration test ────────────────────────────────────────────────────────

def test_migration_adds_columns(tmp_path):
    """Opening a DB created without hierarchy columns should auto-migrate."""
    import sqlite3
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE page_sync (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            notion_page_id  TEXT UNIQUE NOT NULL,
            onenote_page_id TEXT,
            notion_title    TEXT,
            last_notion_edit  DATETIME,
            last_onenote_edit DATETIME,
            last_synced       DATETIME,
            content_hash      TEXT,
            last_source       TEXT,
            sync_status       TEXT DEFAULT 'pending',
            conversion_notes  TEXT
        )
    """)
    conn.execute("INSERT INTO page_sync (notion_page_id, sync_status) VALUES ('old-page', 'synced')")
    conn.commit()
    conn.close()

    db = SyncStateDB(db_path=db_path)
    record = db.get_by_notion_id("old-page")
    assert record is not None
    assert record["parent_notion_id"] is None
    assert record["onenote_section_id"] is None

    db.upsert_page(
        notion_page_id="old-page",
        parent_notion_id="some-parent",
        onenote_section_id="sec-1",
        page_level=0,
    )
    record = db.get_by_notion_id("old-page")
    assert record["parent_notion_id"] == "some-parent"
    assert record["onenote_section_id"] == "sec-1"
    assert record["page_level"] == 0
