import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

from smart_group_qq import build_handler, qq_observer
from smart_group_qq.policy import PolicyEngine, compile_rule_list, semantic_moderation
from smart_group_qq.store import Store


class FakeContext:
    plugin_id = "smart_group_qq"
    def __init__(self, values=None):
        self.values = values or {}
    def get_config(self, key, default=None):
        return self.values.get(key, default)


class FakeAdapter:
    def __init__(self, success=True):
        self.success = success
        self.sent = []
    async def send(self, chat_id, content, reply_to=None):
        self.sent.append((chat_id, content, reply_to))
        return SimpleNamespace(success=self.success)

class ParticipationLLM:
    def __init__(self, parsed=None, error=None, started=None, release=None):
        self.parsed = parsed
        self.error = error
        self.started = started
        self.release = release
        self.calls = []

    async def acomplete_structured(self, **kwargs):
        self.calls.append(kwargs)
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return SimpleNamespace(parsed=self.parsed)


class ObserverAdapter:
    def __init__(self, *, allowed=True, timestamp=None):
        self.allowed = allowed
        self.timestamp = timestamp or datetime.now(timezone.utc)
        self.dispatched = []

    def _is_group_allowed(self, group_id, member_id):
        return self.allowed

    def _parse_qq_timestamp(self, raw):
        return self.timestamp

    async def _on_message(self, event_type, data):
        self.dispatched.append((event_type, data))


def observer_payload(message_id, *, group="group-a", text="请帮我查一下", member="member-a", mentions=None):
    payload = {
        "op": 0,
        "t": "GROUP_MESSAGE_CREATE",
        "d": {
            "id": message_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "content": text,
            "group_openid": group,
            "author": {"member_openid": member},
            "attachments": [],
        },
    }
    if mentions is not None:
        payload["d"]["mentions"] = mentions
    return payload


def event(text, message_id="msg-1", *, platform="qqbot", chat_type="group", group="group-a", member="member-a"):
    source = SimpleNamespace(platform=SimpleNamespace(value=platform), chat_type=chat_type, chat_id=group, user_id=member, chat_name="群")
    return SimpleNamespace(text=text, message_id=message_id, source=source)


class PluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.adapter = FakeAdapter()
        self.gateway = SimpleNamespace(adapters={}, _session_key_for_source=lambda source: "qqbot:group:" + source.chat_id)
        self.source_platform = "qqbot"

    def tearDown(self):
        self.store.close()

    def make_event(self, text, message_id="msg-1"):
        source = SimpleNamespace(platform=self.source_platform, chat_type="group", chat_id="group-a", user_id="member-a", chat_name="群")
        self.gateway.adapters = {self.source_platform: self.adapter}
        return SimpleNamespace(text=text, message_id=message_id, source=source)

    async def test_non_group_passes_and_normal_message_rewrites(self):
        handler = build_handler(FakeContext(), self.store)
        self.gateway.adapters = {self.source_platform: self.adapter}
        dm = event("hello", platform="qqbot", chat_type="dm")
        self.assertEqual(handler(dm, self.gateway)["action"], "rewrite")
        self.assertTrue(self.adapter._smart_group_qq_formatting)
        self.assertEqual(self.adapter.format_message("**你好**"), "你好")
        result = handler(self.make_event("<@bot> hello"), self.gateway)
        self.assertEqual(result["action"], "rewrite")
        self.assertRegex(result["text"], r"\[群记忆键:[0-9a-f]{12}\]\n")
        self.assertRegex(result["text"], r"\[群成员:m-[0-9a-f]{20}\]: hello\n\n\[群聊最终输出协议\]")
        self.assertNotIn("摘要后新增上下文", result["text"])

    async def test_voice_transcript_keeps_group_policy_and_session_isolation(self):
        handler = build_handler(FakeContext(), self.store)
        voice = self.make_event("[Voice] 项目口令是北斗", "voice-1")
        voice.message_type = "voice"
        result = handler(voice, self.gateway)
        self.assertEqual(result["action"], "rewrite")
        self.assertRegex(result["text"], r"\[群成员:m-[0-9a-f]{20}\]: \[Voice\] 项目口令是北斗")

        other = self.make_event("这个群知道什么？", "other-1")
        other.source.chat_id = "group-b"
        other_result = handler(other, self.gateway)
        self.assertNotIn("项目口令是北斗", other_result["text"])

        dm = event("[Voice] 私聊内容", platform="qqbot", chat_type="dm")
        dm.message_type = "voice"
        self.assertEqual(handler(dm, self.gateway)["action"], "rewrite")

    async def test_static_precedes_keyword_and_is_idempotent(self):
        settings = {
            "moderation": {"static_rules": [{"id": "block", "match": "contains", "pattern": "bad", "notice": "blocked"}]},
            "keyword_replies": [{"id": "reply", "match": "contains", "pattern": "bad", "reply": "keyword"}],
        }
        handler = build_handler(FakeContext(settings), self.store)
        item = self.make_event("bad")
        self.assertEqual(handler(item, self.gateway)["action"], "skip")
        self.assertEqual(handler(item, self.gateway)["reason"], "duplicate")
        await asyncio.sleep(0)
        self.assertEqual(self.adapter.sent, [("group-a", "blocked", "msg-1")])

    async def test_duty_roster_is_local_and_idempotent(self):
        handler = build_handler(FakeContext(), self.store)
        item = self.make_event("<@bot> /值日表", "roster-1")
        expected = "【本周值日表｜2026年9月13日—9月19日】\n孙：轮休"

        with patch("smart_group_qq.duty_roster_text", return_value=expected) as render:
            first = handler(item, self.gateway)
            second = handler(item, self.gateway)
            await asyncio.sleep(0)

        self.assertEqual(first["action"], "skip")
        self.assertEqual(second["reason"], "duplicate")
        self.assertEqual(render.call_count, 2)
        render.assert_called_with()
        self.assertEqual(self.adapter.sent, [("group-a", expected, "roster-1")])

    async def test_model_aliases_delegate_to_native_session_switch(self):
        handler = build_handler(FakeContext(), self.store)
        gemini = handler(self.make_event("<@bot> / gemini", "model-gemini"), self.gateway)
        deepseek = handler(self.make_event("<@bot> /deepseek", "model-deepseek"), self.gateway)
        self.assertEqual(gemini, {
            "action": "rewrite",
            "text": "/model gemini-3.8-flash-high --session",
        })
        self.assertEqual(deepseek, {
            "action": "rewrite",
            "text": "/model deepseek/deepseek-v4.1-flash --session",
        })
        self.assertEqual(self.store.get_history("group-a"), [])
        self.assertEqual(self.adapter.sent, [])

    async def test_reasoning_aliases_delegate_to_native_session_switch(self):
        handler = build_handler(FakeContext(), self.store)
        for effort in ("low", "medium", "high", "max"):
            with self.subTest(effort=effort):
                result = handler(
                    self.make_event(f"<@bot> /{effort}", f"reasoning-{effort}"),
                    self.gateway,
                )
                self.assertEqual(result, {
                    "action": "rewrite",
                    "text": f"/reasoning {effort} --session",
                })
        self.assertEqual(self.store.get_history("group-a"), [])
        self.assertEqual(self.adapter.sent, [])

    async def test_group_native_compress_and_new_pass_through_without_context_wrap(self):
        """Regression: @bot /compress must reach Hermes, not durable group context.

        When Hermes latches the ineffective-compression breaker it tells users to
        run /compress or /new. Those must not be rewritten into [本群私有上下文]
        payloads, or recovery loops forever and keeps stuffing old transcript.
        """
        handler = build_handler(FakeContext(), self.store)
        cases = {
            "<@bot> /compress": "/compress",
            "<@bot> /new": "/new",
            "／compress": "/compress",
            "<@bot> /compress --force": "/compress --force",
            "<@bot> /commands": "/commands",
        }
        for index, (raw, expected) in enumerate(cases.items()):
            with self.subTest(raw=raw):
                result = handler(self.make_event(raw, f"native-compress-{index}"), self.gateway)
                self.assertEqual(result, {"action": "rewrite", "text": expected})
                self.assertNotIn("本群私有上下文", result["text"])
                self.assertNotIn("群记忆键", result["text"])
        self.assertEqual(self.store.get_history("group-a"), [])
        self.assertEqual(self.adapter.sent, [])

    async def test_group_unknown_native_commands_are_not_forwarded(self):
        handler = build_handler(FakeContext(), self.store)
        for index, raw in enumerate(("<@bot> /update", "<@bot> /platform pause", "<@bot> /reload-mcp")):
            with self.subTest(raw=raw):
                result = handler(self.make_event(raw, f"unknown-native-{index}"), self.gateway)
                self.assertEqual(result["action"], "rewrite")
                self.assertNotEqual(result["text"], raw.removeprefix("<@bot> "))
                self.assertIn("群记忆键", result["text"])
        self.assertEqual(self.adapter.sent, [])

    async def test_private_aliases_delegate_to_native_session_commands(self):

        handler = build_handler(FakeContext(), self.store)
        self.gateway.adapters = {self.source_platform: self.adapter}
        cases = {
            "/deepseek": "/model deepseek/deepseek-v4.1-flash --session",
            "/gemini": "/model gemini-3.8-flash-high --session",
            "/low": "/reasoning low --session",
            "/medium": "/reasoning medium --session",
            "/high": "/reasoning high --session",
            "/max": "/reasoning max --session",
        }
        for index, (command, rewritten) in enumerate(cases.items()):
            with self.subTest(command=command):
                result = handler(event(
                    command,
                    f"dm-alias-{index}",
                    platform="qqbot",
                    chat_type="dm",
                    group="dm-a",
                ), self.gateway)
                self.assertEqual(result, {"action": "rewrite", "text": rewritten})
        self.assertEqual(self.adapter.sent, [])

    async def test_private_plugin_commands_work_except_duty_roster(self):
        handler = build_handler(FakeContext(), self.store)
        self.gateway.adapters = {self.source_platform: self.adapter}
        for index, command in enumerate(("/help", "/status", "/rules", "/kb", "/我的记忆")):
            with self.subTest(command=command):
                result = handler(event(
                    command,
                    f"dm-command-{index}",
                    platform="qqbot",
                    chat_type="dm",
                    group="dm-a",
                ), self.gateway)
                self.assertEqual(result, {"action": "skip", "reason": "command_or_policy_handled"})
        roster = handler(event(
            "/值日表",
            "dm-roster",
            platform="qqbot",
            chat_type="dm",
            group="dm-a",
        ), self.gateway)
        self.assertEqual(roster, {"action": "skip", "reason": "command_or_policy_handled"})
        self.assertEqual(handler(event(
            "/commands",
            "dm-native",
            platform="qqbot",
            chat_type="dm",
            group="dm-a",
        ), self.gateway), {"action": "rewrite", "text": "/commands"})
        self.assertEqual(handler(event(
            "/update",
            "dm-unknown-native",
            platform="qqbot",
            chat_type="dm",
            group="dm-a",
        ), self.gateway), {"action": "rewrite", "text": "/update"})
        await asyncio.sleep(0)
        replies = [content for _, content, _ in self.adapter.sent]
        self.assertTrue(any("【小栖 · 常驻 AI 角色】" in content for content in replies))
        self.assertIn("该功能仅群聊可用。", replies)

    async def test_keyword_send_failure_fails_open_and_can_retry(self):
        self.adapter.success = False
        settings = {"keyword_replies": [{"id": "hello", "match": "exact", "pattern": "hi", "reply": "hello"}]}
        handler = build_handler(FakeContext(settings), self.store)
        first = handler(self.make_event("hi"), self.gateway)
        self.assertEqual(first["action"], "skip")
        await asyncio.sleep(0.01)
        self.adapter.success = True
        second = handler(self.make_event("hi"), self.gateway)
        self.assertEqual(second["action"], "skip")
        await asyncio.sleep(0.01)
        self.assertEqual(len(self.adapter.sent), 2)

    async def test_audit_does_not_store_message_or_reply_body(self):
        settings = {"keyword_replies": [{"id": "secret-rule", "match": "contains", "pattern": "needle-secret", "reply": "reply-secret"}]}
        handler = build_handler(FakeContext(settings), self.store)
        handler(self.make_event("needle-secret"), self.gateway)
        await asyncio.sleep(0)
        dump = "\n".join(str(tuple(row)) for row in self.store.db.execute("select * from audit_events"))
        self.assertNotIn("needle-secret", dump)
        self.assertNotIn("reply-secret", dump)

    async def test_knowledge_add_and_rag_injection_are_group_scoped(self):
        handler = build_handler(FakeContext({"knowledge": {"allow_group_members_manage": True}}), self.store)
        added = handler(self.make_event("/kb add 发布手册 | 蓝色环境在周五发布", "kb-1"), self.gateway)
        self.assertEqual(added["action"], "skip")
        await asyncio.sleep(0)
        query = handler(self.make_event("周五发布哪个环境？", "ask-1"), self.gateway)
        self.assertIn("知识库:发布手册#", query["text"])
        self.assertIn("蓝色环境在周五发布", query["text"])

        other = self.make_event("周五发布哪个环境？", "ask-2")
        other.source.chat_id = "group-b"
        result = handler(other, self.gateway)
        self.assertNotIn("发布手册", result["text"])

    async def test_static_moderation_precedes_knowledge_commands(self):
        settings = {
            "knowledge": {"allow_group_members_manage": True},
            "moderation": {"static_rules": [{
                "id": "credential", "match": "contains", "pattern": "sk-secret-value", "notice": "blocked",
            }]},
        }
        handler = build_handler(FakeContext(settings), self.store)
        result = handler(self.make_event("/kb add 密钥 | sk-secret-value", "kb-secret"), self.gateway)
        self.assertEqual(result["action"], "skip")
        await asyncio.sleep(0)
        self.assertEqual(handler.knowledge.list_documents("group-a"), [])

    async def test_ambient_message_is_context_only_and_never_sends(self):
        handler = build_handler(FakeContext(), self.store)
        await handler.observe_nonmention({
            "group_id": "group-a", "member_id": "member-b", "message_id": "ambient-1",
            "text": "项目代号是北斗", "timestamp": None, "image_paths": [],
        })
        self.assertEqual(self.adapter.sent, [])
        result = handler(self.make_event("项目代号是什么？", "ask-ambient"), self.gateway)
        self.assertIn("项目代号是北斗", result["text"])
        row = self.store.get_history("group-a", 2)[0]
        self.assertEqual(row["source_kind"], "ambient")

    async def test_disabled_ambient_rejects_async_and_fast_ingestion(self):
        handler = build_handler(FakeContext({"ambient": {"enabled": False}}), self.store)
        await handler.observe_nonmention({
            "group_id": "group-a", "member_id": "member-b", "message_id": "disabled-async",
            "text": "不应保存", "timestamp": None, "image_paths": [],
        })
        payload = {"d": {
            "id": "disabled-fast", "group_openid": "group-a", "content": "也不应保存",
            "author": {"member_openid": "member-b"},
        }}
        self.adapter._is_group_allowed = lambda group_id, member_id: True
        self.assertFalse(handler.observe_nonmention.fast_ingest(self.adapter, payload))
        self.assertEqual(self.store.get_history("group-a"), [])

    async def test_at_uses_only_recent_ambient_window(self):
        handler = build_handler(FakeContext({"ambient": {
            "context_window_messages": 3, "context_window_seconds": 3600,
        }}), self.store)
        for index in range(6):
            await handler.observe_nonmention({
                "group_id": "group-a", "member_id": "member-b", "message_id": f"ambient-{index}",
                "text": f"旁听消息{index}", "timestamp": None, "image_paths": [],
            })
        result = handler(self.make_event("刚才说了什么？", "ask-recent"), self.gateway)
        self.assertNotIn("旁听消息2", result["text"])
        self.assertIn("旁听消息3", result["text"])
        self.assertIn("旁听消息5", result["text"])

    async def test_member_memory_commands_and_prompt_injection(self):
        handler = build_handler(FakeContext({"member_memory": {"auto_extract": False}}), self.store)
        saved = handler(self.make_event("/记住我：职责=后端发布", "profile-save"), self.gateway)
        self.assertEqual(saved["action"], "skip")
        await asyncio.sleep(0)
        query = handler(self.make_event("我负责什么？", "profile-query"), self.gateway)
        self.assertIn("当前成员的本群专属记忆", query["text"])
        self.assertIn("后端发布", query["text"])
        session_store = SimpleNamespace(calls=[], reset_session=lambda *args, **kwargs: session_store.calls.append((args, kwargs)))
        forgotten = handler(
            self.make_event("/忘记我", "profile-forget"), self.gateway, session_store=session_store,
        )
        self.assertEqual(forgotten["action"], "skip")
        await asyncio.sleep(0)
        self.assertEqual(len(session_store.calls), 1)
        after = handler(self.make_event("还记得吗？", "profile-after"), self.gateway)
        self.assertNotIn("后端发布", after["text"])

    async def test_participation_disabled_keeps_ambient_history_without_reply(self):
        ctx = FakeContext({"ambient": {"participation": {"enabled": False}}})
        ctx.llm = ParticipationLLM({"reply": True, "confidence": 1.0})
        handler = build_handler(ctx, self.store)
        adapter = ObserverAdapter()

        await qq_observer._observe_message(
            adapter,
            observer_payload("participation-disabled", text="只是闲聊"),
            handler.observe_nonmention,
        )

        self.assertEqual(ctx.llm.calls, [])
        self.assertEqual(adapter.dispatched, [])
        rows = self.store.get_history("group-a")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_kind"], "ambient")

    async def test_participation_rejection_and_classifier_error_are_silent(self):
        cases = (
            ({"reply": False, "confidence": 1.0}, None),
            ({"reply": True, "confidence": 0.69}, None),
            (None, RuntimeError("classifier unavailable")),
        )
        for index, (parsed, error) in enumerate(cases):
            with self.subTest(index=index):
                ctx = FakeContext({
                    "ambient": {"participation": {
                        "enabled": True, "min_confidence": 0.70, "wake_words": [],
                        "debounce_seconds": 0, "max_wait_seconds": 0,
                    }},
                })
                ctx.llm = ParticipationLLM(parsed, error=error)
                handler = build_handler(ctx, self.store)
                adapter = ObserverAdapter()
                await qq_observer._observe_message(
                    adapter,
                    observer_payload(f"participation-silent-{index}", group=f"silent-{index}"),
                    handler.observe_nonmention,
                )
                await asyncio.sleep(0)
                self.assertEqual(len(ctx.llm.calls), 1)
                self.assertEqual(adapter.dispatched, [])

    async def test_high_confidence_participation_dispatches_native_group_path_once(self):
        ctx = FakeContext({
            "ambient": {"participation": {
            "enabled": True, "cooldown_seconds": 5, "max_age_seconds": 120, "debounce_seconds": 0, "max_wait_seconds": 0,
            }},
        })
        ctx.llm = ParticipationLLM({"reply": True, "confidence": 0.99})
        handler = build_handler(ctx, self.store)
        adapter = ObserverAdapter()
        item = observer_payload("participation-selected", text="请帮我查一下发布状态")

        await qq_observer._observe_message(adapter, item, handler.observe_nonmention)
        await asyncio.sleep(0)
        await qq_observer._observe_message(adapter, item, handler.observe_nonmention)
        await asyncio.sleep(0)

        self.assertEqual(len(ctx.llm.calls), 1)
        self.assertEqual(len(adapter.dispatched), 1)
        self.assertEqual(adapter.dispatched[0][0], "GROUP_AT_MESSAGE_CREATE")
        self.assertTrue(adapter.dispatched[0][1]["_smart_group_qq_nonmention"])
        self.assertEqual(
            self.store.get_history("group-a")[0]["text"],
            "请帮我查一下发布状态",
        )

    async def test_participation_cooldown_is_independent_per_group(self):
        ctx = FakeContext({
            "ambient": {"participation": {
            "enabled": True, "cooldown_seconds": 5, "max_age_seconds": 120, "debounce_seconds": 0, "max_wait_seconds": 0,
            }},
        })
        ctx.llm = ParticipationLLM({"reply": True, "confidence": 0.99})
        handler = build_handler(ctx, self.store)
        adapter = ObserverAdapter()

        await qq_observer._observe_message(
            adapter, observer_payload("cooldown-a1", group="group-a"), handler.observe_nonmention,
        )
        await qq_observer._observe_message(
            adapter, observer_payload("cooldown-a2", group="group-a"), handler.observe_nonmention,
        )
        await qq_observer._observe_message(
            adapter, observer_payload("cooldown-b1", group="group-b"), handler.observe_nonmention,
        )
        await asyncio.sleep(0)

        self.assertEqual(len(ctx.llm.calls), 2)
        self.assertEqual(
            [item[1]["id"] for item in adapter.dispatched],

            ["cooldown-a2", "cooldown-b1"],
        )

    async def test_participation_batches_messages_then_dispatches_once(self):
        ctx = FakeContext({
            "ambient": {"participation": {
                "enabled": True, "cooldown_seconds": 5, "max_age_seconds": 120,
                "debounce_seconds": 0.05, "max_wait_seconds": 0.05, "wake_words": [],
            }},
        })
        ctx.llm = ParticipationLLM({"reply": True, "confidence": 0.99})
        handler = build_handler(ctx, self.store)
        adapter = ObserverAdapter()

        await qq_observer._observe_message(
            adapter,
            observer_payload("batch-1", text="请帮我查一下发布状态"),
            handler.observe_nonmention,
        )
        await qq_observer._observe_message(
            adapter,
            observer_payload("batch-2", text="对还有后续环境问题"),
            handler.observe_nonmention,
        )
        self.assertEqual(ctx.llm.calls, [])
        self.assertEqual(adapter.dispatched, [])
        flush = next(iter(handler.batch_tasks.values()), None)
        self.assertIsNotNone(flush)
        await asyncio.wait_for(flush, timeout=1)
        if flush.cancelled():
            raise AssertionError("batch flush was cancelled")
        if flush.exception():
            raise AssertionError(flush.exception())
        self.assertEqual(len(ctx.llm.calls), 1)
        self.assertEqual([item[1]["id"] for item in adapter.dispatched], ["batch-2"])
    async def test_later_member_batch_waits_for_older_same_group_batch(self):
        ctx = FakeContext({
            "ambient": {"participation": {
                "enabled": True, "cooldown_seconds": 0,
                "debounce_seconds": 0.01, "max_wait_seconds": 0.01,
                "wake_words": ["机器人"],
            }},
        })
        ctx.llm = ParticipationLLM({"reply": True, "confidence": 0.99})
        handler = build_handler(ctx, self.store)
        adapter = ObserverAdapter()
        order = []
        entered = asyncio.Event()
        release = asyncio.Event()

        async def dispatch(_event_type, data):
            order.append(data["id"])
            if data["id"] == "fifo-a":
                entered.set()
                await release.wait()
            adapter.dispatched.append((_event_type, data))

        adapter._on_message = dispatch
        await qq_observer._observe_message(
            adapter,
            observer_payload("fifo-a", member="member-a", text="机器人 请处理 A"),
            handler.observe_nonmention,
        )
        await qq_observer._observe_message(
            adapter,
            observer_payload("fifo-b", member="member-b", text="机器人 请处理 B"),
            handler.observe_nonmention,
        )
        tasks = list(handler.batch_tasks.values())
        await asyncio.wait_for(entered.wait(), timeout=1)
        await asyncio.sleep(0.03)
        self.assertEqual(order, ["fifo-a"])
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=1)
        self.assertEqual(order, ["fifo-a", "fifo-b"])

    async def test_mention_bypasses_participation_cooldown(self):
        ctx = FakeContext({
            "ambient": {"participation": {
                "enabled": True, "cooldown_seconds": 5, "max_age_seconds": 120, "debounce_seconds": 0, "max_wait_seconds": 0,
                "wake_words": ["机器人"],
            }},
        })
        ctx.llm = ParticipationLLM({"reply": True, "confidence": 0.99})
        handler = build_handler(ctx, self.store)
        adapter = ObserverAdapter()

        await qq_observer._observe_message(
            adapter, observer_payload("mention-seed", text="请帮我查一下发布状态"),
            handler.observe_nonmention,
        )
        await asyncio.sleep(0)
        await qq_observer._observe_message(
            adapter,
            observer_payload(
                "mention-during-cd", text="<@bot> 还在冷却也要回",
                mentions=[{"bot": True}],
            ),
            handler.observe_nonmention,
        )
        await asyncio.sleep(0)

        self.assertEqual(len(ctx.llm.calls), 1)
        self.assertEqual(
            [item[1]["id"] for item in adapter.dispatched],
            ["mention-seed", "mention-during-cd"],
        )
        self.assertTrue(adapter.dispatched[1][1]["_smart_group_qq_nonmention"])

    async def test_at_other_person_stays_silent(self):
        ctx = FakeContext({
            "ambient": {"participation": {
                "enabled": True, "cooldown_seconds": 5, "max_age_seconds": 120, "debounce_seconds": 0, "max_wait_seconds": 0,
                "wake_words": ["机器人"],
            }},
        })
        ctx.llm = ParticipationLLM({"reply": True, "confidence": 0.99})
        handler = build_handler(ctx, self.store)
        adapter = ObserverAdapter()

        await qq_observer._observe_message(
            adapter,
            observer_payload("at-other", text="@佛系章鱼哥 好想来你去吗"),
            handler.observe_nonmention,
        )

        self.assertEqual(ctx.llm.calls, [])
        self.assertEqual(adapter.dispatched, [])

    async def test_structured_other_mention_stays_silent(self):
        ctx = FakeContext({
            "ambient": {"participation": {
                "enabled": True, "cooldown_seconds": 5, "max_age_seconds": 120, "debounce_seconds": 0, "max_wait_seconds": 0,
                "wake_words": ["机器人"],
            }},
        })
        ctx.llm = ParticipationLLM({"reply": True, "confidence": 0.99})
        handler = build_handler(ctx, self.store)
        adapter = ObserverAdapter()

        await qq_observer._observe_message(
            adapter,
            observer_payload(
                "structured-other",
                text="<@!member-b> 好想来你去吗",
                mentions=[{"bot": False, "username": "佛系章鱼哥"}],
            ),
            handler.observe_nonmention,
        )

        self.assertEqual(ctx.llm.calls, [])
        self.assertEqual(adapter.dispatched, [])

    async def test_structured_bot_mention_bypasses_participation_cooldown(self):
        ctx = FakeContext({
            "ambient": {"participation": {
                "enabled": True, "cooldown_seconds": 5, "max_age_seconds": 120, "debounce_seconds": 0, "max_wait_seconds": 0,
                "wake_words": ["机器人"],
            }},
        })
        ctx.llm = ParticipationLLM({"reply": True, "confidence": 0.99})
        handler = build_handler(ctx, self.store)
        adapter = ObserverAdapter()

        await qq_observer._observe_message(
            adapter, observer_payload("mention-seed", text="请帮我查一下发布状态"),
            handler.observe_nonmention,
        )
        await asyncio.sleep(0)
        await qq_observer._observe_message(
            adapter,
            observer_payload(
                "bot-mention-during-cd",
                text="<@!bot-id> 还在冷却也要回",
                mentions=[{"bot": True}],
            ),
            handler.observe_nonmention,
        )
        await asyncio.sleep(0)

        self.assertEqual(len(ctx.llm.calls), 1)
        self.assertEqual(
            [item[1]["id"] for item in adapter.dispatched],
            ["mention-seed", "bot-mention-during-cd"],
        )

    async def test_wake_word_messages_remain_deliverable_without_success_cooldown(self):
        ctx = FakeContext({
            "ambient": {"participation": {
                "enabled": True, "cooldown_seconds": 5, "max_age_seconds": 120, "debounce_seconds": 0, "max_wait_seconds": 0,
                "wake_words": ["机器人"],
            }},
        })
        ctx.llm = ParticipationLLM({"reply": True, "confidence": 0.99})
        handler = build_handler(ctx, self.store)
        adapter = ObserverAdapter()

        await qq_observer._observe_message(
            adapter, observer_payload("wake-1", text="机器人 帮我看下"),
            handler.observe_nonmention,
        )
        await asyncio.sleep(0)
        await qq_observer._observe_message(
            adapter, observer_payload("wake-2", text="机器人 再问一次"),
            handler.observe_nonmention,
        )
        await asyncio.sleep(0)

        self.assertEqual(ctx.llm.calls, [])
        self.assertEqual([item[1]["id"] for item in adapter.dispatched], ["wake-1", "wake-2"])

        with patch("smart_group_qq.time.monotonic", return_value=time.monotonic() + 6):
            await qq_observer._observe_message(
                adapter, observer_payload("wake-3", text="机器人 冷却过后"),
                handler.observe_nonmention,
            )
            await asyncio.sleep(0)
        self.assertEqual(
            [item[1]["id"] for item in adapter.dispatched],
            ["wake-1", "wake-2", "wake-3"],
        )

    async def test_classifier_accepts_configured_min_confidence(self):
        ctx = FakeContext({
            "ambient": {"participation": {
                "enabled": True, "cooldown_seconds": 5, "max_age_seconds": 120, "debounce_seconds": 0, "max_wait_seconds": 0,
                "min_confidence": 0.70, "wake_words": [],
            }},
        })
        ctx.llm = ParticipationLLM({"reply": True, "confidence": 0.70})
        handler = build_handler(ctx, self.store)
        adapter = ObserverAdapter()
        await qq_observer._observe_message(
            adapter, observer_payload("conf-70", text="请帮我查一下发布状态"),
            handler.observe_nonmention,
        )
        await asyncio.sleep(0)
        self.assertEqual(len(adapter.dispatched), 1)

        ctx_low = FakeContext({
            "ambient": {"participation": {
                "enabled": True, "cooldown_seconds": 5, "max_age_seconds": 120, "debounce_seconds": 0, "max_wait_seconds": 0,
                "min_confidence": 0.70, "wake_words": [],
            }},
        })
        ctx_low.llm = ParticipationLLM({"reply": True, "confidence": 0.69})
        handler_low = build_handler(ctx_low, self.store)
        adapter_low = ObserverAdapter()
        await qq_observer._observe_message(
            adapter_low,
            observer_payload("conf-69", group="group-b", text="请帮我查一下发布状态"),
            handler_low.observe_nonmention,
        )
        await asyncio.sleep(0)
        self.assertEqual(adapter_low.dispatched, [])

    async def test_private_message_receives_only_private_character_context(self):
        handler = build_handler(FakeContext(), self.store)
        result = handler(
            event("你好", message_id="dm-1", chat_type="dm", group="user-a"),
            self.gateway,
        )
        self.assertEqual(result["action"], "rewrite")
        self.assertIn("[当前私聊消息]", result["text"])
        self.assertNotIn("群聊最终输出协议", result["text"])

    async def test_nonmention_slash_command_with_mention_prefix_is_not_executed(self):
        ctx = FakeContext({"ambient": {"participation": {"enabled": True, "debounce_seconds": 0, "max_wait_seconds": 0}}})
        ctx.llm = ParticipationLLM({"reply": True, "confidence": 1.0})
        handler = build_handler(ctx, self.store)
        adapter = ObserverAdapter()

        await qq_observer._observe_message(
            adapter,
            observer_payload("nonmention-slash", text="<@bot> /reset"),
            handler.observe_nonmention,
        )

        self.assertEqual(ctx.llm.calls, [])
        self.assertEqual(adapter.dispatched, [])
        self.assertEqual(self.store.memory_epoch("group-a"), 0)
        self.assertEqual(self.store.get_history("group-a")[0]["source_kind"], "ambient")


    async def test_non_allowlisted_group_never_classifies_or_dispatches(self):
        ctx = FakeContext({"ambient": {"participation": {"enabled": True, "debounce_seconds": 0, "max_wait_seconds": 0}}})
        ctx.llm = ParticipationLLM({"reply": True, "confidence": 1.0})
        handler = build_handler(ctx, self.store)
        adapter = ObserverAdapter(allowed=False)

        await qq_observer._observe_message(
            adapter,
            observer_payload("non-allowlisted", group="blocked-group"),
            handler.observe_nonmention,
        )

        self.assertEqual(ctx.llm.calls, [])
        self.assertEqual(adapter.dispatched, [])
        self.assertEqual(self.store.get_history("blocked-group"), [])

    async def test_stale_and_replayed_nonmention_never_trigger_participation(self):
        ctx = FakeContext({"ambient": {"participation": {"enabled": True, "debounce_seconds": 0, "max_wait_seconds": 0}}})
        ctx.llm = ParticipationLLM({"reply": True, "confidence": 1.0})
        handler = build_handler(ctx, self.store)
        adapter = ObserverAdapter(timestamp=datetime.now(timezone.utc) - timedelta(seconds=300))
        item = observer_payload("stale-or-replay")

        await qq_observer._observe_message(adapter, item, handler.observe_nonmention)
        adapter.timestamp = datetime.now(timezone.utc)
        await qq_observer._observe_message(adapter, item, handler.observe_nonmention)

        self.assertEqual(ctx.llm.calls, [])
        self.assertEqual(adapter.dispatched, [])
        self.assertEqual(len(self.store.get_history("group-a")), 1)

    async def _assert_slow_participation_cancelled_by(self, invalidation_text):
        started = asyncio.Event()
        release = asyncio.Event()
        ctx = FakeContext({"ambient": {"participation": {"enabled": True, "debounce_seconds": 0, "max_wait_seconds": 0}}})
        ctx.llm = ParticipationLLM(
            {"reply": True, "confidence": 1.0}, started=started, release=release,
        )
        handler = build_handler(ctx, self.store)
        adapter = ObserverAdapter()
        task = asyncio.create_task(qq_observer._observe_message(
            adapter,
            observer_payload("slow-participation", text="请帮我处理这个问题"),
            handler.observe_nonmention,
        ))
        await asyncio.wait_for(started.wait(), timeout=1)

        invalidation = handler(
            self.make_event(invalidation_text, "invalidate-participation"),
            self.gateway,
        )
        self.assertIn(invalidation["action"], {"rewrite", "skip"})
        release.set()
        await asyncio.wait_for(task, timeout=1)
        await asyncio.sleep(0)
        self.assertEqual(adapter.dispatched, [])

    async def test_slow_participation_is_cancelled_by_at_message(self):
        await self._assert_slow_participation_cancelled_by("<@bot> 正在处理")

    async def test_slow_participation_is_cancelled_by_reset(self):
        await self._assert_slow_participation_cancelled_by("<@bot> /reset")

    async def test_post_llm_does_not_record_before_successful_send(self):
        handler = build_handler(FakeContext(), self.store)
        result = handler(self.make_event("你好", "ask-post"), self.gateway)
        handler.post_llm_call(
            session_id="qqbot:group:group-a", user_message=result["text"],
            assistant_response="你好，群友。", model="deepseek/deepseek-v4.1-flash",
            platform=SimpleNamespace(value="qqbot"),
        )
        rows = self.store.get_history("group-a")
        self.assertEqual([row["role"] for row in rows], ["user"])
        self.assertNotIn("你好，群友。", [row["text"] for row in rows])

    async def test_member_memory_source_history_tracks_each_event(self):
        started = asyncio.Event()
        resume = asyncio.Event()
        calls = 0

        class LLM:
            async def acomplete_structured(self, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    started.set()
                await resume.wait()
                text = str(kwargs.get("input", ""))
                if "成员A的原句" in text:
                    fact = "成员A的事实"
                    evidence = "成员A的原句"
                else:
                    fact = "成员B的事实"
                    evidence = "成员B的原句"
                return {"parsed": {"facts": [{
                    "category": "project", "key": "身份事实", "value": fact,
                    "confidence": 0.99, "evidence": evidence,
                }]}}

        ctx = FakeContext({"member_memory": {"auto_extract": True}})
        ctx.llm = LLM()
        self.store.set_member_consent("group-a", "member-a", "opted_in")
        self.store.set_member_consent("group-a", "member-b", "opted_in")
        handler = build_handler(ctx, self.store)

        member_a = self.make_event("我负责成员A的原句", "member-a-message")
        member_a.source.user_id = "member-a"
        member_b = self.make_event("我负责成员B的原句", "member-b-message")
        member_b.source.user_id = "member-b"
        handler(member_a, self.gateway)
        handler(member_b, self.gateway)
        await started.wait()
        resume.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)


        facts_a = self.store.list_member_memory_facts("group-a", "member-a")
        facts_b = self.store.list_member_memory_facts("group-a", "member-b")
        self.assertEqual(len(facts_a), 1)
        self.assertEqual(facts_a[0]["fact_value"], "成员A的事实")
        self.assertEqual(facts_b[0]["fact_value"], "成员B的事实")
        self.assertEqual(len(facts_b), 1)
        history_a = next(row for row in self.store.get_history("group-a") if row["message_id"] == "member-a-message")
        history_b = next(row for row in self.store.get_history("group-a") if row["message_id"] == "member-b-message")
        self.assertEqual(facts_a[0]["source_history_id"], history_a["id"])
        self.assertNotEqual(facts_a[0]["source_history_id"], history_b["id"])
        self.assertEqual(facts_b[0]["source_history_id"], history_b["id"])

    async def test_reset_does_not_resurrect_stale_or_post_hook_assistant_output(self):
        handler = build_handler(FakeContext(), self.store)
        old = handler(self.make_event("旧问题", "old-question"), self.gateway)
        handler.memory.reset("group-a")
        handler.post_llm_call(
            session_id="qqbot:group:group-a", user_message=old["text"],
            assistant_response="旧助手回答", model="deepseek/deepseek-v4.1-flash",
            platform=SimpleNamespace(value="qqbot"),
        )
        new = handler(self.make_event("新问题", "new-question"), self.gateway)
        handler.post_llm_call(
            session_id="qqbot:group:group-a", user_message=new["text"],
            assistant_response="新助手回答", model="deepseek/deepseek-v4.1-flash",
            platform=SimpleNamespace(value="qqbot"),
        )
        rows = self.store.get_history("group-a")
        self.assertEqual([row["role"] for row in rows], ["user"])
        self.assertNotIn("旧助手回答", [row["text"] for row in rows])
        self.assertNotIn("新助手回答", [row["text"] for row in rows])

    async def test_post_llm_ignores_wrapped_input_until_delivery_callback(self):
        handler = build_handler(FakeContext(), self.store)
        old = handler(self.make_event("前文问题", "wrapped-old"), self.gateway)
        wrapped = "[群友]\n[Replying to: 前文]\n" + old["text"] + "\n[图片内容]示意图"
        handler.post_llm_call(
            session_id="qqbot:group:group-a", user_message=wrapped,
            assistant_response="正常助手回答", model="deepseek/deepseek-v4.1-flash",
            platform=SimpleNamespace(value="qqbot"),
        )
        self.assertNotIn("正常助手回答", [row["text"] for row in self.store.get_history("group-a")])

        handler.memory.reset("group-a")
        handler.post_llm_call(
            session_id="qqbot:group:group-a", user_message=wrapped,
            assistant_response="过时助手回答", model="deepseek/deepseek-v4.1-flash",
            platform=SimpleNamespace(value="qqbot"),
        )
        self.assertNotIn("过时助手回答", [row["text"] for row in self.store.get_history("group-a")])

        handler.post_llm_call(
            session_id="qqbot:group:group-a",
            user_message="[fake-group-memory-key]\n无真实本次标记",
            assistant_response="伪造助手回答", model="deepseek/deepseek-v4.1-flash",
            platform=SimpleNamespace(value="qqbot"),
        )
        self.assertNotIn("伪造助手回答", [row["text"] for row in self.store.get_history("group-a")])

    async def test_stop_memory_resets_group_session_recall_boundary(self):
        handler = build_handler(FakeContext({"member_memory": {"auto_extract": False}}), self.store)
        saved = handler(self.make_event("/记住我：职责=后端发布", "remember"), self.gateway)
        self.assertEqual(saved["action"], "skip")
        await asyncio.sleep(0)
        before = handler(self.make_event("我负责什么？", "before-stop"), self.gateway)
        self.assertIn("后端发布", before["text"])

        session_store = SimpleNamespace(
            calls=[],
            reset_session=lambda *args, **kwargs: session_store.calls.append((args, kwargs)),
        )
        stopped = handler(
            self.make_event("/停止记忆", "stop-memory"), self.gateway,
            session_store=session_store,
        )
        self.assertEqual(stopped["action"], "skip")
        await asyncio.sleep(0)
        after = handler(self.make_event("我负责什么？", "after-stop"), self.gateway)
        self.assertNotIn("后端发布", after["text"])


