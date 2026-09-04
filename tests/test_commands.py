from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

from smart_group_qq.commands import clean_text, parse_command


class CommandTests(unittest.TestCase):
    def test_mentions_and_fullwidth_slash(self):
        self.assertEqual(parse_command("<@!bot> ／help").name, "help")
        self.assertEqual(parse_command("@机器人 /clear").name, "reset")
        self.assertEqual(clean_text("<@bot>  问题"), "问题")

    def test_unknown_command_passes_through(self):
        self.assertIsNone(parse_command("/unknown"))
        self.assertIsNone(parse_command("普通问题"))


if __name__ == "__main__":
    unittest.main()
