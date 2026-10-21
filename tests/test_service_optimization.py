import asyncio
from pathlib import Path
import sys
import tempfile
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))
sys.path.insert(0, str(ROOT / "tests"))
from smart_group_qq import build_handler, _configure_adapter
from smart_group_qq.store import Store
from smart_group_qq.delivery import LocalReplies, send_failure_state
from smart_group_qq.response import ReplyRegistry
from smart_group_qq.private_scope import legacy_evidence
from smart_group_qq import llm_policy
from test_smart_group_qq import FakeContext, FakeAdapter, event


SUMMARY = dict(summary="已确定项目方向", topics=["项目"], facts=["项目名为星河"],
               decisions=["使用 Python"], todos=[], open_questions=[], participants=[])


class ServiceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:", member_secret="synthetic-scope-secret")
        self.ctx = FakeContext()
        self.ctx.llm = SimpleNamespace(acomplete_structured=AsyncMock(return_value=SimpleNamespace(parsed=SUMMARY)))
        self.adapter = FakeAdapter()
        self.gateway = SimpleNamespace(adapters={"qqbot": self.adapter},
            _is_user_authorized_for_source=lambda _: True, _check_slash_access=lambda *_: None,
            _handle_reset_command=AsyncMock(), _is_session_run_current=lambda *_: True)
        self.handler = build_handler(self.ctx, self.store)

    async def asyncTearDown(self):
        for task in tuple(self.handler.local_replies.tasks.values()):
            task.cancel()
        await asyncio.gather(*tuple(self.handler.local_replies.tasks.values()), return_exceptions=True)
        await self.handler.scheduler.close()
        self.store.close()

    async def ask(self, text, ident, *, private=False):
        result = await self.handler(event(text, ident, platform="qqbot", chat_type="dm" if private else "group",
                                          group="scope-a", member="member-a"), self.gateway)
        await asyncio.gather(*tuple(self.handler.local_replies.tasks.values()))
        return result

    async def test_private_memory_recall_correction_and_forget(self):
        await self.ask("/记住我：项目=星河探针", "remember", private=True)
        result = await self.ask("我的项目是什么？", "recall", private=True)
        self.assertIn("星河探针", result["text"])
        self.assertNotIn("星河探针", str(self.store.list_member_memory_facts("scope-a", "member-a")))
        await self.ask("/纠正记忆：项目=月海探针", "correct", private=True)
        result = await self.ask("我的项目是什么？", "new-recall", private=True)
        self.assertIn("月海探针", result["text"])
        self.assertNotIn("星河探针", result["text"])
        await self.ask("/忘记我", "forget", private=True)
        result = await self.ask("我的项目是什么？", "forgotten", private=True)
        self.assertNotIn("月海探针", result["text"])
        self.assertEqual(self.store.list_member_memory_facts("dm:scope-a", "member-a"), [])

    async def test_summary_queries_reuse_completed_result(self):
        self.handler.memory.record("scope-a", "member-a", "项目确定使用 Python", "source")
        await self.ask("/summary", "summary-one")
        self.assertEqual(self.ctx.llm.acomplete_structured.await_count, 1)
        self.assertEqual(self.handler.memory.status("scope-a")["pending_count"], 0)
        await self.ask("/summary", "summary-two")
        await self.ask("/summary status", "summary-status")
        await self.ask("/summary retry", "summary-retry-caught-up")
        self.assertEqual(self.ctx.llm.acomplete_structured.await_count, 1)
        self.assertIn("已更新", self.adapter.sent[-1][1])
        kwargs = self.ctx.llm.acomplete_structured.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 300)
        self.assertNotIn("max_tokens", kwargs)

    async def test_summary_retry_is_one_attempt_when_still_failing(self):
        self.handler.memory.record("scope-a", "member-a", "项目决策", "source")
        self.ctx.llm.acomplete_structured.side_effect = TimeoutError("synthetic")
        for _ in range(3):
            await self.handler.memory.refresh_ai(self.ctx, "scope-a", force=True, max_batches=1)
        self.assertTrue(self.handler.memory.status("scope-a")["blocked"])
        await self.ask("/summary", "blocked-query")
        self.assertEqual(self.ctx.llm.acomplete_structured.await_count, 3)
        await self.ask("/summary retry", "explicit-retry")
        self.assertEqual(self.ctx.llm.acomplete_structured.await_count, 4)
        self.assertTrue(self.handler.memory.status("scope-a")["blocked"])
        self.assertIn("本次整理未成功", self.adapter.sent[-1][1])

    async def test_first_query_joins_background_summary_and_only_one_final_reply(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def complete(**kwargs):
            entered.set()
            await release.wait()
            return SimpleNamespace(parsed=SUMMARY)
        self.ctx.llm.acomplete_structured.side_effect = complete
        self.handler.memory.record("scope-a", "member-a", "项目确定使用 Python", "source")
        background, _ = self.handler.scheduler.summary("scope-a", retry=True)
        await asyncio.wait_for(entered.wait(), 1)
        for ident in ("first-query", "later-query"):
            await self.handler(event("/summary", ident, group="scope-a", member="member-a"), self.gateway)
        await asyncio.sleep(0)
        self.assertTrue(any("正在整理" in item[1] for item in self.adapter.sent))
        self.assertEqual(self.ctx.llm.acomplete_structured.await_count, 1)
        release.set()
        await background
        await asyncio.gather(*tuple(self.handler.local_replies.tasks.values()))
        self.assertEqual(sum("【当前摘要】" in item[1] for item in self.adapter.sent), 1)

    async def test_slow_summary_does_not_block_another_scope(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def complete(**kwargs):
            if not entered.is_set():
                entered.set()
                await release.wait()
            return SimpleNamespace(parsed=SUMMARY)
        self.ctx.llm.acomplete_structured.side_effect = complete
        for scope in ("slow", "fast"):
            self.handler.memory.record(scope, "member-a", "项目有明确决定", scope)
        slow, _ = self.handler.scheduler.summary("slow", explicit=True)
        await asyncio.wait_for(entered.wait(), 1)
        fast, _ = self.handler.scheduler.summary("fast", explicit=True)
        result = await asyncio.wait_for(asyncio.shield(fast), 1)
        self.assertEqual(result["pending_count"], 0)
        self.assertFalse(slow.done())
        release.set()
        await slow

    async def test_local_command_known_failure_resumes_without_reexecuting(self):
        class RefusedOnce(FakeAdapter):
            async def send(self, chat_id, content, reply_to=None):
                self.sent.append((chat_id, content, reply_to))
                return SimpleNamespace(success=len(self.sent) > 1,
                                       delivery_status="failed", retryable=True)
        adapter = RefusedOnce()
        self.gateway.adapters["qqbot"] = adapter
        await self.ask("/记住我：项目=星河探针", "same-event")
        before = self.store.list_member_memory_facts("scope-a", "member-a")
        await self.ask("/记住我：项目=不应执行", "same-event")
        after = self.store.list_member_memory_facts("scope-a", "member-a")
        self.assertEqual(before, after)
        self.assertEqual(len(adapter.sent), 2)
        self.assertEqual(adapter.sent[0][1], adapter.sent[1][1])
        self.assertEqual(self.store.get_command_receipt("scope-a", "same-event")["delivery_status"], "sent")

    async def test_request_policy_cancellation_reaches_transport(self):
        cancelled = asyncio.Event()
        async def request(**kwargs):
            self.assertEqual(kwargs["timeout"], 0.01)
            self.assertNotIn("max_tokens", kwargs)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with patch.object(llm_policy, "REQUEST_TIMEOUT_SECONDS", 0.01):
            with self.assertRaises(TimeoutError):
                await llm_policy.complete(request, max_tokens=1000)
        self.assertTrue(cancelled.is_set())


class IdentityAndDeliveryTests(unittest.TestCase):
    def test_readonly_provenance_includes_ended_conflicting_groups(self):
        with tempfile.TemporaryDirectory() as directory:
            db = sqlite3.connect(Path(directory) / "state.db")
            with db:
                db.execute("CREATE TABLE sessions(source,chat_id,user_id,chat_type,origin_json,ended_at)")
                db.executemany("INSERT INTO sessions VALUES(?,?,?,?,?,?)", [
                    ("qqbot", "same-id", "owner", "dm", "{}", None),
                    ("qqbot", "same-id", "owner", "group", "{}", 1)])
            db.close()
            gateway = SimpleNamespace(session_store=SimpleNamespace(_routing_home=directory))
            evidence = legacy_evidence(gateway, "same-id")
            self.assertEqual({row["chat_type"] for row in evidence["sessions"]}, {"dm", "group"})

    def test_retryable_network_failure_remains_ambiguous(self):
        self.assertEqual(send_failure_state(SimpleNamespace(retryable=True, error="ReadTimeout")), "unknown")
        self.assertEqual(send_failure_state(SimpleNamespace(retryable=True, error="QQ Bot API error [429] rate limit")), "failed")
        self.assertEqual(send_failure_state(SimpleNamespace(retryable=True, error="Not connected")), "failed")


if __name__ == "__main__":
    unittest.main()
