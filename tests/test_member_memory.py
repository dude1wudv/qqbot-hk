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
                    {"category": "project", "key": "当前项目", "value": "北斗", "confidence": 0.92,
                     "evidence": "我负责北斗"},
                    {"category": "preference", "key": "颜色", "value": "蓝色", "confidence": 0.6,
                     "evidence": "我负责北斗"},
                ]}}

        ctx = type("Ctx", (), {"llm": LLM()})()
        self.memory.touch("group-a", "member-a")
        self.assertEqual(await self.memory.extract(ctx, "group-a", "member-a", "我负责北斗", source_kind="ambient"), 0)
        self.store.set_member_consent("group-a", "member-a", "opted_in")
        stored = await self.memory.extract(ctx, "group-a", "member-a", "我负责北斗", source_kind="ambient")
        self.assertEqual(stored, 1)
        self.assertIn("北斗", self.memory.presentation("group-a", "member-a"))

    def test_explicit_correction_supersedes_inferred_fact_across_categories(self):
        self.store.set_member_consent("group-a", "member-a", "opted_in")
        self.store.add_member_memory_fact(
            "group-a", "member-a", "project", "项目", "旧项目",
            confidence=0.95, explicitness="inferred",
        )
        self.memory.remember("group-a", "member-a", "项目=新项目")
        self.store.add_member_memory_fact(
            "group-a", "member-a", "role", "项目", "旧推断",
            confidence=0.99, explicitness="inferred",
        )
        values = [item["fact_value"] for item in self.memory.facts("group-a", "member-a")]
        self.assertEqual(values, ["新项目"])
        self.assertIn("新项目", self.memory.presentation("group-a", "member-a"))
        self.assertNotIn("旧项目", self.memory.presentation("group-a", "member-a"))

    def test_older_source_cannot_supersede_newer_source(self):
        self.memory.touch("group-a", "member-a")
        member_ref = self.store.member_ref_for("group-a", "member-a")
        older = self.store.append_history(
            "group-a", role="user", member_id=member_ref, text="我负责旧项目",
            message_id="older", source_kind="addressed",
        )
        newer = self.store.append_history(
            "group-a", role="user", member_id=member_ref, text="我负责新项目",
            message_id="newer", source_kind="addressed",
        )
        self.store.set_member_consent("group-a", "member-a", "opted_in")
        self.store.add_member_memory_fact(
            "group-a", "member-a", "project", "项目", "新项目",
            confidence=0.95, explicitness="inferred", source_history_id=newer,
        )
        self.store.add_member_memory_fact(
            "group-a", "member-a", "project", "项目", "旧项目",
            confidence=0.99, explicitness="inferred", source_history_id=older,
        )
        self.assertEqual([item["fact_value"] for item in self.memory.facts("group-a", "member-a")], ["新项目"])

    async def test_model_facts_require_input_evidence_and_finite_confidence(self):
        class LLM:
            async def acomplete_structured(self, **kwargs):
                return {"parsed": {"facts": [
                    {"category": "project", "key": "项目", "value": "北斗", "confidence": 0.95,
                     "evidence": "我负责北斗"},
                    {"category": "project", "key": "伪造", "value": "银河", "confidence": 0.99,
                     "evidence": "我负责银河"},
                    {"category": "project", "key": "缺证据", "value": "星河", "confidence": 0.99},
                    {"category": "project", "key": "非有限", "value": "天枢", "confidence": float("nan"),
                     "evidence": "我负责北斗"},
                ]}}

        self.store.set_member_consent("group-a", "member-a", "opted_in")
        stored = await self.memory.extract(
            type("Ctx", (), {"llm": LLM()})(), "group-a", "member-a", "我负责北斗",
            source_kind="addressed",
        )
        self.assertEqual(stored, 1)
        self.assertEqual([item["fact_value"] for item in self.memory.facts("group-a", "member-a")], ["北斗"])

    async def test_forget_then_reopt_in_blocks_old_extraction(self):
        started = __import__("asyncio").Event()
        resume = __import__("asyncio").Event()

        class LLM:
            async def acomplete_structured(self, **kwargs):
                started.set()
                await resume.wait()
                return {"parsed": {"facts": [{
                    "category": "project", "key": "项目", "value": "旧提取", "confidence": 0.95,
                    "evidence": "我负责旧提取",
                }]}}

        self.store.set_member_consent("group-a", "member-a", "opted_in")
        task = __import__("asyncio").create_task(self.memory.extract(
            type("Ctx", (), {"llm": LLM()})(), "group-a", "member-a", "我负责旧提取",
            source_kind="ambient",
        ))
        await started.wait()
        self.memory.forget("group-a", "member-a")
        self.memory.remember("group-a", "member-a", "偏好=保留新授权")
        resume.set()
        self.assertEqual(await task, 0)
        values = [item["fact_value"] for item in self.memory.facts("group-a", "member-a")]
        self.assertIn("保留新授权", values)
        self.assertNotIn("旧提取", values)

    def test_query_relevance_keeps_older_matching_project(self):
        memory = MemberMemory(self.store, min_confidence=0.85, max_profile_facts=2)
        memory.remember("group-a", "member-a", "项目=星河项目")
        memory.remember("group-a", "member-a", "回复长度=简短")
        memory.remember("group-a", "member-a", "语言=中文")
        memory.remember("group-a", "member-a", "格式=列表")
        self.assertIn("星河项目", [item["fact_value"] for item in memory.facts("group-a", "member-a", query="星河项目")])
        self.assertEqual(memory.facts("group-b", "member-a", query="星河项目"), [])
        memory.opt_out("group-a", "other-member")
        self.assertEqual(memory.facts("group-a", "other-member", query="星河项目"), [])

    def test_invalid_remember_does_not_change_opted_out_consent(self):
        self.memory.remember("group-a", "member-a", "偏好=简短回答")
        self.memory.opt_out("group-a", "member-a")
        for text in ("   ", "银行卡=123"):
            with self.assertRaises(ValueError):
                self.memory.remember("group-a", "member-a", text)
        self.assertEqual(self.memory.consent("group-a", "member-a"), "opted_out")
        self.assertEqual(self.memory.facts("group-a", "member-a"), [])

    async def test_forget_during_extraction_cannot_recreate_profile(self):
        started = __import__("asyncio").Event()
        resume = __import__("asyncio").Event()

        class LLM:
            async def acomplete_structured(self, **kwargs):
                started.set()
                await resume.wait()
                return {"parsed": {"facts": [{
                    "category": "project", "key": "项目", "value": "北斗", "confidence": 0.95,
                    "evidence": "我负责北斗",
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
