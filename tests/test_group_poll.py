import asyncio
from contextlib import closing
from pathlib import Path
import sys
import time
import sqlite3
import tempfile
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

    def test_limits_and_expiry_commits_even_when_vote_is_rejected(self):
        for question, options in (
            ("", ("a", "b")), ("x" * 201, ("a", "b")),
            ("q", ("a",)), ("q", tuple(str(i) for i in range(6))),
            ("q", ("a", "")), ("q", ("a", "x" * 101)),
        ):
            with self.subTest(question=question, options=options), self.assertRaises(ValueError):
                self.store.apply_group_poll("g", "u", "create", question=question, options=options)
        poll = self.store.apply_group_poll("g", "u", "create", question="q" * 200, options=("a", "b" * 100), now=100)
        result = self.store.apply_group_poll("g", "u", "vote", poll_id=poll["poll_id"], option_no=1, now=86500)
        self.assertIn("error", result)
        status = self.store.db.execute("SELECT status FROM group_polls WHERE poll_id=?", (poll["poll_id"],)).fetchone()[0]
        self.assertEqual(status, "expired")
        self.assertEqual(list(self.store.db.execute("PRAGMA foreign_key_check")), [])

    def test_forget_removes_votes_and_cascades_only_unfinished_owned_polls(self):
        owned = self.store.apply_group_poll("g", "u", "create", question="remove-content", options=("a", "b"))
        self.store.apply_group_poll("g", "other", "vote", poll_id=owned["poll_id"], option_no=1)
        elsewhere = self.store.apply_group_poll("other-group", "u", "create", question="keep-other-group", options=("a", "b"))
        self.store.forget_group_member("g", "u")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM group_poll_options WHERE poll_id=?", (owned["poll_id"],)).fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM group_poll_votes WHERE poll_id=?", (owned["poll_id"],)).fetchone()[0], 0)
        self.assertEqual(self.store.apply_group_poll("other-group", "u", "show")["poll_id"], elsewhere["poll_id"])
        closed = self.store.apply_group_poll("g", "u", "create", question="keep-results", options=("a", "b"))
        self.store.apply_group_poll("g", "u", "vote", poll_id=closed["poll_id"], option_no=1)
        self.store.apply_group_poll("g", "u", "close", poll_id=closed["poll_id"])
        self.store.forget_group_member("g", "u")
        result = self.store.apply_group_poll("g", "other", "show")
        self.assertEqual(result["status"], "closed")
        self.assertEqual(sum(option["votes"] for option in result["options"]), 0)
        self.assertEqual(list(self.store.db.execute("PRAGMA foreign_key_check")), [])

    def test_schema4_migration_keeps_history_facts_and_character_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "schema4.db"
            previous = Store(path, member_secret="test-only-secret")
            previous.append_history("g", role="user", member_id="synthetic-ref", text="保留历史", message_id="m")
            previous.set_member_consent("g", "u", "opted_in")
            previous.add_member_memory_fact("g", "u", "self", "项目", "保留项目", explicitness="explicit")
            with previous.transaction() as db:
                db.execute("INSERT INTO character_state VALUES (?,?)", ("g", '{"pet":{"name":"保留宠物"}}'))
            previous.close()
            with closing(sqlite3.connect(path)) as db, db:
                db.executescript(
                    "DROP TABLE group_poll_votes; DROP TABLE group_poll_options; DROP TABLE group_polls;"
                    "PRAGMA user_version=4;"
                )
            for _ in range(2):
                migrated = Store(path, member_secret="test-only-secret")
                try:
                    self.assertEqual(migrated.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                    self.assertEqual(migrated.get_history("g")[0]["text"], "保留历史")
                    self.assertEqual(migrated.list_member_memory_facts("g", "u")[0]["fact_value"], "保留项目")
                    self.assertIn("保留宠物", migrated.db.execute("SELECT payload FROM character_state").fetchone()[0])
                    self.assertEqual(list(migrated.db.execute("PRAGMA foreign_key_check")), [])
                finally:
                    migrated.close()

    def test_parser_is_exact_and_preserves_payload(self):
        command = parse_poll_command("<@bot> ／ 群投票 创建 周末活动 | Ａ | 大写Option")
        self.assertEqual(command.question, "周末活动")
        self.assertEqual(command.options, ("Ａ", "大写Option"))
        self.assertEqual(parse_poll_command("/群投票 选择 abc nope").action, "invalid")
        self.assertIsNone(parse_poll_command("/群投票x"))
        self.assertIsNone(parse_poll_command("/投票 1"))


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

    async def test_denied_missing_permission_dm_and_nonmention_have_no_side_effects(self):
        handler = build_handler(Context(), self.store)
        original_check = self.gateway._check_slash_access
        for mode in ("unauthorized", "missing", "denied", "exception", "synthetic", "dm", "no-id"):
            with self.subTest(mode=mode):
                self.gateway._is_user_authorized_for_source = lambda source: mode != "unauthorized"
                if mode == "missing":
                    self.gateway._check_slash_access = None
                elif mode == "denied":
                    self.gateway._check_slash_access = lambda source, name: "denied"
                elif mode == "exception":
                    def fail(source, name):
                        raise RuntimeError("permission unavailable")
                    self.gateway._check_slash_access = fail
                else:
                    self.gateway._check_slash_access = original_check
                incoming = group_event("/群投票 创建 q | a | b", "" if mode == "no-id" else mode, synthetic=mode == "synthetic")
                if mode == "dm":
                    incoming.source.chat_type = "dm"
                before = list(self.store.db.iterdump())
                await handler(incoming, self.gateway)
                await asyncio.sleep(0)
                self.assertEqual(list(self.store.db.iterdump()), before)
                self.assertEqual(self.adapter.sent, [])

    async def test_revote_group_isolation_audit_privacy_close_and_forget_commands(self):
        handler = build_handler(Context(), self.store)
        await self.run_command(handler, "/群投票 创建 private-question | private-option-a | private-option-b", "create")
        poll_id = self.store.apply_group_poll("group-a", "member-a", "show")["poll_id"]
        await self.run_command(handler, f"/群投票 选择 {poll_id} 1", "a1")
        await self.run_command(handler, f"/群投票 选择 {poll_id} 2", "a2")
        await self.run_command(handler, f"/群投票 选择 {poll_id} 1", "b1", member="member-b")
        await self.run_command(handler, "/群投票", "result")
        result = self.adapter.sent[-1][1]
        self.assertIn("总票数：2", result)
        self.assertEqual(result.count("（1票）"), 2)
        self.assertNotIn("member-a", result)
        self.assertNotIn(self.store.member_ref_for("group-a", "member-a"), result)
        await self.run_command(handler, f"/群投票 选择 {poll_id} 1", "cross-group", group="group-b")
        self.assertIn("没有找到", self.adapter.sent[-1][1])
        await self.run_command(handler, f"/群投票 结束 {poll_id}", "b-close", member="member-b")
        self.assertIn("只有创建者", self.adapter.sent[-1][1])
        await self.run_command(handler, "/忘记我", "forget-b", member="member-b")
        self.assertEqual(sum(o["votes"] for o in self.store.apply_group_poll("group-a", "member-a", "show")["options"]), 1)
        await self.run_command(handler, "/忘记我", "forget-a")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM group_polls").fetchone()[0], 0)
        audit = str([tuple(row) for row in self.store.db.execute("SELECT * FROM audit_events")])
        for value in ("private-question", "private-option", "member-a", "member-b"):
            self.assertNotIn(value, audit)



if __name__ == "__main__":
    unittest.main()
