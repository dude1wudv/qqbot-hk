import asyncio
import hashlib
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MEDIA_SPEC = importlib.util.spec_from_file_location(
    "qqbot_hk_media", ROOT / "plugins" / "smart_group_qq" / "media.py"
)
MEDIA = importlib.util.module_from_spec(MEDIA_SPEC)
sys.modules["qqbot_hk_media"] = MEDIA
MEDIA_SPEC.loader.exec_module(MEDIA)
CONTEXT_SPEC = importlib.util.spec_from_file_location(
    "qqbot_context", ROOT / "runtime" / "qqbot_context.py"
)
CONTEXT = importlib.util.module_from_spec(CONTEXT_SPEC)
sys.modules[CONTEXT_SPEC.name] = CONTEXT
CONTEXT_SPEC.loader.exec_module(CONTEXT)
PATCH_SPEC = importlib.util.spec_from_file_location(
    "patch_hermes_qq_context", ROOT / "scripts" / "patch-hermes-qq-context.py"
)
PATCH = importlib.util.module_from_spec(PATCH_SPEC)
PATCH_SPEC.loader.exec_module(PATCH)


def history(count):
    return [{"role": "user" if index % 2 == 0 else "assistant",
             "content": f"row-{index}"} for index in range(count)]


class ContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.metadata = types.ModuleType("agent.model_metadata")
        self.metadata.estimate_messages_tokens_rough = lambda messages: 0
        sys.modules["agent"] = types.ModuleType("agent")
        sys.modules["agent.model_metadata"] = self.metadata

    def tearDown(self):
        sys.modules.pop("agent.auxiliary_client", None)
        sys.modules.pop("agent.model_metadata", None)
        sys.modules.pop("agent", None)

    async def test_threshold_is_strictly_greater_than_80000(self):
        runner = SimpleNamespace()
        compactor = CONTEXT.Compactor(summarizer=lambda *_: None)
        entry = SimpleNamespace(session_key="key", session_id="sid", last_prompt_tokens=0)
        self.metadata.estimate_messages_tokens_rough = lambda _: 80_000
        await compactor.boundary(runner, entry, [], "quick", 1)
        self.assertNotIn("key", compactor.jobs)
        self.metadata.estimate_messages_tokens_rough = lambda _: 80_001
        await compactor.boundary(runner, entry, [], "quick", 1)
        job = compactor.jobs["key"]
        self.assertEqual(job.session_id, "sid")
        compactor.discard("key")

    async def test_scheduling_returns_without_waiting_for_summary(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_summary(snapshot, previous):
            started.set()
            await release.wait()
            return "summary"

        compactor = CONTEXT.Compactor(summarizer=slow_summary)
        rows = history(2)
        compactor.start("key", "sid", rows)
        await asyncio.wait_for(started.wait(), timeout=1)
        self.assertFalse(compactor.jobs["key"].task.done())
        self.assertEqual(compactor.jobs["key"].count, 2)
        release.set()
        self.assertEqual(await compactor.jobs["key"].task, "summary")

    def test_handoff_contains_summary_then_exactly_latest_ten_rows(self):
        rows = history(14)
        result = CONTEXT.handoff("brief", rows)
        self.assertEqual([row["content"] for row in result],
                         [CONTEXT.SUMMARY_PREFIX + "brief"] +
                         [row["content"] for row in rows[-10:]])
        self.assertEqual([row["role"] for row in result],
                         ["assistant"] + [row["role"] for row in rows[-10:]])


    def test_handoff_preserves_user_content_and_api_content(self):
        rows = history(14)
        rows[4]["content"] = "durable user text"
        rows[4]["api_content"] = "API-side user text"

        result = CONTEXT.handoff("brief", rows)
        user_row = next(row for row in result if row.get("content") == "durable user text")

        self.assertEqual(len(result), 11)
        self.assertEqual(result[0]["content"], CONTEXT.SUMMARY_PREFIX + "brief")
        self.assertEqual(user_row["api_content"], "API-side user text")

    async def test_summarize_projects_api_content_without_images_or_hidden_reasoning(self):
        captured = []

        async def fake_call_llm(**kwargs):
            captured.append(kwargs["messages"])
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="brief"),
                finish_reason="stop",
            )])

        agent = sys.modules["agent"]
        auxiliary_client = types.ModuleType("agent.auxiliary_client")
        auxiliary_client.async_call_llm = fake_call_llm
        agent.auxiliary_client = auxiliary_client
        sys.modules["agent.auxiliary_client"] = auxiliary_client

        await CONTEXT.summarize([{
            "role": "user",
            "content": "durable user text",
            "api_content": [
                {"type": "text", "text": "API-side user text"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,secret"}},
            ],
            "reasoning": "hidden reasoning",
        }])

        self.assertEqual(len(captured), 1)
        history_text = captured[0][1]["content"]
        self.assertIn("API-side user text", history_text)
        self.assertNotIn("durable user text", history_text)
        self.assertNotIn("image_url", history_text)
        self.assertNotIn("data:image", history_text)
        self.assertNotIn("hidden reasoning", history_text)

    async def test_failed_summary_retains_parent_and_does_not_publish(self):
        async def failed(*_):
            raise RuntimeError("synthetic failure")

        compactor = CONTEXT.Compactor(summarizer=failed)
        rows = history(1)
        compactor.start("key", "parent", rows)
        await compactor.jobs["key"].task
        self.assertIsNone(compactor.jobs["key"].task.result())
        self.assertEqual(compactor.jobs["key"].session_id, "parent")

    async def test_summary_timeout_retains_parent(self):
        async def hung(*_):
            await asyncio.Event().wait()

        compactor = CONTEXT.Compactor(summarizer=hung)
        compactor.start("key", "parent", history(1))
        job = compactor.jobs["key"]

        async def timeout(awaitable, timeout):
            awaitable.close()
            raise asyncio.TimeoutError

        with patch.object(CONTEXT.asyncio, "wait_for", side_effect=timeout):
            await job.task
        self.assertIsNone(job.task.result())
        self.assertEqual(job.session_id, "parent")

    async def test_reset_session_id_discards_completed_summary(self):
        compactor = CONTEXT.Compactor(summarizer=lambda *_: asyncio.sleep(0, result="brief"))
        entry = SimpleNamespace(session_key="key", session_id="old", last_prompt_tokens=0)
        rows = history(1)
        compactor.start("key", "old", rows)
        await compactor.jobs["key"].task
        entry.session_id = "reset-new"
        with patch.object(CONTEXT, "publish", side_effect=AssertionError("must not publish")):
            result = await compactor.boundary(SimpleNamespace(), entry, rows, "quick", 1)
        self.assertIs(result, rows)
        self.assertNotIn("key", compactor.jobs)

    async def test_transcript_mutation_rejects_completed_summary(self):
        compactor = CONTEXT.Compactor(summarizer=lambda *_: asyncio.sleep(0, result="brief"))
        entry = SimpleNamespace(session_key="key", session_id="sid", last_prompt_tokens=0)
        rows = history(2)
        compactor.start("key", "sid", rows)
        await compactor.jobs["key"].task
        changed = [*rows]
        changed[0] = {"role": "user", "content": "altered"}
        with patch.object(CONTEXT, "publish", side_effect=AssertionError("must not publish")):
            result = await compactor.boundary(SimpleNamespace(), entry, changed, "quick", 1)
        self.assertIs(result, changed)
        self.assertNotIn("key", compactor.jobs)

    async def test_ten_or_fewer_new_rows_are_preserved_in_handoff(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def summarize(snapshot, previous):
            started.set()
            await release.wait()
            return "brief"

        compactor = CONTEXT.Compactor(summarizer=summarize)
        rows = [{"role": "user", "content": f"row-{index}-" + ("x" * 200)}
                for index in range(14)]
        compactor.start("key", "sid", rows)
        await asyncio.wait_for(started.wait(), timeout=1)
        added_during_summary = [
            {"role": "user", "content": f"new-{index}-" + ("y" * 200)}
            for index in range(3)
        ]
        current = rows + added_during_summary
        release.set()
        await compactor.jobs["key"].task
        published = []
        entry = SimpleNamespace(session_key="key", session_id="sid", last_prompt_tokens=0)

        def publish(*args):
            published.append(args)
            args[1].session_id = "child-session"
            return "child-session"

        with patch.object(CONTEXT, "publish", side_effect=publish):
            result = await compactor.boundary(SimpleNamespace(), entry, current, "quick", 1)
        self.assertEqual(entry.session_id, "child-session")
        self.assertEqual(result, CONTEXT.handoff("brief", current))
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0][2], current)
        self.assertEqual([item["content"] for item in result[1:]],
                         [item["content"] for item in current[-10:]])

    async def test_more_than_ten_new_rows_starts_delta_summary_without_dropping_rows(self):
        summarized = []
        gate = asyncio.Event()

        async def summarize(snapshot, previous):
            summarized.append((snapshot, previous))
            if previous:
                await gate.wait()
            return "brief-1" if not previous else "brief-2"

        compactor = CONTEXT.Compactor(summarizer=summarize)
        base = history(3)
        compactor.start("key", "sid", base)
        await compactor.jobs["key"].task
        additions = history(11)
        current = base + additions
        with patch.object(CONTEXT, "publish", side_effect=AssertionError("too early")):
            result = await compactor.boundary(
                SimpleNamespace(), SimpleNamespace(session_key="key", session_id="sid", last_prompt_tokens=0),
                current, "quick", 1)
        self.assertIs(result, current)
        delta_job = compactor.jobs["key"]
        self.assertEqual(delta_job.count, len(current))
        await asyncio.sleep(0)
        self.assertEqual(summarized[1][0], additions)
        self.assertEqual(summarized[1][1], "brief-1")
        gate.set()
        await delta_job.task
        self.assertEqual(delta_job.task.result(), "brief-2")

    async def test_group_session_keys_isolate_background_jobs(self):
        compactor = CONTEXT.Compactor(summarizer=lambda *_: asyncio.sleep(0, result="brief"))
        compactor.start("group-a", "session-a", history(1))
        compactor.start("group-b", "session-b", history(1))
        self.assertEqual(compactor.jobs["group-a"].session_id, "session-a")
        self.assertEqual(compactor.jobs["group-b"].session_id, "session-b")
        self.assertIsNot(compactor.jobs["group-a"], compactor.jobs["group-b"])
        await asyncio.gather(*(job.task for job in compactor.jobs.values()))

    async def test_non_qq_boundary_does_not_schedule(self):
        runner = SimpleNamespace()
        result = await CONTEXT.context_boundary(
            runner, SimpleNamespace(platform="telegram"), None, [], "key", 1)
        self.assertEqual(result, [])
        self.assertFalse(hasattr(runner, "_qq_context_compactor"))

    def test_patch_rejects_nonmatching_source_sha(self):
        source = "def target():\n    return 1\n"
        with self.assertRaisesRegex(ValueError, "SHA mismatch"):
            PATCH.patch_source(source, "0" * 64, [("return 1", "return 2")])

    def test_patch_application_is_idempotent(self):
        source = "def target():\n    return 1\n"
        replacements = [("return 1", "return 2")]
        patched = PATCH.patch_source(source, hashlib.sha256(source.encode()).hexdigest(), replacements)
        self.assertEqual(patched.count(PATCH.MARKER), 1)
        self.assertEqual(patched.count("return 2"), 1)
        self.assertEqual(PATCH.patch_source(patched, "wrong", replacements), patched)


if __name__ == "__main__":
    unittest.main()
