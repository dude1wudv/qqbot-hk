"""Compatibility, ambiguity and authorization regressions for local intents."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
from smart_group_qq import build_handler
from smart_group_qq.character import ResidentCharacter, quiet_duration
from smart_group_qq.commands import normalize_command_text, parse_profile_command
from smart_group_qq.interaction import resolve_interaction
from smart_group_qq.store import Store
from test_smart_group_qq import FakeContext, FakeAdapter, event


class ParsingTests(unittest.TestCase):
    def test_configuration_equivalence(self):
        for raw in (
            "切到 DeepSeek",
            "切到DeepSeek吧",
            "请把模型切换到 DeepSeek",
            "/deepseek",
            "／ ＤＥＥＰＳＥＥＫ",
            "/配置 模型：DeepSeek",
            "/设置 model=DeepSeek",
            "/MODEL DeepSeek --session",
            "小栖，切到DeepSeek",
        ):
            with self.subTest(raw=raw):
                result = resolve_interaction(raw, wake_words=("小栖",))
                self.assertEqual(result.text, "/deepseek")
                self.assertFalse(result.error)
        for raw in (
            "推理调高",
            "推理强度调到高",
            "/推理 高",
            "/配置 推理强度：high",
            "／ HIGH",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(resolve_interaction(raw).text, "/high")

    def test_queries_and_private_native_compatibility(self):
        for raw, expected in (
            ("/配置 模型", "/model"),
            ("/配置 推理强度", "/reasoning"),
            ("现在用的什么模型？", "/model"),
        ):
            self.assertEqual(resolve_interaction(raw).text, expected)
            self.assertTrue(resolve_interaction(raw).native)
        raw = "/model custom/model --provider custom"
        self.assertEqual(resolve_interaction(raw, private=True).text, raw)
        self.assertTrue(resolve_interaction(raw).error)
        self.assertTrue(resolve_interaction("/配置 不存在 true").error)
        self.assertTrue(resolve_interaction("/reasoning high --global").error)

    def test_chat_never_changes_configuration(self):
        for text in (
            "不要切到DeepSeek",
            "如果切到DeepSeek会怎样",
            "怎么切到DeepSeek",
            "“切到DeepSeek”",
            "他说切到DeepSeek",
            "切到DeepSeek并删除记忆",
            "切到DeepSeek\n再来聊天",
            "这个模型推理强度很高",
            "把名字改成土豆",
            "新增目标是什么意思",
            "完成目标的方法是什么",
        ):
            with self.subTest(text=text):
                self.assertIsNone(resolve_interaction(text))

    def test_payload_and_command_boundaries(self):
        raw = "／ 记住我：项目叫 MyProject ＡＢＣ，联系人 <@member> 负责测试"
        parsed = parse_profile_command(raw)
        self.assertEqual(
            parsed.argument, "项目叫 MyProject ＡＢＣ，联系人 <@member> 负责测试"
        )
        for raw in ("/忘记我这个命令怎么用", "/停止记忆是什么意思", "/忘记我 不要执行"):
            self.assertIsNone(parse_profile_command(raw))
        self.assertTrue(resolve_interaction("/忘记我 不要执行").error)
        self.assertEqual(normalize_command_text("／ STATUS"), "/status")

    def test_context_required_for_pronouns(self):
        self.assertTrue(resolve_interaction("以后叫它土豆吧").error)
        self.assertEqual(
            resolve_interaction("以后叫它土豆吧", pet_focused=True).text,
            "/宠物取名 土豆",
        )
        self.assertEqual(
            resolve_interaction("把宠物改名为“土豆”").text, "/宠物取名 土豆"
        )
        self.assertTrue(resolve_interaction("这个梗你可得记着").error)

    def test_quiet_duration_validation(self):
        self.assertEqual(quiet_duration("半小时"), 1800)
        self.assertEqual(quiet_duration("10分钟"), 600)
        for raw in ("0分钟", "25小时", "-1分钟", "永久"):
            with self.assertRaises(ValueError):
                quiet_duration(raw)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.adapter = FakeAdapter()
        self.gateway = SimpleNamespace(
            adapters={"qqbot": self.adapter},
            _session_key_for_source=lambda source: "qqbot:group:" + source.chat_id,
        )
        self.handler = build_handler(FakeContext(), self.store)
        self.char = ResidentCharacter(self.store)

    async def send(self, text, mid, member="member-a", **kwargs):
        result = self.handler(event(text, mid, member=member, **kwargs), self.gateway)
        await asyncio.sleep(0)
        return result

    async def test_models_full_ids_aliases_and_effort_in_both_scopes(self):
        for chat_type in ("group", "dm"):
            for alias, model in (("DeepSeek", "deepseek/deepseek-v4.1-flash"),):
                for raw in (f"/{alias}", f"<@bot>/{alias}", f"\u200b/{alias}", f"/model {model}", f"/配置 模型 {model}"):
                    with self.subTest(chat_type=chat_type, raw=raw):
                        result = await self.send(raw, chat_type + raw, chat_type=chat_type)
                        self.assertEqual(result, {"action": "rewrite", "text": f"/model {model} --session"})
            result = await self.send("\ufeff<@bot>/XHIGH", chat_type + "effort", chat_type=chat_type)
            self.assertEqual(result, {"action": "rewrite", "text": "/reasoning xhigh"})
        payload = "/model Vendor/MyCaseSensitiveModel --provider MyProvider"
        self.assertEqual((await self.send(payload, "native-payload", chat_type="dm"))["text"], payload)
        raw = "\u200b<@bot>/记住我：项目叫 MyProject ＡＢＣ"
        self.assertEqual(parse_profile_command(raw).argument, "项目叫 MyProject ＡＢＣ")

    async def test_natural_configuration_uses_native_session_scope(self):
        for index, raw in enumerate(
            ("切到DeepSeek", "/配置 模型 DeepSeek", "／ DEEPSEEK")
        ):
            result = await self.send(raw, str(index))
            self.assertEqual(
                result,
                {
                    "action": "rewrite",
                    "text": "/model deepseek/deepseek-v4.1-flash --session",
                },
            )
        self.assertEqual(
            (await self.send("推理调高", "high"))["text"], "/reasoning high"
        )

    async def test_denied_sender_cannot_mutate_or_reply(self):
        self.gateway._is_user_authorized_for_source = lambda source: False
        result = await self.send("把宠物改名为土豆", "denied")
        self.assertEqual(result, {"action": "allow"})
        self.assertEqual(self.adapter.sent, [])
        self.assertNotEqual(self.char.state("group-a")["pet"]["name"], "土豆")

    async def test_slash_permission_applies_to_natural_commands(self):
        self.gateway._check_slash_access = lambda source, name: "没有权限"
        result = await self.send("把宠物改名为土豆", "denied-slash")
        self.assertEqual(result["action"], "skip")
        self.assertIn("没有权限", self.adapter.sent[-1][1])
        self.assertNotEqual(self.char.state("group-a")["pet"]["name"], "土豆")

    async def test_pet_focus_owner_and_duplicate_clarification(self):
        await self.send("以后叫它土豆吧", "ambiguous")
        self.assertIn("你是想", self.adapter.sent[-1][1])
        await self.send("看看宠物", "focus")
        result = await self.send("以后叫它土豆吧", "ambiguous")
        self.assertEqual(result["reason"], "duplicate")
        await self.send("以后叫它土豆吧", "other", member="member-b")
        self.assertNotEqual(self.char.state("group-a")["pet"]["name"], "土豆")
        await self.send("以后叫它土豆吧", "rename")
        self.assertEqual(self.char.state("group-a")["pet"]["name"], "土豆")

    async def test_goal_name_resolution_is_owned_and_unambiguous(self):
        await self.send("新增目标：部署测试", "g1")
        await self.send("新增目标：部署文档", "g2")
        await self.send("完成目标 部署", "ambiguous-goal")
        self.assertNotIn("已处理", self.adapter.sent[-1][1])
        await self.send("完成目标 部署测试", "other-goal", member="member-b")
        self.assertNotIn("已处理", self.adapter.sent[-1][1])
        await self.send("完成目标 部署测试", "complete")
        self.assertIn("已处理", self.adapter.sent[-1][1])

    async def test_nonaddressed_message_cannot_execute(self):
        incoming = event("把宠物改名为土豆", "ambient")
        incoming.raw_message = {"_smart_group_qq_nonmention": True}
        self.handler(incoming, self.gateway)
        await asyncio.sleep(0)
        self.assertNotEqual(self.char.state("group-a")["pet"]["name"], "土豆")

    async def test_private_and_group_pet_state_are_separate(self):
        await self.send("把宠物改名为私聊土豆", "private-name", chat_type="dm")
        self.assertEqual(self.char.state("dm:group-a")["pet"]["name"], "私聊土豆")
        self.assertNotEqual(self.char.state("group-a")["pet"]["name"], "私聊土豆")
