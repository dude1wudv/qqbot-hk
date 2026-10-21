"""QQ request deadlines reuse Hermes' request-local socket abort lifecycle."""
from __future__ import annotations

import time

from qqbot_llm_policy import REQUEST_TIMEOUT_SECONDS


def is_qq_agent(agent):
    platform = getattr(agent, "platform", None)
    return getattr(platform, "value", platform) == "qqbot"


def request_policy(agent, kwargs):
    if not is_qq_agent(agent):
        return kwargs
    request = dict(kwargs)
    for name in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        request.pop(name, None)
    if isinstance(request.get("extra_body"), dict):
        request["extra_body"] = dict(request["extra_body"])
        for name in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
            request["extra_body"].pop(name, None)
        if not request["extra_body"]:
            request.pop("extra_body")
    request["timeout"] = REQUEST_TIMEOUT_SECONDS
    return request


def start_request(request):
    if is_qq_agent(request.agent):
        request._qq_request_started = time.monotonic()


def request_expired(request, *, mark=True):
    started = getattr(request, "_qq_request_started", None)
    if started is None or time.monotonic() - started < REQUEST_TIMEOUT_SECONDS:
        return False
    done = getattr(request, "_call_done", None)
    if getattr(request, "done", False) or (done is not None and done.is_set()):
        return False
    thread = getattr(request, "thread", None)
    if thread is not None and not thread.is_alive():
        return False
    # Native interrupt handling marks this attempt cancelled before shutting
    # down its socket. It does not retry the same request after the deadline.
    if mark:
        mark_expired(request)
    return True


def mark_expired(request):
    request.agent._qq_deadline_exceeded = True
    request.agent._interrupt_requested = True


def bound_inline_timeout(agent, timeout):
    return min(timeout, REQUEST_TIMEOUT_SECONDS) if is_qq_agent(agent) else timeout


def finish_turn_result(agent, result):
    if not is_qq_agent(agent) or not getattr(agent, "_qq_deadline_exceeded", False):
        return result
    return {**result, "final_response": "这次模型请求已超过 5 分钟，我已停止等待。原对话已保留，可以重新提问。",
            "failed": True, "completed": False, "partial": False, "interrupted": False,
            "failure_reason": "qq_request_deadline", "error": None, "interrupt_message": None}
