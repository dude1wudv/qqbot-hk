#!/usr/bin/env python3
"""Fail-closed QQ auto-new recovery patch for pinned Hermes gateway sessions."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys


EXPECTED_ORIGINAL_SHA256 = "488c8f0e3a50381a4e6ad4573c820a23065b97efda6e7ce1020fe3e17f416ac8"
PATCH_MARKER = '# QQBOT_HK_COMPRESSION_RECOVERY_PATCH = "v1"'


class PatchError(RuntimeError):
    pass


HELPER_ANCHOR = """    async def _hm_offer_pairing_code(self, source: SessionSource) -> None:
"""

HELPER = f'''    {PATCH_MARKER}
    async def _hm_auto_new_on_ineffective_compression(
        self, event: "MessageEvent", source: SessionSource
    ) -> bool:
        """Rotate a tripped QQ session before an ordinary message reaches the agent."""
        try:
            # Explicit commands remain user-controlled: manual /compress must still
            # get its forced retry, while /new follows the normal command response.
            if event.get_command():
                return False
            platforms = getattr(getattr(self, "config", None), "platforms", None)
            platform_config = platforms.get(source.platform) if platforms is not None else None
            extra = getattr(platform_config, "extra", None)
            if not isinstance(extra, dict) or extra.get(
                "auto_new_on_compression_ineffective"
            ) is not True:
                return False
            session_db = getattr(self, "_session_db", None)
            async_store = getattr(self, "async_session_store", None)
            if session_db is None or async_store is None:
                return False
            entry = await async_store.get_or_create_session(source)
            ineffective = int(
                await session_db.get_compression_ineffective_count(entry.session_id) or 0
            )
            fallback = int(
                await session_db.get_compression_fallback_streak(entry.session_id) or 0
            )
            if ineffective < 2 and fallback < 2:
                return False
            reset_event = dataclasses.replace(event, text="/new")
            await self._handle_reset_command(reset_event)
            adapter = self._adapter_for_source(source)
            if adapter is not None:
                await adapter.send(
                    source.chat_id,
                    "检测到上下文连续压缩无效，已自动开启新会话并继续处理本条消息。",
                    reply_to=getattr(event, "message_id", None),
                )
            logger.warning(
                "Auto-rotated QQ session after compression breaker tripped "
                "(ineffective=%d fallback=%d)",
                ineffective,
                fallback,
            )
            return True
        except Exception as exc:
            # Recovery is best-effort and must never turn a transient state-read
            # failure into dropped inbound traffic.
            logger.warning("QQ compression auto-new recovery failed: %s", exc)
            return False

'''

CALL_ANCHOR = """        if not getattr(event, "_bot_loop_admitted", False) and not self._admit_bot_message_for_source(source):
            return None
        return event, source, False
"""

CALL_REPLACEMENT = """        if not getattr(event, "_bot_loop_admitted", False) and not self._admit_bot_message_for_source(source):
            return None
        await self._hm_auto_new_on_ineffective_compression(event, source)
        return event, source, False
"""


def sha256_text(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def verify_patched_source(source: str) -> None:
    required = {
        PATCH_MARKER: 1,
        "async def _hm_auto_new_on_ineffective_compression(": 1,
        '"auto_new_on_compression_ineffective"': 1,
        "await session_db.get_compression_ineffective_count": 1,
        "await session_db.get_compression_fallback_streak": 1,
        "await self._handle_reset_command(reset_event)": 1,
        "await self._hm_auto_new_on_ineffective_compression(event, source)": 1,
    }
    for sentinel, expected_count in required.items():
        actual = source.count(sentinel)
        if actual != expected_count:
            raise PatchError(
                f"patched sentinel mismatch for {sentinel!r}: expected {expected_count}, got {actual}"
            )
    if CALL_ANCHOR in source:
        raise PatchError("unpatched inbound call anchor remains")
    try:
        compile(source, "run_inbound.py", "exec")
    except SyntaxError as exc:
        raise PatchError(f"patched inbound module is not valid Python: {exc}") from exc


def patch_source(source: str, expected_sha256: str) -> str:
    if PATCH_MARKER in source:
        verify_patched_source(source)
        return source
    actual_sha256 = sha256_text(source)
    if actual_sha256 != expected_sha256:
        raise PatchError(
            f"pinned gateway inbound SHA mismatch: expected {expected_sha256}, got {actual_sha256}"
        )
    if source.count(HELPER_ANCHOR) != 1:
        raise PatchError("helper insertion anchor mismatch")
    if source.count(CALL_ANCHOR) != 1:
        raise PatchError("inbound call anchor mismatch")
    patched = source.replace(HELPER_ANCHOR, HELPER + HELPER_ANCHOR, 1)
    patched = patched.replace(CALL_ANCHOR, CALL_REPLACEMENT, 1)
    verify_patched_source(patched)
    return patched


def patch_file(path: Path, expected_sha256: str = EXPECTED_ORIGINAL_SHA256) -> bool:
    source = path.read_text(encoding="utf-8")
    patched = patch_source(source, expected_sha256)
    if patched == source:
        return False
    stat = path.stat()
    temporary = path.with_name(path.name + ".qqbot-hk-compression-recovery.tmp")
    temporary.write_text(patched, encoding="utf-8", newline="\n")
    os.chmod(temporary, stat.st_mode)
    os.replace(temporary, path)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default="/opt/hermes/gateway/run_inbound.py")
    args = parser.parse_args()
    try:
        changed = patch_file(Path(args.path))
    except (OSError, UnicodeError, PatchError) as exc:
        print(f"ERROR: Hermes compression recovery patch failed: {exc}", file=sys.stderr)
        return 1
    print(f"HERMES_COMPRESSION_RECOVERY_PATCH={'applied' if changed else 'already-applied'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
