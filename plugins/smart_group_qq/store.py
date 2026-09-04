"""Persistent, redacted state for the Smart Group QQ plugin.

The plugin storage API returns a profile-scoped sqlite connection.  This module
only stores message text in the bounded group-history table (needed for local
summaries); audit rows deliberately contain metadata, never message or reply
content.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


PENDING_STALE_SECONDS = 300.0


class Store:
    """Small SQLite repository backed by Hermes ``plugin_storage.plugin_db()``."""

    def __init__(self, database: Any = None):
        self._owned = False
        if database is None:
            database = ":memory:"
        if isinstance(database, (str, Path)):
            self.db = sqlite3.connect(str(database), check_same_thread=False)
            self._owned = True
        else:
            self.db = database
        # A plugin_db connection is a sqlite3 connection in Hermes 0.21.0.
        # Keep this setup deliberately narrow so no alternate storage backend
        # is silently invented.
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    @classmethod
    def from_context(cls, ctx: Any) -> "Store":
        storage = getattr(ctx, "plugin_storage", None)
        if storage is None and isinstance(ctx, dict):
            storage = ctx.get("plugin_storage")
        if storage is not None:
            db_factory = getattr(storage, "plugin_db", None)
            if callable(db_factory):
                return cls(db_factory())
        # This fallback is only useful for import/unit tests.  A running
        # Hermes context always supplies plugin_storage.plugin_db().
        return cls(":memory:")

    def _init_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS message_claims (
                platform TEXT NOT NULL,
                message_id TEXT NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                claimed_at REAL NOT NULL,
                completed_at REAL,
                PRIMARY KEY (platform, message_id, action)
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                platform TEXT NOT NULL,
                chat_id TEXT,
                message_id TEXT,
                action TEXT,
                rule_id TEXT,
                source TEXT,
                confidence REAL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS group_memories (
                group_id TEXT PRIMARY KEY,
                summary TEXT NOT NULL DEFAULT '',
                window_size INTEGER NOT NULL DEFAULT 20,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS group_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                role TEXT NOT NULL,
                member_id TEXT,
                text TEXT NOT NULL,
                message_id TEXT,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_group_history_time
                ON group_history(group_id, id DESC);
            """
        )
        self.db.commit()

    def close(self) -> None:
        if self._owned:
            self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.db
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def claim_message(
        self,
        platform: str,
        message_id: str,
        action: str,
        *,
        now: float | None = None,
        stale_after: float = PENDING_STALE_SECONDS,
    ) -> bool:
        """Atomically claim a side effect.

        Completed claims are permanent.  A failed claim or a pending claim older
        than ``stale_after`` may be retried; a fresh pending claim is rejected.
        """
        platform, message_id, action = str(platform), str(message_id), str(action)
        if not message_id:
            # Events without an id cannot be made idempotent.  Let the caller
            # perform the operation once rather than deduplicating all such
            # events under an empty key.
            return True
        now = time.time() if now is None else float(now)
        with self.transaction() as db:
            row = db.execute(
                "SELECT status, claimed_at FROM message_claims "
                "WHERE platform=? AND message_id=? AND action=?",
                (platform, message_id, action),
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO message_claims "
                    "(platform,message_id,action,status,claimed_at) VALUES (?,?,?,?,?)",
                    (platform, message_id, action, "pending", now),
                )
                return True
            status = str(row["status"])
            age = now - float(row["claimed_at"])
            if status in {"sent", "completed", "success"}:
                return False
            if status == "pending" and age < stale_after:
                return False
            db.execute(
                "UPDATE message_claims SET status='pending', claimed_at=?, completed_at=NULL "
                "WHERE platform=? AND message_id=? AND action=?",
                (now, platform, message_id, action),
            )
            return True

    # Short aliases make this repository convenient for a hook and for callers
    # that use the terminology from Hermes' own delivery ledger.
    claim = claim_message

    def finish_claim(
        self,
        platform: str,
        message_id: str,
        action: str,
        *,
        success: bool,
        now: float | None = None,
    ) -> None:
        if not message_id:
            return
        stamp = time.time() if now is None else float(now)
        status = "sent" if success else "failed"
        self.db.execute(
            "UPDATE message_claims SET status=?, completed_at=? "
            "WHERE platform=? AND message_id=? AND action=?",
            (status, stamp, str(platform), str(message_id), str(action)),
        )
        self.db.commit()

    complete_claim = finish_claim

    def record_audit(
        self,
        event_type: str,
        *,
        platform: str = "qqbot",
        chat_id: str | None = None,
        message_id: str | None = None,
        action: str | None = None,
        rule_id: str | None = None,
        source: str | None = None,
        confidence: float | None = None,
    ) -> None:
        """Record metadata only; this method has no text/reply parameters."""
        self.db.execute(
            "INSERT INTO audit_events "
            "(event_type,platform,chat_id,message_id,action,rule_id,source,confidence,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                str(event_type), str(platform),
                None if chat_id is None else str(chat_id),
                None if message_id is None else str(message_id),
                None if action is None else str(action),
                None if rule_id is None else str(rule_id),
                None if source is None else str(source),
                None if confidence is None else float(confidence),
                time.time(),
            ),
        )
        self.db.commit()

    audit = record_audit

    def append_history(
        self,
        group_id: str,
        *,
        role: str,
        text: str,
        member_id: str | None = None,
        message_id: str | None = None,
        created_at: float | None = None,
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO group_history "
            "(group_id,role,member_id,text,message_id,created_at) VALUES (?,?,?,?,?,?)",
            (
                str(group_id), str(role),
                None if member_id is None else str(member_id), str(text),
                None if message_id is None else str(message_id),
                time.time() if created_at is None else float(created_at),
            ),
        )
        self.db.commit()
        return int(cur.lastrowid)

    add_history = append_history

    def get_history(self, group_id: str, limit: int | None = None) -> list[sqlite3.Row]:
        if limit is None:
            rows = self.db.execute(
                "SELECT * FROM group_history WHERE group_id=? ORDER BY id ASC",
                (str(group_id),),
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM (SELECT * FROM group_history WHERE group_id=? "
                "ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                (str(group_id), int(limit)),
            ).fetchall()
        return list(rows)

    def clear_group(self, group_id: str) -> None:
        self.db.execute("DELETE FROM group_history WHERE group_id=?", (str(group_id),))
        self.db.execute("DELETE FROM group_memories WHERE group_id=?", (str(group_id),))
        self.db.commit()

    reset_group = clear_group

    def get_memory(self, group_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM group_memories WHERE group_id=?", (str(group_id),)
        ).fetchone()

    def set_memory(
        self, group_id: str, summary: str, *, window_size: int = 20, updated_at: float | None = None
    ) -> None:
        self.db.execute(
            "INSERT INTO group_memories(group_id,summary,window_size,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(group_id) DO UPDATE SET summary=excluded.summary, "
            "window_size=excluded.window_size, updated_at=excluded.updated_at",
            (
                str(group_id), str(summary), int(window_size),
                time.time() if updated_at is None else float(updated_at),
            ),
        )
        self.db.commit()

    def count_history(self, group_id: str) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM group_history WHERE group_id=?", (str(group_id),)
        ).fetchone()
        return int(row["n"])

    def trim_history(self, group_id: str, keep: int) -> int:
        """Keep only the newest bounded rows for one group."""
        keep = max(0, int(keep))
        with self.transaction() as db:
            cursor = db.execute(
                "DELETE FROM group_history WHERE group_id=? AND id NOT IN "
                "(SELECT id FROM group_history WHERE group_id=? ORDER BY id DESC LIMIT ?)",
                (str(group_id), str(group_id), keep),
            )
            return max(0, int(cursor.rowcount))

    def integrity_check(self) -> bool:
        row = self.db.execute("PRAGMA integrity_check").fetchone()
        return bool(row and str(row[0]).lower() == "ok")


__all__ = ["Store", "PENDING_STALE_SECONDS"]
