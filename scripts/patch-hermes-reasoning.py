#!/usr/bin/env python3
"""Fail-closed Sub2API reasoning patch for the pinned Hermes adapter."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys


EXPECTED_ORIGINAL_SHA256 = "9d7bd2e7e639fb55a5aaf176dfe6d21b51ffbced1bf9a68adf9cc6bc835214d4"
PATCH_MARKER = '# QQBOT_HK_REASONING_PATCH = "v1"'
SUB2API_BASE_URL = "http://sub2api:8080/v1"


class PatchError(RuntimeError):
    pass


OLD = """    if reasoning_config and isinstance(reasoning_config, dict):
        kwargs.update(_thinking_kwargs(reasoning_config, model, effective_max_tokens))
"""
NEW = f"""    if reasoning_config and isinstance(reasoning_config, dict):
        {PATCH_MARKER}
        if _normalize_base_url_text(base_url).rstrip(\"/\") == \"{SUB2API_BASE_URL}\":
            effort = str(reasoning_config.get(\"effort\") or \"medium\").strip().lower()
            kwargs.setdefault(\"extra_body\", {{}})[\"reasoning_effort\"] = effort
        else:
            kwargs.update(_thinking_kwargs(reasoning_config, model, effective_max_tokens))
"""


def sha256_text(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def verify_patched_source(source: str) -> None:
    required = {
        PATCH_MARKER: 1,
        f'== "{SUB2API_BASE_URL}"': 1,
        '["reasoning_effort"] = effort': 1,
    }
    for sentinel, expected_count in required.items():
        actual = source.count(sentinel)
        if actual != expected_count:
            raise PatchError(
                f"patched sentinel mismatch for {sentinel!r}: "
                f"expected {expected_count}, got {actual}"
            )
    if OLD in source:
        raise PatchError("unpatched reasoning request path remains")
    try:
        compile(source, "anthropic_adapter.py", "exec")
    except SyntaxError as exc:
        raise PatchError(f"patched adapter is not valid Python: {exc}") from exc


def patch_source(source: str, expected_sha256: str) -> str:
    if PATCH_MARKER in source:
        verify_patched_source(source)
        return source

    actual_sha256 = sha256_text(source)
    if actual_sha256 != expected_sha256:
        raise PatchError(
            "pinned Anthropic adapter SHA mismatch: "
            f"expected {expected_sha256}, got {actual_sha256}"
        )
    actual_count = source.count(OLD)
    if actual_count != 1:
        raise PatchError(
            "source sentinel mismatch for reasoning request path: "
            f"expected 1, got {actual_count}"
        )
    patched = source.replace(OLD, NEW)
    verify_patched_source(patched)
    return patched


def patch_file(path: Path, expected_sha256: str = EXPECTED_ORIGINAL_SHA256) -> bool:
    source = path.read_text(encoding="utf-8")
    patched = patch_source(source, expected_sha256)
    if patched == source:
        return False

    stat = path.stat()
    temporary = path.with_name(path.name + ".qqbot-hk-reasoning.tmp")
    temporary.write_text(patched, encoding="utf-8", newline="\n")
    os.chmod(temporary, stat.st_mode)
    os.replace(temporary, path)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "path",
        nargs="?",
        default="/opt/hermes/agent/anthropic_adapter.py",
    )
    args = parser.parse_args()
    try:
        changed = patch_file(Path(args.path))
    except (OSError, UnicodeError, PatchError) as exc:
        print(f"ERROR: Hermes reasoning patch failed: {exc}", file=sys.stderr)
        return 1
    print(f"HERMES_REASONING_PATCH={'applied' if changed else 'already-applied'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
