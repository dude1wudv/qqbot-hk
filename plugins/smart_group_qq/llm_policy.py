"""One request policy for QQ business calls, with cancellation propagated to I/O."""
from __future__ import annotations

import asyncio
import inspect

REQUEST_TIMEOUT_SECONDS = 300


async def call(awaitable):
    """Bound one model request, not an entire multi-chunk maintenance job."""
    return await asyncio.wait_for(awaitable, timeout=REQUEST_TIMEOUT_SECONDS)


async def complete(llm, **kwargs):
    """Use native PluginLlm routing; the compression/vision routes default to low."""
    operation = llm if callable(llm) else getattr(llm, "acomplete_structured", None)
    if not callable(operation):
        raise RuntimeError("structured model interface unavailable")
    kwargs.pop("max_tokens", None)
    kwargs.pop("max_completion_tokens", None)
    kwargs.pop("max_output_tokens", None)
    kwargs["timeout"] = REQUEST_TIMEOUT_SECONDS
    kwargs.setdefault("task", "compression")
    result = operation(**kwargs)
    if not inspect.isawaitable(result):
        return result
    return await call(result)
