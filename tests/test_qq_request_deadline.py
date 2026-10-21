import asyncio
import importlib.util
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_qq_context import ROOT, POLICY

SPEC = importlib.util.spec_from_file_location("qqbot_request_deadline", ROOT / "runtime/qqbot_request_deadline.py")
DEADLINE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = DEADLINE
SPEC.loader.exec_module(DEADLINE)


class RequestDeadlineTests(unittest.IsolatedAsyncioTestCase):
    def test_qq_wire_omits_caps_preserves_reasoning_and_leaves_other_platforms_unchanged(self):
        original = {"max_tokens": 42, "max_completion_tokens": 42, "max_output_tokens": 42,
                    "extra_body": {"max_tokens": 42, "retain": True}, "reasoning_effort": "high"}
        request = DEADLINE.request_policy(SimpleNamespace(platform="qqbot"), original)
        self.assertEqual(request, {"extra_body": {"retain": True}, "reasoning_effort": "high", "timeout": 300})
        self.assertEqual(original["max_tokens"], 42)
        self.assertIs(DEADLINE.request_policy(SimpleNamespace(platform="telegram"), original), original)

    def test_deadline_uses_elapsed_wall_clock_despite_progress_and_ignores_completed_call(self):
        request = SimpleNamespace(agent=SimpleNamespace(platform="qqbot"))
        with patch.object(DEADLINE.time, "monotonic", return_value=10):
            DEADLINE.start_request(request)
        request.last_chunk_time = {"t": 309.9}
        with patch.object(DEADLINE.time, "monotonic", return_value=310):
            self.assertTrue(DEADLINE.request_expired(request))
        self.assertTrue(request.agent._interrupt_requested)
        self.assertTrue(request.agent._qq_deadline_exceeded)
        request.done = True
        with patch.object(DEADLINE.time, "monotonic", return_value=311):
            self.assertFalse(DEADLINE.request_expired(request))

    async def test_auxiliary_deadline_cancels_io_and_executes_finally(self):
        closed = asyncio.Event()

        async def waiting_transport():
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

        with patch.object(POLICY, "REQUEST_TIMEOUT_SECONDS", 0.01):
            with self.assertRaises(TimeoutError):
                await POLICY.call(waiting_transport())
        self.assertTrue(closed.is_set())

    def test_terminal_deadline_has_honest_notice_and_does_not_change_user_stop(self):
        original = {"interrupted": True, "messages": [{"role": "user", "content": "kept"}]}
        agent = SimpleNamespace(platform="qqbot", _qq_deadline_exceeded=True)
        result = DEADLINE.finish_turn_result(agent, original)
        self.assertEqual(result["failure_reason"], "qq_request_deadline")
        self.assertFalse(result["completed"])
        self.assertIn("5 分钟", result["final_response"])
        self.assertEqual(result["messages"], original["messages"])
        agent._qq_deadline_exceeded = False
        self.assertIs(DEADLINE.finish_turn_result(agent, original), original)
