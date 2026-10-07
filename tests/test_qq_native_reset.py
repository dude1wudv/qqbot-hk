import asyncio
import inspect
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))
ZoneInfo("Asia/Shanghai")

from smart_group_qq import _reset_gateway_session, build_handler
from smart_group_qq.store import Store


class QQNativeResetTests(unittest.IsolatedAsyncioTestCase):
    async def test_reset_uses_native_gateway_handler_and_preserves_source_event(self):
        source = SimpleNamespace(chat_name="测试群", marker=object())
        event = SimpleNamespace(text="/other", source=source, marker="original")
        native_reset = AsyncMock()
        session_store = SimpleNamespace(reset_session=Mock())
        gateway = SimpleNamespace(
            _handle_reset_command=native_reset,
            session_store=session_store,
        )
        self.assertEqual(tuple(inspect.signature(_reset_gateway_session).parameters), ("gateway", "event"))

        await _reset_gateway_session(gateway, event)

        native_reset.assert_awaited_once()
        reset_event = native_reset.await_args.args[0]
        self.assertIsNot(reset_event, event)
        self.assertEqual(reset_event.text, "/reset")
        session_store.reset_session.assert_not_called()
        self.assertEqual((event.text, event.source, event.marker), ("/other", source, "original"))

    async def test_confirmation_waits_for_native_reset_for_reset_and_privacy_commands(self):
        store = Store(":memory:", member_secret="test-only-secret")
        self.addCleanup(store.close)

        class Context:
            @staticmethod
            def get_config(_key, default=None):
                return default

        class Adapter:
            def __init__(self):
                self.sent = []

            async def send(self, chat_id, content, reply_to=None):
                self.sent.append((chat_id, content, reply_to))
                return SimpleNamespace(success=True)

        handler = build_handler(Context(), store)
        adapter = Adapter()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def block_until_released(native_event):
            self.assertEqual(native_event.text, "/reset")
            entered.set()
            await release.wait()

        gateway = SimpleNamespace(
            adapters={"qqbot": adapter},
            _is_user_authorized_for_source=lambda _source: True,
            _check_slash_access=lambda _source, _name: None,
            _handle_reset_command=AsyncMock(side_effect=block_until_released),
        )
        cases = (
            ("/reset", "reset"),
            ("/纠正记忆：职责=测试", "correct"),
            ("/停止记忆", "opt-out"),
            ("/忘记我", "forget"),
        )
        for index, (command, message_id) in enumerate(cases):
            with self.subTest(command=command):
                entered.clear()
                release.clear()
                incoming = SimpleNamespace(
                    text=command,
                    message_id=f"{message_id}-{index}",
                    source=SimpleNamespace(
                        platform="qqbot", chat_type="group", chat_id="group", user_id="member",
                    ),
                )
                sent_before = len(adapter.sent)
                task = asyncio.create_task(handler(incoming, gateway))
                await asyncio.wait_for(entered.wait(), timeout=1)
                self.assertEqual(len(adapter.sent), sent_before)
                release.set()
                result = await task
                await asyncio.sleep(0)
                self.assertEqual(result["action"], "skip")
                self.assertEqual(len(adapter.sent), index + 1)
                gateway._handle_reset_command.assert_awaited()

        self.assertEqual(gateway._handle_reset_command.await_count, len(cases))


if __name__ == "__main__":
    unittest.main()
