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
    for model in (MODEL, "xiaomi/mimo-v2.6-flash"):
        default = build(None, model)
        require(default.get("reasoning_effort") == "medium", f"{model}: transport fallback effort is not medium")
        levels = ("low", "medium", "high", "max") if model == MODEL else ("low", "medium", "high", "xhigh", "max")
        for effort in levels:
            request = build(effort, model)
            require(request.get("reasoning_effort") == effort, f"{model}: Chat request did not carry {effort}")
            require("reasoning" not in (request.get("extra_body") or {}), "duplicate reasoning body remains")
        require("reasoning_effort" not in build("xhigh", model, enabled=False), f"{model}: disabled thinking forced an effort")
    require(build("xhigh").get("reasoning_effort") == "max", "DeepSeek xhigh must be clamped to max")
    require(build("xhigh", "xiaomi/mimo-v2.6-flash", supports_reasoning=False).get("reasoning_effort") == "xhigh",
            "MiMo explicit xhigh was lost when model is unlisted")
    other = build("xhigh", "meta/muse-spark-1.3-contributor", supports_reasoning=False)
    require("reasoning_effort" not in other, "retired dialogue model was routed as DeepSeek")
    print("HERMES_SUB2API_CHAT=passed MODELS=deepseek,mimo DEEPSEEK_XHIGH=max MIMO_XHIGH=xhigh")


if __name__ == "__main__":
    main()
