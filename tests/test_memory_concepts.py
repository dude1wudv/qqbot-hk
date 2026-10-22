import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
from smart_group_qq.memory import (
    CONCEPT_INDEX_LIMIT, CONCEPTS_PER_ITEM, GroupMemory, SUMMARY_SCHEMA,
    normalize_summary_payload,
)
from smart_group_qq.store import Store


def summary(**fields):
    return {
        "summary": "本群安排已经整理。", "topics": [], "facts": [],
        "decisions": [], "todos": [], "open_questions": [], "participants": [],
        **fields,
    }


def tag(section, index, *concepts):
    return {"section": section, "index": index, "concepts": list(concepts)}


class ConceptNormalizationTests(unittest.TestCase):
    def test_normalization_remaps_blank_and_duplicate_source_indices(self):
        payload = normalize_summary_payload(summary(
            facts=["", "  周末去海边  ", "周末去海边", "今晚线上碰面"],
            concept_index=[tag("facts", 1, "郊游"), tag("facts", 2, "outing"),
                           tag("facts", 3, "远程聚会")],
        ))
        self.assertEqual(payload["facts"], ["周末去海边", "今晚线上碰面"])
        self.assertEqual(payload["concept_index"], [
            tag("facts", 0, "郊游", "outing"), tag("facts", 1, "远程聚会"),
        ])
        self.assertEqual(normalize_summary_payload(payload), payload)

    def test_invalid_index_data_is_discarded_without_losing_summary(self):
        invalid_entries = [
            None, "arbitrary", {}, tag("summary", 0, "新事实"),
            tag([], 0, "错误类型"), tag("facts", True, "布尔序号"),
            tag("facts", -1, "负序号"), tag("facts", 20, "不存在"),
            {"section": "facts", "index": 0, "concepts": "not a list"},
            tag("facts", 0, "", "\n", "x" * 33, "a\nb", "token=private-token", None, {}),
        ]
        payload = normalize_summary_payload(summary(facts=["保留的原始事实"], concept_index=invalid_entries))
        self.assertEqual(payload["facts"], ["保留的原始事实"])
        self.assertEqual(payload["summary"], "本群安排已经整理。")
        self.assertNotIn("concept_index", payload)
        for malformed in (None, {}, "invalid", 123):
            self.assertNotIn("concept_index", normalize_summary_payload(summary(concept_index=malformed)))

    def test_limits_drop_removed_facts_and_bound_each_retained_index(self):
        payload = normalize_summary_payload(summary(
            facts=[f"事实{i}" for i in range(30)], decisions=[f"决定{i}" for i in range(15)],
            concept_index=[tag("facts", i, *[f"别名{n}" for n in range(15)]) for i in range(30)]
            + [tag("decisions", i, "计划") for i in range(15)],
        ))
        self.assertEqual(len(payload["concept_index"]), CONCEPT_INDEX_LIMIT)
        for entry in payload["concept_index"]:
            self.assertLess(entry["index"], len(payload[entry["section"]]))
            self.assertLessEqual(len(entry["concepts"]), CONCEPTS_PER_ITEM)

    def test_legacy_payload_keeps_the_original_shape_and_schema_property_is_optional(self):
        original = summary(facts=["原有事实"])
        self.assertEqual(normalize_summary_payload(original), original)
        self.assertNotIn("concept_index", SUMMARY_SCHEMA["required"])


class MemoryConceptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:", member_secret="test-only-secret")
        self.addCleanup(self.store.close)
        self.memory = GroupMemory(self.store)

    def save(self, payload, *, group="g"):
        self.store.set_memory(group, payload["summary"], structured=payload, model="existing-model")

    async def refresh(self, payload):
        calls = []

        class LLM:
            async def acomplete_structured(inner, **kwargs):
                calls.append(kwargs)
                return {"parsed": payload}

        self.memory.record("g", "member", "补充本群安排", "next")
        result = await self.memory.refresh_ai(SimpleNamespace(llm=LLM()), "g", force=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["task"], "compression")
        return result

    def test_synonym_recall_returns_only_original_fact_and_is_group_scoped(self):
        original = summary(facts=["明早一同出门跑步"])
        self.save(original)
        self.assertNotIn("明早一同出门跑步", self.memory.recall("g", "晨练", overview_chars=0))
        indexed = summary(facts=original["facts"], concept_index=[tag("facts", 0, "晨练", "exercise")])
        self.save(indexed)
        self.assertEqual(self.memory.recall("g", "晨练", overview_chars=0), "事实：明早一同出门跑步")
        self.assertNotIn("exercise", self.memory.recall("g", "exercise"))
        self.assertNotIn("concept_index", self.memory.presentation("g"))
        self.assertEqual(self.memory.recall("other", "晨练"), "")

    def test_alias_match_can_select_whole_fact_before_unrelated_decision(self):
        self.save(summary(
            facts=["明早一同出门跑步"], decisions=["今晚整理财务清单"],
            concept_index=[tag("facts", 0, "晨练")],
        ))
        self.assertEqual(
            self.memory.recall("g", "晨练", char_budget=12, overview_chars=0),
            "事实：明早一同出门跑步",
        )
        self.assertEqual(self.memory.recall("g", "晨练", char_budget=5, overview_chars=0), "")

    async def test_labels_persist_in_existing_json_with_one_model_call(self):
        payload = summary(facts=["明早一同出门跑步"], concept_index=[tag("facts", 0, "晨练")])
        await self.refresh(payload)
        self.assertEqual(self.store.memory_payload("g")["structured"]["concept_index"], payload["concept_index"])
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 7)
        self.assertIsNone(self.store.get_compaction_job("g"))

    async def test_invalid_optional_labels_do_not_fail_compaction_or_cursor_progress(self):
        await self.refresh(summary(facts=["原始事实"], concept_index=[tag("facts", 99, "悬空标签"), {}]))
        saved = self.store.memory_payload("g")
        self.assertEqual(saved["last_history_id"], 1)
        self.assertEqual(saved["structured"]["facts"], ["原始事实"])
        self.assertNotIn("concept_index", saved["structured"])
        self.assertIsNone(self.store.get_compaction_job("g"))

    async def test_incremental_summary_preserves_tags_only_for_unchanged_items(self):
        self.save(summary(
            facts=["明早一同出门跑步", "周五发布"], todos=["检查服务连通"],
            concept_index=[tag("facts", 0, "晨练"), tag("facts", 1, "release"),
                           tag("todos", 0, "巡检")],
        ))
        result = await self.refresh(summary(facts=["改到周六发布", "明早一同出门跑步"]))
        self.assertEqual(result["concept_index"], [tag("facts", 1, "晨练")])
        self.assertNotIn("release", str(result))
        self.assertNotIn("巡检", str(result))

    async def test_new_tags_replace_old_tags_for_the_same_item(self):
        self.save(summary(facts=["明早一同出门跑步"], concept_index=[tag("facts", 0, "旧别名")]))
        result = await self.refresh(summary(
            facts=["明早一同出门跑步"], concept_index=[tag("facts", 0, "晨练")],
        ))
        self.assertEqual(result["concept_index"], [tag("facts", 0, "晨练")])

    def test_deterministic_fallback_keeps_valid_prior_index(self):
        payload = summary(facts=["明早一同出门跑步"], concept_index=[tag("facts", 0, "晨练")])
        self.save(payload)
        self.memory.record("g", "member", "补充事项", "next")
        self.memory.compact("g")
        self.assertEqual(self.store.memory_payload("g")["structured"]["concept_index"], payload["concept_index"])

    async def test_good_summary_and_index_survive_model_failure(self):
        payload = summary(facts=["明早一同出门跑步"], concept_index=[tag("facts", 0, "晨练")])
        self.save(payload)
        before = self.store.memory_payload("g")
        self.memory.record("g", "member", "新的事项", "next")

        class BrokenLLM:
            async def acomplete_structured(inner, **kwargs):
                raise RuntimeError("unavailable")

        await self.memory.refresh_ai(SimpleNamespace(llm=BrokenLLM()), "g", force=True)
        self.assertEqual(self.store.memory_payload("g"), before)
        self.assertEqual(self.store.get_compaction_job("g")["attempt"], 1)

    async def test_forget_during_refresh_cannot_restore_derivative_labels(self):
        started, release = asyncio.Event(), asyncio.Event()
        self.store.set_member_consent("g", "member", "opted_in")
        self.memory.record("g", "member", "待删除来源", "next")

        class WaitingLLM:
            async def acomplete_structured(inner, **kwargs):
                started.set()
                await release.wait()
                return {"parsed": summary(facts=["待删除来源"], concept_index=[tag("facts", 0, "独有别名")])}

        pending = asyncio.create_task(self.memory.refresh_ai(SimpleNamespace(llm=WaitingLLM()), "g", force=True))
        await started.wait()
        self.store.forget_group_member("g", "member")
        release.set()
        result = await pending
        self.assertNotIn("独有别名", json.dumps(result, ensure_ascii=False))
        self.assertEqual(self.store.memory_payload("g"), {})
        self.assertEqual(self.memory.recall("g", "独有别名"), "")


if __name__ == "__main__":
    unittest.main()
