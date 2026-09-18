import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

from smart_group_qq.attention import AttentionManager, AttentionMode


class AttentionTests(unittest.TestCase):
    def test_success_selects_mode_and_continuation_is_member_and_topic_scoped(self):
        attention = AttentionManager(ttl_seconds=120, relevance_threshold=0.35)
        attention.record_success("group-a", "member-a", "Windows 发布失败怎么排查", direct=False, message_id="reply-1", now=100.0)
        self.assertEqual(attention.get("group-a", now=101).mode, AttentionMode.ACTIVE_TOPIC)
        self.assertEqual(attention.continuation_score("group-a", "member-a", "那 Windows 发布呢？", now=101), 20)
        self.assertEqual(attention.continuation_score("group-a", "member-b", "那 Windows 发布呢？", now=101), 0)
        self.assertTrue(attention.has_reply_id("group-a", "reply-1", now=101))

        attention.record_success("group-a", "member-a", "直接呼叫", direct=True, message_id="reply-2", now=110)
        self.assertEqual(attention.get("group-a", now=110).mode, AttentionMode.DIRECT_CONVERSATION)
        self.assertEqual(attention.get("group-a", now=231).mode, AttentionMode.PASSIVE)
        self.assertEqual(attention.continuation_score("group-a", "member-a", "Windows 发布", now=231), 0)

    def test_reply_ids_are_bounded_and_expire(self):
        attention = AttentionManager(max_reply_ids=2, reply_ttl_seconds=10)
        attention.record_success("g", "m", "q", direct=False, message_id="r1", now=100)
        attention.record_success("g", "m", "q", direct=False, message_id="r2", now=101)
        attention.record_success("g", "m", "q", direct=False, message_id="r3", now=102)
        self.assertFalse(attention.has_reply_id("g", "r1", now=102))
        self.assertTrue(attention.has_reply_id("g", "r3", now=102))
        self.assertFalse(attention.has_reply_id("g", "r3", now=113))

    def test_group_capacity_does_not_evict_active_state(self):
        attention = AttentionManager(max_groups=2)
        attention.record_success("active", "m", "q", direct=False, now=1)
        attention.get("passive", now=2)
        attention.record_success("new", "m", "q", direct=False, now=3)
        self.assertTrue(attention.can_accept("active"))
        self.assertEqual(attention.get("active", now=3).mode, AttentionMode.ACTIVE_TOPIC)
        self.assertEqual(attention.get("passive", now=3).mode, AttentionMode.PASSIVE)
        self.assertEqual(attention.get("new", now=3).mode, AttentionMode.ACTIVE_TOPIC)


if __name__ == "__main__":
    unittest.main()
