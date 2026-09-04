from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

from smart_group_qq.memory import GroupMemory, normalize_member_message, normalize_summary_payload
from smart_group_qq.store import Store


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

        self.memory.record("group-a", "member", "并发消息", "m1")
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

    async def test_model_failure_keeps_previous_memory_and_adds_new_text(self):
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
        self.assertIn("新增事实", payload["summary"])
        self.assertEqual(payload["topics"], ["旧话题"])
        self.assertEqual(self.store.memory_payload("group-a")["model"], "deterministic-fallback")

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
                self.assertIn("knowledge_documents", {
                    row[0] for row in migrated.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                })
            finally:
                migrated.close()


if __name__ == "__main__":
    unittest.main()
