#!/usr/bin/env python3
"""Offline native monitor/stream cancellation contract; no provider or QQ traffic."""
import asyncio
import contextlib
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import qqbot_llm_policy as policy
import qqbot_request_deadline as deadline
from agent import chat_completion_helpers as helpers
from agent.chat_completion_nonstream import _NonStreamRequest
from agent.auxiliary_client import _aggregate_chat_stream_async


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def native_abort_checks():
    for streaming in (True, False):
        read_socket, peer = socket.socketpair()
        stopped = threading.Event()
        closed = threading.Event()
        activity = []
        aborts = []

        def abort(client, reason):
            aborts.append(reason)
            with contextlib.suppress(OSError):
                client.shutdown(socket.SHUT_RDWR)

        def close(client, reason):
            client.close()
            closed.set()

        agent = SimpleNamespace(platform="qqbot", api_mode="chat_completions", provider="synthetic",
                                base_url="https://synthetic.invalid/v1", model="synthetic-model",
                                _interrupt_requested=False, _touch_activity=lambda *a: None,
                                _emit_wait_notice=lambda *a: None,
                                _abort_request_openai_client=abort, _close_request_openai_client=close)

        class Stream(helpers._StreamingCall):
            def _resolve_stale_timeout(self):
                self._stream_stale_timeout = 600

            def _run_call(self):
                self.clients.set_client(read_socket)
                self._start_stream_attempt()
                try:
                    while read_socket.recv(1):
                        activity.append(1)
                        self.last_chunk_time["t"] = time.time()
                finally:
                    self.clients.close_once("request_error_cleanup")
                    self._call_done.set()

        class Nonstream(_NonStreamRequest):
            def _emit_wait_notice(self, *args, **kwargs):
                pass

            def _call(self):
                self.clients.set_client(read_socket)
                try:
                    read_socket.recv(1)
                finally:
                    self.clients.close_once("request_error_cleanup")

        def send_chunks():
            while not stopped.wait(0.01):
                try:
                    peer.send(b"x")
                except OSError:
                    return

        writer = threading.Thread(target=send_chunks, daemon=True) if streaming else None
        try:
            with patch.object(deadline, "REQUEST_TIMEOUT_SECONDS", 0.12), patch.object(
                    helpers, "_resolve_nonstream_watchdogs", return_value=SimpleNamespace(
                        codex=False, ttfb_enabled=False, idle_enabled=False, stale_timeout=600)):
                request = Stream(agent, {"model": "synthetic"}, None) if streaming else Nonstream(agent, {"model": "synthetic"})
                if writer:
                    writer.start()
                started = time.monotonic()
                try:
                    request.run()
                except InterruptedError:
                    pass
                else:
                    raise AssertionError("QQ deadline did not end the native request")
                worker = request.worker if streaming else request.thread
                worker.join(timeout=1)
                require(not worker.is_alive(), "cancelled request kept a worker alive")
                require(closed.is_set() and aborts, "native abort did not shut down and close the request socket")
                require(agent._qq_deadline_exceeded, "deadline was not identified separately from user cancellation")
                require(time.monotonic() - started < 2, "deadline did not bound actual transport lifetime")
                if streaming:
                    require(len(activity) >= 3, "stream did not continuously progress before the total deadline")
        finally:
            stopped.set()
            peer.close()
            read_socket.close()
            if writer:
                writer.join(timeout=1)


async def auxiliary_close_check():
    class Stream:
        closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()

        async def close(self):
            self.closed = True

    stream = Stream()
    with patch.object(policy, "REQUEST_TIMEOUT_SECONDS", 0.02):
        try:
            await policy.call(_aggregate_chat_stream_async(stream, model="synthetic"))
        except TimeoutError:
            pass
        else:
            raise AssertionError("auxiliary request did not time out")
    require(stream.closed, "auxiliary cancellation did not close the native async stream")


def inline_abort_check():
    aborted = []
    agent = SimpleNamespace(platform="qqbot", _interrupt_requested=False,
                            _abort_request_openai_client=lambda client, reason: aborted.append(reason))
    request = helpers._InlineRequest(agent, {}, 300, time.time())
    request._qq_request_started = time.monotonic() - 301
    request.client = object()
    native_abort = request.abort
    abort_returns = []

    def record_native_abort(reason):
        result = native_abort(reason)
        abort_returns.append(result)
        return result

    with patch.object(request, "abort", side_effect=record_native_abort):
        request._on_stale()
    # Native abort's boolean means newly STALE, not successfully cancelled.
    require(abort_returns == [False] and request.cancelled,
            "inline check did not execute the native interrupt-abort return-False path")
    require(aborted == ["qq_request_deadline"] and agent._qq_deadline_exceeded and agent._interrupt_requested,
            "inline abort failed to preserve the deadline outcome")
    for terminal_state in ("done", "cancelled"):
        agent = SimpleNamespace(platform="qqbot", _interrupt_requested=False)
        request = helpers._InlineRequest(agent, {}, 300, time.time())
        setattr(request, terminal_state, True)
        # Simulate completion/cancellation winning after the timer's first check.
        with patch.object(deadline, "request_expired", return_value=True):
            request._on_stale()
        require(not getattr(agent, "_qq_deadline_exceeded", False),
                "late inline timer overwrote a completed or cancelled request")


def wire_check():
    raw = {"model": "synthetic", "max_tokens": 8192, "max_completion_tokens": 4000,
           "extra_body": {"max_tokens": 8000}, "reasoning_effort": "high"}
    with patch.object(helpers, "_build_api_kwargs_for_mode", return_value=raw):
        request = helpers.build_api_kwargs(SimpleNamespace(platform="qqbot"), [], None)
    require(not any(key in request for key in ("max_tokens", "max_completion_tokens", "max_output_tokens")),
            "main request still has an output cap")
    require(request["reasoning_effort"] == "high" and request["timeout"] == 300,
            "request policy lost explicit session reasoning or 300 second timeout")
    require(deadline.request_policy(SimpleNamespace(platform="telegram"), raw) is raw,
            "QQ policy changed another platform")


if __name__ == "__main__":
    wire_check()
    native_abort_checks()
    inline_abort_check()
    asyncio.run(auxiliary_close_check())
    print("QQ_REQUEST_DEADLINE=300s STREAM_AND_NONSTREAM_ABORT=passed INLINE_ABORT=passed AUX_STREAM_CLOSE=passed SESSION_REASONING=preserved OUTPUT_CAP=absent")
