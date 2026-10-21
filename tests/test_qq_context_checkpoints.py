import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_qq_context import CONTEXT, ROOT

sys.path.insert(0, str(ROOT / "plugins"))
from smart_group_qq.store import Store


class CheckpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.sqlite"
        self.store = Store(self.path, member_secret="synthetic-checkpoint-secret")
        self.addCleanup(lambda: self.store.close())
        self.rows = [{"role": "user", "content": "synthetic-history-" * 20}]
        for attr, value in (("CHUNK_CHARS", 160), ("RETRY_DELAYS", (0, 0))):
            active = patch.object(CONTEXT, attr, value)
            active.start()
            self.addCleanup(active.stop)

    async def test_restart_resumes_next_chunk_without_resending_completed_prefix(self):
        blocked = asyncio.Event()
        reached_second = asyncio.Event()
        first_calls = []

        async def first(fragment, previous, focus):
            first_calls.append(fragment)
            if previous:
                reached_second.set()
                await blocked.wait()
            return "saved-first-chunk"

        old = CONTEXT.Compactor(store=self.store, chunk_summarizer=first)
        old.start("key", "parent", self.rows, scope="group")
        await asyncio.wait_for(reached_second.wait(), 1)
        old_task = old.jobs["key"].task
        old.discard("key")
        await asyncio.gather(old_task, return_exceptions=True)
        checkpoint = self.store.get_context_checkpoint("key")
        self.assertEqual(checkpoint["chunk_index"], 1)
        self.assertEqual(checkpoint["policy_version"], CONTEXT.POLICY_VERSION)
        self.assertEqual(checkpoint["chunk_total"], len(CONTEXT.history_chunks(self.rows)))
        self.assertEqual(checkpoint["partial_summary"], "saved-first-chunk")
        self.store.close()
        self.store = Store(self.path, member_secret="synthetic-checkpoint-secret")
        calls = []

        async def resumed(fragment, previous, focus):
            calls.append((fragment, previous))
            return "resumed-summary"

        new = CONTEXT.Compactor(store=self.store, chunk_summarizer=resumed)
        reordered = [{key: row[key] for key in reversed(row)} for row in self.rows]
        new.start("key", "parent", reordered, scope="group")
        self.assertEqual(await new.jobs["key"].task, "resumed-summary")
        self.assertEqual(calls[0], (first_calls[1], "saved-first-chunk"))
        self.assertNotIn(first_calls[0], [row[0] for row in calls])
        self.assertEqual(self.store.get_context_checkpoint("key")["status"], "ready")

    def test_chunks_keep_message_boundaries_and_only_split_oversized_single_rows(self):
        rows = [{"role": "user", "content": "a" * 55},
                {"role": "assistant", "content": "b" * 55},
                {"role": "user", "content": "short"}]
        chunks = CONTEXT.history_chunks(rows)
        self.assertEqual([json.loads(line) for chunk in chunks for line in chunk.splitlines()], rows)
        self.assertTrue(all(len(chunk) <= CONTEXT.CHUNK_CHARS for chunk in chunks))
        self.assertEqual(CONTEXT.history_chunks(self.rows), CONTEXT.history_chunks(
            [{key: row[key] for key in reversed(row)} for row in self.rows]))
        self.assertEqual("".join(CONTEXT.history_chunks(self.rows)),
                         json.dumps(self.rows[0], ensure_ascii=False, sort_keys=True))

    async def test_incompatible_policy_or_chunk_size_discards_old_partial_summary(self):
        previous_inputs = []

        async def summarize(fragment, previous, focus):
            previous_inputs.append(previous)
            return "new-summary"

        for changed in ({"policy_version": 1}, {"chunk_chars": 159}):
            compactor = CONTEXT.Compactor(store=self.store, chunk_summarizer=summarize)
            compactor.start("key", "parent", self.rows, scope="group")
            await compactor.jobs["key"].task
            original = self.store.get_context_checkpoint("key")
            compactor._save("key", original, {**original, **changed, "partial_summary": "obsolete-policy"})
            previous_inputs.clear()
            restarted = CONTEXT.Compactor(store=self.store, chunk_summarizer=summarize)
            restarted.start("key", "parent", self.rows, scope="group")
            await restarted.jobs["key"].task
            current = self.store.get_context_checkpoint("key")
            self.assertNotEqual(current["checkpoint_id"], original["checkpoint_id"])
            self.assertEqual(previous_inputs[0], "")
            self.assertNotIn("obsolete-policy", previous_inputs)

    async def test_native_reset_cancels_entire_scope_without_touching_same_id_dm(self):
        started, closed = set(), set()

        async def blocked(fragment, previous, focus):
            identity = json.loads(fragment)["content"]
            started.add(identity)
            try:
                await asyncio.Event().wait()
            finally:
                closed.add(identity)

        compactor = CONTEXT.Compactor(store=self.store, chunk_summarizer=blocked)
        for key, scope in (("key", "group"), ("same-scope", "group"), ("dm-key", "dm:group")):
            compactor.start(key, "parent", [{"role": "user", "content": key}], scope=scope)
        tasks = {key: job.task for key, job in compactor.jobs.items()}
        while len(started) < 3:
            await asyncio.sleep(0)
        runner = SimpleNamespace(_qq_context_compactor=compactor)
        source = SimpleNamespace(platform="qqbot", chat_type="group", chat_id="group")
        await CONTEXT.cancel_context_scope(runner, source, "key")
        self.assertEqual(closed, {"key", "same-scope"})
        self.assertTrue(tasks["key"].cancelled())
        self.assertTrue(tasks["same-scope"].cancelled())
        self.assertFalse(tasks["dm-key"].done())
        self.assertIsNone(self.store.get_context_checkpoint("key"))
        self.assertIsNone(self.store.get_context_checkpoint("same-scope"))
        self.assertIsNotNone(self.store.get_context_checkpoint("dm-key"))
        compactor.discard("dm-key", invalidate=True)
        await asyncio.gather(tasks["dm-key"], return_exceptions=True)

    async def test_native_reset_after_restart_invalidates_persisted_checkpoint(self):
        compactor = CONTEXT.Compactor(store=self.store, chunk_summarizer=lambda *args: asyncio.sleep(0, result="saved"))
        compactor.start("key", "parent", self.rows, scope="group")
        await compactor.jobs["key"].task
        runner = SimpleNamespace(_qq_memory_store=self.store)
        source = SimpleNamespace(platform="qqbot", chat_type="group", chat_id="group")
        await CONTEXT.cancel_context_scope(runner, source, "key")
        self.assertIsNone(self.store.get_context_checkpoint("key"))

    async def test_three_failures_stop_only_failed_chunk_and_manual_retry_keeps_progress(self):
        calls = []

        async def flaky(fragment, previous, focus):
            calls.append(previous)
            if previous:
                raise ValueError("synthetic invalid JSON")
            return "valid-prefix"

        compactor = CONTEXT.Compactor(store=self.store, chunk_summarizer=flaky)
        compactor.start("key", "parent", self.rows, scope="group")
        await compactor.jobs["key"].task
        checkpoint = self.store.get_context_checkpoint("key")
        self.assertEqual((checkpoint["chunk_index"], checkpoint["failure_count"], checkpoint["status"]), (1, 3, "failed"))
        self.assertEqual(calls.count(""), 1)
        self.assertFalse(compactor.start("key", "parent", self.rows + [{"role": "user", "content": "new"}], scope="group"))
        compactor.chunk_summarizer = lambda *args: asyncio.sleep(0, result="recovered")
        self.assertTrue(compactor.start("key", "parent", self.rows, scope="group", force=True))
        await compactor.jobs["key"].task
        self.assertEqual(self.store.get_context_checkpoint("key")["failure_count"], 0)

    async def test_epoch_invalidation_during_call_cannot_restore_checkpoint(self):
        reached = asyncio.Event()
        release = asyncio.Event()

        async def delayed(*args):
            reached.set()
            await release.wait()
            return "must-not-reappear"

        compactor = CONTEXT.Compactor(store=self.store, chunk_summarizer=delayed)
        compactor.start("key", "parent", self.rows, scope="group")
        await reached.wait()
        self.store.clear_group("group")
        release.set()
        self.assertIsNone(await compactor.jobs["key"].task)
        self.assertIsNone(self.store.get_context_checkpoint("key"))

    async def test_background_slots_are_released_between_chunks(self):
        semaphore = asyncio.Semaphore(2)
        active = peak = 0

        async def summarize(*args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return "brief"

        compactor = CONTEXT.Compactor(store=self.store, semaphore=semaphore, chunk_summarizer=summarize)
        for i in range(5):
            compactor.start(f"key-{i}", f"parent-{i}", self.rows, scope=f"group-{i}")
        await asyncio.gather(*(job.task for job in compactor.jobs.values()))
        self.assertEqual(peak, 2)
        self.assertEqual(semaphore._value, 2)

    async def test_source_mutation_restarts_snapshot_but_never_uses_old_summary(self):
        summaries = []

        async def summarize(fragment, previous, focus):
            summaries.append(previous)
            return "brief"

        compactor = CONTEXT.Compactor(store=self.store, chunk_summarizer=summarize)
        compactor.start("key", "parent", self.rows, scope="group")
        await compactor.jobs["key"].task
        summaries.clear()
        changed = [{"role": "user", "content": "changed-source"}]
        compactor.start("key", "parent", changed, scope="group")
        await compactor.jobs["key"].task
        self.assertEqual(summaries, [""])

    async def test_manual_compress_reuses_active_job_and_does_not_publish_without_turn_lease(self):
        release = asyncio.Event()
        compactor = CONTEXT.Compactor(store=self.store, chunk_summarizer=lambda *args: release.wait())
        rows = [{"role": "user", "content": f"row-{i}"} for i in range(12)]
        runner = SimpleNamespace(_qq_memory_store=self.store, _qq_context_compactor=compactor)
        source = SimpleNamespace(platform="qqbot", chat_type="group", chat_id="group")
        entry = SimpleNamespace(session_key="key", session_id="parent")
        request = SimpleNamespace(partial=False, keep_last=None, focus_topic="")
        with patch.object(CONTEXT, "publish", side_effect=AssertionError("requires real turn lease")):
            result = await CONTEXT.manual_compress(runner, source, entry, rows, request)
            original = compactor.jobs["key"].task
            await CONTEXT.manual_compress(runner, source, entry, rows, request)
            self.assertIs(compactor.jobs["key"].task, original)
        self.assertIn("下一条消息", result)
        compactor.discard("key")
        await asyncio.gather(original, return_exceptions=True)
