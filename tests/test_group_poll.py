import asyncio
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

from smart_group_qq import build_handler
from smart_group_qq.polls import parse_poll_command
from smart_group_qq.store import SCHEMA_VERSION, Store


class Context:
    plugin_id = "smart_group_qq"

    def get_config(self, key, default=None):
        return default


class Adapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, content, reply_to=None):
        self.sent.append((chat_id, content, reply_to))
        return SimpleNamespace(success=True)


def group_event(text, message_id, group="group-a", member="member-a", synthetic=False):
    event = SimpleNamespace(
        text=text, message_id=message_id,
        source=SimpleNamespace(platform="qqbot", chat_type="group", chat_id=group, user_id=member),
    )
    if synthetic:
        event.raw_message = {"_smart_group_qq_nonmention": True}
    return event


class PollStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:", member_secret="test-only-secret")
        self.addCleanup(self.store.close)

    def test_schema_and_rules(self):
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        created = self.store.apply_group_poll("a", "creator", "create", question="周末活动", options=("写代码", "玩游戏"), now=100)
        poll_id = created["poll_id"]
        self.assertEqual(len(created["options"]), 2)
        self.assertIn("error", self.store.apply_group_poll("a", "other", "create", question="第二个", options=("A", "B"), now=101))
        voted = self.store.apply_group_poll("a", "creator", "vote", poll_id=poll_id, option_no=1, now=102)
        self.store.apply_group_poll("a", "creator", "vote", poll_id=poll_id, option_no=2, now=103)
        self.store.apply_group_poll("a", "other", "vote", poll_id=poll_id, option_no=1, now=104)
        counts = [item["votes"] for item in voted["options"]]
        self.assertEqual(counts, [1, 0])
        shown = self.store.apply_group_poll("a", "other", "show", now=105)
        self.assertEqual([item["votes"] for item in shown["options"]], [1, 1])
        self.assertIn("error", self.store.apply_group_poll("a", "other", "close", poll_id=poll_id, now=106))
        self.assertEqual(self.store.apply_group_poll("a", "creator", "close", poll_id=poll_id, now=107)["status"], "closed")
        self.assertIn("error", self.store.apply_group_poll("a", "other", "vote", poll_id=poll_id, option_no=1, now=108))

    def test_expiry_group_isolation_and_forget(self):
        created = self.store.apply_group_poll("a", "creator", "create", question="Q", options=("A", "B"), now=100)
        poll_id = created["poll_id"]
        self.store.apply_group_poll("a", "other", "vote", poll_id=poll_id, option_no=1, now=101)
        self.store.apply_group_poll("b", "other", "create", question="Other", options=("A", "B"), now=101)
        expired = self.store.apply_group_poll("a", "other", "show", now=100 + 86400)
        self.assertEqual(expired["status"], "expired")
        live = self.store.apply_group_poll("b", "other", "show", now=102)
        self.assertEqual(live["status"], "open")
        self.store.apply_group_poll("a", "creator", "create", question="New", options=("A", "B"), now=200)
        self.store.forget_group_member("a", "creator")
        self.assertEqual(self.store.apply_group_poll("a", "other", "show", now=201)["error"], "本群暂无投票。")


class PollIngressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:", member_secret="test-only-secret")
        self.adapter = Adapter()
        self.gateway = SimpleNamespace(
            adapters={"qqbot": self.adapter},
            _is_user_authorized_for_source=lambda source: True,
            _check_slash_access=lambda source, name: None,
            _handle_reset_command=AsyncMock(),
        )
        self.addCleanup(self.store.close)

    async def run_command(self, handler, text, message_id, **kwargs):
        result = await handler(group_event(text, message_id, **kwargs), self.gateway)
        await asyncio.sleep(0)
        return result

    async def test_command_flow_claim_nonmention_and_reset_preservation(self):
        self.assertEqual(parse_poll_command("/群投票").action, "show")
        self.assertEqual(parse_poll_command("/群投票 选择 abc123 2").option_no, 2)
        handler = build_handler(Context(), self.store)
        created = await self.run_command(handler, "/群投票 创建 周末活动 | 写代码 | 玩游戏", "create")
        self.assertEqual(created["action"], "skip")
        body = self.adapter.sent[-1][1]
        poll_id = body.split("【群投票 ", 1)[1].split("｜", 1)[0]
        await self.run_command(handler, f"/群投票 选择 {poll_id} 1", "vote-1")
        duplicate = await self.run_command(handler, f"/群投票 选择 {poll_id} 1", "vote-1")
        self.assertEqual(duplicate["reason"], "duplicate")
        sent = len(self.adapter.sent)
        await self.run_command(handler, "/群投票 查看", "nonmention", synthetic=True)
        self.assertEqual(len(self.adapter.sent), sent)
        await self.run_command(handler, "/reset", "reset")
        await self.run_command(handler, "/群投票", "show")
        self.assertIn("群投票", self.adapter.sent[-1][1])
        dm = group_event("/群投票", "dm", group="dm", member="member-a")
        dm.source.chat_type = "dm"
        self.assertEqual(await handler(dm, self.gateway), {"action": "allow"})
        self.assertEqual(len(self.adapter.sent), sent + 2)


if __name__ == "__main__":
    unittest.main()
