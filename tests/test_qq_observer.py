import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock, patch

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

from smart_group_qq import qq_observer


class FakeQQAdapter:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.original_calls = []
        self.handled = 0
        self.normal_seen = {}
        self.attachment_calls = []
        self.parsed_timestamps = []

    def _dispatch_payload(self, payload):
        self.original_calls.append(payload)
        return "original-result"

    def _is_group_allowed(self, group_id, member_id):
        return self.allowed

    async def _process_attachments(self, attachments):
        self.attachment_calls.append(attachments)
        return {
            "image_urls": ["/opt/data/media/image.jpg"],
            "image_media_types": ["image/jpeg"],
            "voice_transcripts": ["[Voice] hello"],
            "attachment_info": "[file: note.txt (/opt/data/media/note.txt)]",
        }

    def _parse_qq_timestamp(self, raw):
        self.parsed_timestamps.append(raw)
        return datetime(2026, 9, 3, 1, 2, 3, tzinfo=timezone.utc)

    async def handle_message(self, event):
        self.handled += 1


def payload(message_id="message-1", *, group="group-1", member="member-1", text="hello"):
    return {
        "op": 0,
        "t": "GROUP_MESSAGE_CREATE",
        "d": {
            "id": message_id,
            "timestamp": "2026-09-03T01:02:03+00:00",
            "content": text,
            "group_openid": group,
            "author": {"member_openid": member},
            "attachments": [{
                "content_type": "image/jpeg",
                "filename": "image.jpg",
                "url": "https://signed.example.invalid/image.jpg",
            }],
        },
    }


class ObserverTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.resolve = patch.object(qq_observer, "_resolve_adapter_class", return_value=FakeQQAdapter)
        self.resolve.start()
        self.addCleanup(self._restore_patch)

    def _restore_patch(self):
        qq_observer.uninstall_nonmention_observer()
        self.resolve.stop()

    async def test_observes_allowlisted_group_and_normalizes_attachments(self):
        records = []

        async def callback(record):
            records.append(record)

        qq_observer.install_nonmention_observer(callback)
        adapter = FakeQQAdapter()
        result = adapter._dispatch_payload(payload())
        await asyncio.sleep(0)

        self.assertIsNone(result)
        self.assertEqual(adapter.original_calls, [])
        self.assertEqual(adapter.handled, 0)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["group_id"], "group-1")
        self.assertEqual(records[0]["member_id"], "member-1")
        self.assertEqual(records[0]["message_id"], "message-1")
        self.assertIn("hello", records[0]["text"])
        self.assertIn("[Voice] hello", records[0]["text"])
        self.assertIn("note.txt", records[0]["text"])
        self.assertEqual(records[0]["image_paths"], ["/opt/data/media/image.jpg"])
        self.assertEqual(records[0]["media_types"], ["image/jpeg"])
        self.assertEqual(records[0]["attachment_info"], "[file: note.txt (/opt/data/media/note.txt)]")
        self.assertEqual(records[0]["timestamp"].year, 2026)
        self.assertEqual(adapter.parsed_timestamps, ["2026-09-03T01:02:03+00:00"])
        self.assertEqual(adapter.attachment_calls, [payload()["d"]["attachments"]])
        self.assertNotIn("message-1", adapter.normal_seen)
        self.assertNotIn("url", records[0]["raw"].get("attachments", [{}])[0])

    async def test_non_allowlisted_group_is_dropped(self):
        records = []
        qq_observer.install_nonmention_observer(records.append)
        adapter = FakeQQAdapter(allowed=False)

        adapter._dispatch_payload(payload())
        await asyncio.sleep(0)

        self.assertEqual(records, [])
        self.assertEqual(adapter.attachment_calls, [])
        self.assertEqual(adapter.handled, 0)

    async def test_at_message_keeps_original_dispatch_path(self):
        records = []
        qq_observer.install_nonmention_observer(records.append)
        adapter = FakeQQAdapter()
        at_event = payload()
        at_event["t"] = "GROUP_AT_MESSAGE_CREATE"

        result = adapter._dispatch_payload(at_event)
        await asyncio.sleep(0)

        self.assertEqual(result, "original-result")
        self.assertEqual(len(adapter.original_calls), 1)
        self.assertEqual(records, [])
        self.assertEqual(adapter.handled, 0)

    async def test_duplicate_message_id_is_observed_once_without_normal_cache_pollution(self):
        records = []
        qq_observer.install_nonmention_observer(records.append)
        adapter = FakeQQAdapter()

        adapter._dispatch_payload(payload())
        adapter._dispatch_payload(payload())
        await asyncio.sleep(0)

        self.assertEqual(len(records), 1)
        self.assertEqual(len(adapter.attachment_calls), 1)
        self.assertEqual(adapter.normal_seen, {})
        self.assertEqual(len(adapter._smart_group_qq_nonmention_seen), 1)

    async def test_callback_exception_isolated_and_dispatch_survives(self):
        logger = Mock()

        async def callback(record):
            raise RuntimeError("observer failure")

        qq_observer.install_nonmention_observer(callback, logger=logger)
        adapter = FakeQQAdapter()
        result = adapter._dispatch_payload(payload())
        await asyncio.sleep(0)

        self.assertIsNone(result)
        self.assertEqual(adapter.original_calls, [])
        self.assertEqual(adapter.handled, 0)
        logger.warning.assert_called_once()

    async def test_install_is_idempotent_and_second_callback_replaces_first(self):
        first = []
        second = []
        qq_observer.install_nonmention_observer(first.append)
        dispatch = FakeQQAdapter._dispatch_payload
        qq_observer.install_nonmention_observer(second.append)

        self.assertIs(FakeQQAdapter._dispatch_payload, dispatch)
        adapter = FakeQQAdapter()
        adapter._dispatch_payload(payload())
        await asyncio.sleep(0)

        self.assertEqual(first, [])
        self.assertEqual(len(second), 1)

    def test_dispatch_without_running_loop_is_safe(self):
        records = []
        qq_observer.install_nonmention_observer(records.append)
        adapter = FakeQQAdapter()

        result = adapter._dispatch_payload(payload())

        self.assertIsNone(result)
        self.assertEqual(adapter.original_calls, [])
        self.assertEqual(records, [])
        self.assertEqual(adapter.handled, 0)


if __name__ == "__main__":
    import unittest

    unittest.main()
