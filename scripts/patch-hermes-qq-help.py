#!/usr/bin/env python3
"""Fail-closed QQ /help override for the pinned Hermes gateway."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys


EXPECTED_ORIGINAL_SHA256 = "cb7a1b99575fb17913a27a3f97039aa98700acad88dba5f62bed314146ae64bd"
PATCH_MARKER = '# QQBOT_HK_HELP_PATCH = "v1"'


class PatchError(RuntimeError):
    pass


OLD = '''    async def _handle_help_command(self, event: MessageEvent) -> str:
        """Handle /help command - list available commands."""
        return self._telegramized_command_reply(event, _execute("help").text)
'''
NEW = f'''    async def _handle_help_command(self, event: MessageEvent) -> str:
        """Handle /help command - list available commands."""
        {PATCH_MARKER}
        if event.source.platform == Platform.QQBOT:
            try:
                from smart_group_qq.commands import help_text as qq_help_text
                return (
                    qq_help_text()
                    + "\\n\\n【Hermes 原生命令（折叠）】\\n"
                    + "发送 /commands 查看完整原生命令列表。"
                )
            except Exception:
                logger.exception("QQ custom help menu unavailable")
        return self._telegramized_command_reply(event, _execute("help").text)
'''


def sha256_text(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def verify_patched_source(source: str) -> None:
    required = {
        PATCH_MARKER: 1,
        "from smart_group_qq.commands import help_text as qq_help_text": 1,
        "【Hermes 原生命令（折叠）】": 1,
        "发送 /commands 查看完整原生命令列表。": 1,
    }
    for sentinel, expected_count in required.items():
        actual = source.count(sentinel)
        if actual != expected_count:
            raise PatchError(
                f"patched sentinel mismatch for {sentinel!r}: expected {expected_count}, got {actual}"
            )
    if OLD in source:
        raise PatchError("unpatched QQ help handler remains")
    try:
        compile(source, "slash_commands.py", "exec")
    except SyntaxError as exc:
        raise PatchError(f"patched slash commands are not valid Python: {exc}") from exc


def patch_source(source: str, expected_sha256: str) -> str:
    if PATCH_MARKER in source:
        verify_patched_source(source)
        return source
    actual_sha256 = sha256_text(source)
    if actual_sha256 != expected_sha256:
        raise PatchError(
            f"pinned slash commands SHA mismatch: expected {expected_sha256}, got {actual_sha256}"
        )
    actual_count = source.count(OLD)
    if actual_count != 1:
        raise PatchError(f"source sentinel mismatch for QQ help handler: expected 1, got {actual_count}")
    patched = source.replace(OLD, NEW)
    verify_patched_source(patched)
    return patched


def patch_file(path: Path, expected_sha256: str = EXPECTED_ORIGINAL_SHA256) -> bool:
    source = path.read_text(encoding="utf-8")
    patched = patch_source(source, expected_sha256)
    if patched == source:
        return False
    stat = path.stat()
    temporary = path.with_name(path.name + ".qqbot-hk-help.tmp")
    temporary.write_text(patched, encoding="utf-8", newline="\n")
    os.chmod(temporary, stat.st_mode)
    os.replace(temporary, path)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default="/opt/hermes/gateway/slash_commands.py")
    args = parser.parse_args()
    try:
        changed = patch_file(Path(args.path))
    except (OSError, UnicodeError, PatchError) as exc:
        print(f"ERROR: Hermes QQ help patch failed: {exc}", file=sys.stderr)
        return 1
    print(f"HERMES_QQ_HELP_PATCH={'applied' if changed else 'already-applied'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