class PolicyTests(unittest.IsolatedAsyncioTestCase):
    def test_invalid_rule_config_fails_open(self):
        rules, errors = compile_rule_list([{"id": "x", "match": "regex", "pattern": "[", "reply": "x"}], result_field="reply")
        self.assertEqual(rules, ())
        self.assertTrue(errors)

    async def test_semantic_disabled_and_bad_output_fail_open(self):
        class LLM:
            calls = 0
            async def complete_structured(self, *args, **kwargs):
                self.calls += 1
                return "not-json"
        ctx = SimpleNamespace(llm=LLM())
        disabled = await semantic_moderation(ctx, "text", enabled=False)
        self.assertFalse(disabled.blocked)
        self.assertEqual(ctx.llm.calls, 0)
        invalid = await semantic_moderation(ctx, "text", enabled=True)
        self.assertFalse(invalid.blocked)

    async def test_semantic_timeout_fails_open(self):
        class SlowLLM:
            async def complete_structured(self, *args, **kwargs):
                await asyncio.sleep(1)
                return {"action": "block", "confidence": 1.0}
        decision = await semantic_moderation(
            SimpleNamespace(llm=SlowLLM()), "text", enabled=True, timeout_seconds=0.01
        )
        self.assertFalse(decision.blocked)
        self.assertEqual(decision.source, "semantic_error")


if __name__ == "__main__":
    unittest.main()
