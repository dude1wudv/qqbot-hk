#!/usr/bin/env python3
"""Behavior smoke for Sub2API Chat Completions reasoning fields."""

from agent.transports.chat_completions import ChatCompletionsTransport


BASE_URL = "http://sub2api:8080/v1"
MODEL = "deepseek/deepseek-v4.1-flash"


def build(effort: str | None) -> dict:
    reasoning = {"enabled": True}
    if effort is not None:
        reasoning["effort"] = effort
    return ChatCompletionsTransport().build_kwargs(
        model=MODEL,
        messages=[{"role": "user", "content": "ping"}],
        tools=None,
        base_url=BASE_URL,
        supports_reasoning=True,
        reasoning_config=reasoning,
    )


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def main() -> None:
    default = build(None)
    require(default.get("reasoning_effort") == "medium", "default effort is not medium")
    require("reasoning" not in (default.get("extra_body") or {}), "duplicate reasoning body remains")
    for effort in ("low", "medium", "high", "max"):
        request = build(effort)
        require(
            request.get("reasoning_effort") == effort,
            f"Chat request did not carry {effort} reasoning effort",
        )
    print("HERMES_SUB2API_CHAT=passed DEFAULT=medium LEVELS=low,medium,high,max")


if __name__ == "__main__":
    main()
