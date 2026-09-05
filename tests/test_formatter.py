from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

from smart_group_qq.formatter import format_for_qq, split_message


class FormatterTests(unittest.TestCase):
    def test_markdown_plain_text_degradation(self):
        value = format_for_qq("# 标题\n**重点**\n- 项\n> 引用\n[站点](https://example.com)\n```py\nx=1\n```")
        self.assertIn("标题", value)
        self.assertIn("重点", value)
        self.assertIn("· 项", value)
        self.assertIn("引用", value)
        self.assertIn("站点 (https://example.com)", value)
        self.assertIn("代码（py）：", value)
        for markdown in ("#", "**", "```", "【", "」", "📌", "▎"):
            self.assertNotIn(markdown, value)

    def test_tables_tasks_and_inline_markup_become_plain_text(self):
        value = format_for_qq(
            "| 名称 | 值 |\n| --- | --- |\n| `mode` | ~~old~~ |\n\n- [x] 已做\n- [ ] 待做\n---"
        )
        self.assertEqual(value, "名称；值\nmode；old\n\n已完成：已做\n待办：待做")

    def test_split_is_bounded_and_lossless_for_simple_text(self):
        chunks = split_message("a" * 31, max_chars=10)
        self.assertEqual("".join(chunks), "a" * 31)
        self.assertTrue(all(len(item) <= 10 for item in chunks))


if __name__ == "__main__":
    unittest.main()
