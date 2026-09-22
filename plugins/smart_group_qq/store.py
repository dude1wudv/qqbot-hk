"""Persistent, redacted state for the Smart Group QQ plugin.

The plugin storage API returns a profile-scoped sqlite connection.  This module
only stores message text in the bounded group-history table (needed for local
summaries); audit rows deliberately contain metadata, never message or reply
content.
"""
from __future__ import annotations

import heapq
import math
import sqlite3
import time
import hashlib
import hmac
import json
import os
import re
import threading
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


PENDING_STALE_SECONDS = 300.0
SCHEMA_VERSION = 4
_MEMBER_REF_NAMESPACE = b"smart_group_qq/member-ref/v1"
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{12,}\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+"),
)


class Store:
    """Small SQLite repository backed by Hermes ``plugin_storage.plugin_db()``."""

    def __init__(self, database: Any = None, *, member_secret: str | bytes | None = None):
        self._owned = False
        if database is None:
            database = ":memory:"
        if isinstance(database, (str, Path)):
            self.db = sqlite3.connect(str(database), check_same_thread=False)
            self._owned = True
        else:
            self.db = database
        self._lock = threading.RLock()
        self._member_secret = (
            str(member_secret).encode("utf-8")
            if isinstance(member_secret, str)
            else bytes(member_secret)
            if member_secret is not None
            else _MEMBER_REF_NAMESPACE
        )
        # A plugin_db connection is a sqlite3 connection in Hermes 0.21.0.
        # Keep this setup deliberately narrow so no alternate storage backend
        # is silently invented.
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        try:
            self._init_schema()
        except Exception:
            if self._owned:
                self.db.close()
            raise

    @classmethod
    def from_context(cls, ctx: Any) -> "Store":
        storage = getattr(ctx, "plugin_storage", None)
        if storage is None and isinstance(ctx, dict):
            storage = ctx.get("plugin_storage")
        if storage is not None:
            db_factory = getattr(storage, "plugin_db", None)
            if callable(db_factory):
                return cls(db_factory(), member_secret=os.environ.get("QQ_CLIENT_SECRET"))
        # This fallback is only useful for import/unit tests.  A running
        # Hermes context always supplies plugin_storage.plugin_db().
        return cls(":memory:")

    def _init_schema(self) -> None:
        version_row = self.db.execute("PRAGMA user_version").fetchone()
        try:
            schema_version = int(version_row[0]) if version_row else 0
        except (TypeError, ValueError, IndexError):
            schema_version = 0
        stored_schema_version = schema_version
        if schema_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"smart_group_qq database schema {schema_version} is newer than supported {SCHEMA_VERSION}"
            )
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS character_state (
                scope TEXT PRIMARY KEY, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS character_items (
                id TEXT NOT NULL, scope TEXT NOT NULL, kind TEXT NOT NULL,
                owner TEXT NOT NULL, text TEXT NOT NULL, evidence TEXT NOT NULL,
                status TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
                PRIMARY KEY(scope,id)
            );
            CREATE INDEX IF NOT EXISTS character_items_scope ON character_items(scope,kind,expires);
            CREATE TABLE IF NOT EXISTS character_relations (
                scope TEXT NOT NULL, owner TEXT NOT NULL, count INTEGER NOT NULL,
                PRIMARY KEY(scope,owner)
            );
            CREATE TABLE IF NOT EXISTS character_commands (
                scope TEXT NOT NULL, message_id TEXT NOT NULL, result TEXT NOT NULL,
                created REAL NOT NULL, PRIMARY KEY(scope,message_id)
            );
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
                structured_json TEXT NOT NULL DEFAULT '{}',
                last_history_id INTEGER NOT NULL DEFAULT 0,
                model TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS group_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                role TEXT NOT NULL,
                member_id TEXT,
                text TEXT NOT NULL,
                message_id TEXT,
                source_kind TEXT NOT NULL DEFAULT 'addressed',
                media_json TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_group_history_time
                ON group_history(group_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_group_history_dedupe
                ON group_history(group_id, role, message_id);
            CREATE TABLE IF NOT EXISTS knowledge_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                title TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'text',
                content_hash TEXT NOT NULL,
                source_message_id TEXT,
                created_by TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(group_id, content_hash)
            );
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
                group_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(document_id, chunk_index)
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_documents_group
                ON knowledge_documents(group_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_group
                ON knowledge_chunks(group_id, document_id, chunk_index);
            """
        )
        # Version 1 contains the structured-memory/history columns added after
        # the original summary-only schema. Keep the migration explicit for
        # databases created before PRAGMA user_version was introduced.
        if schema_version < 1:
            self._ensure_column("group_memories", "structured_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column("group_memories", "last_history_id", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("group_memories", "model", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("group_memories", "version", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column("group_history", "source_kind", "TEXT NOT NULL DEFAULT 'addressed'")
            self._ensure_column("group_history", "media_json", "TEXT NOT NULL DEFAULT '[]'")
            schema_version = 1
        else:
            # Handle a database whose previous migration was interrupted.
            self._ensure_column("group_memories", "structured_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column("group_memories", "last_history_id", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("group_memories", "model", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("group_memories", "version", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column("group_history", "source_kind", "TEXT NOT NULL DEFAULT 'addressed'")
            self._ensure_column("group_history", "media_json", "TEXT NOT NULL DEFAULT '[]'")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_group_history_ambient_time "
            "ON group_history(group_id, source_kind, created_at DESC, id DESC)"
        )
        if schema_version < 2:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS compaction_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    from_history_id INTEGER NOT NULL DEFAULT 0,
                    to_history_id INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempt INTEGER NOT NULL DEFAULT 0,
                    next_retry_at REAL NOT NULL DEFAULT 0,
                    lease_until REAL,
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(group_id, from_history_id, to_history_id)
                );
                CREATE INDEX IF NOT EXISTS idx_compaction_jobs_ready
                    ON compaction_jobs(status, next_retry_at, id);
                CREATE INDEX IF NOT EXISTS idx_compaction_jobs_group
                    ON compaction_jobs(group_id, id DESC);
                CREATE TABLE IF NOT EXISTS group_members (
                    group_id TEXT NOT NULL,
                    member_ref TEXT NOT NULL,
                    member_digest TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    consent_status TEXT NOT NULL DEFAULT 'unknown',
                    first_seen_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    profile_version INTEGER NOT NULL DEFAULT 1,
                    deleted_at REAL,
                    PRIMARY KEY (group_id, member_ref),
                    UNIQUE (group_id, member_digest)
                );
                CREATE INDEX IF NOT EXISTS idx_group_members_seen
                    ON group_members(group_id, last_seen_at DESC);
                CREATE TABLE IF NOT EXISTS member_memory_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    member_ref TEXT NOT NULL,
                    category TEXT NOT NULL,
                    fact_key TEXT NOT NULL,
                    fact_value TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    explicitness TEXT NOT NULL DEFAULT 'inferred',
                    source_history_id INTEGER,
                    expires_at REAL,
                    status TEXT NOT NULL DEFAULT 'active',
                    supersedes_id INTEGER,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_member_facts_active
                    ON member_memory_facts(group_id, member_ref, status, expires_at);
                CREATE INDEX IF NOT EXISTS idx_member_facts_key
                    ON member_memory_facts(group_id, member_ref, category, fact_key, id DESC);
                """
            )
            schema_version = 2
        self._ensure_column("member_memory_facts", "evidence", "TEXT NOT NULL DEFAULT ''")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS group_memory_epochs ("
            "group_id TEXT PRIMARY KEY, epoch INTEGER NOT NULL DEFAULT 0)"
        )
        if stored_schema_version < SCHEMA_VERSION:
            self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.db.commit()

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        columns = {str(row[1]) for row in self.db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def close(self) -> None:
        if self._owned:
            with self._lock:
                self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                if not self.db.in_transaction:
                    self.db.execute("BEGIN IMMEDIATE")
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
        with self.transaction() as db:
            db.execute(
                "UPDATE message_claims SET status=?, completed_at=? "
                "WHERE platform=? AND message_id=? AND action=?",
                (status, stamp, str(platform), str(message_id), str(action)),
            )

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
        with self.transaction() as db:
            db.execute(
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

    audit = record_audit

    @staticmethod
    def redact_text(value: Any) -> str:
        text = str(value or "")
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub("[已隐藏敏感信息]", text)
        return text

    def append_history(
        self,
        group_id: str,
        *,
        role: str,
        text: str,
        member_id: str | None = None,
        message_id: str | None = None,
        source_kind: str = "addressed",
        media: Any = None,
        created_at: float | None = None,
        expected_epoch: int | None = None,
    ) -> int:
        with self.transaction() as db:
            if expected_epoch is not None and self.memory_epoch(group_id) != expected_epoch:
                return 0
            if message_id:
                existing = db.execute(
                    "SELECT id FROM group_history WHERE group_id=? AND role=? AND message_id=? LIMIT 1",
                    (str(group_id), str(role), str(message_id)),
                ).fetchone()
                if existing is not None:
                    return int(existing["id"])
            cur = db.execute(
                "INSERT INTO group_history "
                "(group_id,role,member_id,text,message_id,source_kind,media_json,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    str(group_id), str(role),
                    None if member_id is None else str(member_id), self.redact_text(text),
                    None if message_id is None else str(message_id), str(source_kind or "addressed"),
                    json.dumps(media or [], ensure_ascii=False, separators=(",", ":")),
                    time.time() if created_at is None else float(created_at),
                ),
            )
            return int(cur.lastrowid)

    add_history = append_history

    def get_history_message(self, group_id: str, message_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self.db.execute(
                "SELECT * FROM group_history WHERE group_id=? AND role='user' AND message_id=? LIMIT 1",
                (str(group_id), str(message_id)),
            ).fetchone()

    def get_history(self, group_id: str, limit: int | None = None) -> list[sqlite3.Row]:
        with self._lock:
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

    def get_history_since(self, group_id: str, after_id: int = 0, limit: int = 200) -> list[sqlite3.Row]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM group_history WHERE group_id=? AND id>? ORDER BY id ASC LIMIT ?",
                (str(group_id), max(0, int(after_id)), max(1, int(limit))),
            ).fetchall()
        return list(rows)

    def get_recent_history_since(self, group_id: str, after_id: int = 0, limit: int = 12) -> list[sqlite3.Row]:
        """Return the newest history rows after a cursor in chronological order."""

        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM (SELECT * FROM group_history "
                "WHERE group_id=? AND id>? ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                (str(group_id), max(0, int(after_id)), max(1, int(limit))),
            ).fetchall()
        return list(rows)

    def recent_ambient_history(
        self,
        group_id: str,
        *,
        before_id: int | None = None,
        before_time: float | None = None,
        limit: int = 12,
        max_age_seconds: float | None = 900,
        now: float | None = None,
    ) -> list[sqlite3.Row]:
        """Return recent non-@ rows before an event, oldest-to-newest.

        ``before_id`` and ``before_time`` are exclusive upper bounds.  Supplying
        both is useful when event IDs and timestamps can arrive out of order.
        ``max_age_seconds`` is relative to ``now`` and may be ``None`` to disable
        the age bound.  The query is deliberately group-scoped.
        """

        conditions = ["group_id=?", "source_kind='ambient'"]
        params: list[Any] = [str(group_id)]
        if before_id is not None:
            conditions.append("id<?")
            params.append(max(0, int(before_id)))
        if before_time is not None:
            conditions.append("created_at<?")
            params.append(float(before_time))
        if max_age_seconds is not None:
            age = max(0.0, float(max_age_seconds))
            stamp = time.time() if now is None else float(now)
            conditions.append("created_at>=?")
            params.append(stamp - age)
        where = " AND ".join(conditions)
        params.append(max(1, int(limit)))
        with self._lock:
            rows = self.db.execute(
                f"SELECT * FROM (SELECT * FROM group_history WHERE {where} "
                "ORDER BY created_at DESC, id DESC LIMIT ?) "
                "ORDER BY created_at ASC, id ASC",
                tuple(params),
            ).fetchall()
        return list(rows)

    def latest_history_id(self, group_id: str) -> int:
        with self._lock:
            row = self.db.execute(
                "SELECT COALESCE(MAX(id),0) AS n FROM group_history WHERE group_id=?", (str(group_id),)
            ).fetchone()
        return int(row["n"])

    def enrich_history(
        self,
        group_id: str,
        message_id: str,
        text: str | None = None,
        media: Any = None,
    ) -> bool:
        """Atomically enrich an existing ambient row after media processing.

        The operation is intentionally update-only: a late media callback must
        never create a history row for an event that was not ingested. Text and
        media are merged idempotently so a retried callback cannot duplicate
        the same transcript/description.
        """

        incoming_text = self.redact_text(text).strip()
        with self.transaction() as db:
            row = db.execute(
                "SELECT id,text,media_json FROM group_history "
                "WHERE group_id=? AND role='user' AND message_id=? AND source_kind='ambient' "
                "ORDER BY id DESC LIMIT 1",
                (str(group_id), str(message_id)),
            ).fetchone()
            if row is None:
                return False
            current_text = str(row["text"] or "").strip()
            if incoming_text and incoming_text not in current_text:
                merged_text = "\n\n".join(item for item in (current_text, incoming_text) if item)
            else:
                merged_text = current_text
            try:
                current_media = json.loads(str(row["media_json"] or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                current_media = []
            if not isinstance(current_media, list):
                current_media = []
            incoming_media = media if isinstance(media, (list, tuple, set)) else ([] if media is None else [media])
            merged_media = list(current_media)
            for item in incoming_media:
                if item not in merged_media:
                    merged_media.append(item)
            db.execute(
                "UPDATE group_history SET text=?, media_json=? WHERE id=?",
                (
                    merged_text,
                    json.dumps(merged_media, ensure_ascii=False, separators=(",", ":")),
                    int(row["id"]),
                ),
            )
            return True

    def memory_epoch(self, group_id: str) -> int:
        with self._lock:
            row = self.db.execute(
                "SELECT epoch FROM group_memory_epochs WHERE group_id=?", (str(group_id),)
            ).fetchone()
        return int(row["epoch"]) if row is not None else 0

    def _advance_memory_epoch(self, group_id: str) -> None:
        """Invalidate in-flight work inside the caller's write transaction."""
        self.db.execute(
            "INSERT INTO group_memory_epochs(group_id,epoch) VALUES(?,1) "
            "ON CONFLICT(group_id) DO UPDATE SET epoch=epoch+1",
            (str(group_id),),
        )

    def clear_group(self, group_id: str) -> None:
        with self.transaction() as db:
            self._advance_memory_epoch(group_id)
            for table in ("character_state", "character_items", "character_relations", "character_commands"):
                db.execute(f"DELETE FROM {table} WHERE scope=?", (str(group_id),))
            db.execute("DELETE FROM group_history WHERE group_id=?", (str(group_id),))
            db.execute("DELETE FROM group_memories WHERE group_id=?", (str(group_id),))
            db.execute("DELETE FROM compaction_jobs WHERE group_id=?", (str(group_id),))

    reset_group = clear_group

    def get_memory(self, group_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self.db.execute(
                "SELECT * FROM group_memories WHERE group_id=?", (str(group_id),)
            ).fetchone()

    def set_memory(
        self,
        group_id: str,
        summary: str,
        *,
        window_size: int = 20,
        structured: Any = None,
        last_history_id: int = 0,
        model: str = "",
        version: int = 1,
        updated_at: float | None = None,
        expected_epoch: int | None = None,
    ) -> bool:
        payload = structured if isinstance(structured, dict) else {}
        with self.transaction() as db:
            if expected_epoch is not None and self.memory_epoch(group_id) != expected_epoch:
                return False
            db.execute(
                "INSERT INTO group_memories"
                "(group_id,summary,window_size,structured_json,last_history_id,model,version,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(group_id) DO UPDATE SET summary=excluded.summary, "
                "window_size=excluded.window_size, structured_json=excluded.structured_json, "
                "last_history_id=excluded.last_history_id, model=excluded.model, "
                "version=excluded.version, updated_at=excluded.updated_at",
                (
                    str(group_id), str(summary), int(window_size),
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    max(0, int(last_history_id)), str(model or ""), max(1, int(version)),
                    time.time() if updated_at is None else float(updated_at),
                ),
            )
        return True

    def memory_payload(self, group_id: str) -> dict[str, Any]:
        row = self.get_memory(group_id)
        if row is None:
            return {}
        try:
            structured = json.loads(str(row["structured_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            structured = {}
        if not structured and str(row["summary"] or "").strip():
            structured = {
                "summary": str(row["summary"]).strip(),
                "topics": [], "facts": [], "decisions": [], "todos": [],
                "open_questions": [], "participants": [],
            }
        return {
            "summary": str(row["summary"] or ""),
            "structured": structured if isinstance(structured, dict) else {},
            "last_history_id": int(row["last_history_id"] or 0),
            "model": str(row["model"] or ""),
            "version": int(row["version"] or 1),
            "updated_at": float(row["updated_at"]),
        }

    def enqueue_compaction_job(
        self,
        group_id: str,
        from_history_id: int = 0,
        to_history_id: int | None = None,
        *,
        now: float | None = None,
    ) -> int:
        """Create or extend one durable compaction job for a group."""

        group = str(group_id)
        start = max(0, int(from_history_id))
        target = self.latest_history_id(group) if to_history_id is None else max(0, int(to_history_id))
        if target <= start:
            return 0
        stamp = time.time() if now is None else float(now)
        with self.transaction() as db:
            active = db.execute(
                "SELECT id,to_history_id,status FROM compaction_jobs "
                "WHERE group_id=? AND status IN ('pending','running','failed') "
                "ORDER BY id DESC LIMIT 1",
                (group,),
            ).fetchone()
            if active is not None:
                # A failed job retains its original cursor; only extend its
                # target.  A running lease remains owned by its worker.
                new_target = max(int(active["to_history_id"]), target)
                if str(active["status"]) == "failed":
                    db.execute(
                        "UPDATE compaction_jobs SET to_history_id=?, updated_at=? WHERE id=?",
                        (new_target, stamp, int(active["id"])),
                    )
                elif new_target != int(active["to_history_id"]):
                    db.execute(
                        "UPDATE compaction_jobs SET to_history_id=?, updated_at=? WHERE id=?",
                        (new_target, stamp, int(active["id"])),
                    )
                return int(active["id"])
            cur = db.execute(
                "INSERT INTO compaction_jobs "
                "(group_id,from_history_id,to_history_id,status,attempt,next_retry_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (group, start, target, "pending", 0, stamp, stamp, stamp),
            )
            return int(cur.lastrowid)

    def get_compaction_job(self, group_id: str, *, include_completed: bool = False) -> sqlite3.Row | None:
        statuses = "" if include_completed else "AND status NOT IN ('completed','cancelled')"
        with self._lock:
            return self.db.execute(
                f"SELECT * FROM compaction_jobs WHERE group_id=? {statuses} ORDER BY id DESC LIMIT 1",
                (str(group_id),),
            ).fetchone()

    def list_compaction_jobs(self, group_id: str | None = None, *, limit: int = 100) -> list[sqlite3.Row]:
        with self._lock:
            if group_id is None:
                rows = self.db.execute(
                    "SELECT * FROM compaction_jobs ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
                ).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT * FROM compaction_jobs WHERE group_id=? ORDER BY id DESC LIMIT ?",
                    (str(group_id), max(1, int(limit))),
                ).fetchall()
        return list(rows)

    def claim_compaction_job(
        self,
        job_id: int | None = None,
        *,
        group_id: str | None = None,
        lease_seconds: float = PENDING_STALE_SECONDS,
        now: float | None = None,
        force: bool = False,
    ) -> sqlite3.Row | None:
        """Lease a pending/failed compaction job atomically."""

        stamp = time.time() if now is None else float(now)
        with self.transaction() as db:
            if job_id is not None:
                row = db.execute(
                    "SELECT * FROM compaction_jobs WHERE id=?", (int(job_id),)
                ).fetchone()
            else:
                group_clause = " AND group_id=?" if group_id is not None else ""
                params: list[Any] = [stamp]
                if group_id is not None:
                    params.append(str(group_id))
                row = db.execute(
                    "SELECT * FROM compaction_jobs WHERE "
                    "((status IN ('pending','failed') AND (next_retry_at<=? OR ?)) OR "
                    "(status='running' AND lease_until IS NOT NULL AND lease_until<=?))"
                    + group_clause + " ORDER BY id ASC LIMIT 1",
                    (stamp, bool(force), stamp, *params[1:]),
                ).fetchone()
            if row is None:
                return None
            status = str(row["status"])
            ready = status in {"pending", "failed"} and (force or float(row["next_retry_at"] or 0) <= stamp)
            expired = status == "running" and row["lease_until"] is not None and float(row["lease_until"]) <= stamp
            if not (ready or expired):
                return None
            lease_until = stamp + max(1.0, float(lease_seconds))
            db.execute(
                "UPDATE compaction_jobs SET status='running', attempt=attempt+1, "
                "lease_until=?, updated_at=? WHERE id=?",
                (lease_until, stamp, int(row["id"])),
            )
            return db.execute("SELECT * FROM compaction_jobs WHERE id=?", (int(row["id"]),)).fetchone()

    def finish_compaction_job(
        self,
        job_id: int,
        success: bool,
        *,
        error: str | None = None,
        retry_delay: float | None = None,
        now: float | None = None,
    ) -> None:
        """Complete a job or retain it as a retryable failure."""

        stamp = time.time() if now is None else float(now)
        with self.transaction() as db:
            row = db.execute("SELECT attempt FROM compaction_jobs WHERE id=?", (int(job_id),)).fetchone()
            if row is None:
                return
            if success:
                db.execute(
                    "UPDATE compaction_jobs SET status='completed', lease_until=NULL, "
                    "error='', next_retry_at=0, updated_at=? WHERE id=?",
                    (stamp, int(job_id)),
                )
                return
            attempt = max(1, int(row["attempt"] or 1))
            delay = min(3600.0, max(1.0, float(retry_delay) if retry_delay is not None else 5.0 * (2 ** min(attempt - 1, 8))))
            db.execute(
                "UPDATE compaction_jobs SET status='failed', lease_until=NULL, error=?, "
                "next_retry_at=?, updated_at=? WHERE id=?",
                (str(error or "compaction failed")[:500], stamp + delay, stamp, int(job_id)),
            )

    def defer_compaction_job(
        self,
        job_id: int,
        *,
        from_history_id: int,
        to_history_id: int | None = None,
        now: float | None = None,
    ) -> None:
        """Return a leased job to the queue after a bounded batch."""

        stamp = time.time() if now is None else float(now)
        target = max(0, int(to_history_id)) if to_history_id is not None else max(0, int(from_history_id))
        with self.transaction() as db:
            if to_history_id is None:
                row = db.execute("SELECT group_id FROM compaction_jobs WHERE id=?", (int(job_id),)).fetchone()
                target = self.latest_history_id(str(row["group_id"])) if row else max(0, int(from_history_id))
            db.execute(
                "UPDATE compaction_jobs SET from_history_id=?, to_history_id=?, status='pending', "
                "lease_until=NULL, next_retry_at=?, error='', updated_at=? WHERE id=?",
                (max(0, int(from_history_id)), target, stamp, stamp, int(job_id)),
            )

    def count_history(self, group_id: str) -> int:
        with self._lock:
            row = self.db.execute(
                "SELECT COUNT(*) AS n FROM group_history WHERE group_id=?", (str(group_id),)
            ).fetchone()
        return int(row["n"])

    def trim_history(self, group_id: str, keep: int) -> int:
        """Trim summarized rows while never deleting data ahead of the memory cursor."""
        keep = max(0, int(keep))
        with self.transaction() as db:
            cursor = db.execute(
                "DELETE FROM group_history WHERE group_id=? "
                "AND id<=COALESCE((SELECT last_history_id FROM group_memories WHERE group_id=?),0) "
                "AND id NOT IN "
                "(SELECT id FROM group_history WHERE group_id=? ORDER BY id DESC LIMIT ?)",
                (str(group_id), str(group_id), str(group_id), keep),
            )
            return max(0, int(cursor.rowcount))

    def purge_expired_history(
        self,
        group_id: str | None = None,
        *,
        max_age_seconds: float,
        now: float | None = None,
    ) -> int:
        """Delete history older than the configured retention window."""

        cutoff = (time.time() if now is None else float(now)) - max(0.0, float(max_age_seconds))
        with self.transaction() as db:
            if group_id is None:
                cursor = db.execute(
                    "DELETE FROM group_history WHERE created_at<? AND id<=COALESCE("
                    "(SELECT last_history_id FROM group_memories WHERE group_id=group_history.group_id),0)",
                    (cutoff,),
                )
            else:
                cursor = db.execute(
                    "DELETE FROM group_history WHERE group_id=? AND created_at<? "
                    "AND id<=COALESCE((SELECT last_history_id FROM group_memories WHERE group_id=?),0)",
                    (str(group_id), cutoff, str(group_id)),
                )
            return max(0, int(cursor.rowcount))

    def purge_history_by_source(
        self,
        source_kind: str,
        max_age_seconds: float,
        *,
        group_id: str | None = None,
        now: float | None = None,
    ) -> int:
        """Delete old history of one source kind for scheduled maintenance."""

        source = str(source_kind or "").strip()
        if not source:
            raise ValueError("source_kind is required")
        cutoff = (time.time() if now is None else float(now)) - max(0.0, float(max_age_seconds))
        with self.transaction() as db:
            if group_id is None:
                cursor = db.execute(
                    "DELETE FROM group_history WHERE source_kind=? AND created_at<? "
                    "AND id<=COALESCE((SELECT last_history_id FROM group_memories WHERE group_id=group_history.group_id),0)",
                    (source, cutoff),
                )
            else:
                cursor = db.execute(
                    "DELETE FROM group_history WHERE group_id=? AND source_kind=? AND created_at<? "
                    "AND id<=COALESCE((SELECT last_history_id FROM group_memories WHERE group_id=?),0)",
                    (str(group_id), source, cutoff, str(group_id)),
                )
            return max(0, int(cursor.rowcount))

    def purge_operational_metadata(
        self,
        *,
        audit_age_seconds: float,
        claim_age_seconds: float,
        now: float | None = None,
    ) -> dict[str, int]:
        """Bound metadata growth without touching conversation content."""

        stamp = time.time() if now is None else float(now)
        with self.transaction() as db:
            audits = db.execute(
                "DELETE FROM audit_events WHERE created_at<?",
                (stamp - max(0.0, float(audit_age_seconds)),),
            )
            claims = db.execute(
                "DELETE FROM message_claims WHERE COALESCE(completed_at,claimed_at)<? "
                "AND status<>'pending'",
                (stamp - max(0.0, float(claim_age_seconds)),),
            )
        return {
            "audit_events": max(0, int(audits.rowcount)),
            "message_claims": max(0, int(claims.rowcount)),
        }

    def list_memory_backlog_groups(self, limit: int = 100) -> list[dict[str, Any]]:
        """List groups whose history cursor is behind their latest row."""

        with self._lock:
            rows = self.db.execute(
                "SELECT h.group_id, MAX(h.id) AS latest_history_id, "
                "COALESCE(m.last_history_id,0) AS memory_cursor, "
                "SUM(CASE WHEN h.id>COALESCE(m.last_history_id,0) THEN 1 ELSE 0 END) AS pending_count, "
                "MIN(CASE WHEN h.id>COALESCE(m.last_history_id,0) THEN h.created_at END) AS oldest_pending_at "
                "FROM group_history h LEFT JOIN group_memories m ON m.group_id=h.group_id "
                "GROUP BY h.group_id "
                "HAVING MAX(h.id)>COALESCE(m.last_history_id,0) "
                "ORDER BY MAX(h.id) DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def member_digest(group_id: Any, member_id: Any, secret: str | bytes | None = None) -> str:
        """Return a stable, non-reversible identifier for a group member."""

        key = (
            str(secret).encode("utf-8")
            if isinstance(secret, str)
            else bytes(secret)
            if secret is not None
            else _MEMBER_REF_NAMESPACE
        )
        payload = (str(group_id) + "\x00" + str(member_id)).encode("utf-8")
        return hmac.new(key, payload, hashlib.sha256).hexdigest()

    @classmethod
    def member_ref(
        cls,
        group_id: Any,
        member_id: Any,
        secret: str | bytes | None = None,
    ) -> str:
        return "m-" + cls.member_digest(group_id, member_id, secret)[:20]

    stable_member_ref = member_ref

    def _member_identity(self, group_id: Any, member_id: Any) -> tuple[str, str]:
        digest = self.member_digest(group_id, member_id, self._member_secret)
        return "m-" + digest[:20], digest

    def member_ref_for(self, group_id: Any, member_id: Any) -> str:
        """Derive a prompt-safe member reference with this store's runtime secret."""

        return self._member_identity(group_id, member_id)[0]

    @staticmethod
    def _bounded_text(value: Any, limit: int = 2000) -> str:
        text = " ".join(str(value or "").replace("\x00", " ").split())
        return text[:max(1, int(limit))]

    def upsert_group_member(
        self,
        group_id: str,
        member_id: str,
        *,
        display_name: str = "",
        consent_status: str | None = None,
        metadata: Any = None,
        increment_messages: bool = False,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Create/touch a member without persisting the raw platform ID."""

        group = str(group_id)
        raw_member = str(member_id or "").strip()
        if not group or not raw_member:
            raise ValueError("group_id and member_id are required")
        ref, digest = self._member_identity(group, raw_member)
        name = self._bounded_text(display_name, 200)
        consent = str(consent_status or "unknown").strip().lower() or "unknown"
        if consent not in {"unknown", "opted_in", "opted_out"}:
            raise ValueError("invalid consent_status")
        metadata_value = metadata if isinstance(metadata, dict) else {}
        stamp = time.time() if now is None else float(now)
        with self.transaction() as db:
            db.execute(
                "INSERT INTO group_members "
                "(group_id,member_ref,member_digest,display_name,consent_status,first_seen_at,last_seen_at,message_count,metadata_json,profile_version,deleted_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,NULL) "
                "ON CONFLICT(group_id,member_ref) DO UPDATE SET "
                "display_name=CASE WHEN excluded.display_name<>'' THEN excluded.display_name ELSE group_members.display_name END, "
                "consent_status=CASE WHEN excluded.consent_status<>'unknown' THEN excluded.consent_status ELSE group_members.consent_status END, "
                "last_seen_at=excluded.last_seen_at, "
                "message_count=group_members.message_count+excluded.message_count, "
                "metadata_json=CASE WHEN excluded.metadata_json<>'{}' THEN excluded.metadata_json ELSE group_members.metadata_json END, "
                "deleted_at=NULL",
                (
                    group, ref, digest, name, consent, stamp, stamp,
                    1 if increment_messages else 0,
                    json.dumps(metadata_value, ensure_ascii=False, separators=(",", ":")), 1,
                ),
            )
            row = db.execute(
                "SELECT * FROM group_members WHERE group_id=? AND member_ref=?", (group, ref)
            ).fetchone()
        return dict(row) if row is not None else {}

    def get_group_member(
        self,
        group_id: str,
        member_id: str | None = None,
        *,
        member_ref: str | None = None,
    ) -> dict[str, Any] | None:
        ref = str(member_ref or "")
        if not ref:
            if not member_id:
                raise ValueError("member_id or member_ref is required")
            ref, _ = self._member_identity(group_id, member_id)
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM group_members WHERE group_id=? AND member_ref=? AND deleted_at IS NULL",
                (str(group_id), ref),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_group_members(self, group_id: str, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        condition = "" if include_deleted else "AND deleted_at IS NULL"
        with self._lock:
            rows = self.db.execute(
                f"SELECT * FROM group_members WHERE group_id=? {condition} ORDER BY last_seen_at DESC",
                (str(group_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_member_consent(
        self,
        group_id: str,
        member_id: str,
        consent_status: str,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        value = str(consent_status or "").strip().lower()
        if value not in {"unknown", "opted_in", "opted_out"}:
            raise ValueError("invalid consent_status")
        self.upsert_group_member(group_id, member_id, now=now)
        ref, _ = self._member_identity(group_id, member_id)
        with self.transaction() as db:
            db.execute(
                "UPDATE group_members SET consent_status=?, profile_version=profile_version+1 "
                "WHERE group_id=? AND member_ref=?",
                (value, str(group_id), ref),
            )
            row = db.execute(
                "SELECT * FROM group_members WHERE group_id=? AND member_ref=?",
                (str(group_id), ref),
            ).fetchone()
        return dict(row) if row is not None else {}

    def forget_group_member(self, group_id: str, member_id: str, *, hard_delete: bool = True) -> int:
        ref, _ = self._member_identity(group_id, member_id)
        with self.transaction() as db:
            self._advance_memory_epoch(group_id)
            db.execute("DELETE FROM character_items WHERE scope=? AND (owner=? OR kind IN ('discovery','episode'))", (str(group_id), ref))
            db.execute("DELETE FROM character_relations WHERE scope=? AND owner=?", (str(group_id), ref))
            db.execute("DELETE FROM character_commands WHERE scope=?", (str(group_id),))
            db.execute("DELETE FROM character_state WHERE scope=?", (str(group_id),))
            facts = db.execute(
                "DELETE FROM member_memory_facts WHERE group_id=? AND member_ref=?",
                (str(group_id), ref),
            )
            if hard_delete:
                member = db.execute(
                    "DELETE FROM group_members WHERE group_id=? AND member_ref=?",
                    (str(group_id), ref),
                )
            else:
                member = db.execute(
                    "UPDATE group_members SET deleted_at=?, consent_status='opted_out', profile_version=profile_version+1 "
                    "WHERE group_id=? AND member_ref=?",
                    (time.time(), str(group_id), ref),
                )
            history = db.execute(
                "DELETE FROM group_history WHERE group_id=? AND (member_id=? OR role='assistant')",
                (str(group_id), ref),
            )
            # A summary may contain facts derived from the removed rows. Drop
            # it and its jobs so the next refresh rebuilds from retained data.
            memories = db.execute("DELETE FROM group_memories WHERE group_id=?", (str(group_id),))
            jobs = db.execute("DELETE FROM compaction_jobs WHERE group_id=?", (str(group_id),))
            return sum(
                max(0, int(cursor.rowcount))
                for cursor in (facts, member, history, memories, jobs)
            )

    forget_member = forget_group_member
    delete_group_member = forget_group_member

    @staticmethod
    def _normalize_fact_key(value: Any) -> str:
        return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())

    def add_member_memory_fact(
        self,
        group_id: str,
        member_id: str,
        category: str,
        fact_key: str,
        fact_value: str,
        *,
        confidence: float = 0.5,
        explicitness: str = "inferred",
        source_history_id: int | None = None,
        expires_at: float | None = None,
        now: float | None = None,
        evidence: str = "",
        expected_epoch: int | None = None,
        expected_profile_version: int | None = None,
    ) -> dict[str, Any]:
        """Apply a sourced fact without undoing explicit corrections or newer evidence."""
        category_text = self._bounded_text(category, 80)
        key_text = self._bounded_text(fact_key, 120)
        value_text = self._bounded_text(fact_value, 2000)
        if not category_text or not key_text or not value_text:
            raise ValueError("category, fact_key and fact_value are required")
        explicit = str(explicitness or "inferred").strip().lower()
        if explicit not in {"explicit", "inferred"}:
            raise ValueError("invalid explicitness")
        score = float(confidence)
        if not math.isfinite(score):
            raise ValueError("confidence must be finite")
        score = min(1.0, max(0.0, score))
        ref, _ = self._member_identity(group_id, member_id)
        stamp = time.time() if now is None else float(now)
        normalized_key = self._normalize_fact_key(key_text)
        evidence_text = self._bounded_text(evidence, 400)
        with self.transaction() as db:
            if expected_epoch is not None and self.memory_epoch(group_id) != expected_epoch:
                raise ValueError("member memory source was invalidated")
            member = db.execute(
                "SELECT consent_status,profile_version FROM group_members WHERE group_id=? AND member_ref=? "
                "AND deleted_at IS NULL",
                (str(group_id), ref),
            ).fetchone()
            if member is None or str(member["consent_status"]) != "opted_in":
                raise ValueError("member memory requires active opt-in consent")
            if expected_profile_version is not None and int(member["profile_version"]) != expected_profile_version:
                raise ValueError("member memory consent changed")
            source = None
            if source_history_id is not None:
                source = db.execute(
                    "SELECT id,created_at FROM group_history WHERE id=? AND group_id=? AND member_id=? AND role='user'",
                    (int(source_history_id), str(group_id), ref),
                ).fetchone()
                if source is None:
                    raise ValueError("member fact source does not belong to this member")
            rows = db.execute(
                "SELECT * FROM member_memory_facts WHERE group_id=? AND member_ref=? AND status='active' "
                "AND (expires_at IS NULL OR expires_at>?) ORDER BY id DESC",
                (str(group_id), ref, stamp),
            ).fetchall()
            matches = [row for row in rows if self._normalize_fact_key(row["fact_key"]) == normalized_key]
            existing = max(
                matches, key=lambda row: (row["explicitness"] == "explicit", int(row["id"])), default=None
            )
            if existing is not None and explicit == "inferred":
                protected = existing["explicitness"] == "explicit"
                if not protected and existing["source_history_id"] is not None:
                    previous_source = db.execute(
                        "SELECT id,created_at FROM group_history WHERE id=?", (existing["source_history_id"],)
                    ).fetchone()
                    protected = source is None or (
                        previous_source is not None
                        and (float(source["created_at"]), int(source["id"]))
                        < (float(previous_source["created_at"]), int(previous_source["id"]))
                    ) or (
                        previous_source is None and source is not None
                        and int(source["id"]) < int(existing["source_history_id"])
                    )
                if protected:
                    return {**dict(existing), "deduplicated": True, "applied": False}
            same_value = existing is not None and str(existing["fact_value"]) == value_text
            for row in matches:
                if same_value and int(row["id"]) == int(existing["id"]):
                    continue
                db.execute(
                    "UPDATE member_memory_facts SET status='superseded', updated_at=? WHERE id=?",
                    (stamp, int(row["id"])),
                )
            if same_value:
                db.execute(
                    "UPDATE member_memory_facts SET confidence=MAX(confidence,?), "
                    "explicitness=CASE WHEN ?='explicit' THEN 'explicit' ELSE explicitness END, "
                    "source_history_id=COALESCE(?,source_history_id), expires_at=?, updated_at=?, "
                    "evidence=CASE WHEN ?<>'' THEN ? ELSE evidence END WHERE id=?",
                    (score, explicit, source_history_id, expires_at, stamp, evidence_text, evidence_text, int(existing["id"])),
                )
                row = db.execute("SELECT * FROM member_memory_facts WHERE id=?", (int(existing["id"]),)).fetchone()
                return {**dict(row), "deduplicated": True, "applied": True}
            supersedes_id = int(existing["id"]) if existing is not None else None
            cur = db.execute(
                "INSERT INTO member_memory_facts "
                "(group_id,member_ref,category,fact_key,fact_value,confidence,explicitness,source_history_id,expires_at,status,supersedes_id,created_at,updated_at,evidence) "
                "VALUES(?,?,?,?,?,?,?,?,?,'active',?,?,?,?)",
                (
                    str(group_id), ref, category_text, key_text, value_text, score, explicit,
                    None if source_history_id is None else int(source_history_id),
                    None if expires_at is None else float(expires_at), supersedes_id, stamp, stamp, evidence_text,
                ),
            )
            row = db.execute("SELECT * FROM member_memory_facts WHERE id=?", (int(cur.lastrowid),)).fetchone()
        return {**dict(row), "deduplicated": False, "applied": True}

    def list_member_memory_facts(
        self,
        group_id: str,
        member_id: str | None = None,
        *,
        member_ref: str | None = None,
        include_expired: bool = False,
        include_inactive: bool = False,
        now: float | None = None,
        query: str = "",
    ) -> list[dict[str, Any]]:
        ref = str(member_ref or "")
        if not ref:
            if not member_id:
                raise ValueError("member_id or member_ref is required")
            ref, _ = self._member_identity(group_id, member_id)
        conditions = ["group_id=?", "member_ref=?"]
        params: list[Any] = [str(group_id), ref]
        if not include_inactive:
            conditions.append("status='active'")
        if not include_expired:
            conditions.append("(expires_at IS NULL OR expires_at>?)")
            params.append(time.time() if now is None else float(now))
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM member_memory_facts WHERE " + " AND ".join(conditions) + " ORDER BY id ASC",
                tuple(params),
            ).fetchall()
        if include_inactive:
            return [dict(row) for row in rows]
        # Older databases may contain the same field under multiple categories.
        selected: dict[str, sqlite3.Row] = {}
        for row in rows:
            key = self._normalize_fact_key(row["fact_key"])
            previous = selected.get(key)
            rank = (row["explicitness"] == "explicit", int(row["id"]))
            if previous is None or rank > (previous["explicitness"] == "explicit", int(previous["id"])):
                selected[key] = row
        results = [dict(row) for row in selected.values()]
        if query:
            terms = self._search_terms(query)
            results.sort(
                key=lambda row: (
                    len(terms & self._search_terms(f"{row['fact_key']} {row['fact_value']}")),
                    row["explicitness"] == "explicit",
                    float(row["updated_at"]),
                    int(row["id"]),
                ),
                reverse=True,
            )
        else:
            results.sort(key=lambda row: int(row["id"]))
        return results

    get_member_memory_facts = list_member_memory_facts

    def expire_member_memory_facts(
        self,
        group_id: str | None = None,
        *,
        member_id: str | None = None,
        now: float | None = None,
    ) -> int:
        stamp = time.time() if now is None else float(now)
        conditions = ["status='active'", "expires_at IS NOT NULL", "expires_at<=?"]
        params: list[Any] = [stamp]
        if group_id is not None:
            conditions.append("group_id=?")
            params.append(str(group_id))
        if member_id is not None:
            if group_id is None:
                raise ValueError("group_id is required with member_id")
            ref, _ = self._member_identity(group_id, member_id)
            conditions.append("member_ref=?")
            params.append(ref)
        with self.transaction() as db:
            cursor = db.execute(
                "UPDATE member_memory_facts SET status='expired', updated_at=? WHERE "
                + " AND ".join(conditions),
                (stamp, *params),
            )
            return max(0, int(cursor.rowcount))

    @staticmethod
    def _split_chunks(text: str, size: int, overlap: int) -> list[str]:
        value = str(text or "").strip()
        size = max(200, int(size))
        overlap = max(0, min(int(overlap), size // 3))
        if not value:
            return []
        chunks: list[str] = []
        start = 0
        while start < len(value):
            end = min(len(value), start + size)
            if end < len(value):
                boundary = max(value.rfind("\n", start + size // 2, end), value.rfind("。", start + size // 2, end))
                if boundary > start:
                    end = boundary + 1
            piece = value[start:end].strip()
            if piece:
                chunks.append(piece)
            if end >= len(value):
                break
            start = max(start + 1, end - overlap)
        return chunks

    def add_knowledge_document(
        self,
        group_id: str,
        title: str,
        text: str,
        source: str = "text",
        created_by: str | None = None,
        message_id: str | None = None,
        chunk_size: int = 900,
        overlap: int = 120,
    ) -> dict[str, Any]:
        content = str(text or "").strip()
        if not content:
            raise ValueError("knowledge text is empty")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        now = time.time()
        chunks = self._split_chunks(content, chunk_size, overlap)
        with self.transaction() as db:
            existing = db.execute(
                "SELECT id,title FROM knowledge_documents WHERE group_id=? AND content_hash=?",
                (str(group_id), digest),
            ).fetchone()
            if existing is not None:
                numeric_id = int(existing["id"])
                count = db.execute(
                    "SELECT COUNT(*) AS n FROM knowledge_chunks WHERE document_id=?", (numeric_id,)
                ).fetchone()
                return {
                    "id": "doc-" + str(numeric_id), "doc_id": "doc-" + str(numeric_id),
                    "title": str(existing["title"]), "chunk_count": int(count["n"]),
                    "deduplicated": True,
                }
            cur = db.execute(
                "INSERT INTO knowledge_documents"
                "(group_id,title,source,content_hash,source_message_id,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (str(group_id), str(title).strip()[:200] or "未命名资料", str(source)[:40], digest,
                 None if message_id is None else str(message_id),
                 None if created_by is None else str(created_by), now, now),
            )
            doc_id = int(cur.lastrowid)
            db.executemany(
                "INSERT INTO knowledge_chunks(document_id,group_id,chunk_index,text,created_at) VALUES(?,?,?,?,?)",
                [(doc_id, str(group_id), index, chunk, now) for index, chunk in enumerate(chunks)],
            )
        public_id = "doc-" + str(doc_id)
        return {
            "id": public_id, "doc_id": public_id,
            "title": str(title).strip()[:200] or "未命名资料",
            "chunk_count": len(chunks), "deduplicated": False,
        }

    def list_knowledge_documents(self, group_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT d.id,d.title,d.source,d.created_at,COUNT(c.id) AS chunks "
                "FROM knowledge_documents d LEFT JOIN knowledge_chunks c ON c.document_id=d.id "
                "WHERE d.group_id=? GROUP BY d.id ORDER BY d.id DESC",
                (str(group_id),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["doc_id"] = "doc-" + str(item["id"])
            item["chunk_count"] = int(item.pop("chunks", 0))
            result.append(item)
        return result

    def remove_knowledge_document(self, group_id: str, doc_id: int) -> bool:
        raw_id = str(doc_id)
        numeric_id = int(raw_id[4:] if raw_id.startswith("doc-") else raw_id)
        with self.transaction() as db:
            cur = db.execute(
                "DELETE FROM knowledge_documents WHERE group_id=? AND id=?", (str(group_id), numeric_id)
            )
            return int(cur.rowcount) > 0

    def clear_knowledge(self, group_id: str) -> int:
        with self.transaction() as db:
            cur = db.execute("DELETE FROM knowledge_documents WHERE group_id=?", (str(group_id),))
            return max(0, int(cur.rowcount))

    @staticmethod
    def _search_terms(value: str) -> set[str]:
        normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
        terms = set(re.findall(r"[a-z0-9_]{2,}|[\u3400-\u9fff]", normalized))
        cjk = "".join(re.findall(r"[\u3400-\u9fff]", normalized))
        terms.update(cjk[index:index + 2] for index in range(max(0, len(cjk) - 1)))
        return {term for term in terms if term}

    def search_knowledge(self, group_id: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
        needle = unicodedata.normalize("NFKC", str(query or "")).casefold().strip()
        if not needle:
            return []
        qterms = self._search_terms(needle)
        with self._lock:
            rows = self.db.execute(
                "SELECT c.id,c.document_id,c.chunk_index,c.text,d.title,d.source "
                "FROM knowledge_chunks c JOIN knowledge_documents d ON d.id=c.document_id "
                "WHERE c.group_id=?",
                (str(group_id),),
            )

            def candidates():
                for row in rows:
                    haystack = unicodedata.normalize("NFKC", f"{row['title']} {row['text']}").casefold()
                    overlap = len(qterms & self._search_terms(haystack))
                    score = overlap + (12 if needle in haystack else 0) + (4 if needle in str(row["title"]).casefold() else 0)
                    if score > 0:
                        yield score, int(row["id"]), row

            # Search every document, retaining only top-k rows in memory.
            matches = heapq.nlargest(
                max(1, int(limit)), candidates(), key=lambda item: (item[0], item[1])
            )
        results = []
        for score, _, row in matches:
            item = dict(row)
            item["doc_id"] = "doc-" + str(item.pop("document_id"))
            item["chunk_id"] = item["doc_id"] + "-chunk-" + str(int(item.pop("chunk_index")) + 1)
            item["score"] = float(score)
            results.append(item)
        return results

    def integrity_check(self) -> bool:
        with self._lock:
            row = self.db.execute("PRAGMA integrity_check").fetchone()
        return bool(row and str(row[0]).lower() == "ok")


__all__ = ["Store", "PENDING_STALE_SECONDS"]
