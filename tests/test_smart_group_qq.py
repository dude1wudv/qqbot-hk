import asyncio
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

from smart_group_qq import _describe_images, build_handler
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
        dm = event("hello", platform="qqbot", chat_type="dm")
        self.assertEqual(handler(dm, self.gateway)["action"], "allow")
        result = handler(self.make_event("<@bot> hello"), self.gateway)
        self.assertEqual(result["action"], "rewrite")
        self.assertRegex(result["text"], r"^\[群记忆键:[0-9a-f]{12}\]\n")
        self.assertTrue(result["text"].endswith("[群成员:member]: hello"))
        self.assertNotIn("摘要后新增上下文", result["text"])

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

    async def test_post_llm_records_assistant_output(self):
        handler = build_handler(FakeContext(), self.store)
        result = handler(self.make_event("你好", "ask-post"), self.gateway)
        handler.post_llm_call(
            session_id="qqbot:group:group-a", user_message=result["text"],
            assistant_response="你好，群友。", model="deepseek-v4-flash-0731",
            platform=SimpleNamespace(value="qqbot"),
        )
        rows = self.store.get_history("group-a")
        self.assertEqual(rows[-1]["role"], "assistant")
        self.assertEqual(rows[-1]["text"], "你好，群友。")

    async def test_vision_helper_uses_auxiliary_vision_task(self):
        class LLM:
            async def acomplete_structured(inner_self, **kwargs):
                inner_self.kwargs = kwargs
                return {"parsed": {"description": "一张流程图", "visible_text": "发布", "facts": ["箭头向右"]}}

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "sample.png"
            image.write_bytes(b"not-a-real-image-but-valid-for-routing-test")
            ctx = FakeContext({"media_cache_roots": [directory]})
            ctx.llm = LLM()
            description = await _describe_images(ctx, [str(image)])
        self.assertIn("一张流程图", description)
        self.assertEqual(ctx.llm.kwargs["task"], "vision")
        self.assertEqual(ctx.llm.kwargs["input"][1]["type"], "image")

    def test_config_pins_models_and_qq_access_boundaries(self):
        config = yaml.safe_load((ROOT / "config" / "hermes-config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(config["model"]["default"], "deepseek-v4-flash-0731")
        self.assertEqual(config["agent"]["image_input_mode"], "text")
        vision = config["auxiliary"]["vision"]
        self.assertEqual(vision["model"], "gemini-3.8-flash-high")
        self.assertEqual(vision["fallback_chain"][0]["model"], "gpt-5.6-luna")
        qq_extra = config["platforms"]["qqbot"]["extra"]
        self.assertEqual(qq_extra["dm_policy"], "open")
        self.assertEqual(qq_extra["group_policy"], "allowlist")
        self.assertEqual(qq_extra["group_allow_from"], [])


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
