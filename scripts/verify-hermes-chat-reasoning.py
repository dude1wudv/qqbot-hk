#!/usr/bin/env python3
"""Behavior smoke for Sub2API Chat Completions reasoning fields."""

from agent.transports.chat_completions import ChatCompletionsTransport


BASE_URL = "http://sub2api:8080/v1"
MODEL = "deepseek/deepseek-v4.1-flash"


def build(effort: str | None, model: str = MODEL, *, enabled=True, supports_reasoning=True) -> dict:
    reasoning = {"enabled": enabled}
    if effort is not None:
        reasoning["effort"] = effort
    return ChatCompletionsTransport().build_kwargs(
        model=model,
        messages=[{"role": "user", "content": "ping"}],
        tools=None,
        base_url=BASE_URL,
        supports_reasoning=supports_reasoning,
        reasoning_config=reasoning,
    )


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def main() -> None:
    default = build(None)
    require(default.get("reasoning_effort") == "medium", "transport fallback effort is not medium")
    for effort in ("low", "medium", "high", "max"):
        request = build(effort)
        require(request.get("reasoning_effort") == effort, f"DeepSeek Chat request did not carry {effort}")
        require("reasoning" not in (request.get("extra_body") or {}), "duplicate reasoning body remains")
    require(build("xhigh").get("reasoning_effort") == "max", "DeepSeek xhigh must be clamped to max")
    require("reasoning_effort" not in build("xhigh", enabled=False), "disabled thinking forced an effort")
    other = build("xhigh", "meta/muse-spark-1.3-contributor", supports_reasoning=False)
    require("reasoning_effort" not in other, "retired dialogue model was routed as DeepSeek")
    print("HERMES_SUB2API_CHAT=passed MODEL=deepseek LEVELS=low,medium,high,max XHIGH=max")


if __name__ == "__main__":
    main()
