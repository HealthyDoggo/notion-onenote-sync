"""SQLite state database tracking page mappings, sync timestamps, and content hashes."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import config


def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = db_path or config.DB_PATH
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_MIGRATION_COLUMNS = [
    ("parent_notion_id", "TEXT"),
    ("onenote_section_id", "TEXT"),
    ("page_level", "INTEGER DEFAULT -1"),
    ("section_mode", "TEXT DEFAULT 'grouped'"),
    ("sync_section_name", "TEXT"),
    ("sync_section_group_path", "TEXT"),
    ("sync_use_flat_section", "INTEGER DEFAULT 0"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the initial schema."""
    cursor = conn.execute("PRAGMA table_info(page_sync)")
    existing = {row[1] for row in cursor.fetchall()}
    for col_name, col_type in _MIGRATION_COLUMNS:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE page_sync ADD COLUMN {col_name} {col_type}")
    conn.commit()


def init_db(db_path: Optional[Path] = None) -> None:
    conn = _connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS page_sync (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            notion_page_id  TEXT UNIQUE NOT NULL,
            onenote_page_id TEXT,
            notion_title    TEXT,
            last_notion_edit  DATETIME,
            last_onenote_edit DATETIME,
            last_synced       DATETIME,
            content_hash      TEXT,
            last_source       TEXT CHECK(last_source IN ('notion', 'onenote')),
            sync_status       TEXT DEFAULT 'pending'
                              CHECK(sync_status IN ('synced', 'pending', 'conflict', 'error')),
            conversion_notes  TEXT,
            parent_notion_id  TEXT,
            onenote_section_id TEXT,
            page_level        INTEGER DEFAULT -1,
            section_mode      TEXT DEFAULT 'grouped',
            sync_section_name     TEXT,
            sync_section_group_path TEXT,
            sync_use_flat_section INTEGER DEFAULT 0
        )
    """)
    _migrate(conn)
    conn.commit()
    conn.close()


class SyncStateDB:
    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = db_path or config.DB_PATH
        init_db(self._db_path)

    def _conn(self) -> sqlite3.Connection:
        return _connect(self._db_path)

    def upsert_page(
        self,
        notion_page_id: str,
        *,
        onenote_page_id: Optional[str] = None,
        notion_title: Optional[str] = None,
        last_notion_edit: Optional[datetime] = None,
        last_onenote_edit: Optional[datetime] = None,
        content_hash: Optional[str] = None,
        last_source: Optional[str] = None,
        sync_status: str = "pending",
        conversion_notes: Optional[str] = None,
        parent_notion_id: Optional[str] = None,
        onenote_section_id: Optional[str] = None,
        section_mode: Optional[str] = None,
        sync_section_name: Optional[str] = None,
        sync_section_group_path: Optional[str] = None,
        sync_use_flat_section: Optional[bool] = None,
    ) -> None:
        conn = self._conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO page_sync (
                notion_page_id, onenote_page_id, notion_title,
                last_notion_edit, last_onenote_edit, last_synced,
                content_hash, last_source, sync_status, conversion_notes,
                parent_notion_id, onenote_section_id, section_mode,
                sync_section_name, sync_section_group_path, sync_use_flat_section
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(notion_page_id) DO UPDATE SET
                onenote_page_id   = COALESCE(excluded.onenote_page_id, page_sync.onenote_page_id),
                notion_title      = COALESCE(excluded.notion_title, page_sync.notion_title),
                last_notion_edit  = COALESCE(excluded.last_notion_edit, page_sync.last_notion_edit),
                last_onenote_edit = COALESCE(excluded.last_onenote_edit, page_sync.last_onenote_edit),
                last_synced       = excluded.last_synced,
                content_hash      = excluded.content_hash,
                last_source       = COALESCE(excluded.last_source, page_sync.last_source),
                sync_status       = excluded.sync_status,
                conversion_notes  = COALESCE(excluded.conversion_notes, page_sync.conversion_notes),
                parent_notion_id  = COALESCE(excluded.parent_notion_id, page_sync.parent_notion_id),
                onenote_section_id= COALESCE(excluded.onenote_section_id, page_sync.onenote_section_id),
                section_mode      = COALESCE(excluded.section_mode, page_sync.section_mode),
                sync_section_name       = COALESCE(excluded.sync_section_name, page_sync.sync_section_name),
                sync_section_group_path = COALESCE(excluded.sync_section_group_path, page_sync.sync_section_group_path),
                sync_use_flat_section   = COALESCE(excluded.sync_use_flat_section, page_sync.sync_use_flat_section)
            """,
            (
                notion_page_id, onenote_page_id, notion_title,
                last_notion_edit.isoformat() if last_notion_edit else None,
                last_onenote_edit.isoformat() if last_onenote_edit else None,
                now,
                content_hash, last_source, sync_status, conversion_notes,
                parent_notion_id, onenote_section_id, section_mode,
                sync_section_name, sync_section_group_path,
                int(sync_use_flat_section) if sync_use_flat_section is not None else None,
            ),
        )
        conn.commit()
        conn.close()

    def get_by_notion_id(self, notion_page_id: str) -> Optional[dict]:
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM page_sync WHERE notion_page_id = ?",
            (notion_page_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def get_by_onenote_id(self, onenote_page_id: str) -> Optional[dict]:
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM page_sync WHERE onenote_page_id = ?",
            (onenote_page_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def get_children(self, parent_notion_id: str) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM page_sync WHERE parent_notion_id = ? ORDER BY notion_title",
            (parent_notion_id,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_section_id_for_page(self, notion_page_id: str) -> Optional[str]:
        """Walk up the parent chain to find the OneNote section ID for a page."""
        conn = self._conn()
        current_id = notion_page_id
        for _ in range(10):  # guard against cycles
            row = conn.execute(
                "SELECT parent_notion_id, onenote_section_id FROM page_sync WHERE notion_page_id = ?",
                (current_id,),
            ).fetchone()
            if not row:
                break
            if row["onenote_section_id"]:
                conn.close()
                return row["onenote_section_id"]
            if not row["parent_notion_id"]:
                break
            current_id = row["parent_notion_id"]
        conn.close()
        return None

    def get_all(self) -> list[dict]:
        conn = self._conn()
        rows = conn.execute("SELECT * FROM page_sync ORDER BY last_synced DESC").fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_conflicts(self) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM page_sync WHERE sync_status = 'conflict' ORDER BY last_synced DESC"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_errors(self) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM page_sync WHERE sync_status = 'error' ORDER BY last_synced DESC"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def set_status(self, notion_page_id: str, status: str) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE page_sync SET sync_status = ? WHERE notion_page_id = ?",
            (status, notion_page_id),
        )
        conn.commit()
        conn.close()

    def get_last_sync_time(self) -> Optional[str]:
        conn = self._conn()
        row = conn.execute(
            "SELECT MAX(last_synced) AS ts FROM page_sync"
        ).fetchone()
        conn.close()
        return row["ts"] if row else None

    def reset_subtree(self, root_notion_id: str) -> None:
        """Clear all OneNote IDs for a root page and every descendant.

        Used when section_mode changes (flat ↔ grouped) so that the next sync
        recreates everything in the correct structure. The old OneNote section
        or section group will be orphaned — manual cleanup in OneNote required.
        """
        conn = self._conn()
        conn.execute(
            """
            WITH RECURSIVE descendants(id) AS (
                SELECT notion_page_id FROM page_sync WHERE notion_page_id = ?
                UNION ALL
                SELECT p.notion_page_id FROM page_sync p
                JOIN descendants d ON p.parent_notion_id = d.id
            )
            UPDATE page_sync
            SET onenote_page_id    = NULL,
                onenote_section_id = NULL,
                section_mode       = NULL,
                sync_status        = 'pending'
            WHERE notion_page_id IN (SELECT id FROM descendants)
            """,
            (root_notion_id,),
        )
        conn.commit()
        conn.close()

    def delete_page(self, notion_page_id: str) -> None:
        conn = self._conn()
        conn.execute(
            "DELETE FROM page_sync WHERE notion_page_id = ?",
            (notion_page_id,),
        )
        conn.commit()
        conn.close()

    def count_by_status(self) -> dict[str, int]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT sync_status, COUNT(*) AS cnt FROM page_sync GROUP BY sync_status"
        ).fetchall()
        conn.close()
        return {r["sync_status"]: r["cnt"] for r in rows}
