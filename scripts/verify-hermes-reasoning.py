#!/usr/bin/env python3
"""Behavior smoke for Sub2API reasoning fields in the pinned Hermes adapter."""

from agent.anthropic_adapter import build_anthropic_kwargs


BASE_URL = "http://sub2api:8080/v1"
MODEL = "deepseek/deepseek-v4.1-flash"


def build(effort: str | None) -> dict:
    reasoning = {"enabled": True}
    if effort is not None:
        reasoning["effort"] = effort
    return build_anthropic_kwargs(
        model=MODEL,
        messages=[{"role": "user", "content": "ping"}],
        tools=None,
        max_tokens=256,
        reasoning_config=reasoning,
        base_url=BASE_URL,
    )


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def main() -> None:
    default = build(None)
    require(default.get("extra_body") == {"reasoning_effort": "medium"}, "default effort is not medium")
    require("thinking" not in default, "Sub2API request retained incompatible Anthropic thinking")
    for effort in ("low", "medium", "high", "max"):
        request = build(effort)
        require(
            request.get("extra_body") == {"reasoning_effort": effort},
            f"Sub2API request did not carry {effort} reasoning effort",
        )
    print("HERMES_SUB2API_REASONING=passed DEFAULT=medium LEVELS=low,medium,high,max")


if __name__ == "__main__":
    main()
