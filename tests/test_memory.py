from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

from smart_group_qq.memory import GroupMemory, normalize_member_message
from smart_group_qq.store import Store


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.memory = GroupMemory(self.store, window_size=3, idle_seconds=3600, summary_chars=80)

    def tearDown(self):
        self.store.close()

    def test_member_label_and_group_isolation(self):
        self.assertEqual(normalize_member_message("abcdef123", "你好"), "[群成员:abcdef]: 你好")
        self.memory.record("group-a", "member-a", "A1", "m1")
        self.memory.record("group-b", "member-b", "B1", "m2")
        self.assertIn("A1", self.memory.summary("group-a"))
        self.assertNotIn("B1", self.memory.summary("group-a"))

    def test_window_compaction_and_reset_are_group_scoped(self):
        for index in range(5):
            self.memory.record("group-a", "member", f"A{index}", f"m{index}")
        self.memory.record("group-b", "member", "B", "b")
        self.assertLessEqual(self.store.count_history("group-a"), 3)
        self.assertIsNotNone(self.store.get_memory("group-a"))
        self.memory.reset("group-a")
        self.assertEqual(self.store.count_history("group-a"), 0)
        self.assertEqual(self.store.count_history("group-b"), 1)


if __name__ == "__main__":
    unittest.main()
