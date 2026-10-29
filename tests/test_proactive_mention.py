import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

from smart_group_qq.proactive import ProactiveMentionManager, ProactiveMentionPolicy


class ProactiveMentionTests(unittest.TestCase):
    def test_disabled_by_default_and_openid_is_process_local(self):
        manager = ProactiveMentionManager()
        self.assertIsNone(manager.candidate("g", []))
        self.assertFalse(manager.register_target("g", "m-ref", "openid-secret"))

    def test_selects_recent_topic_member_and_claims_cooldown(self):
        manager = ProactiveMentionManager({"enabled": True, "cooldown_seconds": 60, "max_per_day": 2})
        manager.register_target("g", "m-ref", "openid-secret", "小明", now=100)
        topics = [{"id": "topic-1", "member_refs": ["m-ref"], "summary": "部署失败"}]
        candidate = manager.candidate("g", topics, now=101)
        self.assertEqual(candidate["member_ref"], "m-ref")
        self.assertTrue(manager.claim(candidate, now=101))
        self.assertIsNone(manager.candidate("g", topics, now=102))
        # The outgoing text carries the QQ mention but no pseudonymous ref.
        rendered = manager.render(candidate)
        self.assertIn("<@openid-secret>", rendered)
        self.assertNotIn("m-ref", rendered)

    def test_uncertain_send_is_not_retried_and_daily_budget_is_bounded(self):
        manager = ProactiveMentionManager({"enabled": True, "cooldown_seconds": 60, "max_per_day": 1})
        manager.register_target("g", "ref", "member", now=100)
        topic = [{"member_refs": ["ref"], "summary": "话题"}]
        candidate = manager.candidate("g", topic, now=100)
        self.assertTrue(manager.claim(candidate, now=100))
        # Claim is consumed before transport, so an ambiguous failure cannot
        # cause a duplicate proactive mention.
        self.assertIsNone(manager.candidate("g", topic, now=1000))
        self.assertFalse(manager.claim(candidate, now=1000))

    def test_policy_clamps_untrusted_values(self):
        policy = ProactiveMentionPolicy.from_config({"enabled": True, "cooldown_seconds": 1, "max_per_day": 999})
        self.assertEqual(policy.cooldown_seconds, 60)
        self.assertEqual(policy.max_per_day, 24)


if __name__ == "__main__":
    unittest.main()
