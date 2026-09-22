import asyncio
import json
import unittest
from types import SimpleNamespace

from test_smart_group_qq import FakeContext, ParticipationLLM, ObserverAdapter, observer_payload, event
from smart_group_qq import build_handler, qq_observer, _configure_adapter
from smart_group_qq.response import ReplyRegistry, ReplyRequest
from smart_group_qq.store import Store


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sharing_is_classified_and_budget_does_not_block_name_calls(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        ctx = FakeContext({"ambient": {"participation": {
            "enabled": True, "debounce_seconds": 0, "max_wait_seconds": 0,
            "wake_words": ["机器人"],
        }}})
        ctx.llm = ParticipationLLM({"reply": True, "confidence": 0.9, "engaged": False})
        handler = build_handler(ctx, store)
        adapter = ObserverAdapter()
        for index, text in enumerate(("今天终于把部署搞定了", "这游戏的新地图做得真不错", "今天买的西瓜真的好甜", "机器人过来看看")):
            await qq_observer._observe_message(adapter, observer_payload(str(index), text=text), handler.observe_nonmention)
            await asyncio.sleep(0)
        self.assertEqual([item[1]["id"] for item in adapter.dispatched], ["0", "1", "3"])
        self.assertEqual(len(ctx.llm.calls), 2)

    async def test_retreat_requires_actual_engagement_and_allows_other_members(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        ctx = FakeContext({"ambient": {"participation": {
            "enabled": True, "debounce_seconds": 0, "max_wait_seconds": 0, "wake_words": [],
        }}})
        ctx.llm = ParticipationLLM({"reply": True, "confidence": 0.9, "engaged": False})
        handler = build_handler(ctx, store)
        handler.attention.record_success("group-a", "member-a", "Windows 部署失败", direct=False)
        handler.attention.record_success("group-a", "member-a", "Windows 部署失败", direct=False)
        adapter = ObserverAdapter()
        for index, engaged in enumerate((False, True)):
            ctx.llm.parsed["engaged"] = engaged
            await qq_observer._observe_message(adapter, observer_payload(
                f"topic-{index}", text="Windows 部署失败", member="member-b",
            ), handler.observe_nonmention)
            await asyncio.sleep(0)
        self.assertEqual([item[1]["id"] for item in adapter.dispatched], ["topic-1"])
        self.assertEqual(handler.attention.get("group-a").unanswered, 0)

    def make_delivery(self, *, fail_at=None, cancel_at=None):
        delivered = []
        registry = ReplyRegistry(lambda _: 0, lambda *a, **k: None, lambda r, result: delivered.append(r.pending_message))
        record = ReplyRequest("a" * 32, "g", "m", "anchor", 0, "nonmention", ("anchor",), "分享", False)
        registry.register(record)
        content = registry.transform(json.dumps({"action": "reply", "message": "终于跑通了。可以歇口气了！你呢？"}), "[群对话标记:" + record.request_ref + "]")

        class Adapter:
            def __init__(self):
                self.calls = []

            async def send(self, chat_id, content, reply_to=None, metadata=None):
                self.calls.append((chat_id, content, reply_to))
                count = len(self.calls)
                if count == cancel_at:
                    registry.cancel_group("g")
                return SimpleNamespace(success=count != fail_at, message_id=f"bubble-{count}")

        adapter = Adapter()
        _configure_adapter(adapter, registry)
        return adapter, registry, record, content, delivered

    async def test_bubbles_keep_anchor_and_retry_only_unsent_text(self):
        adapter, registry, record, content, delivered = self.make_delivery(fail_at=2)
        self.assertFalse((await adapter.send("g", content, reply_to="anchor")).success)
        self.assertEqual(delivered, [])
        self.assertTrue((await adapter.send("g", content, reply_to="anchor")).success)
        self.assertEqual(adapter.calls, [
            ("g", "终于跑通了。", "anchor"),
            ("g", "可以歇口气了！", "anchor"),
            ("g", "可以歇口气了！", "anchor"),
            ("g", "你呢？", "anchor"),
        ])
        self.assertEqual(delivered, ["终于跑通了。\n可以歇口气了！\n你呢？"])
        self.assertEqual(record.sent_message_ids, ["bubble-1", "bubble-3", "bubble-4"])
        self.assertEqual(registry.size, 0)

    async def test_cancellation_stops_remaining_bubbles(self):
        adapter, registry, record, content, delivered = self.make_delivery(cancel_at=1)
        self.assertFalse((await adapter.send("g", content, reply_to="anchor")).success)
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(delivered, [])

    async def test_real_handler_records_one_turn_and_all_bubble_references(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        handler = build_handler(FakeContext(), store)

        class Adapter:
            def __init__(self):
                self.calls = []

            async def send(self, chat_id, content, reply_to=None):
                self.calls.append(content)
                return SimpleNamespace(success=True, message_id=f"r{len(self.calls)}")

        adapter = Adapter()
        gateway = SimpleNamespace(adapters={"qqbot": adapter})
        rewritten = handler(event("<@bot> 今天终于部署好了", "ask"), gateway)
        content = handler.transform_llm_output(
            response_text=json.dumps({"action": "reply", "message": "终于跑通了。可以歇口气了！"}),
            user_message=rewritten["text"], platform="qqbot",
        )
        await adapter.send("group-a", content, reply_to="ask")
        self.assertEqual(len(adapter.calls), 2)
        self.assertEqual(len([row for row in store.get_history("group-a") if row["role"] == "assistant"]), 1)
        self.assertTrue(handler.attention.has_reply_id("group-a", "r1"))
        self.assertTrue(handler.attention.has_reply_id("group-a", "r2"))


if __name__ == "__main__":
    unittest.main()
