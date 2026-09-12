#!/usr/bin/env python3
"""Fail-closed Sub2API Chat reasoning patch for the pinned Hermes transport."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys


EXPECTED_ORIGINAL_SHA256 = "8606a5d18ea7c0b8c9c3f442799f83df49f76f5bb70b534acb30ce5cb393c47c"
PATCH_MARKER = '# QQBOT_HK_CHAT_REASONING_PATCH = "v1"'
SUB2API_BASE_URL = "http://sub2api:8080/v1"


class PatchError(RuntimeError):
    pass


PATCHES = (
    (
        "    KIMI_K3_EFFORTS, KIMI_K3_OVERRIDES, OPENAI_COMPAT_WIRE_EFFORTS, TOKENHUB_EFFORTS, clamp_effort,\n",
        "    DEEPSEEK_V4_EFFORTS, DEEPSEEK_V4_OVERRIDES, KIMI_K3_EFFORTS, KIMI_K3_OVERRIDES,\n"
        "    OPENAI_COMPAT_WIRE_EFFORTS, TOKENHUB_EFFORTS, clamp_effort,\n",
        "DeepSeek effort imports",
    ),
    (
        "        thinking_off = isinstance(reasoning_config, dict) and reasoning_config.get(\"enabled\") is False\n"
        "        _e = requested_effort(reasoning_config)\n",
        "        thinking_off = isinstance(reasoning_config, dict) and reasoning_config.get(\"enabled\") is False\n"
        "        _e = requested_effort(reasoning_config)\n"
        f"        {PATCH_MARKER}\n"
        f"        is_sub2api_deepseek = (\n"
        f"            str(params.get(\"base_url\") or \"\").strip().rstrip(\"/\") == \"{SUB2API_BASE_URL}\"\n"
        "            and \"deepseek-v4\" in (model or \"\").lower()\n"
        "        )\n"
        "        if is_sub2api_deepseek and not thinking_off:\n"
        "            api_kwargs[\"reasoning_effort\"] = clamp_effort(\n"
        "                _e or \"medium\", DEEPSEEK_V4_EFFORTS, DEEPSEEK_V4_OVERRIDES\n"
        "            )\n",
        "Sub2API top-level reasoning effort",
    ),
    (
        "        if supports_reasoning and not is_lmstudio:\n",
        "        if supports_reasoning and not is_lmstudio and not is_sub2api_deepseek:\n",
        "generic reasoning-body exclusion",
    ),
)


def sha256_text(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def verify_patched_source(source: str) -> None:
    required = {
        PATCH_MARKER: 1,
        "DEEPSEEK_V4_EFFORTS, DEEPSEEK_V4_OVERRIDES": 2,
        'api_kwargs["reasoning_effort"] = clamp_effort(': 1,
        "and not is_sub2api_deepseek": 1,
    }
    for sentinel, expected_count in required.items():
        actual = source.count(sentinel)
        if actual != expected_count:
            raise PatchError(
                f"patched sentinel mismatch for {sentinel!r}: expected {expected_count}, got {actual}"
            )
    for index, (old, _, label) in enumerate(PATCHES):
        if index != 1 and old in source:
            raise PatchError(f"unpatched source remains for {label}")
    try:
        compile(source, "chat_completions.py", "exec")
    except SyntaxError as exc:
        raise PatchError(f"patched transport is not valid Python: {exc}") from exc


def patch_source(source: str, expected_sha256: str) -> str:
    if PATCH_MARKER in source:
        verify_patched_source(source)
        return source
    actual_sha256 = sha256_text(source)
    if actual_sha256 != expected_sha256:
        raise PatchError(
            f"pinned Chat transport SHA mismatch: expected {expected_sha256}, got {actual_sha256}"
        )
    patched = source
    for old, new, label in PATCHES:
        actual_count = patched.count(old)
        if actual_count != 1:
            raise PatchError(f"source sentinel mismatch for {label}: expected 1, got {actual_count}")
        patched = patched.replace(old, new)
    verify_patched_source(patched)
    return patched


def patch_file(path: Path, expected_sha256: str = EXPECTED_ORIGINAL_SHA256) -> bool:
    source = path.read_text(encoding="utf-8")
    patched = patch_source(source, expected_sha256)
    if patched == source:
        return False
    stat = path.stat()
    temporary = path.with_name(path.name + ".qqbot-hk-chat-reasoning.tmp")
    temporary.write_text(patched, encoding="utf-8", newline="\n")
    os.chmod(temporary, stat.st_mode)
    os.replace(temporary, path)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "path", nargs="?", default="/opt/hermes/agent/transports/chat_completions.py"
    )
    args = parser.parse_args()
    try:
        changed = patch_file(Path(args.path))
    except (OSError, UnicodeError, PatchError) as exc:
        print(f"ERROR: Hermes Chat reasoning patch failed: {exc}", file=sys.stderr)
        return 1
    print(f"HERMES_CHAT_REASONING_PATCH={'applied' if changed else 'already-applied'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
