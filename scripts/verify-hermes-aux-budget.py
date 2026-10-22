#!/usr/bin/env python3
"""Offline checks of real Hermes auxiliary request construction; no API calls."""
from types import SimpleNamespace
from agent.auxiliary_client import _build_call_kwargs, _is_max_tokens_rejection


def main():
    for model in ("deepseek/deepseek-v4.1-flash", "deepseek/deepseek-v4.1-flash-fast"):
        for budget in (None, 180, 5000, 8192):
            kwargs = _build_call_kwargs(
                provider="sub2api_deepseek", model=model,
                messages=[{"role": "user", "content": "offline synthetic input"}],
                max_tokens=budget, timeout=300, task="compression",
                base_url="http://sub2api:8080/v1/",
                reasoning_config={"enabled": True, "effort": "low"},
            )
            assert kwargs.get("model") == model, kwargs.keys()
            assert kwargs.get("reasoning_effort") == "low", kwargs.keys()
            assert kwargs.get("max_tokens", kwargs.get("max_completion_tokens")) == budget, kwargs.keys()
            if budget is None:
                assert "max_tokens" not in kwargs and "max_completion_tokens" not in kwargs
    client = SimpleNamespace(base_url="http://sub2api:8080/v1/")
    assert not _is_max_tokens_rejection(ValueError("unsupported_parameter max_tokens"), client)
    print("HERMES_AUX_BUDGET=passed EXPLICIT_CAP=preserved UNBOUNDED_RETRY=disabled")


if __name__ == "__main__":
    main()
