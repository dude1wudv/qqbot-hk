#!/usr/bin/env python3
"""Bind plugin loading to its manager's credentials as well as its Hermes home."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path


EXPECTED_SHA256 = "8f5761948f135faf5f75c112fcd0a819d8c44eb0652dcbc46517563961764ac3"
MARKER = '# QQBOT_HK_PLUGIN_SCOPE_PATCH = "v1"'
OLD = '''@contextmanager
def _plugin_home_scope(home: Path):
    """Bind discovery and loading to the manager's immutable Hermes home."""
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)
'''
NEW = '''@contextmanager
def _plugin_home_scope(home: Path):
    """Bind plugin registration to the manager's home and matching secret scope."""
    # QQBOT_HK_PLUGIN_SCOPE_PATCH = "v1"
    from agent.secret_scope import (
        build_profile_secret_scope, current_secret_scope, current_secret_scope_home,
        reset_secret_scope, set_secret_scope,
    )
    from hermes_constants import hermes_home_key

    token = set_hermes_home_override(home)
    secret_token = None
    try:
        bound_home = current_secret_scope_home()
        if (current_secret_scope() is None or bound_home is None
                or hermes_home_key(bound_home) != hermes_home_key(home)):
            # Diagnostic imports can discover plugins before _profile_runtime_scope
            # is entered. Resolve this manager's own profile, never an ambient key
            # from another profile. Deadline workers already copy this context.
            secret_token = set_secret_scope(
                build_profile_secret_scope(home), profile_home=str(home),
            )
        yield
    finally:
        if secret_token is not None:
            reset_secret_scope(secret_token)
        reset_hermes_home_override(token)
'''


class PatchError(RuntimeError):
    pass


def patch_source(source: str, expected_sha256: str = EXPECTED_SHA256) -> str:
    if MARKER not in source:
        if hashlib.sha256(source.encode()).hexdigest() != expected_sha256 or source.count(OLD) != 1:
            raise PatchError("pinned plugin loader source or home-scope seam mismatch")
        source = source.replace(OLD, NEW)
    if (source.count(MARKER) != 1 or source.count(NEW) != 1
            or hashlib.sha256(source.replace(NEW, OLD).encode()).hexdigest() != expected_sha256):
        raise PatchError("plugin credential-scope patch is incomplete")
    compile(source, "hermes_cli/plugins_loader.py", "exec")
    return source


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/opt/hermes"))
    args = parser.parse_args()
    path = args.root / "hermes_cli/plugins_loader.py"
    original = path.read_text(encoding="utf-8")
    patched = patch_source(original)
    if patched != original:
        temporary = path.with_name(path.name + ".qqbot-hk.tmp")
        temporary.write_text(patched, encoding="utf-8", newline="\n")
        os.chmod(temporary, path.stat().st_mode)
        os.replace(temporary, path)
    print("HERMES_PLUGIN_SCOPE=patched PROFILE_SECRETS=isolated")


if __name__ == "__main__":
    main()
