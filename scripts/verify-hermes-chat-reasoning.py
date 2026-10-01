#!/usr/bin/env python3
"""Behavior smoke for Sub2API Chat Completions reasoning fields."""

from providers import get_provider_profile
from agent.transports.chat_completions import ChatCompletionsTransport


BASE_URL = "http://sub2api:8080/v1"
MODEL = "deepseek/deepseek-v4.1-flash"


def build(
    effort: str | None,
    model: str = MODEL,
    *,
    enabled=True,
    supports_reasoning=True,
    base_url=BASE_URL,
    provider_profile=None,
) -> dict:
    reasoning = {"enabled": enabled}
    if effort is not None:
        reasoning["effort"] = effort
    params = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "tools": None,
        "base_url": base_url,
        "supports_reasoning": supports_reasoning,
        "reasoning_config": reasoning,
    }
    if provider_profile is not None:
        params["provider_profile"] = provider_profile
    return ChatCompletionsTransport().build_kwargs(**params)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def verify_behavior_matrix(route: str, provider_profile=None) -> None:
    for model in (MODEL, "xiaomi/mimo-v2.6-flash"):
        default = build(None, model, provider_profile=provider_profile)
        require(
            default.get("reasoning_effort") == "medium",
            f"{route} {model}: transport fallback effort is not medium",
        )
        levels = ("low", "medium", "high", "max") if model == MODEL else ("low", "medium", "high", "xhigh", "max")
        for effort in levels:
            request = build(effort, model, provider_profile=provider_profile)
            require(request.get("reasoning_effort") == effort, f"{route} {model}: Chat request did not carry {effort}")
            require("reasoning" not in (request.get("extra_body") or {}), f"{route} duplicate reasoning body remains")
        require(
            "reasoning_effort" not in build("xhigh", model, enabled=False, provider_profile=provider_profile),
            f"{route} {model}: disabled thinking forced an effort",
        )
    require(
        build("xhigh", provider_profile=provider_profile).get("reasoning_effort") == "max",
        f"{route} DeepSeek xhigh must be clamped to max",
    )
    require(
        build("xhigh", "xiaomi/mimo-v2.6-flash", supports_reasoning=False,
              provider_profile=provider_profile).get("reasoning_effort") == "xhigh",
        f"{route} MiMo explicit xhigh was lost when model is unlisted",
    )
    other = build(
        "xhigh", "meta/muse-spark-1.3-contributor", supports_reasoning=False,
        provider_profile=provider_profile,
    )
    other_external = build(
        "xhigh", "meta/muse-spark-1.3-contributor", supports_reasoning=False,
        base_url="https://other.example/v1", provider_profile=provider_profile,
    )
    require(
        other.get("reasoning_effort") == other_external.get("reasoning_effort"),
        f"{route} non-target model was changed by the Sub2API policy",
    )
    if provider_profile is None:
        require("reasoning_effort" not in other, f"{route} retired dialogue model was routed as DeepSeek")
    external = build("xhigh", MODEL, base_url="https://other.example/v1", provider_profile=provider_profile)
    require(
        external.get("reasoning_effort") != "max",
        f"{route} non-Sub2API DeepSeek was changed by the Sub2API policy",
    )


def main() -> None:
    verify_behavior_matrix("legacy")
    custom_profile = get_provider_profile("custom:sub2api_deepseek")
    require(custom_profile is not None, "custom:sub2api_deepseek did not resolve to a provider profile")
    verify_behavior_matrix("custom:sub2api_deepseek", custom_profile)
    print("HERMES_SUB2API_CHAT=passed ROUTES=legacy,custom:sub2api_deepseek DEEPSEEK_XHIGH=max MIMO_XHIGH=xhigh")


if __name__ == "__main__":
    main()
