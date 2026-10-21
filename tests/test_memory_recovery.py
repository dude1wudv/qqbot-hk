import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
from smart_group_qq.memory import GroupMemory
from smart_group_qq.store import Store


class SummaryLLM:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    async def acomplete_structured(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("unavailable")
        return {"parsed": {"summary": "有效摘要", "topics": [], "facts": [],
                           "decisions": [], "todos": [], "open_questions": [], "participants": []}}


class MemoryRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:", member_secret="test-only-secret")
        self.addCleanup(self.store.close)

    def record(self, memory, count, text="待整理"):
        for i in range(count):
            memory.record("g", "m", text, f"m{i}")

    async def test_failed_single_message_retries_after_backoff_without_new_traffic(self):
        memory = GroupMemory(self.store)
        self.record(memory, 1)
        await memory.refresh_ai(SimpleNamespace(llm=SummaryLLM(fail=True)), "g", force=True)
        job = self.store.get_compaction_job("g")
        retry = job["next_retry_at"]
        self.assertFalse(memory.needs_refresh("g", now=retry - 1))
        self.assertTrue(memory.needs_refresh("g", now=retry + 1))
        with patch("time.time", return_value=retry + 1):
            await memory.refresh_ai(SimpleNamespace(llm=SummaryLLM()), "g")
        self.assertEqual(self.store.memory_payload("g")["last_history_id"], 1)
        self.assertIsNone(self.store.get_compaction_job("g"))

    async def test_deferred_tail_finishes_below_new_summary_threshold(self):
        memory = GroupMemory(self.store, compaction_batch_messages=20, max_compaction_batches=1)
        self.record(memory, 21)
        ctx = SimpleNamespace(llm=SummaryLLM())
        await memory.refresh_ai(ctx, "g", force=True)
        self.assertEqual(self.store.memory_payload("g")["last_history_id"], 20)
        self.assertTrue(memory.needs_refresh("g"))
        await memory.refresh_ai(ctx, "g")
        self.assertEqual(self.store.memory_payload("g")["last_history_id"], 21)
        self.assertIsNone(self.store.get_compaction_job("g"))

    async def test_three_failures_stop_automatic_retries_and_force_can_recover(self):
        memory = GroupMemory(self.store)
        self.record(memory, 1)
        self.store.set_memory("g", "原有摘要", structured={"summary": "原有摘要"}, model="valid")
        before = self.store.memory_payload("g")
        llm = SummaryLLM(fail=True)
        ctx = SimpleNamespace(llm=llm)
        await memory.refresh_ai(ctx, "g", force=True)
        for expected in (2, 3):
            retry = self.store.get_compaction_job("g")["next_retry_at"]
            with patch("time.time", return_value=retry + 1):
                await memory.refresh_ai(ctx, "g")
            self.assertEqual(self.store.get_compaction_job("g")["attempt"], expected)
        job = self.store.get_compaction_job("g")
        self.assertFalse(memory.needs_refresh("g", now=job["next_retry_at"] + 100000))
        memory.record("g", "m", "新消息不能绕过失败游标", "new")
        with patch("time.time", return_value=job["next_retry_at"] + 100000):
            await memory.refresh_ai(ctx, "g")
            self.assertIsNone(self.store.claim_compaction_job(job["id"]))
            self.assertIsNone(self.store.claim_compaction_job(group_id="g"))
        self.assertEqual(llm.calls, 3)
        self.assertEqual(self.store.memory_payload("g"), before)
        self.assertEqual(self.store.count_history("g"), 2)
        self.assertEqual(job["status"], "failed")
        llm.fail = False
        await memory.refresh_ai(ctx, "g", force=True)
        self.assertEqual(llm.calls, 4)
        self.assertEqual(self.store.memory_payload("g")["last_history_id"], 2)
        self.assertIsNone(self.store.get_compaction_job("g"))

    async def test_legacy_high_attempt_job_stops_without_migration(self):
        memory = GroupMemory(self.store)
        self.record(memory, 1)
        job_id = self.store.enqueue_compaction_job("g", now=10)
        self.store.db.execute(
            "UPDATE compaction_jobs SET status='failed', attempt=143, next_retry_at=0 WHERE id=?",
            (job_id,),
        )
        self.store.db.commit()
        llm = SummaryLLM()
        await memory.refresh_ai(SimpleNamespace(llm=llm), "g")
        self.assertEqual(llm.calls, 0)
        self.assertEqual(self.store.get_compaction_job("g")["attempt"], 143)
        self.assertEqual(self.store.count_history("g"), 1)
        await memory.refresh_ai(SimpleNamespace(llm=llm), "g", force=True)
        self.assertEqual(llm.calls, 1)
        self.assertIsNone(self.store.get_compaction_job("g"))

    async def test_successful_cursor_resets_budget_before_later_batch_failure(self):
        memory = GroupMemory(self.store, compaction_batch_messages=20)
        self.record(memory, 21)
        job_id = self.store.enqueue_compaction_job("g", now=10)
        self.store.db.execute(
            "UPDATE compaction_jobs SET status='failed', attempt=143, next_retry_at=0 WHERE id=?",
            (job_id,),
        )
        self.store.db.commit()

        class FirstBatchOnly(SummaryLLM):
            async def acomplete_structured(inner, **kwargs):
                inner.fail = inner.calls > 0
                return await super().acomplete_structured(**kwargs)

        llm = FirstBatchOnly()
        await memory.refresh_ai(SimpleNamespace(llm=llm), "g", force=True)
        job = self.store.get_compaction_job("g")
        self.assertEqual(llm.calls, 2)
        self.assertEqual(self.store.memory_payload("g")["last_history_id"], 20)
        self.assertEqual(job["from_history_id"], 20)
        self.assertEqual(job["attempt"], 1)
        self.assertTrue(memory.needs_refresh("g", now=job["next_retry_at"] + 1))
        with patch("time.time", return_value=job["next_retry_at"] + 1):
            await memory.refresh_ai(SimpleNamespace(llm=SummaryLLM()), "g")
        self.assertEqual(self.store.memory_payload("g")["last_history_id"], 21)
        self.assertIsNone(self.store.get_compaction_job("g"))

    async def test_overall_timeout_cancels_wrapper_and_preserves_cursor(self):
        memory = GroupMemory(self.store)
        self.record(memory, 1)
        cancelled = asyncio.Event()

        class SlowLLM:
            async def acomplete_structured(inner, **kwargs):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        with patch("smart_group_qq.memory.SUMMARY_TIMEOUT_SECONDS", 0.01):
            await asyncio.wait_for(memory.refresh_ai(SimpleNamespace(llm=SlowLLM()), "g", force=True), 1)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.store.memory_payload("g")["last_history_id"], 0)
        self.assertEqual(self.store.get_compaction_job("g")["error"], "TimeoutError")
        self.assertEqual(self.store.get_compaction_job("g")["attempt"], 1)

    async def test_error_metadata_contains_type_only(self):
        memory = GroupMemory(self.store)
        self.record(memory, 1)

        class BrokenLLM:
            async def acomplete_structured(inner, **kwargs):
                raise ValueError("private response and secret token")

        await memory.refresh_ai(SimpleNamespace(llm=BrokenLLM()), "g", force=True)
        self.assertEqual(self.store.get_compaction_job("g")["error"], "ValueError")

    async def test_summary_budget_timeout_and_lease_cover_bounded_batches(self):
        memory = GroupMemory(self.store, compaction_batch_messages=20, max_compaction_batches=4)
        self.record(memory, 1, "untrusted-chat-marker")

        class InspectLLM(SummaryLLM):
            async def acomplete_structured(inner, **kwargs):
                self.assertNotIn("max_tokens", kwargs)
                self.assertEqual(kwargs["timeout"], 300)
                self.assertIn("不可信数据", kwargs["system_prompt"])
                self.assertNotIn("untrusted-chat-marker", kwargs["system_prompt"])
                return await super().acomplete_structured(**kwargs)

        with patch.object(self.store, "claim_compaction_job", wraps=self.store.claim_compaction_job) as claim:
            await memory.refresh_ai(SimpleNamespace(llm=InspectLLM()), "g", force=True)
        self.assertEqual(claim.call_args.kwargs["lease_seconds"], 1230)
        self.assertIsNone(self.store.get_compaction_job("g"))

    async def test_first_long_message_cannot_stall_small_summary_budget(self):
        memory = GroupMemory(self.store, summary_input_char_budget=1000)
        self.record(memory, 1, "长" * 1800)
        source, cursor, _ = memory._summary_source("g")
        self.assertTrue(source)
        self.assertLessEqual(len(source), 1000)
        self.assertEqual(cursor, 1)
        await memory.refresh_ai(SimpleNamespace(llm=SummaryLLM()), "g", force=True)
        self.assertEqual(self.store.memory_payload("g")["last_history_id"], 1)

    def test_active_job_respects_lease_and_backoff_despite_new_traffic(self):
        memory = GroupMemory(self.store, compact_after_messages=3)
        self.record(memory, 4)
        job = self.store.enqueue_compaction_job("g", now=100)
        self.store.claim_compaction_job(job, now=100, lease_seconds=20)
        self.assertFalse(memory.needs_refresh("g", now=119))
        self.assertTrue(memory.needs_refresh("g", now=121))
        self.store.finish_compaction_job(job, False, now=121, retry_delay=30)
        self.assertFalse(memory.needs_refresh("g", now=150))
        self.assertTrue(memory.needs_refresh("g", now=151))

    async def test_failure_does_not_replace_last_good_memory_with_raw_tail(self):
        memory = GroupMemory(self.store, summary_chars=200)
        self.store.set_memory("g", "不能丢掉的决定", structured={"summary": "不能丢掉的决定"},
                              model="valid-model", version=7, updated_at=100)
        before = self.store.memory_payload("g")
        self.record(memory, 1, "新的闲聊" * 100)
        await memory.refresh_ai(SimpleNamespace(llm=SummaryLLM(fail=True)), "g", force=True)
        self.assertEqual(self.store.memory_payload("g"), before)
        self.assertEqual(self.store.get_compaction_job("g")["status"], "failed")

    def test_pruned_newer_evidence_still_protects_member_fact(self):
        self.store.set_member_consent("g", "m", "opted_in")
        ref = self.store.member_ref_for("g", "m")
        old = self.store.append_history("g", role="user", member_id=ref, text="旧项目", created_at=10)
        new = self.store.append_history("g", role="user", member_id=ref, text="新项目", created_at=20,
                                        source_kind="ambient")
        self.store.add_member_memory_fact("g", "m", "project", "项目", "新项目", source_history_id=new)
        self.store.set_memory("g", "已摘要", last_history_id=new)
        self.store.purge_history_by_source("ambient", 5, now=30)
        self.store.add_member_memory_fact("g", "m", "project", "项目", "旧项目", source_history_id=old)
        self.assertEqual(self.store.list_member_memory_facts("g", "m")[0]["fact_value"], "新项目")


if __name__ == "__main__":
    unittest.main()
