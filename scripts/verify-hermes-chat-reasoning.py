#!/usr/bin/env python3
"""Behavior smoke for Sub2API Chat Completions reasoning fields."""

from agent.transports.chat_completions import ChatCompletionsTransport


BASE_URL = "http://sub2api:8080/v1"
MODEL = "meta/muse-spark-1.3-contributor"


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
    for model in (MODEL, "xiaomi/mimo-v2.6-flash", "deepseek/deepseek-v4.1-flash"):
        default = build(None, model)
        require(default.get("reasoning_effort") == "medium", "transport fallback effort is not medium")
        levels = ("low", "medium", "high", "max") if "deepseek" in model else ("low", "medium", "high", "xhigh", "max")
        for effort in levels:
            request = build(effort, model)
            require(request.get("reasoning_effort") == effort, f"{model}: Chat request did not carry {effort}")
            require("reasoning" not in (request.get("extra_body") or {}), "duplicate reasoning body remains")
        disabled = build("xhigh", model, enabled=False)
        require("reasoning_effort" not in disabled, "disabled thinking forced an effort")
        if "deepseek" not in model:
            require(build("xhigh", model, supports_reasoning=False).get("reasoning_effort") == "xhigh",
                    "unlisted custom model lost explicit xhigh")
    print("HERMES_SUB2API_CHAT=passed MODELS=muse,mimo,deepseek LEVELS=low,medium,high,xhigh,max")


if __name__ == "__main__":
    main()
