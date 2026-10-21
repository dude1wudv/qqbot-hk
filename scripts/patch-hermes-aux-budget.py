#!/usr/bin/env python3
"""Preserve explicit auxiliary budgets on the pinned local Sub2API route."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path

EXPECTED_SHA256 = "e83807074f3577ac3980f43bfd578ffcc95efb591e38a9427d0e7d33ec7d8e4a"
MARKER = '# QQBOT_HK_AUX_BUDGET_PATCH = "v1"'
HELPER = '''def _qqbot_sub2api_auxiliary_url(value) -> bool:
    return str(value or "").strip().rstrip("/") == "http://sub2api:8080/v1"


'''
PATCHES = (
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
