from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

from smart_group_qq.formatter import format_for_qq, split_message, split_group_reply, trim_chat_followup


class FormatterTests(unittest.TestCase):
    def test_quoted_questions_and_continuations_stay_in_one_bubble(self):
        for text in (
            '看下来对方唯一一句反问是"怎么骗人了？"，没给出任何解释。',
            '他问：“怎么骗人了？”但没有解释。',
            '他说：“先问‘怎么了？’再回答。”',
            '想想这个问题（真的需要吗？），再做决定。',
            '他说：“第一行？\n第二行！”',
        ):
            with self.subTest(text=text):
                self.assertEqual(split_group_reply(text), [text])
        self.assertEqual(split_group_reply('真的吗？！先核实。'), ['真的吗？！', '先核实。'])
        self.assertEqual(split_group_reply('他说“你好！”然后离开。下一句。'),
                         ['他说“你好！”然后离开。', '下一句。'])

    def test_group_bubbles_are_short_and_long_requested_answers_survive(self):
        self.assertEqual(split_group_reply("终于跑通了。\n这次可以歇口气了！"), ["终于跑通了。", "这次可以歇口气了！"])
        chunks = split_group_reply("分享。" * 100)
        self.assertLessEqual(len(chunks), 3)
        self.assertEqual("".join(chunks), "分享。" * 100)
        self.assertTrue(all(len(chunk) <= 1500 for chunk in chunks))
        self.assertEqual("".join(split_group_reply("x" * 1900, direct=True)), "x" * 1900)
        self.assertEqual(split_group_reply("看这个 https://example.com/?q=hello!world", direct=True),
                         ["看这个 https://example.com/?q=hello!world"])
        self.assertEqual(split_group_reply("```python\nprint('hello? world')\n```", direct=True),
                         ["代码（python）：\nprint('hello? world')"])

    def test_canned_followups_removed_but_needed_questions_preserved(self):
        for suffix in ("你呢？", "你怎么看？", "还需要我帮你整理吗？", "要不要我再介绍一下？"):
            self.assertEqual(trim_chat_followup("终于跑通了。" + suffix), "终于跑通了。")
        self.assertEqual(trim_chat_followup("你用的是哪个版本？"), "你用的是哪个版本？")

    def test_reply_envelope_is_rendered_as_plain_text(self):
        raw = (
            "{'action':\"reply\",\"message\":\"确实，官key便宜了。\\n\\n"
            "**luna** 留给长推理。\"}"
        )
        value = format_for_qq(raw)
        self.assertNotIn("action", value)
        self.assertNotIn("{", value)
        self.assertIn("确实，官key便宜了。", value)
        self.assertIn("luna 留给长推理。", value)
        self.assertNotIn("**", value)
        self.assertEqual(format_for_qq('{"action":"ignore","message":null}'), "")
        self.assertEqual(split_group_reply(raw)[0], "确实，官key便宜了。")
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
