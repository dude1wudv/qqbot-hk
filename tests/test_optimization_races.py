import asyncio
from contextlib import closing
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
from smart_group_qq.private_scope import legacy_evidence
from smart_group_qq.scheduler import MemoryScheduler
from smart_group_qq.store import Store


SUMMARY = {"summary": "有效摘要", "topics": [], "facts": [], "decisions": [],
           "todos": [], "open_questions": [], "participants": []}


class SchedulerPrivacyRaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:", member_secret="synthetic")
        self.memory = GroupMemory(self.store)
        self.addCleanup(self.store.close)

    async def test_cancelled_old_worker_cannot_drop_new_epoch_summary(self):
        entered = asyncio.Event()
        calls = []

        async def complete(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                entered.set()
                await asyncio.Event().wait()
            return {"parsed": SUMMARY}

        ctx = SimpleNamespace(llm=SimpleNamespace(acomplete_structured=complete))
        scheduler = MemoryScheduler(ctx, self.memory, MemberMemory(self.store), self.store)
        try:
            self.memory.record("g", "u", "old source", "m1")
            old, _ = scheduler.summary("g", explicit=True)
            await entered.wait()
            self.store.clear_group("g")
            scheduler.cancel_scope("g")
            self.memory.record("g", "u", "new source", "m2")
            new, _ = scheduler.summary("g", explicit=True)
            result = await asyncio.wait_for(asyncio.shield(new), 1)
            self.assertTrue(old.cancelled())
            self.assertEqual(result["pending_count"], 0)
            self.assertEqual(len(calls), 2)
            self.assertNotIn("old source", calls[1]["input"][1]["text"])
        finally:
            await scheduler.close()

    async def test_explicit_retry_pauses_after_failure_before_automatic_ceiling(self):
        calls = []
        failing = True

        async def complete(**kwargs):
            calls.append(kwargs)
            if failing:
                raise TimeoutError("synthetic")
            return {"parsed": SUMMARY}

        ctx = SimpleNamespace(llm=SimpleNamespace(acomplete_structured=complete))
        scheduler = MemoryScheduler(ctx, self.memory, MemberMemory(self.store), self.store)
        try:
            self.memory.record("g", "u", "first source", "m1")
            await self.memory.refresh_ai(ctx, "g", force=True, max_batches=1)
            self.assertEqual(self.memory.status("g")["attempt"], 1)
            future, _ = scheduler.summary("g", explicit=True, retry=True)
            paused = await future
            self.assertEqual(paused["attempt"], 2)
            self.assertEqual(paused["job_status"], "paused")
            self.assertTrue(paused["blocked"])
            self.assertFalse(self.memory.needs_refresh("g", now=10**12))
            job = self.store.get_compaction_job("g")
            self.memory.record("g", "u", "new messages do not unpause", "m2")
            self.assertEqual(self.store.enqueue_compaction_job("g"), job["id"])
            self.assertIsNone(self.store.claim_compaction_job(job["id"], now=10**12))
            self.assertIsNone(self.store.claim_compaction_job(group_id="g", now=10**12))
            scheduler.poll()
            await asyncio.sleep(0)
            self.assertEqual(len(calls), 2)
            failing = False
            recovered, _ = scheduler.summary("g", explicit=True, retry=True)
            self.assertEqual((await recovered)["pending_count"], 0)
            self.assertIsNone(self.store.get_compaction_job("g"))
            self.assertEqual(len(calls), 3)
        finally:
            await scheduler.close()

    def test_pause_persists_and_force_can_claim_without_changing_past_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plugin.db"
            store = Store(path, member_secret="synthetic")
            store.append_history("g", role="user", text="pending")
            job_id = store.enqueue_compaction_job("g", now=100)
            store.claim_compaction_job(job_id, now=100)
            store.finish_compaction_job(job_id, False, error="TimeoutError", now=101)
            self.assertTrue(store.pause_compaction("g", now=102))
            store.close()
            store = Store(path, member_secret="synthetic")
            try:
                self.assertFalse(GroupMemory(store).needs_refresh("g", now=10**12))
                self.assertEqual(store.get_compaction_job("g")["attempt"], 1)
                self.assertIsNone(store.claim_compaction_job(group_id="g", now=10**12))
                forced = store.claim_compaction_job(group_id="g", force=True, now=103)
                self.assertEqual(forced["attempt"], 2)
                self.assertEqual(forced["status"], "running")
            finally:
                store.close()


class PrivateProvenanceRaceTests(unittest.TestCase):
    def test_origin_only_conflicting_group_blocks_private_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            with closing(sqlite3.connect(Path(directory) / "state.db")) as db:
                db.execute("CREATE TABLE sessions(source,chat_id,user_id,chat_type,origin_json)")
                db.executemany("INSERT INTO sessions VALUES(?,?,?,?,?)", [
                    ("qqbot", "same-id", "owner", "dm", "{}"),
                    ("qqbot", None, None, None, json.dumps({"chat_id": "same-id", "user_id": "owner", "chat_type": "group"})),
                    ("qqbot", "unrelated", "owner", "group", "{malformed"),
                ])
                db.commit()
            gateway = SimpleNamespace(session_store=SimpleNamespace(_routing_home=directory))
            proof = legacy_evidence(gateway, "same-id")
            self.assertEqual({row["chat_type"] for row in proof["sessions"]}, {"dm", "group"})
            store = Store(":memory:", member_secret="synthetic")
            try:
                profiles = MemberMemory(store)
                profiles.remember("same-id", "owner", "项目=仅限原群")
                result = store.migrate_private_member("same-id", "owner", evidence=proof)
                self.assertEqual(result["status"], "ambiguous")
                self.assertEqual(store.list_member_memory_facts("dm:same-id", "owner"), [])
                self.assertIn("仅限原群", profiles.presentation("same-id", "owner"))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
