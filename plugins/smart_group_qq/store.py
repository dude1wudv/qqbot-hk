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
import secrets
import threading
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .expressions import STICKER_CATEGORIES


PENDING_STALE_SECONDS = 300.0
MAX_COMPACTION_FAILURES = 3
SCHEMA_VERSION = 7
_HISTORY_WITHOUT_PENDING_EXTRACTION = (
    " AND NOT EXISTS (SELECT 1 FROM member_extraction_jobs j WHERE j.source_history_id=group_history.id "
    "AND j.status IN ('pending','running','failed') AND j.attempt<3)"
)
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{12,}\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+"),
)


class Store:
    """Small SQLite repository backed by Hermes ``plugin_storage.plugin_db()``."""

    def __init__(self, database: Any = None, *, member_secret: str | bytes | None = None):
        if not isinstance(member_secret, (str, bytes)) or not member_secret.strip():
            raise ValueError("QQ_CLIENT_SECRET is required for member pseudonyms")
        self._member_secret = (
            member_secret.encode("utf-8") if isinstance(member_secret, str) else member_secret
        )
        self._owned = False
        if database is None:
            database = ":memory:"
        if isinstance(database, (str, Path)):
            self.db = sqlite3.connect(str(database), check_same_thread=False)
            self._owned = True
        else:
            self.db = database
        self._lock = threading.RLock()
        self._transaction_depth = 0
        self._memory_backlog_cursor = ""
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
        return cls(":memory:", member_secret=os.environ.get("QQ_CLIENT_SECRET"))

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
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS group_polls (
                poll_id TEXT PRIMARY KEY,
                group_id TEXT NOT NULL,
                creator_ref TEXT NOT NULL,
                question TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('open','closed','expired')),
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_group_polls_status_expiry
                ON group_polls(group_id, status, expires_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_group_polls_one_open
                ON group_polls(group_id) WHERE status='open';
            CREATE TABLE IF NOT EXISTS group_poll_options (
                poll_id TEXT NOT NULL REFERENCES group_polls(poll_id) ON DELETE CASCADE,
                option_no INTEGER NOT NULL CHECK(option_no BETWEEN 1 AND 5),
                text TEXT NOT NULL,
                PRIMARY KEY(poll_id, option_no)
            );
            CREATE TABLE IF NOT EXISTS group_poll_votes (
                poll_id TEXT NOT NULL,
                member_ref TEXT NOT NULL,
                option_no INTEGER NOT NULL,
                PRIMARY KEY(poll_id, member_ref),
                FOREIGN KEY(poll_id, option_no)
                    REFERENCES group_poll_options(poll_id, option_no) ON DELETE CASCADE
            );
            """
        )
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS group_stickers (
                group_id TEXT NOT NULL, digest TEXT NOT NULL,
                category TEXT NOT NULL, caption TEXT NOT NULL,
                mime_type TEXT NOT NULL, image_data BLOB NOT NULL,
                created REAL NOT NULL, expires REAL NOT NULL,
                last_used REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(group_id,digest)
            );
            CREATE INDEX IF NOT EXISTS idx_group_stickers_category
                ON group_stickers(group_id,category,expires,last_used);
            CREATE TABLE IF NOT EXISTS group_sticker_sources (
                group_id TEXT NOT NULL, digest TEXT NOT NULL, member_ref TEXT NOT NULL,
                PRIMARY KEY(group_id,digest,member_ref),
                FOREIGN KEY(group_id,digest) REFERENCES group_stickers(group_id,digest) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS group_sticker_delivery (
                group_id TEXT PRIMARY KEY, last_attempt REAL NOT NULL,
                blocked INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        if stored_schema_version < 6:
            # Preserve the platform's refusal across retiring the old character sender.
            self.db.execute(
                "INSERT INTO group_sticker_delivery(group_id,last_attempt,blocked) "
                "SELECT scope,0,1 FROM character_state WHERE json_valid(payload) AND scope!='' "
                "AND scope NOT LIKE 'dm:%' AND json_extract(payload,'$.platform_blocked')=1 "
                "ON CONFLICT(group_id) DO UPDATE SET blocked=1"
            )
            # Retire only the removed feature's generated data, not member goals or history.
            self.db.execute("DELETE FROM character_items WHERE kind='discovery'")
            self.db.execute(
                "DELETE FROM character_items WHERE kind='goal' AND owner='' AND text=?",
                ("发现值得一起玩的开源 AI 项目，关注 AIRI、Mindcraft 和 SillyTavern 的新发布",),
            )
            self.db.execute(
                "UPDATE character_state SET payload=json_remove(payload,"
                "'$.next_tick','$.failures','$.proactive','$.platform_blocked') WHERE json_valid(payload)"
            )
        self._ensure_column("character_commands", "epoch", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("character_commands", "delivery_status", "TEXT NOT NULL DEFAULT 'pending'")
        self._ensure_column("character_commands", "sent_chunks_json", "TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column("character_commands", "updated_at", "REAL NOT NULL DEFAULT 0")
        self._ensure_column("character_commands", "result_prepared", "INTEGER NOT NULL DEFAULT 0")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS member_extraction_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL, member_ref TEXT NOT NULL,
                source_history_id INTEGER NOT NULL, source_digest TEXT NOT NULL,
                source_kind TEXT NOT NULL, epoch INTEGER NOT NULL,
                profile_version INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempt INTEGER NOT NULL DEFAULT 0,
                next_retry_at REAL NOT NULL DEFAULT 0, lease_until REAL,
                error TEXT NOT NULL DEFAULT '', stored INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(group_id,source_history_id,source_digest)
            );
            CREATE INDEX IF NOT EXISTS idx_member_extraction_ready
                ON member_extraction_jobs(status,next_retry_at,id);
            CREATE TABLE IF NOT EXISTS qq_context_checkpoints (
                session_key TEXT PRIMARY KEY, scope TEXT NOT NULL, epoch INTEGER NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1, payload_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_qq_context_scope ON qq_context_checkpoints(scope);
            """
        )
        if stored_schema_version < 7:
            # A pre-upgrade send may already have reached QQ. Never replay it
            # automatically merely because its old receipt lacked delivery state.
            self.db.execute("UPDATE character_commands SET delivery_status='unknown', updated_at=created")
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
            depth = self._transaction_depth
            savepoint = f"qq_store_{depth}"
            try:
                if depth:
                    self.db.execute(f"SAVEPOINT {savepoint}")
                elif not self.db.in_transaction:
                    self.db.execute("BEGIN IMMEDIATE")
                self._transaction_depth += 1
                yield self.db
                if depth:
                    self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    self.db.commit()
            except Exception:
                if depth:
                    self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    self.db.rollback()
                raise
            finally:
                self._transaction_depth = depth

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
        self.db.execute("DELETE FROM member_extraction_jobs WHERE group_id=?", (str(group_id),))
        self.db.execute("DELETE FROM compaction_jobs WHERE group_id=?", (str(group_id),))
        self.db.execute("DELETE FROM qq_context_checkpoints WHERE scope=?", (str(group_id),))
        self.db.execute(
            "UPDATE character_commands SET delivery_status='cancelled',updated_at=? WHERE scope=?",
            (time.time(), str(group_id)),
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

    @staticmethod
    def _receipt_payload(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["prepared"] = bool(result.pop("result_prepared", 0))
        try:
            chunks = json.loads(result.pop("sent_chunks_json", "[]"))
        except (TypeError, ValueError):
            chunks = []
        result["sent_chunks"] = sorted({int(index) for index in chunks if isinstance(index, int) and index >= 0})
        return result

    def get_command_receipt(self, scope: str, message_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM character_commands WHERE scope=? AND message_id=?",
                (str(scope), str(message_id)),
            ).fetchone()
            return self._receipt_payload(row)

    def save_command_receipt(
        self, scope: str, message_id: str, result: str, *, epoch: int | None = None, prepared: bool = False,
    ) -> dict[str, Any] | None:
        if not scope or not message_id:
            raise ValueError("scope and message_id are required")
        stamp = time.time()
        with self.transaction() as db:
            current_epoch = self.memory_epoch(scope)
            if epoch is not None and int(epoch) != current_epoch:
                return None
            db.execute(
                "INSERT INTO character_commands(scope,message_id,result,created,epoch,delivery_status,updated_at,result_prepared) "
                "VALUES(?,?,?,?,?,'pending',?,?) ON CONFLICT(scope,message_id) DO UPDATE SET "
                "result=excluded.result,created=excluded.created,epoch=excluded.epoch,delivery_status='pending',"
                "sent_chunks_json='[]',updated_at=excluded.updated_at,result_prepared=excluded.result_prepared "
                "WHERE character_commands.delivery_status='cancelled' AND character_commands.epoch<>excluded.epoch",
                (str(scope), str(message_id), str(result), stamp, current_epoch, stamp, int(bool(prepared))),
            )
            return self.get_command_receipt(scope, message_id)

    def replace_pending_command_result(
        self, scope: str, message_id: str, result: str, *, expected_epoch: int, prepared: bool = False,
    ) -> bool:
        with self.transaction() as db:
            row = self.get_command_receipt(scope, message_id)
            if (row is None or row["epoch"] != int(expected_epoch)
                    or self.memory_epoch(scope) != int(expected_epoch)
                    or row["delivery_status"] != "pending" or row["sent_chunks"]):
                return False
            db.execute("UPDATE character_commands SET result=?,updated_at=?,result_prepared=? WHERE scope=? AND message_id=?",
                       (str(result), time.time(), int(bool(prepared)), str(scope), str(message_id)))
            return True

    def update_command_receipt(
        self, scope: str, message_id: str, *, delivery_status: str,
        sent_chunks: list[int] | None = None, expected_epoch: int | None = None,
    ) -> bool:
        if delivery_status not in {"pending", "sending", "sent", "failed", "unknown", "cancelled"}:
            raise ValueError("invalid command delivery status")
        with self.transaction() as db:
            row = self.get_command_receipt(scope, message_id)
            if row is None or row["epoch"] != self.memory_epoch(scope):
                return False
            if expected_epoch is not None and row["epoch"] != int(expected_epoch):
                return False
            if row["delivery_status"] in {"sent", "cancelled"} and delivery_status != row["delivery_status"]:
                return False
            chunks = row["sent_chunks"] if sent_chunks is None else sorted(set(row["sent_chunks"]) | {
                int(index) for index in sent_chunks if isinstance(index, int) and index >= 0
            })
            db.execute(
                "UPDATE character_commands SET delivery_status=?,sent_chunks_json=?,updated_at=? "
                "WHERE scope=? AND message_id=?",
                (delivery_status, json.dumps(chunks), time.time(), str(scope), str(message_id)),
            )
            return True

    @staticmethod
    def _context_payload(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        return {**payload, **{key: row[key] for key in ("session_key", "scope", "epoch", "revision", "updated_at")}}

    def get_context_checkpoint(self, session_key: str) -> dict[str, Any] | None:
        with self._lock:
            return self._context_payload(self.db.execute(
                "SELECT * FROM qq_context_checkpoints WHERE session_key=?", (str(session_key),)
            ).fetchone())

    def create_context_checkpoint(
        self, session_key: str, payload: dict[str, Any], *, scope: str, epoch: int, now: float | None = None,
    ) -> dict[str, Any] | None:
        if not session_key or not scope:
            raise ValueError("session_key and scope are required")
        with self.transaction() as db:
            if self.memory_epoch(scope) != int(epoch):
                return None
            db.execute(
                "INSERT INTO qq_context_checkpoints(session_key,scope,epoch,payload_json,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(session_key) DO NOTHING",
                (str(session_key), str(scope), int(epoch), json.dumps(payload, ensure_ascii=False),
                 time.time() if now is None else float(now)),
            )
            return self.get_context_checkpoint(session_key)

    def save_context_checkpoint(
        self, session_key: str, payload: dict[str, Any], *, expected_revision: int,
        expected_epoch: int, now: float | None = None,
    ) -> dict[str, Any] | None:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM qq_context_checkpoints WHERE session_key=?", (str(session_key),)).fetchone()
            if (row is None or int(row["revision"]) != int(expected_revision)
                    or int(row["epoch"]) != int(expected_epoch)
                    or self.memory_epoch(row["scope"]) != int(expected_epoch)):
                return None
            current_payload = json.loads(row["payload_json"])
            if payload.get("checkpoint_id") != current_payload.get("checkpoint_id"):
                return None
            db.execute(
                "UPDATE qq_context_checkpoints SET payload_json=?,revision=revision+1,updated_at=? WHERE session_key=?",
                (json.dumps(payload, ensure_ascii=False), time.time() if now is None else float(now), str(session_key)),
            )
            return self.get_context_checkpoint(session_key)

    def delete_context_checkpoint(
        self, session_key: str, *, expected_revision: int | None = None,
        expected_checkpoint_id: str | None = None,
    ) -> bool:
        with self.transaction() as db:
            if expected_checkpoint_id is not None:
                row = self.get_context_checkpoint(session_key)
                if row is None or row.get("checkpoint_id") != expected_checkpoint_id:
                    return False
            sql = "DELETE FROM qq_context_checkpoints WHERE session_key=?"
            params: tuple[Any, ...] = (str(session_key),)
            if expected_revision is not None:
                sql += " AND revision=?"
                params += (int(expected_revision),)
            return db.execute(sql, params).rowcount > 0

    @staticmethod
    def history_digest(text: Any) -> str:
        return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()

    def enqueue_member_extraction(
        self, group_id: str, member_id: str, source_history_id: int, *,
        source_kind: str = "addressed", now: float | None = None,
    ) -> int:
        ref = self.member_ref_for(group_id, member_id)
        stamp = time.time() if now is None else float(now)
        with self.transaction() as db:
            member = self.get_group_member(group_id, member_ref=ref)
            source = db.execute(
                "SELECT text FROM group_history WHERE id=? AND group_id=? AND member_id=? AND role='user'",
                (int(source_history_id), str(group_id), ref),
            ).fetchone()
            if member is None or member["consent_status"] != "opted_in" or source is None:
                return 0
            digest = self.history_digest(source["text"])
            db.execute(
                "INSERT INTO member_extraction_jobs(group_id,member_ref,source_history_id,source_digest,"
                "source_kind,epoch,profile_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(group_id,source_history_id,source_digest) DO NOTHING",
                (str(group_id), ref, int(source_history_id), digest, str(source_kind),
                 self.memory_epoch(group_id), int(member["profile_version"]), stamp, stamp),
            )
            return int(db.execute(
                "SELECT id FROM member_extraction_jobs WHERE group_id=? AND source_history_id=? AND source_digest=?",
                (str(group_id), int(source_history_id), digest),
            ).fetchone()["id"])

    def list_member_extraction_jobs(
        self, *, ready_only: bool = False, limit: int = 100, now: float | None = None,
    ) -> list[dict[str, Any]]:
        stamp = time.time() if now is None else float(now)
        where, params = "", []
        if ready_only:
            where = (" WHERE attempt<? AND ((status IN ('pending','failed') AND next_retry_at<=?) "
                     "OR (status='running' AND lease_until<=?))")
            params = [MAX_COMPACTION_FAILURES, stamp, stamp]
        with self._lock:
            return [dict(row) for row in self.db.execute(
                "SELECT * FROM member_extraction_jobs" + where + " ORDER BY updated_at,id LIMIT ?",
                (*params, max(1, int(limit))),
            ).fetchall()]

    def get_member_extraction(self, job_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM member_extraction_jobs WHERE id=?", (int(job_id),)).fetchone()
            return dict(row) if row else None

    def member_extraction_source(self, job: Any) -> dict[str, Any] | None:
        """Re-read an authenticated source without persisting another raw-text copy."""
        with self._lock:
            member = self.get_group_member(job["group_id"], member_ref=job["member_ref"])
            if (self.memory_epoch(job["group_id"]) != int(job["epoch"])
                    or member is None or member["consent_status"] != "opted_in"
                    or int(member["profile_version"]) != int(job["profile_version"])):
                return None
            source = self.db.execute(
                "SELECT * FROM group_history WHERE id=? AND group_id=? AND member_id=? AND role='user'",
                (int(job["source_history_id"]), job["group_id"], job["member_ref"]),
            ).fetchone()
            if source is None or self.history_digest(source["text"]) != job["source_digest"]:
                return None
            return dict(source)

    def claim_member_extraction(
        self, job_id: int | None = None, *, now: float | None = None, lease_seconds: float = 330,
    ) -> dict[str, Any] | None:
        stamp = time.time() if now is None else float(now)
        with self.transaction() as db:
            candidates = self.list_member_extraction_jobs(ready_only=True, now=stamp)
            if job_id is not None:
                row = db.execute("SELECT * FROM member_extraction_jobs WHERE id=?", (int(job_id),)).fetchone()
                candidates = [dict(row)] if row else []
            for job in candidates:
                ready = (job["status"] in {"pending", "failed"} and float(job["next_retry_at"]) <= stamp)
                expired = job["status"] == "running" and float(job["lease_until"] or 0) <= stamp
                if int(job["attempt"]) >= MAX_COMPACTION_FAILURES or not (ready or expired):
                    continue
                if self.member_extraction_source(job) is None:
                    db.execute(
                        "UPDATE member_extraction_jobs SET status='cancelled',lease_until=NULL,updated_at=? WHERE id=?",
                        (stamp, job["id"]),
                    )
                    continue
                db.execute(
                    "UPDATE member_extraction_jobs SET status='running',attempt=attempt+1,lease_until=?,updated_at=? WHERE id=?",
                    (stamp + max(1, float(lease_seconds)), stamp, job["id"]),
                )
                return dict(db.execute("SELECT * FROM member_extraction_jobs WHERE id=?", (job["id"],)).fetchone())
            return None

    def finish_member_extraction(
        self, job_id: int, *, status: str = "success", stored: int = 0,
        error: str = "", now: float | None = None, expected_attempt: int | None = None,
    ) -> bool:
        if status not in {"success", "empty", "retryable_failure"}:
            raise ValueError("invalid member extraction outcome")
        stamp = time.time() if now is None else float(now)
        with self.transaction() as db:
            row = db.execute("SELECT * FROM member_extraction_jobs WHERE id=?", (int(job_id),)).fetchone()
            if row is None or row["status"] != "running":
                return False
            if expected_attempt is not None and int(row["attempt"]) != int(expected_attempt):
                return False
            if self.member_extraction_source(row) is None:
                db.execute("DELETE FROM member_extraction_jobs WHERE id=?", (int(job_id),))
                return False
            retry = status == "retryable_failure"
            delay = 60 if int(row["attempt"]) == 1 else 300
            # Only an exception type/code belongs in durable operational metadata.
            safe_error = str(error) if retry and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,79}", str(error)) else ""
            if retry and not safe_error:
                safe_error = "ExtractionError"
            db.execute(
                "UPDATE member_extraction_jobs SET status=?,stored=?,error=?,next_retry_at=?,lease_until=NULL,updated_at=? WHERE id=?",
                ("failed" if retry else "completed", max(0, int(stored)), safe_error,
                 stamp + delay if retry else 0, stamp, int(job_id)),
            )
            return True

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
            # Reset the retry budget atomically with durable progress. A later
            # batch must not inherit failures from a cursor already summarized.
            db.execute(
                "UPDATE compaction_jobs SET from_history_id=?, attempt=0, error='', next_retry_at=0, "
                "status=CASE WHEN status='paused' THEN 'pending' ELSE status END "
                "WHERE group_id=? AND status IN ('pending','running','failed','paused') AND from_history_id<?",
                (max(0, int(last_history_id)), str(group_id), max(0, int(last_history_id))),
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
                "WHERE group_id=? AND status IN ('pending','running','failed','paused') "
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
                    "((status IN ('pending','failed') AND (next_retry_at<=? OR ?) "
                    "AND (attempt<? OR ?)) OR "
                    "(status='paused' AND ?) OR "
                    "(status='running' AND lease_until IS NOT NULL AND lease_until<=? AND (attempt<? OR ?)))"
                    + group_clause + " ORDER BY id ASC LIMIT 1",
                    (stamp, bool(force), MAX_COMPACTION_FAILURES, bool(force), bool(force), stamp,
                     MAX_COMPACTION_FAILURES, bool(force), *params[1:]),
                ).fetchone()
            if row is None:
                return None
            status = str(row["status"])
            ready = status in {"pending", "failed"} and (
                force or (int(row["attempt"] or 0) < MAX_COMPACTION_FAILURES
                          and float(row["next_retry_at"] or 0) <= stamp)
            )
            ready = ready or (status == "paused" and force)
            expired = (status == "running" and row["lease_until"] is not None and float(row["lease_until"]) <= stamp
                       and (force or int(row["attempt"] or 0) < MAX_COMPACTION_FAILURES))
            if not (ready or expired):
                return None
            lease_until = stamp + max(1.0, float(lease_seconds))
            db.execute(
                "UPDATE compaction_jobs SET status='running', attempt=attempt+1, "
                "lease_until=?, updated_at=? WHERE id=?",
                (lease_until, stamp, int(row["id"])),
            )
            return db.execute("SELECT * FROM compaction_jobs WHERE id=?", (int(row["id"]),)).fetchone()

    def pause_compaction(self, group_id: str, *, now: float | None = None) -> bool:
        """Pause a failed explicit retry without fabricating its attempt count."""
        stamp = time.time() if now is None else float(now)
        with self.transaction() as db:
            row = self.get_compaction_job(group_id)
            if row is None or row["status"] not in {"pending", "failed", "paused"}:
                return False
            db.execute(
                "UPDATE compaction_jobs SET status='paused',lease_until=NULL,next_retry_at=0,updated_at=? WHERE id=?",
                (stamp, int(row["id"])),
            )
            return True

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
                "UPDATE compaction_jobs SET status='failed', lease_until=NULL, error=?, attempt=?, "
                "next_retry_at=?, updated_at=? WHERE id=?",
                (str(error or "compaction failed")[:500], attempt, stamp + delay, stamp, int(job_id)),
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
                "(SELECT id FROM group_history WHERE group_id=? ORDER BY id DESC LIMIT ?)" + _HISTORY_WITHOUT_PENDING_EXTRACTION,
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
                    "(SELECT last_history_id FROM group_memories WHERE group_id=group_history.group_id),0)" + _HISTORY_WITHOUT_PENDING_EXTRACTION,
                    (cutoff,),
                )
            else:
                cursor = db.execute(
                    "DELETE FROM group_history WHERE group_id=? AND created_at<? "
                    "AND id<=COALESCE((SELECT last_history_id FROM group_memories WHERE group_id=?),0)" + _HISTORY_WITHOUT_PENDING_EXTRACTION,
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
                    "AND id<=COALESCE((SELECT last_history_id FROM group_memories WHERE group_id=group_history.group_id),0)" + _HISTORY_WITHOUT_PENDING_EXTRACTION,
                    (source, cutoff),
                )
            else:
                cursor = db.execute(
                    "DELETE FROM group_history WHERE group_id=? AND source_kind=? AND created_at<? "
                    "AND id<=COALESCE((SELECT last_history_id FROM group_memories WHERE group_id=?),0)" + _HISTORY_WITHOUT_PENDING_EXTRACTION,
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
            db.execute(
                "DELETE FROM member_extraction_jobs WHERE updated_at<? "
                "AND (status IN ('completed','cancelled') OR attempt>=?) "
                "AND NOT EXISTS(SELECT 1 FROM group_history WHERE id=member_extraction_jobs.source_history_id)",
                (stamp - max(0.0, float(claim_age_seconds)), MAX_COMPACTION_FAILURES),
            )
        return {
            "audit_events": max(0, int(audits.rowcount)),
            "message_claims": max(0, int(claims.rowcount)),
        }

    def list_memory_backlog_groups(self, limit: int = 100) -> list[dict[str, Any]]:
        """Round-robin backlog scans cannot be pinned by paused/quiet groups."""

        with self._lock:
            query = (
                "SELECT h.group_id, MAX(h.id) AS latest_history_id, "
                "COALESCE(m.last_history_id,0) AS memory_cursor, "
                "SUM(CASE WHEN h.id>COALESCE(m.last_history_id,0) THEN 1 ELSE 0 END) AS pending_count, "
                "MIN(CASE WHEN h.id>COALESCE(m.last_history_id,0) THEN h.created_at END) AS oldest_pending_at "
                "FROM group_history h LEFT JOIN group_memories m ON m.group_id=h.group_id "
                "WHERE h.group_id>? "
                "GROUP BY h.group_id "
                "HAVING MAX(h.id)>COALESCE(m.last_history_id,0) "
                "ORDER BY h.group_id ASC LIMIT ?"
            )
            rows = self.db.execute(query, (self._memory_backlog_cursor, max(1, int(limit)))).fetchall()
            if not rows and self._memory_backlog_cursor:
                rows = self.db.execute(query, ("", max(1, int(limit)))).fetchall()
            self._memory_backlog_cursor = str(rows[-1]["group_id"]) if rows else ""
        return [dict(row) for row in rows]

    @staticmethod
    def member_digest(group_id: Any, member_id: Any, secret: str | bytes) -> str:
        """Return a stable, non-reversible identifier for a group member."""

        if not isinstance(secret, (str, bytes)) or not secret.strip():
            raise ValueError("member secret is required")
        key = secret.encode("utf-8") if isinstance(secret, str) else secret
        payload = (str(group_id) + "\x00" + str(member_id)).encode("utf-8")
        return hmac.new(key, payload, hashlib.sha256).hexdigest()

    @classmethod
    def member_ref(
        cls,
        group_id: Any,
        member_id: Any,
        secret: str | bytes,
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

    def migrate_private_member(
        self, chat_id: str, member_id: str, *, evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """Move an exclusively proven legacy DM; never guess from identifier syntax.

        ``evidence`` is assembled by the authenticated gateway from durable
        Hermes route metadata, never from user text or an attachment.
        """
        legacy, scope = str(chat_id), "dm:" + str(chat_id)
        result = {"scope": scope, "status": "ambiguous", "migrated_facts": 0}
        if not legacy or not member_id or not isinstance(evidence, dict) or evidence.get("authenticated_private") is not True:
            return result
        old_ref, _ = self._member_identity(legacy, member_id)
        new_ref, new_digest = self._member_identity(scope, member_id)
        with self.transaction() as db:
            target = self.get_group_member(scope, member_ref=new_ref)
            try:
                metadata = json.loads(target["metadata_json"] or "{}") if target else {}
            except (TypeError, ValueError):
                metadata = {}
            marker = metadata.get("legacy_dm_migration") or {}
            if marker.get("scope") == legacy and marker.get("member_ref") == old_ref:
                return {**result, "status": "already_migrated"}
            source = self.get_group_member(legacy, member_ref=old_ref)
            if source is None:
                return {**result, "status": "absent"}
            routes = [item for item in evidence.get("sessions", []) if isinstance(item, dict)
                      and str(item.get("source", "")) == "qqbot" and str(item.get("chat_id", "")) == legacy]
            private = any(str(item.get("chat_type", "")) in {"dm", "private"}
                          and str(item.get("user_id", "")) == str(member_id) for item in routes)
            conflicting_route = any(str(item.get("chat_type", "")) not in {"dm", "private"} for item in routes)
            others = db.execute("SELECT 1 FROM group_members WHERE group_id=? AND member_ref<>? LIMIT 1",
                                (legacy, old_ref)).fetchone()
            group_history = db.execute(
                "SELECT 1 FROM group_history WHERE group_id=? AND (source_kind='ambient' "
                "OR (role='user' AND COALESCE(member_id,'') NOT IN ('',?))) LIMIT 1", (legacy, old_ref)
            ).fetchone()
            poll = db.execute("SELECT 1 FROM group_polls WHERE group_id=? LIMIT 1", (legacy,)).fetchone()
            group_audit = db.execute(
                "SELECT 1 FROM audit_events WHERE chat_id=? AND (event_type IN "
                "('ambient_ingest','ambient_blocked','group_access_pending') OR source IN ('official','nonmention','fast_path')) LIMIT 1",
                (legacy,),
            ).fetchone()
            if not private or conflicting_route or others or group_history or poll or group_audit:
                if not db.execute("SELECT 1 FROM audit_events WHERE event_type='legacy_scope_ambiguous' AND chat_id=? LIMIT 1", (scope,)).fetchone():
                    self.record_audit("legacy_scope_ambiguous", chat_id=scope, source="scope_migration")
                return result

            stamp = time.time()
            # Scope changes invalidate every in-flight derived write on both sides.
            self._advance_memory_epoch(legacy)
            self._advance_memory_epoch(scope)
            metadata["legacy_dm_migration"] = {"version": 1, "scope": legacy, "member_ref": old_ref}
            consent = target["consent_status"] if target and target["consent_status"] != "unknown" else source["consent_status"]
            db.execute(
                "INSERT INTO group_members(group_id,member_ref,member_digest,display_name,consent_status,"
                "first_seen_at,last_seen_at,message_count,metadata_json,profile_version,deleted_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,NULL) ON CONFLICT(group_id,member_ref) DO UPDATE SET "
                "display_name=excluded.display_name,consent_status=excluded.consent_status,"
                "first_seen_at=excluded.first_seen_at,last_seen_at=excluded.last_seen_at,"
                "message_count=excluded.message_count,metadata_json=excluded.metadata_json,"
                "profile_version=excluded.profile_version,deleted_at=NULL",
                (scope, new_ref, new_digest, (target or {}).get("display_name") or source["display_name"], consent,
                 min(float(source["first_seen_at"]), float((target or source)["first_seen_at"])),
                 max(float(source["last_seen_at"]), float((target or source)["last_seen_at"])),
                 int(source["message_count"]) + int((target or {}).get("message_count", 0)),
                 json.dumps(metadata, ensure_ascii=False),
                 max(int(source["profile_version"]), int((target or {}).get("profile_version", 0))) + 1),
            )
            target_ids = {int(row[0]) for row in db.execute(
                "SELECT id FROM member_memory_facts WHERE group_id=? AND member_ref=?", (scope, new_ref))}
            moved = db.execute(
                "UPDATE member_memory_facts SET group_id=?,member_ref=? WHERE group_id=? AND member_ref=?",
                (scope, new_ref, legacy, old_ref),
            ).rowcount
            # Preserve provenance row ids, while removing legacy namespace reads.
            db.execute("UPDATE group_history SET group_id=?,member_id=CASE WHEN member_id=? THEN ? ELSE member_id END WHERE group_id=?",
                       (scope, old_ref, new_ref, legacy))
            active = db.execute(
                "SELECT * FROM member_memory_facts WHERE group_id=? AND member_ref=? AND status='active' "
                "AND (expires_at IS NULL OR expires_at>?)", (scope, new_ref, stamp)
            ).fetchall()
            winners: dict[str, sqlite3.Row] = {}
            def rank(row: sqlite3.Row) -> tuple[Any, ...]:
                return row["explicitness"] == "explicit", float(row["updated_at"]), int(row["id"]) in target_ids, int(row["id"])
            for row in active:
                key = self._normalize_fact_key(row["fact_key"])
                prior = winners.get(key)
                if prior is None or rank(row) > rank(prior):
                    winners[key] = row
            winner_ids = {int(row["id"]) for row in winners.values()}
            for row in active:
                if int(row["id"]) not in winner_ids:
                    db.execute("UPDATE member_memory_facts SET status='superseded' WHERE id=?", (row["id"],))
            aliases: dict[str, int] = {}
            migrated_documents = 0
            for document in db.execute("SELECT id,content_hash FROM knowledge_documents WHERE group_id=?", (legacy,)).fetchall():
                duplicate = db.execute(
                    "SELECT id FROM knowledge_documents WHERE group_id=? AND content_hash=?", (scope, document["content_hash"])
                ).fetchone()
                if duplicate is not None:
                    # Preserve the historical duplicate in its legacy scope;
                    # explicit aliases keep old doc references usable privately.
                    aliases[str(document["id"])] = int(duplicate["id"])
                    continue
                db.execute("UPDATE knowledge_documents SET group_id=? WHERE id=?", (scope, document["id"]))
                db.execute("UPDATE knowledge_chunks SET group_id=? WHERE document_id=?", (scope, document["id"]))
                migrated_documents += 1
            metadata["legacy_dm_migration"]["document_aliases"] = aliases
            db.execute("UPDATE group_members SET metadata_json=? WHERE group_id=? AND member_ref=?",
                       (json.dumps(metadata, ensure_ascii=False), scope, new_ref))
            # Old summaries may embed obsolete member references; rebuild from
            # the retained source rows rather than copying unverifiable text.
            db.execute("DELETE FROM group_memories WHERE group_id IN (?,?)", (legacy, scope))
            db.execute("DELETE FROM compaction_jobs WHERE group_id IN (?,?)", (legacy, scope))
            db.execute("DELETE FROM group_members WHERE group_id=? AND member_ref=?", (legacy, old_ref))
            self.record_audit("legacy_dm_migrated", chat_id=scope, source="scope_migration", confidence=1.0)
            return {**result, "status": "migrated", "migrated_facts": max(0, moved),
                    "migrated_documents": migrated_documents, "document_aliases": aliases}

    def resolve_legacy_document_id(self, scope: str, document_id: int) -> int:
        """Resolve only aliases produced by a proven private-scope migration."""
        wanted = int(document_id)
        if not str(scope).startswith("dm:"):
            return wanted
        with self._lock:
            for row in self.db.execute("SELECT metadata_json FROM group_members WHERE group_id=?", (str(scope),)):
                try:
                    marker = json.loads(row["metadata_json"]).get("legacy_dm_migration", {})
                    mapped = marker.get("document_aliases", {}).get(str(wanted))
                    if marker.get("version") == 1 and mapped is not None:
                        return int(mapped)
                except (TypeError, ValueError, AttributeError):
                    continue
        return wanted

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
            if value == "opted_out":
                self._remove_member_stickers(db, str(group_id), ref)
                self._advance_memory_epoch(group_id)
            row = db.execute(
                "SELECT * FROM group_members WHERE group_id=? AND member_ref=?",
                (str(group_id), ref),
            ).fetchone()
        return dict(row) if row is not None else {}

    def apply_group_poll(
        self, group_id: str, member_id: str, action: str, *,
        question: str = "", options: tuple[str, ...] = (),
        poll_id: str = "", option_no: int = 0, now: float | None = None,
    ) -> dict[str, Any]:
        """Expire, mutate and count a group poll under one immediate transaction."""
        if action not in {"show", "create", "vote", "close"}:
            raise ValueError("invalid poll action")
        if not group_id or not member_id:
            raise ValueError("group and member are required")
        if action == "create" and (
            not question.strip() or len(question) > 200
            or not 2 <= len(options) <= 5
            or any(not item.strip() or len(item) > 100 for item in options)
        ):
            raise ValueError("invalid poll question or options")
        stamp = time.time() if now is None else float(now)
        if not math.isfinite(stamp):
            raise ValueError("invalid poll time")
        group = str(group_id)
        ref = self.member_ref_for(group, member_id)
        with self.transaction() as db:
            # Do not raise for normal rule failures: expiry must still commit.
            db.execute(
                "UPDATE group_polls SET status='expired' "
                "WHERE group_id=? AND status='open' AND expires_at<=?",
                (group, stamp),
            )
            if action == "create":
                if db.execute(
                    "SELECT 1 FROM group_polls WHERE group_id=? AND status='open'", (group,)
                ).fetchone():
                    return {"error": "本群已有进行中的投票，请先结束或等待过期。"}
                poll_id = secrets.token_hex(5)
                db.execute(
                    "INSERT INTO group_polls VALUES (?,?,?,?, 'open',?,?)",
                    (poll_id, group, ref, question, stamp, stamp + 86400),
                )
                db.executemany(
                    "INSERT INTO group_poll_options VALUES (?,?,?)",
                    [(poll_id, index, text) for index, text in enumerate(options, 1)],
                )
            if action == "show":
                row = db.execute(
                    "SELECT * FROM group_polls WHERE group_id=? "
                    "ORDER BY (status='open') DESC, created_at DESC, rowid DESC LIMIT 1",
                    (group,),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM group_polls WHERE group_id=? AND poll_id=?",
                    (group, poll_id),
                ).fetchone()
            if row is None:
                return {"error": "本群没有找到该投票。" if poll_id else "本群暂无投票。"}
            poll_id = str(row["poll_id"])
            if action in {"vote", "close"}:
                if row["status"] != "open":
                    return {"error": "投票已结束或已过期，不能再修改。"}
                if action == "close":
                    if row["creator_ref"] != ref:
                        return {"error": "只有创建者可以结束这个投票。"}
                    db.execute("UPDATE group_polls SET status='closed' WHERE poll_id=?", (poll_id,))
                else:
                    if not db.execute(
                        "SELECT 1 FROM group_poll_options WHERE poll_id=? AND option_no=?",
                        (poll_id, option_no),
                    ).fetchone():
                        return {"error": "选项编号无效，请查看当前投票。"}
                    db.execute(
                        "INSERT INTO group_poll_votes VALUES (?,?,?) "
                        "ON CONFLICT(poll_id,member_ref) DO UPDATE SET option_no=excluded.option_no",
                        (poll_id, ref, option_no),
                    )
            result = dict(row)
            if action == "close":
                result["status"] = "closed"
            result["options"] = [
                dict(item) for item in db.execute(
                    "SELECT o.option_no, o.text, COUNT(v.member_ref) AS votes "
                    "FROM group_poll_options o LEFT JOIN group_poll_votes v "
                    "ON v.poll_id=o.poll_id AND v.option_no=o.option_no "
                    "WHERE o.poll_id=? GROUP BY o.option_no, o.text ORDER BY o.option_no",
                    (poll_id,),
                )
            ]
            return result

    @staticmethod
    def _remove_member_stickers(db: sqlite3.Connection, group_id: str, member_ref: str) -> None:
        db.execute(
            "DELETE FROM group_sticker_sources WHERE group_id=? AND member_ref=?",
            (group_id, member_ref),
        )
        db.execute(
            "DELETE FROM group_stickers WHERE group_id=? AND NOT EXISTS "
            "(SELECT 1 FROM group_sticker_sources s WHERE s.group_id=group_stickers.group_id "
            "AND s.digest=group_stickers.digest)",
            (group_id,),
        )

    def add_group_sticker(
        self, group_id: str, member_id: str, image_data: bytes, mime_type: str,
        category: str, caption: str, *, expected_epoch: int, expected_profile_version: int,
        retention_days: int = 30, max_per_group: int = 64, max_total: int = 256,
    ) -> bool:
        """Persist an already screened, metadata-free image; never source URLs or raw IDs."""
        if (not group_id or str(group_id).startswith("dm:") or not member_id
                or not isinstance(image_data, bytes) or not 0 < len(image_data) <= 512 * 1024
                or mime_type not in {"image/png", "image/gif"} or category not in STICKER_CATEGORIES):
            raise ValueError("invalid screened sticker")
        caption = self._bounded_text(caption, 80)
        digest = hashlib.sha256(image_data).hexdigest()
        ref = self.member_ref_for(group_id, member_id)
        now = time.time()
        with self.transaction() as db:
            member = self.get_group_member(group_id, member_ref=ref)
            if (self.memory_epoch(group_id) != expected_epoch or not member
                    or member["consent_status"] == "opted_out" or member.get("deleted_at") is not None
                    or member["profile_version"] != expected_profile_version):
                return False
            db.execute("DELETE FROM group_stickers WHERE expires<=?", (now,))
            db.execute(
                "INSERT INTO group_stickers(group_id,digest,category,caption,mime_type,image_data,created,expires) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(group_id,digest) "
                "DO UPDATE SET expires=MAX(group_stickers.expires,excluded.expires)",
                (group_id, digest, category, caption, mime_type, image_data, now,
                 now + min(90, max(1, int(retention_days))) * 86400),
            )
            db.execute("INSERT OR IGNORE INTO group_sticker_sources VALUES(?,?,?)", (group_id, digest, ref))
            db.execute(
                "DELETE FROM group_stickers WHERE group_id=? AND digest NOT IN "
                "(SELECT digest FROM group_stickers WHERE group_id=? ORDER BY last_used DESC,created DESC,digest LIMIT ?)",
                (group_id, group_id, min(128, max(1, int(max_per_group)))),
            )
            db.execute(
                "DELETE FROM group_stickers WHERE (group_id,digest) NOT IN "
                "(SELECT group_id,digest FROM group_stickers ORDER BY last_used DESC,created DESC,digest LIMIT ?)",
                (min(256, max(1, int(max_total))),),
            )
            return db.execute(
                "SELECT 1 FROM group_stickers WHERE group_id=? AND digest=?", (group_id, digest)
            ).fetchone() is not None

    def list_group_stickers(self, group_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self.db.execute(
                "SELECT category,COUNT(*) AS count FROM group_stickers WHERE group_id=? AND expires>? "
                "GROUP BY category ORDER BY category", (str(group_id), time.time()),
            )]

    def group_sticker_metadata(self, group_id: str, digest: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute(
                "SELECT category,caption FROM group_stickers WHERE group_id=? AND digest=? AND expires>?",
                (group_id, digest, time.time()),
            ).fetchone()
            return dict(row) if row else None

    def set_sticker_platform_block(self, group_id: str, blocked: bool) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO group_sticker_delivery(group_id,last_attempt,blocked) VALUES(?,0,?) "
                "ON CONFLICT(group_id) DO UPDATE SET blocked=excluded.blocked",
                (group_id, int(blocked)),
            )

    def sticker_platform_blocked(self, group_id: str) -> bool:
        with self._lock:
            row = self.db.execute("SELECT blocked FROM group_sticker_delivery WHERE group_id=?", (group_id,)).fetchone()
            return bool(row and row[0])

    def reserve_group_sticker(
        self, group_id: str, category: str, *, expected_epoch: int, cooldown_seconds: float = 0,
    ) -> dict[str, Any] | None:
        """Reserve before transport; even an ambiguous delivery consumes the cooldown."""
        now = time.time()
        with self.transaction() as db:
            if self.memory_epoch(group_id) != expected_epoch:
                return None
            prior = db.execute(
                "SELECT last_attempt,blocked FROM group_sticker_delivery WHERE group_id=?", (group_id,)
            ).fetchone()
            if prior and (prior[1] or now - prior[0] < max(0, float(cooldown_seconds))):
                return None
            row = db.execute(
                "SELECT digest,mime_type,image_data FROM group_stickers "
                "WHERE group_id=? AND category=? AND expires>? ORDER BY last_used,created,digest LIMIT 1",
                (group_id, category, now),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE group_stickers SET last_used=? WHERE group_id=? AND digest=?",
                (now, group_id, row["digest"]),
            )
            db.execute(
                "INSERT INTO group_sticker_delivery(group_id,last_attempt) VALUES(?,?) ON CONFLICT(group_id) "
                "DO UPDATE SET last_attempt=excluded.last_attempt", (group_id, now),
            )
            return dict(row)

    def purge_group_stickers(self) -> int:
        with self.transaction() as db:
            expired = db.execute("DELETE FROM group_stickers WHERE expires<=?", (time.time(),))
            db.execute(
                "DELETE FROM group_sticker_delivery WHERE blocked=0 AND group_id NOT IN (SELECT group_id FROM group_stickers)"
            )
            return max(0, expired.rowcount)

    def forget_group_member(self, group_id: str, member_id: str, *, hard_delete: bool = True) -> int:
        ref, _ = self._member_identity(group_id, member_id)
        with self.transaction() as db:
            self._advance_memory_epoch(group_id)
            self._remove_member_stickers(db, str(group_id), ref)
            db.execute(
                "DELETE FROM group_poll_votes WHERE member_ref=? AND poll_id IN "
                "(SELECT poll_id FROM group_polls WHERE group_id=?)",
                (ref, str(group_id)),
            )
            db.execute(
                "DELETE FROM group_polls WHERE group_id=? AND creator_ref=? AND status!='closed'",
                (str(group_id), ref),
            )
            db.execute("DELETE FROM character_items WHERE scope=? AND (owner=? OR kind='episode')", (str(group_id), ref))
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
        member_ref: str | None = None,
        expected_source_digest: str | None = None,
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
        ref = str(member_ref or self.member_ref_for(group_id, member_id))
        if not re.fullmatch(r"m-[a-f0-9]{20}", ref):
            raise ValueError("invalid member reference")
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
                    "SELECT id,created_at,text FROM group_history WHERE id=? AND group_id=? AND member_id=? AND role='user'",
                    (int(source_history_id), str(group_id), ref),
                ).fetchone()
                if source is None:
                    raise ValueError("member fact source does not belong to this member")
                if expected_source_digest is not None and self.history_digest(source["text"]) != expected_source_digest:
                    raise ValueError("member fact source changed during extraction")
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
            results = [
                row for row in results
                if terms & self._search_terms(f"{row['fact_key']} {row['fact_value']}")
            ]
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
