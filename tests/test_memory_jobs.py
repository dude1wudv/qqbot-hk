import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
from smart_group_qq.memory import GroupMemory
from smart_group_qq.member_memory import MemberMemory
from smart_group_qq.store import Store, SCHEMA_VERSION


class DurableMemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:", member_secret="synthetic-secret")
        self.addCleanup(self.store.close)
        self.profiles = MemberMemory(self.store)

    def source(self, scope="g", member="owner", text="我负责北斗", now=100):
        self.store.set_member_consent(scope, member, "opted_in")
        return self.store.append_history(scope, role="user", member_id=self.store.member_ref_for(scope, member),
                                         text=text, message_id=f"source-{scope}", created_at=now)

    def proof(self, chat="private-chat", member="owner"):
        return {"authenticated_private": True, "sessions": [
            {"source": "qqbot", "chat_type": "dm", "chat_id": chat, "user_id": member}]}

    def test_nested_transaction_rollback_includes_member_and_receipt(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.profiles.remember("g", "owner", "职责=北斗")
                self.store.save_command_receipt("g", "m", "done")
                raise RuntimeError("abort synthetic command")
        self.assertIsNone(self.store.get_group_member("g", "owner"))
        self.assertIsNone(self.store.get_command_receipt("g", "m"))

    def test_receipt_is_idempotent_and_cannot_change_after_send_or_reset(self):
        receipt = self.store.save_command_receipt("g", "m", "first", epoch=0)
        self.assertEqual(self.store.save_command_receipt("g", "m", "second")["result"], "first")
        self.assertTrue(self.store.replace_pending_command_result("g", "m", "final", expected_epoch=0))
        self.assertTrue(self.store.update_command_receipt("g", "m", delivery_status="sending", sent_chunks=[0]))
        self.assertFalse(self.store.replace_pending_command_result("g", "m", "late", expected_epoch=0))
        self.store.update_command_receipt("g", "m", delivery_status="failed", sent_chunks=[1])
        self.assertEqual(self.store.get_command_receipt("g", "m")["sent_chunks"], [0, 1])
        with self.store.transaction():
            self.store._advance_memory_epoch("g")
        self.assertFalse(self.store.update_command_receipt("g", "m", delivery_status="sent", expected_epoch=0))
        replaced = self.store.save_command_receipt("g", "m", "reset done", epoch=1)
        self.assertEqual((replaced["epoch"], replaced["sent_chunks"], replaced["result"]), (1, [], "reset done"))

    def test_prepared_receipt_survives_restart_and_new_epoch_resets_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipts.db"
            store = Store(path, member_secret="synthetic-secret")
            final = '代码（python）：\nprint("**literal**")'
            saved = store.save_command_receipt("g", "m", final, prepared=True)
            self.assertTrue(saved["prepared"])
            duplicate = store.save_command_receipt("g", "m", "unprepared duplicate")
            self.assertEqual(duplicate["result"], final)
            self.assertTrue(duplicate["prepared"])
            store.close()
            store = Store(path, member_secret="synthetic-secret")
            try:
                restored = store.get_command_receipt("g", "m")
                self.assertEqual(restored["result"], final)
                self.assertTrue(restored["prepared"])
                with store.transaction():
                    store._advance_memory_epoch("g")
                reset = store.save_command_receipt("g", "m", "new raw result", epoch=1)
                self.assertFalse(reset["prepared"])
                self.assertEqual((reset["result"], reset["epoch"], reset["sent_chunks"]), ("new raw result", 1, []))
            finally:
                store.close()

    def test_pending_result_replacement_declares_its_format_and_is_frozen_after_send(self):
        self.store.save_command_receipt("g", "m", "raw result")
        self.assertFalse(self.store.get_command_receipt("g", "m")["prepared"])
        self.assertTrue(self.store.replace_pending_command_result("g", "m", "formatted", expected_epoch=0, prepared=True))
        self.assertTrue(self.store.get_command_receipt("g", "m")["prepared"])
        self.assertTrue(self.store.replace_pending_command_result("g", "m", "new narrative", expected_epoch=0))
        self.assertFalse(self.store.get_command_receipt("g", "m")["prepared"])
        self.store.replace_pending_command_result("g", "m", "final", expected_epoch=0, prepared=True)
        self.store.update_command_receipt("g", "m", delivery_status="sending", sent_chunks=[0])
        self.assertFalse(self.store.replace_pending_command_result("g", "m", "late", expected_epoch=0))
        self.assertEqual(self.store.get_command_receipt("g", "m")["result"], "final")
        self.assertTrue(self.store.get_command_receipt("g", "m")["prepared"])

    def test_checkpoint_cas_and_privacy_epoch_reject_late_completion(self):
        first = self.store.create_context_checkpoint("key", {"partial_summary": "one", "chunk_index": 1}, scope="g", epoch=0)
        second = self.store.save_context_checkpoint("key", {"partial_summary": "two", "chunk_index": 2},
                                                   expected_revision=first["revision"], expected_epoch=0)
        self.assertEqual(second["revision"], 2)
        self.assertIsNone(self.store.save_context_checkpoint("key", {"partial_summary": "stale"}, expected_revision=1, expected_epoch=0))
        self.store.clear_group("g")
        self.assertIsNone(self.store.get_context_checkpoint("key"))
        self.assertIsNone(self.store.save_context_checkpoint("key", {}, expected_revision=2, expected_epoch=0))
        self.assertIsNone(self.store.create_context_checkpoint("key", {}, scope="g", epoch=0))

    def test_checkpoint_generation_prevents_aba_after_recreation(self):
        first = self.store.create_context_checkpoint("key", {"checkpoint_id": "old"}, scope="g", epoch=0)
        self.store.delete_context_checkpoint("key", expected_checkpoint_id="old")
        new = self.store.create_context_checkpoint("key", {"checkpoint_id": "new"}, scope="g", epoch=0)
        self.assertEqual(first["revision"], new["revision"])
        self.assertIsNone(self.store.save_context_checkpoint("key", {"checkpoint_id": "old", "partial_summary": "late"},
                                                             expected_revision=1, expected_epoch=0))
        self.assertFalse(self.store.delete_context_checkpoint("key", expected_revision=1, expected_checkpoint_id="old"))
        self.assertEqual(self.store.get_context_checkpoint("key")["checkpoint_id"], "new")

    def test_jobs_retry_three_times_and_preserve_sources_until_terminal(self):
        source = self.source()
        job_id = self.store.enqueue_member_extraction("g", "owner", source, now=100)
        self.assertEqual(job_id, self.store.enqueue_member_extraction("g", "owner", source, now=101))
        self.assertEqual(self.store.enqueue_member_extraction("g", "stranger", source), 0)
        self.store.set_memory("g", "summary", last_history_id=source)
        self.assertEqual(self.store.trim_history("g", 0), 0)
        stamp = 100
        for attempt in (1, 2, 3):
            job = self.store.claim_member_extraction(job_id, now=stamp)
            self.assertEqual(job["attempt"], attempt)
            self.assertNotIn("我负责北斗", str(job))
            self.assertTrue(self.store.finish_member_extraction(job_id, status="retryable_failure", error="RuntimeError: private text",
                                                                now=stamp + 1, expected_attempt=attempt))
            saved = self.store.get_member_extraction(job_id)
            self.assertEqual(saved["error"], "ExtractionError")
            stamp = saved["next_retry_at"] + 1
        self.assertIsNone(self.store.claim_member_extraction(job_id, now=stamp))
        self.assertEqual(self.store.list_member_extraction_jobs(ready_only=True, now=stamp), [])
        self.assertEqual(self.store.trim_history("g", 0), 1)

    async def test_extraction_distinguishes_failure_empty_and_success_without_raw_member_id(self):
        source = self.source()
        job_id = self.store.enqueue_member_extraction("g", "owner", source)
        job = self.store.claim_member_extraction(job_id)
        calls = []

        class LLM:
            async def acomplete_structured(inner, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    raise RuntimeError("synthetic failure")
                if len(calls) == 2:
                    return {"parsed": {"facts": []}}
                return {"parsed": {"facts": [{"category": "project", "key": "项目", "value": "北斗",
                                               "confidence": 0.95, "evidence": "我负责北斗"}]}}

        ctx = SimpleNamespace(llm=LLM())
        self.assertEqual((await self.profiles.extract_job(ctx, job)).status, "retryable_failure")
        self.assertEqual((await self.profiles.extract_job(ctx, job)).status, "empty")
        success = await self.profiles.extract_job(ctx, job)
        self.assertEqual((success.status, success.stored), ("success", 1))
        self.assertEqual(calls[-1]["timeout"], 300)
        self.assertNotIn("max_tokens", calls[-1])
        self.assertIn("北斗", self.profiles.presentation("g", "owner"))

    async def test_changed_source_during_extraction_cannot_persist_old_fact(self):
        source = self.source()
        job = self.store.claim_member_extraction(self.store.enqueue_member_extraction("g", "owner", source))
        class LLM:
            async def acomplete_structured(inner, **kwargs):
                with self.store.transaction() as db:
                    db.execute("UPDATE group_history SET text='我负责星河' WHERE id=?", (source,))
                return {"parsed": {"facts": [{"category": "project", "key": "项目", "value": "北斗",
                                               "confidence": 0.95, "evidence": "我负责北斗"}]}}
        self.assertEqual((await self.profiles.extract_job(SimpleNamespace(llm=LLM()), job)).stored, 0)
        self.assertEqual(self.profiles.facts("g", "owner"), [])

    def test_privacy_opt_out_clears_jobs_checkpoints_and_receipts(self):
        source = self.source()
        job = self.store.enqueue_member_extraction("g", "owner", source)
        self.store.create_context_checkpoint("key", {"partial_summary": "private"}, scope="g", epoch=0)
        self.store.save_command_receipt("g", "m", "private")
        self.profiles.opt_out("g", "owner")
        self.assertIsNone(self.store.get_member_extraction(job))
        self.assertIsNone(self.store.get_context_checkpoint("key"))
        self.assertEqual(self.store.get_command_receipt("g", "m")["delivery_status"], "cancelled")

    def test_private_migration_preserves_sources_and_newer_explicit_fact(self):
        source = self.source("private-chat")
        self.store.add_member_memory_fact("private-chat", "owner", "project", "项目", "北斗", source_history_id=source, now=100)
        self.store.add_member_memory_fact("private-chat", "owner", "self", "语言", "中文", explicitness="explicit", now=101)
        self.store.set_member_consent("dm:private-chat", "owner", "opted_in")
        self.store.add_member_memory_fact("dm:private-chat", "owner", "self", "项目", "星河", explicitness="explicit", now=200)
        before_ids = [row["id"] for row in self.store.list_member_memory_facts("private-chat", "owner")]
        result = self.store.migrate_private_member("private-chat", "owner", evidence=self.proof())
        self.assertEqual(result["status"], "migrated")
        self.assertIsNone(self.store.get_group_member("private-chat", "owner"))
        self.assertEqual({row["fact_value"] for row in self.store.list_member_memory_facts("dm:private-chat", "owner")}, {"星河", "中文"})
        source_row = self.store.db.execute("SELECT * FROM group_history WHERE id=?", (source,)).fetchone()
        self.assertEqual((source_row["group_id"], source_row["member_id"]),
                         ("dm:private-chat", self.store.member_ref_for("dm:private-chat", "owner")))
        all_ids = {row["id"] for row in self.store.list_member_memory_facts("dm:private-chat", "owner", include_inactive=True)}
        self.assertTrue(set(before_ids) <= all_ids)
        self.assertEqual(self.store.migrate_private_member("private-chat", "owner", evidence=self.proof())["status"], "already_migrated")
        self.profiles.forget("dm:private-chat", "owner")
        self.assertEqual(self.store.migrate_private_member("private-chat", "owner", evidence=self.proof())["status"], "absent")

    def test_group_collision_and_missing_proof_never_migrate(self):
        self.profiles.remember("private-chat", "owner", "职责=群内职责")
        self.assertEqual(self.store.migrate_private_member("private-chat", "owner", evidence={})["status"], "ambiguous")
        proof = self.proof()
        proof["sessions"].append({"source": "qqbot", "chat_type": "group", "chat_id": "private-chat", "user_id": "owner"})
        self.assertEqual(self.store.migrate_private_member("private-chat", "owner", evidence=proof)["status"], "ambiguous")
        self.assertIsNone(self.store.get_group_member("dm:private-chat", "owner"))
        self.assertIn("群内职责", self.profiles.presentation("private-chat", "owner"))

    def test_private_knowledge_migration_preserves_ids_and_maps_duplicate_ids(self):
        self.profiles.remember("private-chat", "owner", "职责=项目")
        unique = self.store.add_knowledge_document("private-chat", "unique", "only legacy content")
        duplicate = self.store.add_knowledge_document("private-chat", "old title", "shared content")
        target = self.store.add_knowledge_document("dm:private-chat", "current title", "shared content")
        unique_id = int(unique["id"].removeprefix("doc-"))
        duplicate_id = int(duplicate["id"].removeprefix("doc-"))
        target_id = int(target["id"].removeprefix("doc-"))
        chunks_before = [row[0] for row in self.store.db.execute("SELECT id FROM knowledge_chunks WHERE document_id=?", (unique_id,))]
        result = self.store.migrate_private_member("private-chat", "owner", evidence=self.proof())
        self.assertEqual(result["migrated_documents"], 1)
        self.assertEqual(result["document_aliases"], {str(duplicate_id): target_id})
        self.assertEqual(self.store.resolve_legacy_document_id("dm:private-chat", duplicate_id), target_id)
        self.assertEqual(self.store.resolve_legacy_document_id("group", duplicate_id), duplicate_id)
        self.assertEqual([row[0] for row in self.store.db.execute("SELECT id FROM knowledge_chunks WHERE document_id=? AND group_id='dm:private-chat'", (unique_id,))], chunks_before)
        self.assertEqual(self.store.db.execute("SELECT group_id FROM knowledge_documents WHERE id=?", (duplicate_id,)).fetchone()[0], "private-chat")

    def test_backlog_scan_rotates_past_first_page(self):
        for index in range(5):
            self.store.append_history(f"g{index}", role="user", text="pending")
        self.assertEqual([item["group_id"] for item in self.store.list_memory_backlog_groups(2)], ["g0", "g1"])
        self.assertEqual([item["group_id"] for item in self.store.list_memory_backlog_groups(2)], ["g2", "g3"])
        self.assertEqual([item["group_id"] for item in self.store.list_memory_backlog_groups(2)], ["g4"])
        self.assertEqual([item["group_id"] for item in self.store.list_memory_backlog_groups(2)], ["g0", "g1"])

    def test_structured_recall_keeps_relevant_fact_after_long_overview(self):
        memory = GroupMemory(self.store)
        self.store.set_memory("g", "长概述" * 400, structured={"summary": "长概述" * 400,
            "facts": ["星河端口是 443", "无关颜色是蓝色"], "decisions": ["下周发布"], "todos": ["给星河补测试"]})
        recalled = memory.recall("g", "星河端口", char_budget=800)
        self.assertLessEqual(len(recalled), 800)
        self.assertIn("事实：星河端口是 443", recalled)
        self.assertIn("待办：给星河补测试", recalled)
        self.assertNotIn("无关颜色", recalled)

    def test_v6_upgrade_preserves_records_and_marks_old_delivery_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            first = Store(path, member_secret="synthetic-secret")
            first.save_command_receipt("g", "m", "old receipt")
            first.db.execute("PRAGMA user_version=6")
            first.db.commit()
            first.close()
            upgraded = Store(path, member_secret="synthetic-secret")
            self.assertEqual(upgraded.get_command_receipt("g", "m")["delivery_status"], "unknown")
            self.assertEqual(upgraded.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            upgraded.close()


if __name__ == "__main__":
    unittest.main()
