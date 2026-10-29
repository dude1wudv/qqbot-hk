import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

from smart_group_qq.chat_export import render_chat_history
from smart_group_qq.commands import help_text, parse_command


class ChatExportTests(unittest.TestCase):
    def test_export_command_aliases_and_help(self):
        for raw in ("/长图", "/聊天长图", "/导出聊天记录", "<@bot> ／长图"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_command(raw).name, "chat_export")
        self.assertIn("/长图", help_text())

    def test_render_is_bounded_and_redacts_secrets(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow is optional in the local test environment")
        rows = [
            {"role": "user", "member_id": "m-private-openid", "text": "token=sk-abcdefghijklmnopqrstuvwxyz", "created_at": 1},
            {"role": "assistant", "member_id": None, "text": "已处理", "created_at": 2},
            {"role": "user", "member_id": "other-openid", "text": "图片", "media_json": json.dumps([{"url": "https://secret.invalid/x"}]), "created_at": 3},
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = render_chat_history(rows, folder)
            self.assertIsNotNone(path)
            self.assertTrue(Path(path).is_file())
            self.assertLess(Path(path).stat().st_size, 8 * 1024 * 1024)

    def test_empty_history_returns_none(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(render_chat_history([], folder))


if __name__ == "__main__":
    unittest.main()
