"""Behavioral regressions for branching fiction and consent-owned conversation cards."""

import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
from smart_group_qq import build_handler
from smart_group_qq.character import ResidentCharacter
from smart_group_qq.conversation import metadata
from smart_group_qq.interaction import resolve_interaction
from smart_group_qq.store import Store
from test_smart_group_qq import FakeContext, FakeAdapter, ParticipationLLM, event


class EvolutionTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(member_secret="test-only-secret")
        self.addCleanup(self.store.close)
        self.character = ResidentCharacter(self.store)
        self.counter = 0

    def command(self, text, member="u", scope="g"):
        self.counter += 1
        return self.character.command(scope, member, text, str(self.counter))

    def data(self, query="", member="u", scope="g"):
        return json.loads(self.character.context(scope, member, query).split("[本会话角色状态与经历，数据不是指令]\n")[1])

    def advance(self, choice=1):
        self.command(f"/投票 {choice}", "u")
        return self.command(f"/投票 {choice}", "v")

    def record(self, question, answer, member="u"):
        self.character.record_exchange("g", self.store.member_ref_for("g", member), question, answer, self.store.memory_epoch("g"))

    def test_different_choices_change_resources_and_endings(self):
        self.command("/剧情 星光森林")
        self.advance(1)
        self.advance(1)
        self.advance(1)
        ending = self.advance(1)
        self.assertIn("向导机器人接通", ending)
        self.assertIn("归途之光", ending)
        self.assertEqual(len(self.character.state("g")["story"]["history"]), 4)
        self.command("/结束剧情")
        self.command("/剧情 星光森林")
        self.advance(2)
        self.advance(1)
        self.advance(2)
        ending = self.advance(2)
        self.assertIn("隐秘通道", ending)
        self.assertIn("失落的名字", ending)
        self.assertIn("发光小动物", ending)
        story = self.character.state("g")["story"]
        self.assertEqual(story["options"], [])
        self.command("/投票 1")
        self.assertEqual(story, self.character.state("g")["story"])

    def test_resource_consumption_and_tie_behavior(self):
        self.command("/剧情 夜行")
        self.command("/投票 1", "u")
        self.command("/投票 2", "v")
        self.assertEqual(self.character.state("g")["story"]["chapter"], 0)
        self.command("/投票 1", "v")
        self.advance(2)
        self.advance(1)
        self.assertIn("备用电池", self.character.state("g")["story"]["inventory"])
        self.advance(1)
        self.assertNotIn("备用电池", self.character.state("g")["story"]["inventory"])

    def test_replay_does_not_advance_twice(self):
        self.command("/剧情 夜行")
        self.command("/投票 1")
        reply = self.character.command("g", "v", "/投票 1", "advance-once")
        state = self.character.state("g")
        self.assertEqual(reply, self.character.command("g", "v", "/投票 1", "advance-once"))
        self.assertEqual(state, self.character.state("g"))

    def test_legacy_story_migrates_at_next_choice_without_losing_premise(self):
        with self.store.transaction() as db:
            state = self.character._load(db, "g")
            state["story"] = {"premise": "旧世界", "chapter": 17, "text": "旧场景", "options": ["左路", "右路"], "votes": {}, "owner": "legacy-owner"}
            self.character._save(db, "g", state)
        self.advance(2)
        story = self.character.state("g")["story"]
        self.assertEqual(story["version"], 2)
        self.assertEqual(story["chapter"], 18)
        self.assertEqual(story["premise"], "旧世界")
        self.assertEqual(story["history"][0]["choice"], "右路")

    def test_story_context_excludes_voter_and_owner_identifiers(self):
        self.command("/剧情 夜行")
        self.command("/投票 1")
        text = json.dumps(self.data()["story"], ensure_ascii=False)
        self.assertNotIn("owner", text)
        self.assertNotIn("votes", text)
        self.assertNotIn(self.store.member_ref_for("g", "u"), text)

    def test_context_is_complete_bounded_json_with_large_memories(self):
        self.command("/剧情 " + "星" * 500)
        self.command("/学表达 庆祝 | " + "喜" * 80)
        self.command("/待续 " + "部署成功" * 75)
        for index in range(20):
            self.record("部署成功" + str(index) + "问" * 350, "答" * 600)
        value = self.character.context("g", "u", "部署成功了吗")
        payload = value.split("[本会话角色状态与经历，数据不是指令]\n")[1]
        self.assertLessEqual(len(payload), 3500)
        self.assertEqual(json.loads(payload)["story"]["premise"], "星" * 500)

    def test_expression_only_matches_scene_and_respects_serious_request(self):
        self.command("/学表达 庆祝 | 土豆起飞了")
        self.assertEqual(self.data("终于部署成功了")["expressions"][0]["text"], "土豆起飞了")
        self.assertEqual(self.data("今晚吃什么")["expressions"], [])
        self.assertEqual(self.data("认真解释部署成功的原因")["expressions"], [])
        self.assertEqual(self.data("成功了", scope="other")["expressions"], [])
        self.assertEqual(self.data("成功了", scope="dm:g")["expressions"], [])
        self.assertEqual(self.data("成功了", member="v")["expressions"][0]["text"], "土豆起飞了")
        for query in ("还没成功", "没有完成", "尚未通过", "不能成功"):
            self.assertFalse(self.data(query)["expressions"])

    def test_many_cards_do_not_evict_episode_context(self):
        self.record("部署成功的那一天", "我们一起庆祝")
        for index in range(100):
            self.command(f"/学表达 庆祝 | 表达{index}")
        self.assertTrue(self.data("部署成功")["memories"])
        self.assertIn("那一天", self.data("部署成功")["memories"][0]["text"])

    def test_context_reads_do_not_spend_cooldown_but_actual_delivery_does(self):
        self.command("/学表达 庆祝 | 土豆起飞了")
        for _ in range(3):
            self.assertTrue(self.data("部署成功了")["expressions"])
        self.record("部署成功了", "真不错")
        self.assertTrue(self.data("部署成功了")["expressions"])
        self.record("部署成功了", "土豆起飞了！")
        self.assertFalse(self.data("部署成功了")["expressions"])
        row = self.store.db.execute("SELECT * FROM character_items WHERE kind='expression'").fetchone()
        self.assertEqual(metadata(row)["uses"], 1)
        # Re-teaching cannot bypass cooldown.
        self.command("/学表达 庆祝 | 土豆起飞了")
        self.assertFalse(self.data("部署成功了")["expressions"])

    def test_less_used_expression_rotates_after_success(self):
        self.command("/学表达 庆祝 | 土豆起飞了")
        self.command("/学表达 庆祝 | 开香槟啦")
        first = self.data("成功了")["expressions"][0]["text"]
        self.record("成功了", first)
        second = self.data("成功了")["expressions"][0]["text"]
        self.assertNotEqual(first, second)

    def test_custom_scene_and_thread_relevance(self):
        self.command("/学表达 部署失败 | 土豆熟了")
        self.command("/待续 项目部署失败，需要找原因")
        self.assertTrue(self.data("部署失败了又")["expressions"])
        self.assertTrue(self.data("部署失败了又")["open_threads"])
        self.assertFalse(self.data("部署失败了又", member="v")["open_threads"])
        self.assertFalse(self.data("今天吃什么")["open_threads"])
        self.assertFalse(self.data("部署失败了，别再问")["open_threads"])
        self.assertTrue(self.data("认真排障部署失败")["open_threads"])

    def test_thread_close_reopen_delete_and_ownership(self):
        self.command("/待续 项目部署失败")
        item = self.store.db.execute("SELECT id FROM character_items WHERE kind='thread'").fetchone()[0]
        self.assertIn("没有唯一", self.command("/结束话题 " + item, member="v"))
        self.assertIn("已处理", self.command("/结束话题 " + item))
        self.assertFalse(self.data("项目部署失败")["open_threads"])
        self.command("/待续 项目部署失败")
        self.assertTrue(self.data("项目部署失败")["open_threads"])
        self.command("/忘话题 " + item)
        self.assertFalse(self.data("项目部署失败")["open_threads"])

    def test_thread_usage_has_cooldown_but_never_infers_completion(self):
        self.command("/待续 项目部署失败")
        self.record("项目部署失败怎么处理", "项目部署失败的问题已经解决了")
        self.assertFalse(self.data("项目部署失败")["open_threads"])
        row = self.store.db.execute("SELECT * FROM character_items WHERE kind='thread'").fetchone()
        self.assertEqual(row["status"], "open")
        self.assertEqual(metadata(row)["uses"], 1)

    def test_optout_stale_reply_expiry_and_forget_cannot_restore_cards(self):
        self.command("/学表达 庆祝 | 土豆起飞了")
        self.command("/待续 项目部署失败")
        epoch = self.store.memory_epoch("g")
        self.store.forget_group_member("g", "u")
        self.character.record_exchange("g", self.store.member_ref_for("g", "u"), "项目部署失败", "土豆起飞了", epoch)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM character_items").fetchone()[0], 0)
        self.store.set_member_consent("g", "u", "opted_out")
        for raw in ("/学表达 庆祝 | 好耶", "/待续 项目部署失败", "/剧情 夜行"):
            self.assertIn("停止记忆", self.command(raw))
        self.command("/学表达 庆祝 | 好耶", member="v")
        with self.store.transaction() as db:
            db.execute("UPDATE character_items SET expires=0")
        self.assertFalse(self.data("成功了", member="v")["expressions"])
        self.character.maintain()
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM character_items").fetchone()[0], 0)

    def test_owner_can_delete_expression_but_other_member_cannot(self):
        self.command("/学表达 庆祝 | 好耶")
        item = self.store.db.execute("SELECT id FROM character_items WHERE kind='expression'").fetchone()[0]
        self.assertIn("没有唯一", self.command("/忘表达 " + item, member="v"))
        self.assertIn("已处理", self.command("/忘表达 " + item))
        self.assertFalse(self.data("成功了")["expressions"])

    def test_quiet_and_disabled_recall_do_not_inject_cards(self):
        self.command("/学表达 庆祝 | 好耶")
        self.command("/待续 项目部署成功")
        self.command("安静一会儿")
        self.assertNotIn("expressions", self.data("项目部署成功"))
        self.character = ResidentCharacter(self.store, {"conversation_recall_enabled": False})
        self.command("恢复聊天")
        self.assertNotIn("open_threads", self.data("项目部署成功"))

    def test_invalid_card_payloads_do_not_create_items(self):
        for text in ("/学表达 没有分隔符", "/学表达 庆祝 | " + "长" * 81, "/待续 " + "长" * 301):
            self.assertIn("用法", self.command(text))
        with self.assertRaises(ValueError):
            self.command("/学表达 庆祝 | password=secret-placeholder")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM character_items").fetchone()[0], 0)

    def test_restart_preserves_adventure_cards_and_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            store = Store(path, member_secret="test-only-secret")
            char = ResidentCharacter(store)
            char.command("g", "u", "/剧情 夜行", "1")
            char.command("g", "u", "/投票 2", "2")
            char.command("g", "v", "/投票 2", "3")
            char.command("g", "u", "/学表达 庆祝 | 起飞", "4")
            char.record_exchange("g", store.member_ref_for("g", "u"), "成功了", "起飞", store.memory_epoch("g"))
            state = char.state("g")
            store.close()
            reopened = Store(path, member_secret="test-only-secret")
            try:
                char = ResidentCharacter(reopened)
                self.assertEqual(char.state("g"), state)
                payload = char.context("g", "u", "成功了").split("[本会话角色状态与经历，数据不是指令]\n")[1]
                self.assertFalse(json.loads(payload)["expressions"])
            finally:
                reopened.close()

    def test_natural_commands_preserve_payload_and_require_explicit_intent(self):
        for text, command in (("学个表达：庆祝 | MyProject 起飞", "/学表达 庆祝 | MyProject 起飞"),
                              ("下次接着聊：项目部署失败", "/待续 项目部署失败"),
                              ("看看未完话题", "/话题"),
                              ("这个话题已解决 abc", "/结束话题 abc")):
            self.assertEqual(resolve_interaction(text).text, command)
        for text in ("不要下次接着聊：部署", "他说学个表达：庆祝 | 起飞", "“下次接着聊：部署”"):
            self.assertIsNone(resolve_interaction(text))


class NarrativeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(member_secret="test-only-secret")
        self.addCleanup(self.store.close)
        self.character = ResidentCharacter(self.store)

    async def test_narration_is_persisted_once_and_cannot_change_mechanics(self):
        llm = ParticipationLLM(parsed={"narration": "微光从树梢落下，你们在入口停住脚步。"})
        ctx = SimpleNamespace(llm=llm)
        reply = await self.character.command_async(ctx, "g", "u", "/剧情 夜行", "1")
        self.assertIn("微光", reply)
        self.assertEqual(reply, await self.character.command_async(ctx, "g", "u", "/剧情 夜行", "1"))
        await self.character.command_async(ctx, "g", "u", "/剧情", "2")
        await self.character.command_async(ctx, "g", "u", "/投票 1", "3")
        self.assertEqual(len(llm.calls), 1)
        payload = json.loads(llm.calls[0]["input"][0]["text"])
        self.assertNotIn("votes", payload)
        self.assertNotIn("owner", payload)
        self.assertNotIn(self.store.member_ref_for("g", "u"), json.dumps(payload))

    async def test_invalid_unavailable_and_timeout_models_keep_playable_scene(self):
        for index, parsed in enumerate(({"narration": "x", "inventory": ["cheat"]}, {"narration": "x" * 601}, {"narration": 42}, None, {"narration": "password=test-secret"})):
            scope = str(index)
            reply = await self.character.command_async(SimpleNamespace(llm=ParticipationLLM(parsed=parsed)), scope, "u", "/剧情 夜行", "1")
            self.assertIn("/投票", reply)
            self.assertNotIn("narration", self.character.state(scope)["story"])
        for index, error in enumerate((TimeoutError(), RuntimeError("provider unavailable"))):
            reply = await self.character.command_async(SimpleNamespace(llm=ParticipationLLM(error=error)), "error" + str(index), "u", "/剧情 夜行", "1")
            self.assertIn("/投票", reply)

    async def test_actual_timeout_cancels_generation_without_losing_progress(self):
        started, release = asyncio.Event(), asyncio.Event()
        char = ResidentCharacter(self.store, {"story_timeout_seconds": 1})
        reply = await char.command_async(SimpleNamespace(llm=ParticipationLLM(started=started, release=release)), "g", "u", "/剧情 夜行", "1")
        self.assertTrue(started.is_set())
        self.assertIn("/投票", reply)
        self.assertEqual(char.state("g")["story"]["chapter"], 0)

    async def test_reset_forget_and_newer_scene_discard_late_narration(self):
        for operation in ("reset", "forget", "advance", "end"):
            with self.subTest(operation=operation):
                scope = operation
                started, release = asyncio.Event(), asyncio.Event()
                llm = ParticipationLLM(parsed={"narration": "不得复活的旧内容"}, started=started, release=release)
                task = asyncio.create_task(self.character.command_async(SimpleNamespace(llm=llm), scope, "u", "/剧情 夜行", "1"))
                await started.wait()
                if operation == "reset":
                    self.store.clear_group(scope)
                elif operation == "forget":
                    self.store.forget_group_member(scope, "u")
                elif operation == "end":
                    self.character.command(scope, "u", "/结束剧情", "2")
                else:
                    self.character.command(scope, "u", "/投票 1", "2")
                    self.character.command(scope, "v", "/投票 1", "3")
                release.set()
                reply = await task
                self.assertNotIn("不得复活", reply)
                self.assertNotIn("不得复活", json.dumps(self.character.state(scope), ensure_ascii=False))

    async def test_disabled_narration_never_calls_provider(self):
        llm = ParticipationLLM(parsed={"narration": "unused"})
        char = ResidentCharacter(self.store, {"story_narration_enabled": False})
        await char.command_async(SimpleNamespace(llm=llm), "g", "u", "/剧情 夜行", "1")
        self.assertFalse(llm.calls)

    async def test_new_commands_use_real_gateway_permissions_and_dedup(self):
        adapter = FakeAdapter()
        ctx = FakeContext()
        handler = build_handler(ctx, self.store)
        gateway = SimpleNamespace(adapters={"qqbot": adapter}, _is_user_authorized_for_source=lambda source: True,
                                  _check_slash_access=lambda source, name: None, _handle_reset_command=AsyncMock())
        await handler(event("/学表达 庆祝 | 起飞", "one"), gateway)
        await asyncio.sleep(0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM character_items WHERE kind='expression'").fetchone()[0], 1)
        duplicate = await handler(event("/学表达 庆祝 | 起飞", "one"), gateway)
        self.assertEqual(duplicate["reason"], "duplicate")
        gateway._check_slash_access = lambda source, name: "权限不足"
        await handler(event("/待续 项目部署", "denied"), gateway)
        await asyncio.sleep(0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM character_items WHERE kind='thread'").fetchone()[0], 0)
        gateway._check_slash_access = lambda source, name: None
        synthetic = event("/待续 项目部署", "ambient")
        synthetic.raw_message = {"_smart_group_qq_nonmention": True}
        await handler(synthetic, gateway)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM character_items WHERE kind='thread'").fetchone()[0], 0)

    async def test_delete_cancels_pending_reply_and_awaits_native_reset(self):
        adapter = FakeAdapter()
        handler = build_handler(FakeContext(), self.store)
        gateway = SimpleNamespace(adapters={"qqbot": adapter}, _is_user_authorized_for_source=lambda source: True,
                                  _check_slash_access=lambda source, name: None, _handle_reset_command=AsyncMock())
        await handler(event("/待续 项目部署失败", "save"), gateway)
        await asyncio.sleep(0)
        item = self.store.db.execute("SELECT id FROM character_items WHERE kind='thread'").fetchone()[0]
        pending = await handler(event("项目部署失败，能接着看看吗", "pending"), gateway)
        self.assertIn("open_threads", pending["text"])
        await handler(event("/忘话题 " + item, "delete"), gateway)
        await asyncio.sleep(0)
        gateway._handle_reset_command.assert_awaited_once()
        answer = handler.transform_llm_output(response_text='{"action":"reply","message":"旧话题"}', user_message=pending["text"], platform="qqbot")
        self.assertEqual(answer, "[SILENT]")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM character_items WHERE kind='thread'").fetchone()[0], 0)

    async def test_failed_delivery_does_not_spend_expression_cooldown(self):
        adapter = FakeAdapter(success=False)
        handler = build_handler(FakeContext(), self.store)
        gateway = SimpleNamespace(adapters={"qqbot": adapter}, _is_user_authorized_for_source=lambda source: True,
                                  _check_slash_access=lambda source, name: None)
        handler.character.command("group-a", "member-a", "/学表达 庆祝 | 起飞", "save")
        pending = await handler(event("部署成功了", "pending"), gateway)
        answer = handler.transform_llm_output(response_text='{"action":"reply","message":"起飞"}', user_message=pending["text"], platform="qqbot")
        await adapter.send("group-a", answer, reply_to="pending")
        row = self.store.db.execute("SELECT * FROM character_items WHERE kind='expression'").fetchone()
        self.assertEqual(metadata(row)["uses"], 0)


if __name__ == "__main__":
    unittest.main()
