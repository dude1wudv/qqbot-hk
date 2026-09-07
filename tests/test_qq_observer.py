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


def payload(
    message_id="message-1",
    *,
    group="group-1",
    member="member-1",
    text="hello",
    sequence=None,
    display_name="",
    username="",
):
    value = {
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
    if display_name:
        value["d"]["author"]["display_name"] = display_name
    if username:
        value["d"]["author"]["username"] = username
    if sequence is not None:
        value["s"] = sequence
    return value


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
        await asyncio.sleep(0.1)

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
        self.assertIsNone(records[0]["sequence"])
        self.assertEqual(adapter.parsed_timestamps, ["2026-09-03T01:02:03+00:00"])
        self.assertEqual(adapter.attachment_calls, [payload()["d"]["attachments"]])
        self.assertNotIn("message-1", adapter.normal_seen)
        self.assertNotIn("url", records[0]["raw"].get("attachments", [{}])[0])

    async def test_non_allowlisted_group_is_dropped(self):
        records = []
        qq_observer.install_nonmention_observer(records.append)
        adapter = FakeQQAdapter(allowed=False)

        adapter._dispatch_payload(payload())
        await asyncio.sleep(0.01)

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
        await asyncio.sleep(0.01)

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
        await asyncio.sleep(0.01)

        self.assertEqual(len(records), 1)
        self.assertEqual(len(adapter.attachment_calls), 1)
        self.assertEqual(adapter.normal_seen, {})
        self.assertEqual(len(adapter._smart_group_qq_nonmention_seen), 1)

    async def test_captures_gateway_sequence_timestamp_and_member_identity(self):
        records = []
        qq_observer.install_nonmention_observer(records.append)
        adapter = FakeQQAdapter()

        adapter._dispatch_payload(
            payload(
                message_id="metadata-1",
                sequence=42,
                display_name="小明",
                username="xiaoming",
            )
        )
        await asyncio.sleep(0.01)

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["sequence"], 42)
        self.assertEqual(record["gateway_sequence"], 42)
        self.assertEqual(record["event_timestamp_raw"], "2026-09-03T01:02:03+00:00")
        self.assertEqual(record["event_timestamp"], record["timestamp"])
        self.assertEqual(record["display_name"], "小明")
        self.assertEqual(record["username"], "xiaoming")
        self.assertEqual(record["raw"]["sequence"], 42)
        self.assertEqual(record["raw"]["author"]["display_name"], "小明")
        self.assertEqual(record["raw"]["author"]["username"], "xiaoming")

    async def test_slow_media_does_not_block_fast_text_callback(self):
        class SlowQQAdapter(FakeQQAdapter):
            async def _process_attachments(self, attachments):
                self.attachment_calls.append(attachments)
                await asyncio.sleep(0.2)
                return {
                    "image_paths": ["/opt/data/media/slow.jpg"],
                    "image_media_types": ["image/jpeg"],
                }

        records = []
        qq_observer.install_nonmention_observer(records.append)
        adapter = SlowQQAdapter()

        adapter._dispatch_payload(payload("slow-media"))
        await asyncio.sleep(0.01)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["text"], "hello")
        self.assertTrue(records[0]["attachments_pending"])
        self.assertEqual(records[0]["attachment_status"], "pending")

        await asyncio.sleep(0.5)
        self.assertFalse(records[0]["attachments_pending"])
        self.assertEqual(records[0]["attachment_status"], "ready")
        self.assertEqual(records[0]["image_paths"], ["/opt/data/media/slow.jpg"])

    def test_seen_cache_periodically_expires_stale_ids_and_remains_bounded(self):
        adapter = FakeQQAdapter()
        adapter._smart_group_qq_nonmention_seen = {"old": 0.0}
        adapter._smart_group_qq_nonmention_seen_cleanup = -100.0

        with patch.object(qq_observer.time, "monotonic", return_value=4000.0):
            self.assertTrue(qq_observer._claim_message_id(adapter, "new"))

        self.assertNotIn("old", adapter._smart_group_qq_nonmention_seen)
        self.assertIn("new", adapter._smart_group_qq_nonmention_seen)

    async def test_out_of_order_events_keep_sequence_metadata_without_rolling_back_state(self):
        records = []
        qq_observer.install_nonmention_observer(records.append)
        adapter = FakeQQAdapter()

        adapter._dispatch_payload(payload("sequence-12", sequence=12))
        adapter._dispatch_payload(payload("sequence-7", sequence=7))
        await asyncio.sleep(0.1)

        self.assertEqual(adapter._last_seq, 12)
        self.assertEqual({record["sequence"] for record in records}, {7, 12})
        self.assertEqual({record["raw"]["sequence"] for record in records}, {7, 12})

    async def test_callback_failure_retries_and_redelivery_can_succeed(self):
        calls = 0
        records = []

        async def callback(record):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient observer failure")
            records.append(record)

        qq_observer.install_nonmention_observer(callback)
        adapter = FakeQQAdapter()
        item = payload("retry-1")
        adapter._dispatch_payload(item)
        await asyncio.sleep(0.2)

        self.assertEqual(calls, 2)
        self.assertEqual(len(records), 1)
        self.assertEqual(len(adapter._smart_group_qq_nonmention_seen), 1)

        # A successful claim remains idempotent after the retry settles.
        adapter._dispatch_payload(item)
        await asyncio.sleep(0.01)
        self.assertEqual(calls, 2)

    async def test_exhausted_callback_failure_releases_id_for_gateway_redelivery(self):
        calls = 0
        succeed = False
        records = []

        async def callback(record):
            nonlocal calls
            calls += 1
            if not succeed:
                raise RuntimeError("persistent observer failure")
            records.append(record)

        qq_observer.install_nonmention_observer(callback)
        adapter = FakeQQAdapter()
        item = payload("retry-after-give-up")
        adapter._dispatch_payload(item)
        await asyncio.sleep(0.6)

        self.assertEqual(calls, qq_observer._RETRY_ATTEMPTS)
        self.assertEqual(records, [])
        self.assertNotIn("retry-after-give-up", adapter._smart_group_qq_nonmention_seen)

        succeed = True
        adapter._dispatch_payload(item)
        await asyncio.sleep(0.01)
        self.assertEqual(calls, qq_observer._RETRY_ATTEMPTS + 1)
        self.assertEqual(len(records), 1)

    async def test_callback_exception_isolated_and_dispatch_survives(self):
        logger = Mock()

        async def callback(record):
            raise RuntimeError("observer failure")

        qq_observer.install_nonmention_observer(callback, logger=logger)
        adapter = FakeQQAdapter()
        result = adapter._dispatch_payload(payload())
        await asyncio.sleep(0.6)

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
        await asyncio.sleep(0.01)

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
