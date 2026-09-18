import time
import unittest
from types import SimpleNamespace
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

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

    def test_registry_is_fail_closed_for_ignore_invalid_epoch_cancel_and_expiry(self):
        registry, audits, _ = self._registry()
        record = self._record()
        self.assertTrue(registry.register(record))
        marker = "[群对话标记:" + record.request_ref + "]"
        self.assertEqual(registry.transform('{"action":"ignore","message":"解释"}', marker), SILENT_MARKER)
        self.assertEqual(registry.size, 0)
        self.assertEqual(audits[-1][0][0], "output_ignore")

        invalid = self._record(ref="b" * 32)
        registry.register(invalid)
        invalid_marker = "[群对话标记:" + invalid.request_ref + "]"
        self.assertEqual(registry.transform("{bad", invalid_marker), SILENT_MARKER)
        self.assertEqual(audits[-1][0][0], "output_invalid")

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
        output = registry.transform("not-json", "[群对话标记:" + record.request_ref + "]")
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
