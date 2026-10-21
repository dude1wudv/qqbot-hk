"""Complete QQ answers and one disposable waiting notice per active request."""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
from smart_group_qq.formatter import (
    format_for_qq, is_explicit_request, prepare_group_reply, split_group_reply, split_message,
)
from smart_group_qq.response import OUTBOUND_KIND, ReplyRegistry, ReplyRequest


class CompleteOutputTests(unittest.TestCase):
    def test_natural_task_without_old_detail_keyword_keeps_the_entire_letter(self):
        letter = "亲爱的朋友。" + "我希望认真向你道歉，并把事情经过说明白。" * 30
        self.assertEqual(prepare_group_reply(letter, "帮我写封道歉信"), letter)
        self.assertTrue(is_explicit_request("帮我写封道歉信"))
        self.assertFalse(is_explicit_request("你好，小栖"))
        self.assertFalse(is_explicit_request("终于跑通了"))

    def test_long_code_spaces_blank_lines_and_urls_survive_packet_boundaries(self):
        values = [
            "    x='" + "a" * 1511 + "'\n    return x\n",
            "steps:\n  - run: " + "命令 " * 700 + "\n\n\n    next: true\n",
            "https://example.invalid/?query=" + "a" * 4000,
            "中文 English " * 600,
        ]
        for value in values:
            with self.subTest(value=value[:30]):
                pieces = split_message(value)
                self.assertEqual("".join(pieces), value)
                self.assertTrue(all(0 < len(piece) <= 1500 and piece.strip() for piece in pieces))
        code = "```python\n" + values[0] + "\n\n    print(x)\n```"
        rendered = format_for_qq(code)
        self.assertIn("\n\n\n    print(x)", rendered)
        self.assertEqual("".join(split_group_reply(code, direct=True)), rendered)
        self.assertEqual(split_message(""), [])

    def test_json_keys_and_literals_are_not_reinterpreted_as_markdown(self):
        payload = '{\n    "foo_bar_baz": "**literal**",\n    "value": "a_b_c"\n}'
        self.assertEqual(format_for_qq(payload), payload)
        self.assertEqual(split_group_reply(payload, direct=True), [payload])


class ProgressLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def registry(self, *, direct=True, eligible=True):
        self.epoch = 0
        self.audits, self.delivered, self.states = [], [], []
        registry = ReplyRegistry(lambda _: self.epoch,
                                 lambda *args, **kw: self.audits.append((args, kw)),
                                 lambda record, result: self.delivered.append(record))
        record = ReplyRequest("a" * 32, "scope", "member", "message", 0,
                              "official", ("message",), "帮我检查问题", direct,
                              progress_eligible=eligible,
                              delivery_callback=lambda item, state: self.states.append(state))
        registry.register(record)
        return registry, record

    async def test_notice_is_attempted_once_without_consuming_answer_or_delivery_receipt(self):
        registry, record = self.registry()
        notices = []

        async def send():
            notices.append(OUTBOUND_KIND.get())
            return SimpleNamespace(success=True)

        await registry.start_progress(record, send, delay_seconds=0)
        self.assertEqual(notices, ["progress"])
        self.assertEqual(OUTBOUND_KIND.get(), "final")
        self.assertIsNone(registry.start_progress(record, send, delay_seconds=0))
        self.assertFalse(record.consumed)
        self.assertIsNone(record.pending_message)
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.states, [])
        self.assertTrue(registry.send_allowed(record))

    async def test_background_or_casual_turn_never_schedules_notice(self):
        async def send():
            self.fail("unexpected progress send")

        for direct, eligible in ((False, True), (True, False)):
            registry, record = self.registry(direct=direct, eligible=eligible)
            self.assertIsNone(registry.start_progress(record, send, delay_seconds=0))

    async def test_final_output_cancels_notice_and_keeps_final_deliverable(self):
        registry, record = self.registry()
        notices = []

        async def send():
            notices.append(True)

        task = registry.start_progress(record, send, delay_seconds=10)
        registry.transform('{"action":"reply","message":"已完成。"}',
                           "[群对话标记:" + record.request_ref + "]")
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(notices, [])
        self.assertEqual(record.pending_message, "已完成。")
        self.assertTrue(registry.send_allowed(record))

    async def test_epoch_generation_and_group_cancellation_suppress_late_notice(self):
        async def send():
            self.fail("stale progress was delivered")

        registry, record = self.registry()
        task = registry.start_progress(record, send, delay_seconds=0)
        self.epoch += 1
        await task
        registry, record = self.registry()
        await registry.start_progress(record, send, delay_seconds=0, is_current=lambda: False)
        registry, record = self.registry()
        task = registry.start_progress(record, send, delay_seconds=10)
        registry.cancel_group("scope")
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.states, ["cancelled"])

    async def test_finalizer_thread_can_cancel_the_timer(self):
        registry, record = self.registry()

        async def send():
            self.fail("finished worker emitted a notice")

        task = registry.start_progress(record, send, delay_seconds=10)
        await asyncio.to_thread(registry.finish_progress, record)
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(record.phase, "ready")

    async def test_unknown_notice_result_is_not_retried(self):
        registry, record = self.registry()
        attempts = []

        async def send():
            attempts.append(True)
            raise TimeoutError()

        await registry.start_progress(record, send, delay_seconds=0)
        self.assertIsNone(registry.start_progress(record, send, delay_seconds=0))
        self.assertEqual(attempts, [True])
        self.assertFalse(record.consumed)

    async def test_receipt_states_distinguish_known_failure_from_unknown_result(self):
        registry, record = self.registry()
        record.record_on_success = False
        registry.note_delivery(record, "sending")
        registry.finish_send(record, SimpleNamespace(success=False), failure_state="failed")
        self.assertTrue(registry.send_allowed(record))
        self.assertEqual(self.states, ["sending", "failed"])
        registry.finish_send(record, SimpleNamespace(success=False))
        self.assertEqual(self.states[-1], "unknown")
        self.assertFalse(registry.send_allowed(record))
        self.assertEqual(self.delivered, [])

    async def test_live_execution_survives_orphan_ttl_but_keeps_epoch_and_generation_guards(self):
        registry, record = self.registry()
        current = [True]
        self.assertTrue(registry.begin_execution(record, lambda: current[0]))
        record.created_at = time.monotonic() - 10_000
        registry.cleanup()
        self.assertIs(registry.lookup("scope", "message"), record)
        self.assertTrue(registry.send_allowed(record))
        registry.finish_execution(record)
        self.assertFalse(record.execution_active)
        self.assertTrue(registry.send_allowed(record))
        current[0] = False
        self.assertFalse(registry.send_allowed(record))
        current[0] = True
        self.epoch += 1
        self.assertFalse(registry.send_allowed(record))

    async def test_send_confirmed_after_reset_never_recreates_memory_callback(self):
        registry, record = self.registry()
        record.pending_message = "old answer"
        self.assertTrue(registry.begin_execution(record))
        self.epoch += 1
        registry.finish_send(record, SimpleNamespace(success=True))
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.states, ["cancelled"])
        self.assertFalse(record.execution_active)
        self.assertFalse(registry.send_allowed(record))

    async def test_progress_is_not_itself_an_execution_owner_and_cleanup_ends_expired_owner(self):
        registry, record = self.registry()

        async def send():
            self.fail("expired turn emitted a notice")

        task = registry.start_progress(record, send, delay_seconds=10)
        self.assertFalse(record.execution_active)
        record.created_at = time.monotonic() - 10_000
        registry.cleanup()
        await asyncio.gather(task, return_exceptions=True)
        self.assertIsNone(registry.lookup("scope", "message"))
        self.assertFalse(record.execution_active)


if __name__ == "__main__":
    unittest.main()
