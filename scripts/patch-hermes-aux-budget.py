#!/usr/bin/env python3
"""Preserve auxiliary budgets and DeepSeek reasoning on the pinned Sub2API route."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path

EXPECTED_SHA256 = "e83807074f3577ac3980f43bfd578ffcc95efb591e38a9427d0e7d33ec7d8e4a"
LEGACY_MARKER = '# QQBOT_HK_AUX_BUDGET_PATCH = "v1"'
MARKER = '# QQBOT_HK_AUX_BUDGET_PATCH = "v2"'
HELPER = '''def _qqbot_sub2api_auxiliary_url(value) -> bool:
    return str(value or "").strip().rstrip("/") == "http://sub2api:8080/v1"


'''
BUDGET_PATCHES = (
    ('def _forwards_max_tokens(', HELPER + 'def _forwards_max_tokens('),
    ('        _is_anthropic_compat_endpoint(provider, effective_base)\n',
     '        _qqbot_sub2api_auxiliary_url(effective_base)\n'
     '        or _is_anthropic_compat_endpoint(provider, effective_base)\n'),
    ('def _is_max_tokens_rejection(exc: Exception, client: Any) -> bool:\n',
     'def _is_max_tokens_rejection(exc: Exception, client: Any) -> bool:\n'
     '    # Never retry a bounded Sub2API task without its output budget.\n'
     '    if _qqbot_sub2api_auxiliary_url(getattr(client, "base_url", None)):\n'
     '        return False\n'),
)
REASONING_HELPER = '''def _qqbot_sub2api_auxiliary_reasoning(kwargs, model, effective_base, reasoning_config):
    if not _qqbot_sub2api_auxiliary_url(effective_base) or str(model or "").lower() not in {
        "deepseek/deepseek-v4.1-flash", "deepseek/deepseek-v4.1-flash-fast",
    }:
        return
    from agent.reasoning_effort import DEEPSEEK_V4_EFFORTS, DEEPSEEK_V4_OVERRIDES, clamp_effort
    config = reasoning_config if isinstance(reasoning_config, dict) else {}
    if config.get("enabled") is False:
        kwargs.pop("reasoning_effort", None)
    else:
        kwargs["reasoning_effort"] = clamp_effort(
            config.get("effort") or "low", DEEPSEEK_V4_EFFORTS, DEEPSEEK_V4_OVERRIDES)
    extra = kwargs.get("extra_body")
    if isinstance(extra, dict):
        extra.pop("reasoning", None)
        if not extra:
            kwargs.pop("extra_body", None)


'''
PATCHES = BUDGET_PATCHES + (
    ('def _build_call_kwargs(', REASONING_HELPER + 'def _build_call_kwargs('),
    ('    reasoning_config = clamp_reasoning_config(\n'
     '        known_reasoning_floor(reasoning_config, provider_norm, effective_base, model, task))\n',
     '    # Keep explicit/task effort before generic floors/clamps alter the DeepSeek wire value.\n'
     '    _qq_reasoning_config = dict(reasoning_config) if isinstance(reasoning_config, dict) else None\n'
     '    reasoning_config = clamp_reasoning_config(\n'
     '        known_reasoning_floor(reasoning_config, provider_norm, effective_base, model, task))\n'),
    ('    return merge_session_affinity_headers(kwargs, provider, base_url, _runtime_main_value("session_id") or None)\n',
     '    _qqbot_sub2api_auxiliary_reasoning(kwargs, model, effective_base, _qq_reasoning_config)\n'
     '    return merge_session_affinity_headers(kwargs, provider, base_url, _runtime_main_value("session_id") or None)\n'),
)


class PatchError(RuntimeError):
    pass


def verify_source(source: str) -> None:
    if source.count(MARKER) != 1:
        raise PatchError("auxiliary budget marker mismatch")
    for _, new in PATCHES:
        if source.count(new) != 1:
            raise PatchError("auxiliary budget sentinel mismatch")
    compile(source, "auxiliary_client.py", "exec")


def patch_source(source: str, expected_sha256: str = EXPECTED_SHA256) -> str:
    if MARKER in source:
        verify_source(source)
        return source
    if LEGACY_MARKER in source:
        if source.count(LEGACY_MARKER) != 1 or any(source.count(new) != 1 for _, new in BUDGET_PATCHES):
            raise PatchError("legacy auxiliary budget sentinel mismatch")
        for old, new in reversed(BUDGET_PATCHES):
            source = source.replace(new, old, 1)
        source = source.replace("\n" + LEGACY_MARKER + "\n", "", 1)
    if hashlib.sha256(source.encode()).hexdigest() != expected_sha256:
        raise PatchError("pinned auxiliary client SHA mismatch")
    for old, new in PATCHES:
        if source.count(old) != 1:
            raise PatchError("pinned auxiliary client sentinel mismatch")
        source = source.replace(old, new, 1)
    source += "\n" + MARKER + "\n"
    verify_source(source)
    return source


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default="/opt/hermes/agent/auxiliary_client.py")
    path = Path(parser.parse_args().path)
    source = path.read_text(encoding="utf-8")
    patched = patch_source(source)
    if source != patched:
        temporary = path.with_name(path.name + ".qqbot-budget.tmp")
        temporary.write_text(patched, encoding="utf-8", newline="\n")
        os.chmod(temporary, path.stat().st_mode)
        os.replace(temporary, path)
    print("HERMES_AUX_BUDGET_PATCH=verified")


if __name__ == "__main__":
    main()
