import asyncio
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

from smart_group_qq.memory import GroupMemory, normalize_member_message, normalize_summary_payload
from smart_group_qq.store import SCHEMA_VERSION, Store


class MemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.memory = GroupMemory(
            self.store, window_size=3, idle_seconds=3600, summary_chars=800,
            compact_after_messages=3, max_history_rows=100,
        )

    def tearDown(self):
        self.store.close()

    def test_member_label_and_group_isolation(self):
        self.assertEqual(normalize_member_message("abcdef123", "你好"), "[群成员:abcdef]: 你好")
        self.memory.record("group-a", "member-a", "A1", "m1")
        self.memory.record("group-b", "member-b", "B1", "m2")
        self.assertIn("A1", self.memory.background("group-a"))
        self.assertNotIn("B1", self.memory.background("group-a"))

    def test_window_compaction_and_reset_are_group_scoped(self):
        for index in range(5):
            self.memory.record("group-a", "member", f"A{index}", f"m{index}")
        self.memory.record("group-b", "member", "B", "b")
        self.assertTrue(self.memory.needs_refresh("group-a"))
        self.memory.compact("group-a")
        self.assertIsNotNone(self.store.get_memory("group-a"))
        self.memory.reset("group-a")
        self.assertEqual(self.store.count_history("group-a"), 0)
        self.assertEqual(self.store.count_history("group-b"), 1)

    def test_refresh_threshold_counts_only_this_group(self):
        self.memory.record("group-a", "member", "A1", "a1")
        for index in range(20):
            self.memory.record("group-b", "member", f"B{index}", f"b{index}")
        self.assertFalse(self.memory.record("group-a", "member", "A2", "a2"))
        self.assertFalse(self.memory.needs_refresh("group-a"))

    async def test_ai_summary_is_structured_and_persisted(self):
        class Result:
            model = "summary-model"
            parsed = {
                "summary": "讨论了发布计划。",
                "topics": ["发布"],
                "facts": ["服务运行在香港"],
                "decisions": ["周五发布"],
                "todos": ["成员member准备说明"],
                "open_questions": ["具体时间待定"],
                "participants": ["member"],
            }

        class LLM:
            async def acomplete_structured(self, **kwargs):
                self.kwargs = kwargs
                return Result()

        ctx = type("Ctx", (), {"llm": LLM()})()
        self.memory.record("group-a", "member", "我们周五发布，时间待定", "m1")
        payload = await self.memory.refresh_ai(ctx, "group-a", force=True)
        self.assertEqual(payload["decisions"], ["周五发布"])
        self.assertIn("【决定】", self.memory.presentation("group-a"))
        self.assertEqual(self.store.memory_payload("group-a")["model"], "summary-model")

    async def test_concurrent_refreshes_are_serialized(self):
        calls = 0

        class LLM:
            async def acomplete_structured(inner_self, **kwargs):
                nonlocal calls
                calls += 1
                await __import__("asyncio").sleep(0.01)
                return {"parsed": {
                    "summary": "已整理", "topics": [], "facts": [], "decisions": [],
                    "todos": [], "open_questions": [], "participants": [],
                }}

        for index in range(3):
            self.memory.record("group-a", "member", f"并发消息{index}", f"m{index}")
        ctx = type("Ctx", (), {"llm": LLM()})()
        await __import__("asyncio").gather(
            self.memory.refresh_ai(ctx, "group-a"),
            self.memory.refresh_ai(ctx, "group-a"),
        )
        self.assertEqual(calls, 1)

    def test_summary_payload_redacts_secrets(self):
        payload = normalize_summary_payload({
            "summary": "token=secret-value",
            "topics": [], "facts": ["sk-abcdefghijklmnop"], "decisions": [],
            "todos": [], "open_questions": [], "participants": [],
        })
        self.assertNotIn("secret-value", str(payload))
        self.assertNotIn("sk-abcdefghijklmnop", str(payload))

    async def test_model_failure_keeps_previous_memory_and_pending_history(self):
        self.store.set_memory(
            "group-a", "旧摘要", structured={
                "summary": "旧摘要", "topics": ["旧话题"], "facts": [],
                "decisions": [], "todos": [], "open_questions": [], "participants": [],
            },
        )
        self.memory.record("group-a", "member", "新增事实", "new-1")

        class BrokenLLM:
            async def acomplete_structured(self, **kwargs):
                raise RuntimeError("offline")

        payload = await self.memory.refresh_ai(type("Ctx", (), {"llm": BrokenLLM()})(), "group-a", force=True)
        self.assertIn("旧摘要", payload["summary"])
        self.assertNotIn("新增事实", payload["summary"])
        self.assertEqual(payload["topics"], ["旧话题"])
        self.assertEqual(self.store.memory_payload("group-a")["model"], "")
        self.assertEqual(self.store.memory_payload("group-a")["last_history_id"], 0)
        self.assertIn("新增事实", self.memory.background("group-a"))

    def test_existing_v1_database_is_migrated_without_losing_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            db = sqlite3.connect(path)
            try:
                db.executescript("""
                    CREATE TABLE group_memories (
                        group_id TEXT PRIMARY KEY, summary TEXT NOT NULL DEFAULT '',
                        window_size INTEGER NOT NULL DEFAULT 20, updated_at REAL NOT NULL
                    );
                    CREATE TABLE group_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL,
                        role TEXT NOT NULL, member_id TEXT, text TEXT NOT NULL,
                        message_id TEXT, created_at REAL NOT NULL
                    );
                    INSERT INTO group_memories VALUES ('group-a', '保留旧摘要', 20, 1.0);
                """)
                db.commit()
            finally:
                db.close()
            migrated = Store(path)
            try:
                self.assertEqual(migrated.memory_payload("group-a")["structured"]["summary"], "保留旧摘要")
                self.assertEqual(migrated.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                self.assertIn("knowledge_documents", {
                    row[0] for row in migrated.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                })
            finally:
                migrated.close()

    def test_newer_database_schema_is_rejected_without_downgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "future.db"
            db = sqlite3.connect(path)
            try:
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
                db.commit()
            finally:
                db.close()
            with self.assertRaises(RuntimeError):
                Store(path)
            check = sqlite3.connect(path)
            try:
                self.assertEqual(check.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION + 1)
            finally:
                check.close()

    def test_recent_ambient_history_is_group_scoped_and_time_bounded(self):
        rows = [
            ("g1", "a1", "ambient", 100.0),
            ("g1", "a2", "ambient", 101.0),
            ("g1", "addressed", "addressed", 102.0),
            ("g1", "a3", "ambient", 103.0),
            ("g2", "other", "ambient", 104.0),
        ]
        for index, (group, text, source, stamp) in enumerate(rows, 1):
            self.store.append_history(
                group, role="user", member_id="m", text=text, message_id=f"msg-{index}",
                source_kind=source, created_at=stamp,
            )
        result = self.store.recent_ambient_history(
            "g1", before_id=5, before_time=104.0, limit=2, max_age_seconds=None,
        )
        self.assertEqual([row["text"] for row in result], ["a2", "a3"])
        result = self.store.recent_ambient_history(
            "g1", before_id=5, limit=5, max_age_seconds=2, now=104.0,
        )
        self.assertEqual([row["text"] for row in result], ["a3"])

    def test_enrich_history_merges_media_callback_idempotently(self):
        self.store.append_history(
            "group-a", role="user", member_id="member", text="原始文本",
            message_id="ambient-1", source_kind="ambient", media=["photo.jpg"], created_at=100,
        )
        self.assertTrue(self.store.enrich_history(
            "group-a", "ambient-1", "图片描述", ["photo.jpg", "description.txt"],
        ))
        self.assertTrue(self.store.enrich_history(
            "group-a", "ambient-1", "图片描述", ["photo.jpg", "description.txt"],
        ))
        row = self.store.get_history("group-a")[0]
        self.assertEqual(row["text"], "原始文本\n\n图片描述")
        self.assertEqual(__import__("json").loads(row["media_json"]), ["photo.jpg", "description.txt"])
        self.assertFalse(self.store.enrich_history("group-a", "missing", "late", []))

    def test_history_and_late_enrichment_redact_credentials(self):
        self.store.append_history(
            "group-a", role="user", member_id="member", text="token=initial-secret-value",
            message_id="ambient-secret", source_kind="ambient",
        )
        self.store.enrich_history("group-a", "ambient-secret", "api_key=late-secret-value")
        text = self.store.get_history("group-a")[0]["text"]
        self.assertNotIn("initial-secret-value", text)
        self.assertNotIn("late-secret-value", text)
        self.assertIn("已隐藏敏感信息", text)

    def test_maintenance_lists_backlog_and_purges_by_source(self):
        self.store.append_history(
            "group-a", role="user", member_id="m", text="ambient", message_id="a",
            source_kind="ambient", created_at=10,
        )
        self.store.append_history(
            "group-a", role="user", member_id="m", text="addressed", message_id="b",
            source_kind="addressed", created_at=20,
        )
        backlog = self.store.list_memory_backlog_groups()
        self.assertEqual(backlog[0]["group_id"], "group-a")
        self.assertEqual(backlog[0]["latest_history_id"], 2)
        self.assertEqual(backlog[0]["memory_cursor"], 0)
        # Retention never deletes rows that have not crossed the durable
        # compaction cursor.
        self.assertEqual(self.store.purge_history_by_source("ambient", 5, now=20), 0)
        self.store.set_memory("group-a", "摘要", last_history_id=2)
        self.assertEqual(self.store.purge_history_by_source("ambient", 5, now=20), 1)
        self.assertEqual(self.store.count_history("group-a"), 1)

    async def test_compaction_failure_keeps_cursor_and_job_retryable(self):
        self.memory.record("group-a", "member", "需要压缩的内容", "compact-1")

        class BrokenLLM:
            async def acomplete_structured(self, **kwargs):
                raise RuntimeError("offline")

        payload = await self.memory.refresh_ai(
            type("Ctx", (), {"llm": BrokenLLM()})(), "group-a", force=True,
        )
        self.assertIn("需要压缩的内容", payload["summary"])
        self.assertEqual(self.store.memory_payload("group-a")["last_history_id"], 0)
        job = self.store.get_compaction_job("group-a")
        self.assertIsNotNone(job)
        self.assertEqual(job["status"], "failed")
        self.assertGreaterEqual(job["attempt"], 1)

        retry_at = float(job["next_retry_at"])
        error = str(job["error"])
        self.memory.record("group-a", "member", "失败后的新消息", "compact-2")
        extended = self.store.enqueue_compaction_job("group-a", now=retry_at - 1)
        job = self.store.db.execute("SELECT * FROM compaction_jobs WHERE id=?", (extended,)).fetchone()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["next_retry_at"], retry_at)
        self.assertEqual(job["error"], error)

    async def test_compaction_drains_multiple_batches_and_completes_job(self):
        memory = GroupMemory(
            self.store, window_size=3, idle_seconds=3600, summary_chars=800,
            compact_after_messages=3, max_history_rows=100, compaction_batch_messages=20,
        )
        for index in range(45):
            memory.record("group-a", "member", f"消息-{index}", f"multi-{index}")

        class LLM:
            calls = 0
            async def acomplete_structured(inner_self, **kwargs):
                inner_self.calls += 1
                return {"parsed": {
                    "summary": "已整理", "topics": [], "facts": [], "decisions": [],
                    "todos": [], "open_questions": [], "participants": [],
                }}

        llm = LLM()
        await memory.refresh_ai(type("Ctx", (), {"llm": llm})(), "group-a")
        self.assertEqual(self.store.memory_payload("group-a")["last_history_id"], 45)
        self.assertGreaterEqual(llm.calls, 3)
        self.assertEqual(self.store.get_compaction_job("group-a"), None)
        completed = self.store.list_compaction_jobs("group-a", limit=1)[0]
        self.assertEqual(completed["status"], "completed")

    def test_member_storage_digest_consent_conflict_expiry_and_forget(self):
        member = self.store.upsert_group_member(
            "group-a", "openid-secret", display_name="群友", increment_messages=True,
        )
        self.assertTrue(member["member_ref"].startswith("m-"))
        self.assertNotIn("openid-secret", str(member))
        self.assertEqual(member["message_count"], 1)
        self.assertEqual(
            self.store.member_ref("group-a", "openid-secret"), member["member_ref"],
        )
        self.store.set_member_consent("group-a", "openid-secret", "opted_in")
        self.assertEqual(self.store.get_group_member("group-a", "openid-secret")["consent_status"], "opted_in")
        ref = member["member_ref"]
        source_history_id = self.store.append_history(
            "group-a", role="user", member_id=ref, text="明确偏好中文",
            message_id="fact-source", source_kind="addressed",
        )
        first = self.store.add_member_memory_fact(
            "group-a", "openid-secret", "preference", "language", "中文",
            confidence=0.8, explicitness="explicit", source_history_id=source_history_id,
        )
        second = self.store.add_member_memory_fact(
            "group-a", "openid-secret", "preference", "language", "English",
            confidence=0.9, explicitness="explicit", source_history_id=source_history_id,
        )
        self.assertTrue(second["applied"])
        self.assertEqual(second["supersedes_id"], first["id"])
        self.assertEqual(self.store.list_member_memory_facts("group-a", "openid-secret")[0]["fact_value"], "English")
        expiring = self.store.add_member_memory_fact(
            "group-a", "openid-secret", "work", "project", "alpha", expires_at=10,
        )
        self.assertEqual(len(self.store.list_member_memory_facts("group-a", "openid-secret", now=11)), 1)
        self.assertEqual(self.store.expire_member_memory_facts(now=11), 1)
        self.assertGreaterEqual(self.store.forget_group_member("group-a", "openid-secret"), 2)
        self.assertIsNone(self.store.get_group_member("group-a", "openid-secret"))

    def test_knowledge_search_reaches_old_chunk_beyond_recent_window(self):
        wanted = self.store.add_knowledge_document(
            "group-a", "老文档", "唯一锚词青鸾-8472", chunk_size=100, overlap=0,
        )
        for index in range(2000):
            self.store.add_knowledge_document(
                "group-a", f"无关文档-{index}", f"普通内容-{index}",
            )
        results = self.store.search_knowledge("group-a", "青鸾-8472")
        self.assertTrue(any(item["doc_id"] == wanted["doc_id"] for item in results))
        self.assertEqual(self.store.search_knowledge("group-b", "青鸾-8472"), [])

    async def test_forget_during_successful_refresh_cannot_persist_removed_member(self):
        self.store.set_member_consent("group-a", "member-a", "opted_in")
        self.store.set_member_consent("group-a", "member-b", "opted_in")
        self.memory.record("group-a", "member-a", "甲方私密事实-青鸾", "a-source")
        self.memory.record("group-a", "member-b", "乙方保留事实-白鹭", "b-source")
        started = asyncio.Event()
        release = asyncio.Event()

        class LLM:
            async def acomplete_structured(self, **kwargs):
                started.set()
                await release.wait()
                return {"parsed": {
                    "summary": "甲方私密事实-青鸾",
                    "topics": [], "facts": ["甲方私密事实-青鸾"],
                    "decisions": [], "todos": [], "open_questions": [], "participants": [],
                }}

        task = asyncio.create_task(self.memory.refresh_ai(
            type("Ctx", (), {"llm": LLM()})(), "group-a", force=True,
        ))
        await started.wait()
        self.store.forget_group_member("group-a", "member-a")
        release.set()
        payload = await task
        self.assertNotIn("甲方私密事实-青鸾", str(payload))
        self.assertNotIn("甲方私密事实-青鸾", str(self.store.memory_payload("group-a")))
        texts = [row["text"] for row in self.store.get_history("group-a")]
        self.assertNotIn("甲方私密事实-青鸾", texts)
        self.assertIn("乙方保留事实-白鹭", texts)

    async def test_forget_during_failed_refresh_cannot_persist_removed_member(self):
        self.store.set_member_consent("group-a", "member-a", "opted_in")
        self.store.set_member_consent("group-a", "member-b", "opted_in")
        self.memory.record("group-a", "member-a", "甲方私密事实-朱雀", "a-source")
        self.memory.record("group-a", "member-b", "乙方保留事实-玄鸟", "b-source")
        started = asyncio.Event()
        release = asyncio.Event()

        class BrokenLLM:
            async def acomplete_structured(self, **kwargs):
                started.set()
                await release.wait()
                raise RuntimeError("offline")

        task = asyncio.create_task(self.memory.refresh_ai(
            type("Ctx", (), {"llm": BrokenLLM()})(), "group-a", force=True,
        ))
        await started.wait()
        self.store.forget_group_member("group-a", "member-a")
        release.set()
        payload = await task
        self.assertNotIn("甲方私密事实-朱雀", str(payload))
        self.assertNotIn("甲方私密事实-朱雀", str(self.store.memory_payload("group-a")))
        texts = [row["text"] for row in self.store.get_history("group-a")]
        self.assertNotIn("甲方私密事实-朱雀", texts)
        self.assertIn("乙方保留事实-玄鸟", texts)

    def test_forget_removes_assistant_derivatives_but_retains_other_data(self):
        self.store.set_member_consent("group-a", "member-a", "opted_in")
        self.store.set_member_consent("group-a", "member-b", "opted_in")
        ref = self.store.member_ref_for("group-a", "member-a")
        self.store.append_history(
            "group-a", role="user", member_id=ref, text="本人原文-苍龙",
            message_id="a-source", source_kind="addressed",
        )
        self.store.append_history(
            "group-a", role="assistant", text="机器人复述本人事实-苍龙",
            message_id="assistant-copy", source_kind="assistant:test",
        )
        self.store.append_history(
            "group-a", role="user", member_id=self.store.member_ref_for("group-a", "member-b"),
            text="别人无关原文-麒麟", message_id="b-source", source_kind="addressed",
        )
        knowledge = self.store.add_knowledge_document(
            "group-a", "群知识", "群知识文档-凤凰", source="upload",
        )
        self.store.forget_group_member("group-a", "member-a")
        history_text = [row["text"] for row in self.store.get_history("group-a")]
        self.assertNotIn("本人原文-苍龙", history_text)
        self.assertNotIn("机器人复述本人事实-苍龙", history_text)
        self.assertIn("别人无关原文-麒麟", history_text)
        self.assertNotIn("本人原文-苍龙", self.memory.background("group-a"))
        self.assertNotIn("机器人复述本人事实-苍龙", self.memory.background("group-a"))
        self.assertEqual(self.store.search_knowledge("group-a", "凤凰")[0]["doc_id"], knowledge["doc_id"])

    def test_forget_removes_member_history_and_invalidates_derived_summary(self):
        self.store.set_member_consent("group-a", "openid-secret", "opted_in")
        ref = self.store.member_ref_for("group-a", "openid-secret")
        self.store.append_history(
            "group-a", role="user", member_id=ref, text="个人内容",
            message_id="member-row", source_kind="addressed",
        )
        self.store.set_memory("group-a", "含个人内容", last_history_id=1)
        self.store.enqueue_compaction_job("group-a", 0, 1)
        self.store.forget_group_member("group-a", "openid-secret")
        self.assertEqual(self.store.get_history("group-a"), [])
        self.assertEqual(self.store.memory_payload("group-a"), {})
        self.assertEqual(self.store.list_compaction_jobs("group-a"), [])

    def test_operational_metadata_retention_keeps_pending_claims(self):
        self.store.claim_message("qqbot", "old-done", "reply", now=1)
        self.store.finish_claim("qqbot", "old-done", "reply", success=True, now=2)
        self.store.claim_message("qqbot", "old-pending", "reply", now=1)
        self.store.record_audit("event")
        self.store.db.execute("UPDATE audit_events SET created_at=1")
        self.store.db.commit()
        removed = self.store.purge_operational_metadata(
            audit_age_seconds=10, claim_age_seconds=10, now=20,
        )
        self.assertEqual(removed, {"audit_events": 1, "message_claims": 1})
        claims = self.store.db.execute("SELECT message_id FROM message_claims").fetchall()
        self.assertEqual([row[0] for row in claims], ["old-pending"])

    def test_background_can_exclude_recent_rows_for_separate_ambient_injection(self):
        self.memory.record("group-a", "member", "ambient context", "ambient-1", source_kind="ambient")
        self.assertEqual(self.memory.background("group-a", include_recent=False), "")
        self.assertIn("ambient context", self.memory.background("group-a"))


if __name__ == "__main__":
    unittest.main()
