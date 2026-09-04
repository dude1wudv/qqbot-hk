from datetime import datetime, timezone
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

from smart_group_qq.auto_pair import auto_pair_qq_dm, build_auto_pair_handler, utc_deadline


class FakeContext:
    def __init__(self, config):
        self.config = config

    def get_config(self, key, default=None):
        return self.config.get(key, default)


class FakePairingStore:
    def __init__(self):
        self._lock = threading.RLock()
        self.approved = {}

    def is_approved(self, platform, user_id):
        return (platform, user_id) in self.approved

    def _approve_user(self, platform, user_id, user_name=""):
        self.approved[(platform, user_id)] = user_name


def source(*, platform="qqbot", chat_type="dm", user_id="user-a"):
    return SimpleNamespace(
        platform=SimpleNamespace(value=platform),
        chat_type=chat_type,
        chat_id=user_id,
        user_id=user_id,
        user_name="测试用户",
    )


class AutoPairTests(unittest.TestCase):
    def test_active_window_pairs_once_and_allows_first_message(self):
        pairing = FakePairingStore()
        gateway = SimpleNamespace(_pairing_store_for=lambda item: pairing)
        handler = build_auto_pair_handler(FakeContext({
            "auto_pair": {"enabled": True, "until_utc": "2099-01-01T00:00:00Z"},
        }))
        event = SimpleNamespace(source=source())

        self.assertEqual(handler(event=event, gateway=gateway), {"action": "allow"})
        self.assertTrue(pairing.is_approved("qqbot", "user-a"))
        handler(event=event, gateway=gateway)
        self.assertEqual(len(pairing.approved), 1)

    def test_expired_window_does_not_pair(self):
        pairing = FakePairingStore()
        gateway = SimpleNamespace(_pairing_store_for=lambda item: pairing)
        config = {"enabled": True, "until_utc": "2026-09-05T08:00:00Z"}
        self.assertFalse(auto_pair_qq_dm(
            config,
            gateway,
            source(),
            now=datetime(2026, 9, 5, 8, 0, tzinfo=timezone.utc),
        ))
        self.assertEqual(pairing.approved, {})

    def test_non_qq_group_and_invalid_deadline_fail_closed(self):
        pairing = FakePairingStore()
        gateway = SimpleNamespace(_pairing_store_for=lambda item: pairing)
        active = {"enabled": True, "until_utc": "2099-01-01T00:00:00Z"}
        self.assertFalse(auto_pair_qq_dm(active, gateway, source(platform="telegram")))
        self.assertFalse(auto_pair_qq_dm(active, gateway, source(chat_type="group")))
        self.assertIsNone(utc_deadline("not-a-timestamp"))
        self.assertEqual(pairing.approved, {})

    def test_missing_pairing_api_fails_closed(self):
        self.assertFalse(auto_pair_qq_dm(
            {"enabled": True, "until_utc": "2099-01-01T00:00:00Z"},
            SimpleNamespace(),
            source(),
        ))


if __name__ == "__main__":
    unittest.main()
