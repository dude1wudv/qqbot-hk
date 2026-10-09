import json
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
import sys

def _reply_envelope_with_unescaped_controls(message):
    encoded = json.dumps(message, ensure_ascii=False)
    for escaped, actual in (("\\r", "\r"), ("\\n", "\n"), ("\\t", "\t")):
        encoded = encoded.replace(escaped, actual)
    return '{"action":"reply","message":' + encoded + '}'

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

from smart_group_qq.formatter import format_for_qq, split_group_reply

from smart_group_qq.response import (
    INVALID_REPLY_MESSAGE,
    ReplyRegistry,
    ReplyRequest,
    SILENT_MARKER,
    message_text_parts,
    parse_reply_decision,
)


class ResponseTests(unittest.TestCase):
    def test_parse_accepts_only_the_two_field_envelope(self):
        self.assertEqual(parse_reply_decision('{"action":"reply","message":"  先检查超时。 "}'), ("reply", "先检查超时。"))
        self.assertEqual(parse_reply_decision('{"action":"ignore","message":"不应发送的解释"}'), ("ignore", None))

        for value in (
            "not json",
            "[]",
            '{"action":"reply","message":"ok","extra":1}',
            '{"action":"react","message":"ok"}',
            '{"action":"reply","message":"   "}',
            '{"action":"ignore","message":3}',
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_reply_decision(value)
    def test_parse_extracts_json_from_common_model_wrappers(self):
        cases = (
            ('Here is the final answer:\n{"action":"reply","message":"答案"}', ("reply", "答案")),
            ('```json\n{"action":"reply","message":"答案"}\n```\nDone.', ("reply", "答案")),
            ('{"action":"ignore","message":null}\n(Do not send)', ("ignore", None)),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(parse_reply_decision(value), expected)


    def test_parse_accepts_unescaped_control_characters_in_reply_messages(self):
        message_cases = (
            "第一行\n第二行",
            "第一行\r\n第二行",
            '第一行\t包含 "引号" 和 \\反斜杠',
        )
        for message in message_cases:
            with self.subTest(message=message):
                raw = _reply_envelope_with_unescaped_controls(message)
                expected = ("reply", message)
                self.assertEqual(parse_reply_decision(raw), expected)
                self.assertEqual(
                    parse_reply_decision("模型说明：\n" + raw + "\n以上是结果。"),
                    expected,
                )

        escaped_message = '第一行\n包含 "引号" 和 \\反斜杠'
        escaped = json.dumps({"action": "reply", "message": escaped_message}, ensure_ascii=False)
        self.assertEqual(parse_reply_decision(escaped), ("reply", escaped_message))

    def test_marker_extraction_supports_text_and_multimodal_parts_only(self):
        marker = "[群对话标记:" + "a" * 32 + "]"
        self.assertEqual(message_text_parts(marker + " question"), (marker + " question",))
        self.assertEqual(
            message_text_parts([
                {"type": "text", "text": marker},
                {"type": "image_url", "image_url": {"url": "secret"}},
                {"type": "text", "text": "question"},
            ]),
            (marker, "question"),
        )
        self.assertEqual(message_text_parts({"text": marker}), ())

    def _registry(self, epoch=0):
        audits = []
        delivered = []
        registry = ReplyRegistry(
            lambda _group: epoch,
            lambda *args, **kwargs: audits.append((args, kwargs)),
            lambda record, result: delivered.append((record, result)),
        )
        return registry, audits, delivered

    def _record(self, ref="a" * 32, *, direct=False, created_at=None):
        return ReplyRequest(
            request_ref=ref,
            group_id="group-a",
            member_ref="member-a",
            message_id="message-a",
            epoch=0,
            source_kind="addressed" if direct else "nonmention",
            merged_ids=("message-a",),
            question="请检查服务",
            direct=direct,
            created_at=time.monotonic() if created_at is None else created_at,
        )

    def test_parsed_group_answer_is_not_interpreted_as_another_envelope(self):
        literal = '{"action":"ignore","message":null}'
        for direct in (False, True):
            with self.subTest(direct=direct):
                registry, audits, delivered = self._registry()
                record = self._record(direct=direct)
                self.assertTrue(registry.register(record))
                output = registry.transform(
                    json.dumps({"action": "reply", "message": literal}),
                    "[群对话标记:" + record.request_ref + "]",
                )
                self.assertEqual(output, literal)
                self.assertEqual(format_for_qq(output), literal)
                self.assertEqual(split_group_reply(output, direct=direct), [literal])
                self.assertEqual(record.pending_message, literal)
                self.assertEqual(audits, [])
                registry.finish_send(record, SimpleNamespace(success=True))
                self.assertEqual(len(delivered), 1)
                self.assertEqual(delivered[0][0].pending_message, literal)

    def test_native_reply_envelope_is_unwrapped_before_plain_text_formatting(self):
        registry, _, _ = self._registry()
        raw = (
            "{'action':\"reply\",\"message\":\"确实，官key便宜了。\\n\\n"
            "**luna** 留给长推理。\"}"
        )
        output = registry.transform(raw, "无标记的原生 QQ 请求")
        self.assertEqual(output, "确实，官key便宜了。\n\n**luna** 留给长推理。")
        self.assertEqual(format_for_qq(output), "确实，官key便宜了。\n\nluna 留给长推理。")
        self.assertEqual(split_group_reply(output)[0], "确实，官key便宜了。")

    def test_unmarked_and_private_native_envelopes_are_converted_once(self):
        literal = '{"action":"ignore","message":null}'
        cases = (
            ('{"action":"reply","message":"  **答案**  "}', "**答案**"),
            ("{'action':'reply','message':'答案'}", "答案"),
            ('```json\n{"action":"reply","message":"答案"}\n```', "答案"),
            (json.dumps({"action": "reply", "message": literal}), literal),
            ('{"action":"ignore","message":null}', SILENT_MARKER),
            ('{"action":"ignore","message":"不应发送"}', SILENT_MARKER),
            ("普通回答", "普通回答"),
            ('{"other":"普通 JSON"}', '{"other":"普通 JSON"}'),
        )
        for private in (False, True):
            for raw, expected in cases:
                with self.subTest(private=private, raw=raw):
                    registry, audits, delivered = self._registry()
                    user_message = "无标记的原生 QQ 请求"
                    if private:
                        record = self._record(direct=True)
                        record.source_kind = "private"
                        self.assertTrue(registry.register(record))
                        user_message = "[群对话标记:" + record.request_ref + "]"
                    self.assertEqual(registry.transform(raw, user_message), expected)
                    self.assertEqual(delivered, [])
                    if private and expected == SILENT_MARKER:
                        self.assertEqual(registry.size, 0)
                        self.assertIsNone(record.pending_message)
                        self.assertEqual(audits[-1][0][0], "output_ignore")
                    elif private:
                        self.assertEqual(record.pending_message, expected)
                        self.assertEqual(registry.size, 1)
                    else:
                        self.assertEqual(registry.size, 0)
                        self.assertEqual(audits, [])

    def test_registry_accepts_unescaped_control_characters_and_delivers_once(self):
        message = '第一行\n第二行\r\n第三行\t含有 "引号" 和 \\反斜杠'
        raw = _reply_envelope_with_unescaped_controls(message)
        for index, direct in enumerate((False, True)):
            with self.subTest(direct=direct):
                registry, audits, delivered = self._registry()
                record = self._record(ref=("2" if index == 0 else "3") * 32, direct=direct)
                self.assertTrue(registry.register(record))
                marker = "[群对话标记:" + record.request_ref + "]"
                self.assertEqual(registry.transform(raw, marker), message)
                self.assertEqual(record.pending_message, message)
                self.assertIs(registry.pending_for_send("group-a", "message-a"), record)
                self.assertTrue(registry.send_allowed(record))
                self.assertFalse(any(item[0][0] in ("output_invalid", "output_fallback") for item in audits))

                registry.finish_send(record, SimpleNamespace(success=True))
                registry.finish_send(record, SimpleNamespace(success=True))
                self.assertEqual(registry.size, 0)
                self.assertEqual(len(delivered), 1)
                self.assertIs(delivered[0][0], record)
                self.assertEqual(delivered[0][0].pending_message, message)

    def test_silent_marker_is_not_plain_text_fallback(self):
        registry, audits, _ = self._registry()
        record = self._record(ref="4" * 32)
        self.assertTrue(registry.register(record))
        output = registry.transform(SILENT_MARKER, "[群对话标记:" + record.request_ref + "]")
        self.assertEqual(output, SILENT_MARKER)
        self.assertEqual(registry.size, 0)
        self.assertIsNone(record.pending_message)
        self.assertFalse(any(item[0][0] == "output_fallback" for item in audits))


    def test_registry_is_fail_closed_for_ignore_invalid_epoch_cancel_and_expiry(self):
        registry, audits, _ = self._registry()
        record = self._record()
        self.assertTrue(registry.register(record))
        marker = "[群对话标记:" + record.request_ref + "]"
        self.assertEqual(registry.transform('{"action":"ignore","message":"解释"}', marker), SILENT_MARKER)
        self.assertEqual(registry.size, 0)
        self.assertEqual(audits[-1][0][0], "output_ignore")

        malformed_outputs = (
            "{bad",
            '{"action":"reply","message":"truncated',
            '{"action":"reply","message: "missing quote"}',
            '{"action":"reply","message":"ok","extra":1}',
        )
        for index, malformed in enumerate(malformed_outputs):
            invalid = self._record(ref=format(index + 5, "032x"))
            registry.register(invalid)
            invalid_marker = "[群对话标记:" + invalid.request_ref + "]"
            self.assertEqual(registry.transform(malformed, invalid_marker), SILENT_MARKER)
            self.assertEqual(audits[-1][0][0], "output_invalid")
        plain = self._record(ref="1" * 32)
        registry.register(plain)
        self.assertEqual(registry.transform("这是一条正常的中文回复。", "[群对话标记:" + plain.request_ref + "]"), "这是一条正常的中文回复。")
        self.assertEqual(plain.pending_message, "这是一条正常的中文回复。")
        self.assertEqual(audits[-1][0][0], "output_fallback")


        cancelled = self._record(ref="c" * 32)
        registry.register(cancelled)
        registry.cancel_group("group-a", ordinary_only=True)
        self.assertEqual(registry.transform('{"action":"reply","message":"迟到"}', "[群对话标记:" + cancelled.request_ref + "]"), SILENT_MARKER)

        expired = self._record(ref="d" * 32, created_at=time.monotonic() - 901)
        registry.register(expired)
        self.assertEqual(registry.transform('{"action":"reply","message":"过期"}', "[群对话标记:" + expired.request_ref + "]"), SILENT_MARKER)

    def test_direct_invalid_output_gets_one_deterministic_message(self):
        registry, audits, _ = self._registry()
        record = self._record(ref="e" * 32, direct=True)
        registry.register(record)
        output = registry.transform('{"action":"reply","message":3}', "[群对话标记:" + record.request_ref + "]")
        self.assertEqual(output, INVALID_REPLY_MESSAGE)
        self.assertEqual(record.pending_message, INVALID_REPLY_MESSAGE)
        self.assertFalse(record.record_on_success)
        self.assertEqual(audits[-1][0][0], "output_invalid")

    def test_send_success_consumes_once_but_failure_keeps_record(self):
        registry, audits, delivered = self._registry()
        record = self._record(ref="f" * 32)
        registry.register(record)
        marker = "[群对话标记:" + record.request_ref + "]"
        self.assertEqual(registry.transform('{"action":"reply","message":"答案"}', marker), "答案")
        self.assertIs(registry.pending_for_send("group-a", "message-a"), record)
        self.assertTrue(registry.send_allowed(record))
        registry.finish_send(record, SimpleNamespace(success=False))
        self.assertEqual(registry.size, 1)
        self.assertEqual(audits[-1][0][0], "reply_failed")
        registry.finish_send(record, SimpleNamespace(success=True))
        self.assertEqual(registry.size, 0)
        self.assertEqual(len(delivered), 1)
        registry.finish_send(record, SimpleNamespace(success=True))
        self.assertEqual(len(delivered), 1)


if __name__ == "__main__":
    unittest.main()
