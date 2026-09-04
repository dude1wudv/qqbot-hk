"""Persistent, redacted state for the Smart Group QQ plugin.

The plugin storage API returns a profile-scoped sqlite connection.  This module
only stores message text in the bounded group-history table (needed for local
summaries); audit rows deliberately contain metadata, never message or reply
content.
"""
from __future__ import annotations

import sqlite3
import time
import hashlib
import json
import re
import threading
import unicodedata
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
        self._lock = threading.RLock()
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
        self._ensure_column("group_memories", "structured_json", "TEXT NOT NULL DEFAULT '{}'")
        self._ensure_column("group_memories", "last_history_id", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("group_memories", "model", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("group_memories", "version", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("group_history", "source_kind", "TEXT NOT NULL DEFAULT 'addressed'")
        self._ensure_column("group_history", "media_json", "TEXT NOT NULL DEFAULT '[]'")
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
    ) -> int:
        with self.transaction() as db:
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
                    None if member_id is None else str(member_id), str(text),
                    None if message_id is None else str(message_id), str(source_kind or "addressed"),
                    json.dumps(media or [], ensure_ascii=False, separators=(",", ":")),
                    time.time() if created_at is None else float(created_at),
                ),
            )
            return int(cur.lastrowid)

    add_history = append_history

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

    def latest_history_id(self, group_id: str) -> int:
        with self._lock:
            row = self.db.execute(
                "SELECT COALESCE(MAX(id),0) AS n FROM group_history WHERE group_id=?", (str(group_id),)
            ).fetchone()
        return int(row["n"])

    def clear_group(self, group_id: str) -> None:
        with self.transaction() as db:
            db.execute("DELETE FROM group_history WHERE group_id=?", (str(group_id),))
            db.execute("DELETE FROM group_memories WHERE group_id=?", (str(group_id),))

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
    ) -> None:
        payload = structured if isinstance(structured, dict) else {}
        with self.transaction() as db:
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

    def count_history(self, group_id: str) -> int:
        with self._lock:
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
                "WHERE c.group_id=? ORDER BY c.id DESC LIMIT 2000",
                (str(group_id),),
            ).fetchall()
        scored: list[dict[str, Any]] = []
        for row in rows:
            haystack = unicodedata.normalize("NFKC", f"{row['title']} {row['text']}").casefold()
            overlap = len(qterms & self._search_terms(haystack))
            score = overlap + (12 if needle in haystack else 0) + (4 if needle in str(row["title"]).casefold() else 0)
            if score <= 0:
                continue
            item = dict(row)
            item["doc_id"] = "doc-" + str(item.pop("document_id"))
            item["chunk_id"] = item["doc_id"] + "-chunk-" + str(int(item.pop("chunk_index")) + 1)
            item["score"] = float(score)
            scored.append(item)
        scored.sort(key=lambda item: (-item["score"], -int(item["id"])))
        return scored[:max(1, int(limit))]

    def integrity_check(self) -> bool:
        with self._lock:
            row = self.db.execute("PRAGMA integrity_check").fetchone()
        return bool(row and str(row[0]).lower() == "ok")


__all__ = ["Store", "PENDING_STALE_SECONDS"]
