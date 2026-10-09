"""Regressions for actual group-chat failure modes (not a live QQ session)."""
import asyncio
import json
from pathlib import Path
import runpy
import sqlite3
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins'))
from smart_group_qq import _wake_hit, _DEFAULT_WAKE_WORDS, build_handler, qq_observer
from smart_group_qq.formatter import prepare_group_reply, split_group_reply
from smart_group_qq.response import ReplyRegistry, ReplyRequest, parse_reply_decision, SILENT_MARKER
from smart_group_qq.store import Store
from test_smart_group_qq import FakeContext, ParticipationLLM, ObserverAdapter, observer_payload


class ChatReliabilityTests(unittest.TestCase):
    def registry(self, question='我从球状闪电入门，已经忘了', direct=True, private=False):
        registry = ReplyRegistry(lambda _: 0, lambda *a, **k: None, lambda *a: None)
        record = ReplyRequest('a' * 32, 'g', 'u', 'm', 0, 'private' if private else 'official', ('m',), question, direct)
        registry.register(record)
        return registry, record, '[群对话标记:' + record.request_ref + ']'

    def test_direct_plain_reply_is_deliverable(self):
        registry, record, marker = self.registry()
        self.assertEqual(registry.transform('只记得几个画面也正常。', marker), '只记得几个画面也正常。')
        self.assertTrue(record.record_on_success)

    def test_plain_code_with_braces_is_not_a_broken_envelope(self):
        registry, _, marker = self.registry(question='请写代码')
        raw = '```python\nvalue = {"ok": True}\n```'
        self.assertEqual(registry.transform(raw, marker), raw)

    def test_silent_marker_is_not_a_format_error_for_mentions(self):
        registry, _, marker = self.registry()
        self.assertEqual(registry.transform(SILENT_MARKER, marker), SILENT_MARKER)

    def test_screenshot_style_reply_is_short_even_when_mentioned(self):
        raw = '从《球状闪电》入门挺有意思。忘了细节也没关系。' + '这种书留在脑子里的往往是几个画面。' * 20 + '\n你呢？'
        for direct in (True, False):
            registry, record, marker = self.registry(direct=direct)
            result = registry.transform(json.dumps({'action': 'reply', 'message': raw}), marker)
            self.assertEqual(result, '从《球状闪电》入门挺有意思。忘了细节也没关系。')
            self.assertEqual(record.pending_message, result)
            self.assertLessEqual(len(split_group_reply(result)), 2)

    def test_explicit_tasks_and_private_chats_preserve_full_answer(self):
        body = '第一步，检查配置。\n' * 100
        for question, private in [('详细解释这本书', False), ('给我排查步骤', False), ('请写代码', False), ('闲聊', True)]:
            registry, _, marker = self.registry(question=question, private=private)
            self.assertEqual(registry.transform(json.dumps({'action': 'reply', 'message': body}), marker), body.strip())

    def test_code_and_urls_are_not_cut_by_casual_safeguard(self):
        for text in ('```python\n' + 'print(1)\n' * 100 + '```', 'https://example.com/' + 'a' * 200):
            self.assertEqual(prepare_group_reply(text, '看看这个'), text)

    def test_long_first_sentence_prefers_complete_clause(self):
        self.assertEqual(prepare_group_reply('忘了细节也正常，' + '记住那些画面就够了' * 40, '我忘了'), '忘了细节也正常。')
        self.assertLessEqual(len(prepare_group_reply('哈' * 500, '闲聊')), 180)
        quoted = prepare_group_reply('他说“' + '长句' * 200 + '”。', '闲聊')
        self.assertLessEqual(len(quoted), 180)
        self.assertTrue(quoted.endswith('…”'))

    def test_short_reply_avoids_canned_followup_and_many_bubbles(self):
        self.assertEqual(prepare_group_reply('终于跑通了。你呢？', '终于跑通'), '终于跑通了。')
        self.assertEqual(prepare_group_reply('报错在哪一行？', '我遇到点问题'), '报错在哪一行？')
        self.assertEqual(len(split_group_reply('好。行。收到。可以。知道了。')), 2)

    def test_parser_rejects_duplicate_keys_and_multiple_decisions(self):
        for raw in ('{"action":"ignore","action":"reply","message":"泄漏"}',
                    "{'action':'reply','message':'甲','message':'乙'}",
                    '{"action":"ignore","message":null}\n{"action":"reply","message":"冲突"}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_reply_decision(raw)

    def test_parser_skips_unrelated_wrapper_object_without_parsing_answer_twice(self):
        raw = '元数据 {"model":"test"}\n{"action":"reply","message":"答案"}'
        self.assertEqual(parse_reply_decision(raw), ('reply', '答案'))
        nested = json.dumps({'action': 'reply', 'message': '{"action":"ignore","message":null}'})
        self.assertEqual(parse_reply_decision(nested)[1], '{"action":"ignore","message":null}')

    def test_broken_envelope_without_braces_is_not_sent_as_prose(self):
        registry, _, marker = self.registry(direct=False)
        self.assertEqual(registry.transform('action: reply, message: 你好', marker), SILENT_MARKER)

    def test_wake_names_require_real_address(self):
        for text in ('botany 很有趣', '“小栖，过来”是示例', '请问有人知道吗？', '帮我看一下？'):
            self.assertFalse(_wake_hit(text, _DEFAULT_WAKE_WORDS), text)
        for text in ('bot 帮我看看', '小栖，过来', '  BOT: hello'):
            self.assertTrue(_wake_hit(text, _DEFAULT_WAKE_WORDS))

    def test_recovery_patch_refuses_changed_upstream(self):
        patcher = runpy.run_path(str(ROOT / 'scripts/patch-hermes-qq-recovery.py'))
        with self.assertRaisesRegex(ValueError, 'SHA mismatch'):
            patcher['patch_source']('upstream changed')
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            patcher['patch_source'](patcher['MARKER'])

    def test_recovery_scope_and_abandonment(self):
        patcher = runpy.run_path(str(ROOT / 'scripts/patch-hermes-qq-recovery.py'))
        namespace = {}
        exec(patcher['HELPER'], namespace)
        applies = namespace['_qq_group_without_anchor']
        self.assertTrue(applies('qqbot', 'agent:main:qqbot:group:g:u'))
        self.assertTrue(applies('qqbot', 'agent:secondary:qqbot:group:g'))
        self.assertFalse(applies('qqbot', 'agent:main:qqbot:dm:u'))
        self.assertFalse(applies('telegram', 'agent:main:telegram:group:g'))
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.execute('CREATE TABLE delivery_obligations(obligation_id TEXT,state TEXT,content TEXT,updated_at REAL,last_error TEXT)')
        conn.execute("INSERT INTO delivery_obligations VALUES ('old','failed','旧正文',0,NULL)")
        namespace['_abandon_qq_group'](conn, 'old', 10)
        self.assertEqual(conn.execute('SELECT state,content FROM delivery_obligations').fetchone(), ('abandoned', ''))


class TriggerReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_help_plus_question_does_not_bypass_participation_classifier(self):
        store = Store(':memory:', member_secret='test-only-secret')
        self.addCleanup(store.close)
        ctx = FakeContext({'ambient': {'participation': {
            'enabled': True, 'debounce_seconds': 0, 'max_wait_seconds': 0,
        }}})
        ctx.llm = ParticipationLLM({'reply': False, 'confidence': 1.0})
        handler = build_handler(ctx, store)
        adapter = ObserverAdapter()
        await qq_observer._observe_message(adapter, observer_payload('help-question', text='帮我看一下怎么解决？'), handler.observe_nonmention)
        await asyncio.sleep(0)
        self.assertEqual(len(ctx.llm.calls), 1)
        self.assertEqual(adapter.dispatched, [])
