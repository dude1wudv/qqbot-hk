#!/usr/bin/env python3
"""Fail-closed patch for deterministic Hermes gateway plugin discovery."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys


EXPECTED_ORIGINAL_SHA256 = "0e86f4db0a0b3628d09dd6382974115945136bfbedc7cc7078d062e4aabcab19"
PATCH_MARKER = 'QQBOT_HK_PLUGIN_DISCOVERY_PATCH = "v1"'
_DISCOVERY_BLOCK = (
    "        try:\n"
    "            from hermes_cli.plugins import discover_plugins\n"
    "            discover_plugins()\n"
    "        except Exception:\n"
    "            logger.warning(\n"
    "                \"plugin discovery failed at gateway startup\", exc_info=True,\n"
    "            )\n"
)
_PATCHED_DISCOVERY_BLOCK = _DISCOVERY_BLOCK.replace(
    "            discover_plugins()\n",
    "            # A prior import may have cached an empty discovery pass. The gateway\n"
    "            # process must rescan after its final HOME/config scope is active.\n"
    "            discover_plugins(force=True)\n",
)


class PatchError(RuntimeError):
    pass


def sha256_text(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def verify_patched_source(source: str) -> None:
    required = {
        PATCH_MARKER: 1,
        "discover_plugins(force=True)": 1,
    }
    for sentinel, expected_count in required.items():
        actual = source.count(sentinel)
        if actual != expected_count:
            raise PatchError(
                f"patched sentinel mismatch for {sentinel!r}: "
                f"expected {expected_count}, got {actual}"
            )
    if _DISCOVERY_BLOCK in source:
        raise PatchError("unpatched gateway discovery block remains")
    try:
        compile(source, "run.py", "exec")
    except SyntaxError as exc:
        raise PatchError(f"patched gateway source is not valid Python: {exc}") from exc


def patch_source(source: str, expected_sha256: str) -> str:
    if PATCH_MARKER in source:
        verify_patched_source(source)
        return source

    actual_sha256 = sha256_text(source)
    if actual_sha256 != expected_sha256:
        raise PatchError(
            "pinned gateway SHA mismatch: "
            f"expected {expected_sha256}, got {actual_sha256}"
        )
    if source.count("logger = logging.getLogger(__name__)\n") != 1:
        raise PatchError("gateway logger sentinel mismatch")
    if source.count(_DISCOVERY_BLOCK) != 1:
        raise PatchError("gateway discovery block sentinel mismatch")

    patched = source.replace(
        "logger = logging.getLogger(__name__)\n",
        "logger = logging.getLogger(__name__)\n\n"
        "# qqbot-hk: audited against the pinned Hermes image digest.\n"
        f"{PATCH_MARKER}\n",
        1,
    ).replace(_DISCOVERY_BLOCK, _PATCHED_DISCOVERY_BLOCK, 1)
    verify_patched_source(patched)
    return patched


def patch_file(path: Path, expected_sha256: str = EXPECTED_ORIGINAL_SHA256) -> bool:
    source = path.read_text(encoding="utf-8")
    patched = patch_source(source, expected_sha256)
    if patched == source:
        return False

    stat = path.stat()
    temporary = path.with_name(path.name + ".qqbot-hk-plugins.tmp")
    temporary.write_text(patched, encoding="utf-8", newline="\n")
    os.chmod(temporary, stat.st_mode)
    os.replace(temporary, path)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default="/opt/hermes/gateway/run.py")
    args = parser.parse_args()
    try:
        changed = patch_file(Path(args.path))
    except (OSError, UnicodeError, PatchError) as exc:
        print(f"ERROR: Hermes plugin discovery patch failed: {exc}", file=sys.stderr)
        return 1
    print(f"HERMES_PLUGIN_DISCOVERY_PATCH={'applied' if changed else 'already-applied'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
