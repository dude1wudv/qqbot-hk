#!/usr/bin/env python3
"""Synthetic, bounded live checks; run explicitly inside the HK bot container.

This makes four small requests per model, plus at most one retry for each
HTTP 429/5xx failure. It never sends QQ messages or reads conversation data.
Length, latency and token counts describe these fixtures, not intelligence.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
from pathlib import Path
import queue
import socket
import struct
import threading
import time
from typing import Any
import urllib.error
import urllib.request
import zlib


ENDPOINT = "http://sub2api:8080/v1/chat/completions"
KEY_NAME = "SUB2API_DEEPSEEK_API_KEY"
REQUEST_TIMEOUT = 300.0
MAX_RESPONSE_BYTES = 1024 * 1024
MODELS = (
    ("deepseek/deepseek-v4.1-flash", "low"),
    ("deepseek/deepseek-v4.1-flash-fast", "high"),
)
REACTIONS = {"开心", "疑惑", "无语", "鼓励", "晚安", "赞同", "惊讶", "难过", "感谢", "庆祝"}
ECHO_TEXT = "QQ_SYNTHETIC_ECHO"
ECHO_CONFIRMATION = f"ECHO_OK:{ECHO_TEXT}"
ECHO_TOOL = {
    "type": "function",
    "function": {
        "name": "synthetic_echo",
        "description": "Return the supplied synthetic text unchanged. No external side effects.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string", "enum": [ECHO_TEXT]}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
}


def load_api_key(env_file: Path = Path("/opt/data/.env")) -> str:
    """Match verify-server.sh precedence; retain only the requested secret."""
    value = os.environ.get(KEY_NAME, "").strip()
    if value:
        return value
    try:
        with env_file.open(encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if line.startswith("export "):
                    line = line[7:].lstrip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, candidate = line.split("=", 1)
                if name.strip() == KEY_NAME:
                    value = candidate.strip()
    except OSError:
        return ""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def failure(kind: str, status: int | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"type": kind}
    if status is not None:
        error["http_status"] = status
    return error


class ChatClient:
    """One total deadline per logical request, including its possible retry.

    The daemon bounds even DNS or a response that keeps dribbling bytes. On a
    deadline the suite stops issuing requests; exiting closes that connection.
    No retry occurs for a timeout, a partial/invalid response or a model result.
    """

    def __init__(self, api_key: str, timeout: float = REQUEST_TIMEOUT) -> None:
        self._api_key = api_key
        self.timeout = timeout
        self.deadline_expired = False

    def post(self, body: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        started = time.monotonic()
        deadline = started + self.timeout
        state: dict[str, Any] = {"attempts": 0, "http_statuses": []}
        results: queue.Queue = queue.Queue(maxsize=1)
        if self.deadline_expired:
            return None, {"latency_seconds": 0, **state,
                          "error": failure("skipped_after_deadline")}

        def perform() -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            for attempt in range(2):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    results.put((None, failure("request_deadline_exceeded")))
                    return
                state["attempts"] = attempt + 1
                request = urllib.request.Request(
                    ENDPOINT, data=data, method="POST",
                    headers={"Authorization": f"Bearer {self._api_key}",
                             "Content-Type": "application/json"},
                )
                try:
                    with urllib.request.urlopen(request, timeout=remaining) as response:
                        state["http_statuses"].append(response.status)
                        raw = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        results.put((None, failure("response_too_large")))
                        return
                    parsed = json.loads(raw.decode("utf-8"))
                    if not isinstance(parsed, dict) or "error" in parsed:
                        results.put((None, failure("invalid_api_response")))
                        return
                    results.put((parsed, None))
                    return
                except urllib.error.HTTPError as exc:
                    status = exc.code
                    state["http_statuses"].append(status)
                    exc.close()  # Never read or print upstream error bodies.
                    if attempt == 0 and (status == 429 or 500 <= status <= 599):
                        if deadline - time.monotonic() > 1:
                            time.sleep(1)
                            continue
                    results.put((None, failure("http_error", status)))
                    return
                except (TimeoutError, socket.timeout):
                    results.put((None, failure("request_timeout")))
                    return
                except urllib.error.URLError as exc:
                    kind = "request_timeout" if isinstance(exc.reason, TimeoutError) else "transport_error"
                    results.put((None, failure(kind)))
                    return
                except (UnicodeDecodeError, json.JSONDecodeError):
                    results.put((None, failure("invalid_json")))
                    return
                except OSError:
                    results.put((None, failure("transport_error")))
                    return
                except Exception:
                    results.put((None, failure("request_internal_error")))
                    return

        threading.Thread(target=perform, daemon=True).start()
        try:
            response, error = results.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            response, error = None, failure("request_deadline_exceeded")
        if error and error["type"] == "request_deadline_exceeded":
            self.deadline_expired = True
        metrics = {"latency_seconds": round(time.monotonic() - started, 3),
                   "attempts": state["attempts"],
                   "http_statuses": list(state["http_statuses"])}
        if error:
            metrics["error"] = error
        return response, metrics


def numeric_usage(value: Any) -> dict[str, Any]:
    """Keep known token counters only; never echo arbitrary provider metadata."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens",
                 "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "cached_tokens"):
        item = value.get(name)
        if type(item) is int and item >= 0:
            result[name] = item
    for name in ("prompt_tokens_details", "completion_tokens_details"):
        details = numeric_usage(value.get(name)) if isinstance(value.get(name), dict) else {}
        if details:
            result[name] = details
    return result


