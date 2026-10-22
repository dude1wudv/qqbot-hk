import asyncio
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))
from smart_group_qq import build_handler, qq_observer
from smart_group_qq.commands import model_alias_rewrite
from smart_group_qq.interaction import resolve_interaction
from smart_group_qq.response import ReplyRegistry, ReplyRequest, parse_reply_envelope
from smart_group_qq.stickers import reply_category
from smart_group_qq.store import Store
from test_smart_group_qq import FakeContext, FakeAdapter, ParticipationLLM, ObserverAdapter, observer_payload, event


class ReplyReactionTests(unittest.TestCase):
    def test_ambiguous_feedback_requires_clarification(self):
        for text in ("别叫我这个称呼", "别叫我那个称呼", "别叫我这个", "对我别用这个梗"):
            self.assertTrue(resolve_interaction(text).error, text)

    def test_optional_reaction_keeps_text_and_respects_no_sticker_requests(self):
        for value, expected in (("鼓励", "鼓励"), (None, None), ("unknown", None), ({"bad": 1}, None)):
            action, message, reaction, explicit = parse_reply_envelope(json.dumps(
                {"action": "reply", "message": "辛苦啦，慢慢来。", "reaction": value}))
            self.assertEqual((action, message, reaction, explicit), ("reply", "辛苦啦，慢慢来。", expected, True))
        self.assertEqual(reply_category("今天好累", "先歇一会儿。", reaction="鼓励", explicit=True), "鼓励")
        self.assertIsNone(reply_category("不要配图", "哈哈", reaction="开心", explicit=True))
        self.assertIsNone(reply_category("部署出错了", "加油", reaction="鼓励", explicit=True))
        self.assertIsNone(reply_category("好玩", "哈哈", reaction=None, explicit=True))
        self.assertEqual(reply_category("好玩", "哈哈"), "开心")

    def test_finalizer_retains_intent_without_sending_before_receipt(self):
        delivered = []
        registry = ReplyRegistry(lambda _: 0, lambda *a, **kw: None, lambda *args: delivered.append(args))
        record = ReplyRequest("a" * 32, "g", "m", "now", 0, "official", ("now",), "今天好累", True)
        registry.register(record)
        self.assertEqual(registry.transform('{"action":"reply","message":"先歇一会儿。","reaction":"鼓励"}',
                                            "[群对话标记:" + "a" * 32 + "]"), "先歇一会儿。")
        self.assertEqual(record.reaction, "鼓励")
        self.assertTrue(record.reaction_explicit)
        self.assertEqual(delivered, [])


class SocialIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:", member_secret="synthetic-upgrade-test")
        self.ctx = FakeContext({"ambient": {"participation": {"enabled": True,
            "cooldown_seconds": 0, "debounce_seconds": 0, "max_wait_seconds": 0}}})
        self.adapter = FakeAdapter()
        self.gateway = SimpleNamespace(adapters={"qqbot": self.adapter},
            _is_user_authorized_for_source=lambda source: True, _check_slash_access=lambda source, name: None,
            _handle_reset_command=AsyncMock())

    def tearDown(self):
        self.store.close()

    def seed_topics(self, handler):
        handler.attention.record_success("group-a", "a", "周末去爬山", direct=True, message_id="reply-hike")
        hike = handler.attention.topic_context("group-a")[0]["id"]
        handler.attention.record_success("group-a", "b", "服务器发布失败", direct=True, message_id="reply-server")
        return hike

    async def test_classifier_focus_is_passed_once_and_never_changes_send_anchor(self):
        handler = build_handler(self.ctx, self.store)
        hike = self.seed_topics(handler)
        self.ctx.llm = ParticipationLLM({"reply": True, "confidence": .99, "engaged": True,
            "topic_id": hike, "focus_message_ids": ["new-hike", "forged-anchor"], "mode_hint": "casual"})
        observer = ObserverAdapter()
        await qq_observer._observe_message(observer,
            observer_payload("new-hike", text="周末爬山需要带什么", member="member-c"), handler.observe_nonmention)
        await asyncio.sleep(0)
        self.assertEqual(len(self.ctx.llm.calls), 1)
        payload = observer.dispatched[0][1]
        self.assertEqual(payload["id"], "new-hike")
        self.assertEqual(payload["_smart_group_qq_attention"]["focus_message_ids"], ["new-hike"])
        current = event(payload["content"], "new-hike", member="member-c")
        current.source.platform = "qqbot"
        current.raw_message = payload
        rewritten = await handler(current, self.gateway)
        record = handler.response_registry.lookup("group-a", "new-hike")
        self.assertEqual((record.topic_id, record.mode_hint, record.message_id), (hike, "casual", "new-hike"))
        self.assertIn("当前话题资料：周末去爬山", rewritten["text"])
        self.assertNotIn("当前话题资料：服务器", rewritten["text"])
        self.assertIn("reaction", rewritten["text"])

    async def test_official_reference_selects_older_topic_without_classifier(self):
        handler = build_handler(self.ctx, self.store)
        hike = self.seed_topics(handler)
        current = event("那几点出发？", "current-question")
        current.source.platform = "qqbot"
        current.raw_message = {"message_type": 103, "msg_elements": [{"msg_elements": [{"message_id": "reply-hike"}]}]}
        self.ctx.llm = ParticipationLLM(error=AssertionError("Direct references skip classification"))
        await handler(current, self.gateway)
        record = handler.response_registry.lookup("group-a", "current-question")
        self.assertEqual(record.topic_id, hike)
        self.assertEqual(record.message_id, "current-question")
        self.assertEqual(self.ctx.llm.calls, [])

    async def test_batch_focus_changes_the_actual_reply_context(self):
        handler = build_handler(self.ctx, self.store)
        handler.memory.record("group-a", "member-a", "想聊周末登山", "first", source_kind="ambient")
        handler.memory.record("group-a", "member-a", "再聊今天咖啡", "last", source_kind="ambient")
        prompts = []
        for focus in ("first", "last"):
            current = event("想聊周末登山\n再聊今天咖啡", "last")
            current.source.platform = "qqbot"
            current.raw_message = {"_smart_group_qq_nonmention": True,
                "_smart_group_qq_batch_ids": ["first", "last"],
                "_smart_group_qq_attention": {"reply": True, "confidence": 1, "engaged": False,
                    "focus_message_ids": [focus], "mode_hint": "casual"}}
            prompts.append((await handler(current, self.gateway))["text"].split("[本次接话范围]")[-1])
        self.assertIn("想聊周末登山", prompts[0])
        self.assertNotIn("再聊今天咖啡", prompts[0])
        self.assertIn("再聊今天咖啡", prompts[1])
        self.assertNotIn("想聊周末登山", prompts[1])

    async def test_media_receipt_does_not_consume_pending_text_or_send_reaction(self):
        self.adapter.send_image_file = AsyncMock(return_value=SimpleNamespace(success=True, message_id="image-1"))
        handler = build_handler(self.ctx, self.store)
        handler.stickers.queue_reply = Mock(return_value=False)
        current = event("今天好累", "needs-text")
        current.source.platform = "qqbot"
        rewritten = await handler(current, self.gateway)
        text = handler.response_registry.transform(
            '{"action":"reply","message":"先歇一会儿。","reaction":"鼓励"}', rewritten["text"])
        await self.adapter.send_image_file("group-a", "synthetic.png", reply_to="needs-text")
        handler.stickers.queue_reply.assert_not_called()
        record = handler.response_registry.lookup("group-a", "needs-text")
        self.assertIsNotNone(record)
        self.assertFalse(record.consumed)
        await self.adapter.send("group-a", text, reply_to="needs-text")
        handler.stickers.queue_reply.assert_called_once()

    def test_expired_topic_rebinds_every_successful_reply_chunk(self):
        handler = build_handler(self.ctx, self.store)
        old = handler.attention.record_success("group-a", "a", "出门爬山", direct=True, now=100)
        actual = handler.attention.record_success("group-a", "a", "出门爬山", direct=True,
                                                 topic_id=old, message_id="last-bubble", now=221)
        handler.attention.remember_reply("group-a", "first-bubble", topic_id=actual, now=221)
        self.assertNotEqual(actual, old)
        for bubble in ("first-bubble", "last-bubble"):
            self.assertEqual(handler.attention.direct_decision("group-a", "a", "几点？", message_id="now",
                reference_id=bubble, now=222).topic_id, actual)

    async def test_auto_command_is_local_idempotent_and_does_not_reset_history(self):
        routing = ModuleType("qqbot_model_routing")
        routing.clear_manual_model = AsyncMock()
        routing.ModelRoutingBusy = type("ModelRoutingBusy", (RuntimeError,), {})
        handler = build_handler(self.ctx, self.store)
        with patch.dict(sys.modules, {"qqbot_model_routing": routing}):
            current = event("/model auto", "auto-1")
            current.source.platform = "qqbot"
            await handler(current, self.gateway)
            await handler(current, self.gateway)
            await asyncio.sleep(0)
        routing.clear_manual_model.assert_awaited_once_with(self.gateway, current.source)
        self.gateway._handle_reset_command.assert_not_awaited()
        self.assertTrue(any("已恢复自动模型" in item[1] for item in self.adapter.sent))
        self.assertEqual(model_alias_rewrite("/fast"), "/model deepseek/deepseek-v4.1-flash-fast --session")
        self.assertEqual(model_alias_rewrite("/自动模型"), "/model auto")


if __name__ == "__main__":
    unittest.main()
