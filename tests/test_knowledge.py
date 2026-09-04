# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

from smart_group_qq.knowledge import (  # noqa: E402
    DocumentTooLargeError,
    InMemoryKnowledgeStore,
    KnowledgeBase,
    UnsafePathError,
    chunk_text,
    extract_document,
    parse_kb_command,
    redact_document_secrets,
    validate_cache_path,
)


class KnowledgeStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryKnowledgeStore()
        self.kb = KnowledgeBase(self.store, chunk_size=12, overlap=3)

    def test_group_isolation_and_duplicate_documents(self):
        first = self.kb.add_knowledge_document("group-a", "指南", "人工智能群指南", source="upload")
        duplicate = self.kb.add_knowledge_document("group-a", "指南", "人工智能群指南", source="another-message")
        self.kb.add_knowledge_document("group-b", "指南", "人工智能群指南")

        self.assertEqual(first["doc_id"], duplicate["doc_id"])
        self.assertTrue(duplicate["deduplicated"])
        self.assertEqual(len(self.kb.list_knowledge_documents("group-a")), 1)
        self.assertEqual(len(self.kb.list_knowledge_documents("group-b")), 1)
        self.assertEqual(self.kb.search_knowledge("group-a", "人工智能")[0]["doc_id"], first["doc_id"])
        self.assertEqual(self.kb.search_knowledge("missing-group", "人工智能"), [])

    def test_chunking_has_overlap_and_strict_bound(self):
        chunks = chunk_text("abcdefghij klmnopqrst uvwxyz", chunk_size=10, overlap=3)
        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(len(chunk) <= 10 for chunk in chunks))
        self.assertTrue(any(set(chunks[index]) & set(chunks[index + 1]) for index in range(len(chunks) - 1)))

    def test_chinese_lexical_search_returns_document_and_chunk_fields(self):
        self.kb.add_knowledge_document("g", "无关文档", "这是天气和旅行的内容")
        wanted = self.kb.add_knowledge_document(
            "g", "群规", "人工智能知识库支持中文检索和文档问答", chunk_size=20, overlap=2
        )
        result = self.kb.search_knowledge("g", "中文检索", limit=1)
        self.assertEqual(result[0]["doc_id"], wanted["doc_id"])
        self.assertEqual(result[0]["title"], "群规")
        self.assertIn("chunk_id", result[0])
        self.assertIn("text", result[0])
        self.assertGreater(result[0]["score"], 0)

    def test_remove_and_clear_are_group_scoped(self):
        first = self.kb.add_knowledge_document("a", "A", "alpha")
        self.kb.add_knowledge_document("a", "B", "beta")
        self.kb.add_knowledge_document("b", "A", "alpha")
        self.assertTrue(self.kb.remove_knowledge_document("a", first["doc_id"]))
        self.assertEqual(len(self.kb.list_knowledge_documents("a")), 1)
        self.assertEqual(len(self.kb.list_knowledge_documents("b")), 1)
        self.assertEqual(self.kb.clear_knowledge("a"), 1)
        self.assertEqual(self.kb.list_knowledge_documents("a"), [])
        self.assertEqual(len(self.kb.list_knowledge_documents("b")), 1)


class DocumentExtractionTests(unittest.TestCase):
    def test_supported_text_documents_and_docx(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixtures = {
                "note.txt": "纯文本内容",
                "note.md": "# Markdown\n测试正文",
                "rows.csv": "name,value\n测试,1",
                "config.yaml": "name: 测试",
                "data.toml": 'name = "测试"',
            }
            for filename, content in fixtures.items():
                path = root / filename
                path.write_text(content, encoding="utf-8")
                self.assertIn("测试" if filename != "note.txt" else "纯文本", extract_document(path))

            json_path = root / "data.json"
            json_path.write_text(json.dumps({"name": "测试", "enabled": True}), encoding="utf-8")
            self.assertIn('"name": "测试"', extract_document(json_path))

            xml_path = root / "data.xml"
            xml_path.write_text("<root><title>测试</title><body>正文</body></root>", encoding="utf-8")
            self.assertEqual(extract_document(xml_path), "测试\n正文")

            docx_path = root / "guide.docx"
            document_xml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body><w:p><w:r><w:t>第一段</w:t></w:r></w:p>'
                '<w:p><w:r><w:t>第二段</w:t></w:r></w:p></w:body></w:document>'
            )
            with zipfile.ZipFile(docx_path, "w") as archive:
                archive.writestr("word/document.xml", document_xml)
            self.assertEqual(extract_document(docx_path), "第一段\n第二段")

    def test_cache_path_escape_and_limits_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "cache"
            root.mkdir()
            good = root / "good.txt"
            good.write_text("ok", encoding="utf-8")
            outside = Path(directory) / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            self.assertEqual(validate_cache_path(good, root), good.resolve())
            with self.assertRaises(UnsafePathError):
                validate_cache_path(root / ".." / "outside.txt", root)
            with self.assertRaises(DocumentTooLargeError):
                extract_document(good, max_chars=1)


class KnowledgeCommandTests(unittest.TestCase):
    def test_parse_add_list_search_remove_clear_help(self):
        add = parse_kb_command("<@!bot> /kb add 群规 | 请保持友善")
        self.assertEqual((add.action, add.title, add.text), ("add", "群规", "请保持友善"))
        self.assertEqual(parse_kb_command("@机器人 ／kb list").action, "list")
        self.assertEqual(parse_kb_command("/kb search 中文检索").argument, "中文检索")
        self.assertEqual(parse_kb_command("/kb remove doc-123").argument, "doc-123")
        self.assertIsNone(parse_kb_command("/kb clear"))
        self.assertEqual(parse_kb_command("/kb clear confirm").action, "clear")
        self.assertEqual(parse_kb_command("/kb").action, "help")
        self.assertIsNone(parse_kb_command("/kb add 缺少正文"))
        self.assertIsNone(parse_kb_command("/kb unknown"))

    def test_document_secrets_are_redacted_before_indexing(self):
        value = redact_document_secrets("token=secret-value sk-abcdefghijklmnop")
        self.assertNotIn("secret-value", value)
        self.assertNotIn("sk-abcdefghijklmnop", value)


if __name__ == "__main__":
    unittest.main()