def request_body(model: str, effort: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {"model": model, "reasoning_effort": effort, "messages": messages,
            "max_tokens": 2048 if effort == "high" else 1024, "stream": False}


def chat(client: ChatClient, body: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    response, metrics = client.post(body)
    metrics.update({"finish_reason": None, "usage": {}})
    if response is None:
        return None, metrics
    metrics["usage"] = numeric_usage(response.get("usage"))
    actual_model = response.get("model")
    if isinstance(actual_model, str):
        metrics["response_model"] = actual_model[:160]
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        metrics["error"] = failure("invalid_choices")
        return None, metrics
    choice = choices[0]
    reason = choice.get("finish_reason")
    if isinstance(reason, str):
        metrics["finish_reason"] = reason[:40]
    message = choice.get("message")
    if not isinstance(message, dict):
        metrics["error"] = failure("invalid_message")
        return None, metrics
    return message, metrics


def check_text(client: ChatClient, model: str, effort: str) -> dict[str, Any]:
    body = request_body(model, effort, [
        {"role": "system", "content": (
            "你是群友小栖，亲切自然，不抢其他话题。下面是完全虚构的群聊。"
            "只回复最后一个明确问你的问题，用一句不超过60字的短答。"
            "仅输出JSON对象，恰好含action、message、reaction三个字段；"
            "action为reply，message为实际短答，reaction为null或这些类别之一："
            + "、".join(sorted(REACTIONS)) + "。不要输出思考过程或Markdown。")},
        {"role": "user", "content": (
            "[甲] 我在排查一个Python导入错误。\n"
            "[乙] 今晚22:00一起开黑。\n"
            "[丙] 我刚买了杯咖啡，有点苦。\n"
            "[甲] 找到原因了，是工作目录错了。\n"
            "[乙 @小栖] 我刚才说今晚几点开黑？")},
    ])
    message, metrics = chat(client, body)
    result = {**metrics, "ok": False, "text_chars": 0, "correct_target": False}
    if message is None:
        return result
    content = message.get("content")
    if not isinstance(content, str):
        result["error"] = failure("missing_text_content")
        return result
    result["synthetic_reply"] = content[:1000]
    try:
        value = json.loads(content)
    except (ValueError, TypeError):
        result["error"] = failure("invalid_reply_json")
        return result
    if not isinstance(value, dict) or set(value) != {"action", "message", "reaction"}:
        result["error"] = failure("invalid_reply_fields")
        return result
    reply = value.get("message")
    reaction = value.get("reaction")
    valid_reaction = reaction is None or (isinstance(reaction, str) and reaction in REACTIONS)
    if value.get("action") != "reply" or not isinstance(reply, str) or not reply.strip() or not valid_reaction:
        result["error"] = failure("invalid_reply_envelope")
        return result
    result["text_chars"] = len(reply)
    result["correct_target"] = any(term in reply for term in ("22:00", "22：00", "22点", "二十二点", "十点", "10点"))
    result["ok"] = bool(result["correct_target"] and len(reply) <= 60
                        and metrics["finish_reason"] == "stop" and not message.get("tool_calls"))
    if not result["ok"]:
        result["error"] = failure("text_acceptance_failed")
    return result


def check_tool(client: ChatClient, model: str, effort: str) -> dict[str, Any]:
    messages = [{"role": "user", "content": (
        f"This is a synthetic test. Call synthetic_echo exactly once with text={ECHO_TEXT!r}. "
        f"After receiving its result, reply with exactly {ECHO_CONFIRMATION}. "
        "Do not call any more tools.") }]
    body = request_body(model, effort, messages)
    body.update({"tools": [ECHO_TOOL], "tool_choice": "auto"})
    message, first = chat(client, body)
    result: dict[str, Any] = {"ok": False, "tool_exec_count": 0, "initial": first,
                              "reasoning_content_forwarded": False}
    if message is None:
        return result
    calls = message.get("tool_calls")
    if first["finish_reason"] != "tool_calls" or not isinstance(calls, list) or len(calls) != 1:
        result["error"] = failure("expected_one_tool_call")
        return result
    call = calls[0]
    if not isinstance(call, dict):
        result["error"] = failure("invalid_tool_call")
        return result
    function = call.get("function")
    call_id = call.get("id")
    if (call.get("type") != "function" or not isinstance(function, dict)
            or function.get("name") != "synthetic_echo" or not isinstance(call_id, str) or not call_id):
        result["error"] = failure("invalid_tool_identity")
        return result
    try:
        arguments = json.loads(function.get("arguments", ""))
    except (ValueError, TypeError):
        arguments = None
    if arguments != {"text": ECHO_TEXT}:
        result["error"] = failure("invalid_tool_arguments")
        return result
    reasoning = message.get("reasoning_content", "")
    if reasoning is None:
        reasoning = ""
    if not isinstance(reasoning, str):
        result["error"] = failure("invalid_reasoning_content")
        return result
    # The only local execution is here. Retrying the follow-up HTTP request
    # cannot reach this line again, and an additional tool call fails closed.
    echoed = arguments["text"]
    result["tool_exec_count"] = 1
    result["reasoning_content_forwarded"] = True
    result["reasoning_content_chars"] = len(reasoning)
    assistant = {"role": "assistant", "content": message.get("content"),
                 "reasoning_content": reasoning, "tool_calls": calls}
    messages = [*messages, assistant, {"role": "tool", "tool_call_id": call_id, "content": echoed}]
    followup_body = request_body(model, effort, messages)
    followup_body.update({"tools": [ECHO_TOOL], "tool_choice": "none"})
    final, followup = chat(client, followup_body)
    result["followup"] = followup
    if final is None:
        return result
    content = final.get("content")
    result["synthetic_reply"] = content[:1000] if isinstance(content, str) else None
    result["ok"] = bool(isinstance(content, str) and content.strip() == ECHO_CONFIRMATION
                        and not final.get("tool_calls") and followup["finish_reason"] == "stop")
    if not result["ok"]:
        result["error"] = failure("tool_confirmation_failed")
    return result


def blue_png_data_url() -> str:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    pixels = (b"\x00" + b"\x00\x00\xff" * 64) * 64
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def check_image(client: ChatClient, model: str, effort: str) -> dict[str, Any]:
    body = request_body(model, effort, [{"role": "user", "content": [
        {"type": "text", "text": (
            "Inspect the image and name its dominant color. Reply with exactly one uppercase "
            "English color word, with no punctuation or explanation.")},
        {"type": "image_url", "image_url": {"url": blue_png_data_url()}},
    ]}])
    message, metrics = chat(client, body)
    result = {**metrics, "ok": False, "correctness": False,
              "image_size": [64, 64], "expected": "BLUE"}
    if message is None:
        return result
    content = message.get("content")
    result["synthetic_reply"] = content[:1000] if isinstance(content, str) else None
    result["correctness"] = isinstance(content, str) and content.strip() == "BLUE"
    result["ok"] = bool(result["correctness"] and metrics["finish_reason"] == "stop"
                        and not message.get("tool_calls"))
    if not result["ok"]:
        result["error"] = failure("image_acceptance_failed")
    return result


def run_suite(client: ChatClient) -> dict[str, Any]:
    report: dict[str, Any] = {
        "report_version": 1, "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "scope": "fixed_synthetic_fixtures_only", "intelligence_improvement_claimed": False,
        "endpoint": ENDPOINT, "request_timeout_seconds": client.timeout,
        "max_http_retries_per_request": 1, "models": [],
    }
    for model, effort in MODELS:
        text_result = check_text(client, model, effort)
        tool_result = check_tool(client, model, effort)
        image_result = check_image(client, model, effort)
        report["models"].append({
            "model": model, "reasoning_effort": effort,
            "max_tokens": 2048 if effort == "high" else 1024,
            "ok": all(item["ok"] for item in (text_result, tool_result, image_result)),
            "text_chars": text_result["text_chars"],
            "text_latency_seconds": text_result["latency_seconds"],
            "text_usage": text_result["usage"],
            "tool_exec_count": tool_result["tool_exec_count"],
            "image_correctness": image_result["correctness"],
            "text": text_result, "tool": tool_result, "image": image_result,
        })
    report["ok"] = all(item["ok"] for item in report["models"])
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Save the same synthetic JSON report to this server path.")
    args = parser.parse_args(argv)
    try:
        api_key = load_api_key()
        report = run_suite(ChatClient(api_key)) if api_key else {
            "ok": False, "error": failure("missing_api_key"), "models": [],
        }
    except Exception:
        report = {"ok": False, "error": failure("verification_internal_error"), "models": []}
    if args.output:
        try:
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            report["ok"] = False
            report["report_write_error"] = failure("output_write_failed")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
