from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

from smart_group_qq.commands import (
    clean_text,
    help_text,
    model_alias_rewrite,
    parse_command,
    parse_profile_command,
)


class CommandTests(unittest.TestCase):
    def test_mentions_and_fullwidth_slash(self):
        self.assertEqual(parse_command("<@!bot> ／help").name, "help")
        self.assertEqual(parse_command("@机器人 /clear").name, "reset")
        self.assertEqual(parse_command("<@!bot> /值日表").name, "duty_roster")
        self.assertEqual(parse_command("@机器人 ／值日表").name, "duty_roster")
        self.assertEqual(clean_text("<@bot>  问题"), "问题")

    def test_help_lists_duty_roster(self):
        self.assertIn("/值日表 查看本周轮值安排", help_text())
        self.assertIn("/我的记忆", help_text())
        self.assertIn("/gemini", help_text())
        self.assertIn("/deepseek", help_text())

    def test_model_aliases_rewrite_to_session_scoped_native_commands(self):
        self.assertEqual(
            model_alias_rewrite("<@bot> /gemini"),
            "/model gemini-3.8-flash-high --session",
        )
        self.assertEqual(
            model_alias_rewrite("@机器人 / deepseek"),
            "/model deepseek-v4-flash-0731 --session",
        )
        self.assertEqual(
            model_alias_rewrite("／ Gemini"),
            "/model gemini-3.8-flash-high --session",
        )
        self.assertIsNone(model_alias_rewrite("gemini"))

    def test_profile_commands_keep_arguments(self):
        command = parse_profile_command("<@bot> /记住我：我负责后端发布")
        self.assertEqual(command.action, "remember")
        self.assertEqual(command.argument, "我负责后端发布")
        self.assertEqual(parse_profile_command("/我的记忆").action, "show")
        self.assertEqual(parse_profile_command("/忘记我").action, "forget")

    def test_unknown_command_passes_through(self):
        self.assertIsNone(parse_command("/unknown"))
        self.assertIsNone(parse_command("普通问题"))


if __name__ == "__main__":
    unittest.main()
