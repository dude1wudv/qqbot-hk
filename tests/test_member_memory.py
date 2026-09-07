from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

from smart_group_qq.member_memory import MemberMemory
from smart_group_qq.store import Store


class MemberMemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:", member_secret="test-only-secret")
        self.memory = MemberMemory(self.store, min_confidence=0.85, fact_retention_days=30)

    def tearDown(self):
        self.store.close()

    def test_explicit_memory_is_group_scoped_and_forgettable(self):
        self.memory.touch("group-a", "member-a", display_name="小明")
        self.memory.remember("group-a", "member-a", "职责=后端发布")
        self.assertIn("后端发布", self.memory.presentation("group-a", "member-a"))
        self.assertNotIn("后端发布", self.memory.presentation("group-b", "member-a"))
        self.assertNotIn("member-a", str(self.store.list_group_members("group-a")))
        self.memory.forget("group-a", "member-a")
        self.assertEqual(self.memory.facts("group-a", "member-a"), [])

    def test_opt_out_prevents_recall_and_sensitive_fact_is_rejected(self):
        self.memory.remember("group-a", "member-a", "偏好=简短回答")
        self.memory.opt_out("group-a", "member-a")
        self.assertEqual(self.memory.facts("group-a", "member-a"), [])
        with self.assertRaises(ValueError):
            self.memory.remember("group-a", "member-a", "银行卡=123")

    async def test_auto_extract_requires_opt_in_and_confidence(self):
        class LLM:
            async def acomplete_structured(self, **kwargs):
                return {"parsed": {"facts": [
                    {"category": "project", "key": "当前项目", "value": "北斗", "confidence": 0.92},
                    {"category": "preference", "key": "颜色", "value": "蓝色", "confidence": 0.6},
                ]}}

        ctx = type("Ctx", (), {"llm": LLM()})()
        self.memory.touch("group-a", "member-a")
        self.assertEqual(await self.memory.extract(ctx, "group-a", "member-a", "我负责北斗", source_kind="ambient"), 0)
        self.store.set_member_consent("group-a", "member-a", "opted_in")
        stored = await self.memory.extract(ctx, "group-a", "member-a", "我负责北斗", source_kind="ambient")
        self.assertEqual(stored, 1)
        self.assertIn("北斗", self.memory.presentation("group-a", "member-a"))

    async def test_forget_during_extraction_cannot_recreate_profile(self):
        started = __import__("asyncio").Event()
        resume = __import__("asyncio").Event()

        class LLM:
            async def acomplete_structured(self, **kwargs):
                started.set()
                await resume.wait()
                return {"parsed": {"facts": [{
                    "category": "project", "key": "项目", "value": "北斗", "confidence": 0.95,
                }]}}

        self.store.set_member_consent("group-a", "member-a", "opted_in")
        task = __import__("asyncio").create_task(self.memory.extract(
            type("Ctx", (), {"llm": LLM()})(), "group-a", "member-a", "我负责北斗",
            source_kind="ambient",
        ))
        await started.wait()
        self.memory.forget("group-a", "member-a")
        resume.set()
        self.assertEqual(await task, 0)
        self.assertIsNone(self.store.get_group_member("group-a", "member-a"))
        self.assertEqual(self.store.list_member_memory_facts("group-a", "member-a"), [])

    def test_expired_facts_are_not_rendered(self):
        self.store.set_member_consent("group-a", "member-a", "opted_in")
        self.store.add_member_memory_fact(
            "group-a", "member-a", "self", "旧信息", "已过期",
            confidence=1.0, explicitness="explicit", expires_at=time.time() - 1,
        )
        self.assertNotIn("已过期", self.memory.presentation("group-a", "member-a"))


if __name__ == "__main__":
    unittest.main()
