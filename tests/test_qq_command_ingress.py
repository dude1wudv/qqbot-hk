"""Local ingress regressions; the image smoke additionally uses Hermes classes."""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
from smart_group_qq import build_handler
from smart_group_qq.ingress import install_command_ingress, uninstall_command_ingress
from smart_group_qq.store import Store


class Context:
    def __init__(self, ambient=True, participation=True):
        self.values = {"ambient": {"enabled": ambient, "participation": {
            "enabled": participation, "wake_words": ["小分队机器人"]}}}
        self.llm = SimpleNamespace(acomplete_structured=AsyncMock(side_effect=AssertionError("LLM forbidden")))

    def get_config(self, key, default=None):
        return self.values.get(key, default)


def event(text, message_id="test-message", **kwargs):
    return SimpleNamespace(text=text, message_id=message_id, source=SimpleNamespace(
        platform="qqbot", chat_type="group", chat_id="test-group", user_id="test-member"),
        raw_message=kwargs.pop("raw_message", {}), allow_gateway_control=True, internal=False, **kwargs)


class BusyParent:
    async def handle_message(self, item):
        self.default_calls.append(item)
        if self._event_session_key(item) in self._active_sessions:
            self.queued.append(item)
        else:
            await self._dispatch_inline_reply(item)


class LocalQQAdapter(BusyParent):
    def __init__(self, handler, allowed=True):
        self.handler = handler
        self.allowed = allowed
        self._active_sessions = {"test-group": object()}
        self.default_calls, self.queued, self.hook_calls, self.sent = [], [], [], []
        self.gateway = SimpleNamespace(adapters={"qqbot": self},
            _is_user_authorized_for_source=lambda source: self.allowed,
            _session_key_for_source=lambda source: source.chat_id)

    @staticmethod
    def _strip_at_mention(text):
        return text

    def _event_session_key(self, item):
        return item.source.chat_id

    async def _dispatch_inline_reply(self, item):
        self.hook_calls.append(item)
        self.handler(item, self.gateway)
        await asyncio.sleep(0)

    async def send(self, chat_id, content, reply_to=None, **kwargs):
        self.sent.append((chat_id, content, reply_to))
        return SimpleNamespace(success=True)


class IngressTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.addCleanup(uninstall_command_ingress, LocalQQAdapter)
        self.ctx = Context()
        self.handler = build_handler(self.ctx, self.store)
        self.adapter = LocalQQAdapter(self.handler)

    async def test_original_busy_loss_then_same_payload_reaches_local_handler(self):
        for index, (command, reply) in enumerate((("/值日表", "本周值日表"), ("/all", "已恢复群聊自动参与"))):
            with self.subTest(command=command):
                uninstall_command_ingress(LocalQQAdapter)
                self.handler.character.set_group_mode("test-group", "only")
                item = event("@小分队机器人 " + command, str(index))
                before = len(self.adapter.sent)
                hooks = len(self.adapter.hook_calls)
                await self.adapter.handle_message(item)
                self.assertIs(self.adapter.queued[-1], item)
                self.assertEqual(len(self.adapter.hook_calls), hooks)
                self.assertEqual(len(self.adapter.sent), before)
                install_command_ingress(LocalQQAdapter)
                await self.adapter.handle_message(item)
                self.assertEqual(len(self.adapter.hook_calls), hooks + 1)
                self.assertEqual(len(self.adapter.sent), before + 1)
                self.assertIn(reply, self.adapter.sent[-1][1])
        self.ctx.llm.acomplete_structured.assert_not_called()

    async def test_authorization_denial_has_no_reply_or_state_change(self):
        install_command_ingress(LocalQQAdapter)
        self.adapter.allowed = False
        self.handler.character.set_group_mode("test-group", "only")
        before = self.store.db.total_changes
        for command in ("/all", "/值日表"):
            await self.adapter.handle_message(event(command, command))
        self.assertEqual(len(self.adapter.hook_calls), 2)
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(self.handler.character.group_mode("test-group"), "only")
        self.assertEqual(self.store.db.total_changes, before)

    async def test_slash_access_denial_preserves_group_mode_for_authorized_user(self):
        install_command_ingress(LocalQQAdapter)
        denied_calls = []
        denial = "当前命令无权限。"

        def deny(source, command_name):
            denied_calls.append((source, command_name))
            return denial

        self.adapter.gateway._check_slash_access = deny
        self.assertTrue(self.adapter.allowed)
        for command, initial_mode in (("/all", "only"), ("/only", "all")):
            with self.subTest(command=command):
                self.handler.character.set_group_mode("test-group", initial_mode)
                item = event(command, "denied-" + command)
                before = len(self.adapter.sent)
                await self.adapter.handle_message(item)
                self.assertEqual(denied_calls[-1], (item.source, command[1:]))
                self.assertEqual(self.handler.character.group_mode("test-group"), initial_mode)
                self.assertEqual(len(self.adapter.sent), before + 1)
                self.assertEqual(self.adapter.sent[-1], ("test-group", denial, item.message_id))
        self.assertEqual(len(denied_calls), 2)
        self.ctx.llm.acomplete_structured.assert_not_called()

    async def test_native_reset_and_ordinary_messages_keep_default_entry(self):
        install_command_ingress(LocalQQAdapter)
        for command in ("/reset", "/clear", "/new", "/stop", "普通消息"):
            item = event(command, command)
            await self.adapter.handle_message(item)
            self.assertIs(self.adapter.default_calls[-1], item)
        self.assertEqual(self.adapter.hook_calls, [])
        self.adapter._active_sessions.clear()
        item = event("/status")
        await self.adapter.handle_message(item)
        self.assertIs(self.adapter.default_calls[-1], item)

    async def test_synthetic_and_internal_controls_do_not_bypass_or_execute(self):
        install_command_ingress(LocalQQAdapter)
        self.handler.character.set_group_mode("test-group", "only")
        for command in ("/all", "/值日表"):
            item = event(command, command, raw_message={"_smart_group_qq_nonmention": True})
            await self.adapter.handle_message(item)
            self.assertIs(self.adapter.queued[-1], item)
            self.adapter._active_sessions.clear()
            await self.adapter.handle_message(item)
            self.adapter._active_sessions["test-group"] = object()
        for field in ("internal", "allow_gateway_control"):
            item = event("@小分队机器人 /all", field)
            setattr(item, field, field == "internal")
            await self.adapter.handle_message(item)
            self.assertIs(self.adapter.queued[-1], item)
            self.assertEqual(item.text, "@小分队机器人 /all")
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(self.handler.character.group_mode("test-group"), "only")
        self.ctx.llm.acomplete_structured.assert_not_called()

    async def test_install_uninstall_preserves_inherited_methods_and_is_idempotent(self):
        original = LocalQQAdapter.handle_message
        strip = LocalQQAdapter.__dict__["_strip_at_mention"]
        install_command_ingress(LocalQQAdapter)
        wrapped = LocalQQAdapter.handle_message
        install_command_ingress(LocalQQAdapter)
        self.assertIs(LocalQQAdapter.handle_message, wrapped)
        self.assertEqual(LocalQQAdapter._strip_at_mention("@小分队机器人/all"), "/all")
        uninstall_command_ingress(LocalQQAdapter)
        uninstall_command_ingress(LocalQQAdapter)
        self.assertIs(LocalQQAdapter.handle_message, original)
        self.assertNotIn("handle_message", LocalQQAdapter.__dict__)
        self.assertIs(LocalQQAdapter.__dict__["_strip_at_mention"], strip)
        await self.adapter.handle_message(event("/all"))
        self.assertEqual(len(self.adapter.queued), 1)
        self.assertEqual(self.adapter.sent, [])
