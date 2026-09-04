#!/usr/bin/env python3
"""Fail-closed, minimal audio patch for the pinned Hermes QQ adapter."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys


EXPECTED_ORIGINAL_SHA256 = "a317fba054a6affb6e95b7483a1c38c9e4ddd62ef44191a87f864136322261cf"
PATCH_MARKER = 'QQBOT_HK_AUDIO_PATCH = "v1"'


class PatchError(RuntimeError):
    pass


PATCHES = (
    (
        "logger = logging.getLogger(__name__)\n",
        "logger = logging.getLogger(__name__)\n\n"
        "# qqbot-hk: audited against the pinned Hermes image digest.\n"
        f"{PATCH_MARKER}\n",
        1,
        "patch marker",
    ),
    (
        "            message_type=self._detect_message_type(image_urls, image_media_types),",
        "            message_type=(\n"
        "                MessageType.VOICE if voice_transcripts\n"
        "                else self._detect_message_type(image_urls, image_media_types)\n"
        "            ),",
        4,
        "VOICE event preservation",
    ),
    (
        "        # 1. Use QQ's built-in ASR text if available\n"
        "        if asr_refer_text:\n",
        "        # 1. Use QQ's built-in ASR only when explicitly preferred.\n"
        "        prefer_builtin = _resolve_qq_secret(\n"
        "            \"QQ_STT_PREFER_BUILTIN\", \"true\"\n"
        "        ).strip().lower() not in {\"0\", \"false\", \"no\", \"off\"}\n"
        "        if asr_refer_text and prefer_builtin:\n",
        1,
        "built-in ASR preference",
    ),
    (
        "            api_key = stt_cfg.get(\"apiKey\") or stt_cfg.get(\"api_key\", \"\")\n",
        "            api_key = (\n"
        "                stt_cfg.get(\"apiKey\")\n"
        "                or stt_cfg.get(\"api_key\", \"\")\n"
        "                or _resolve_qq_secret(\"QQ_STT_API_KEY\", \"\")\n"
        "            )\n",
        1,
        "YAML STT environment-key fallback",
    ),
    (
        "        \"\"\"Send a voice message natively.\"\"\"\n"
        "        del kwargs\n"
        "        return await self._send_media(\n"
        "            chat_id, audio_path, MEDIA_TYPE_VOICE, \"voice\", caption, reply_to\n"
        "        )\n",
        "        \"\"\"Send a voice message natively with a QQ passive-reply anchor.\"\"\"\n"
        "        del kwargs\n"
        "        reply_to = reply_to or self._last_msg_id.get(chat_id)\n"
        "        return await self._send_media(\n"
        "            chat_id, audio_path, MEDIA_TYPE_VOICE, \"voice\", caption, reply_to\n"
        "        )\n",
        1,
        "QQ media reply anchor",
    ),
)


def sha256_text(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def verify_patched_source(source: str) -> None:
    required = {
        PATCH_MARKER: 1,
        "MessageType.VOICE if voice_transcripts": 4,
        '"QQ_STT_PREFER_BUILTIN", "true"': 1,
        'or _resolve_qq_secret("QQ_STT_API_KEY", "")': 1,
        "reply_to = reply_to or self._last_msg_id.get(chat_id)": 1,
    }
    for sentinel, expected_count in required.items():
        actual = source.count(sentinel)
        if actual != expected_count:
            raise PatchError(
                f"patched sentinel mismatch for {sentinel!r}: "
                f"expected {expected_count}, got {actual}"
            )
    for index, (old, _, _, label) in enumerate(PATCHES):
        if index == 0:  # The marker insertion intentionally retains the logger line.
            continue
        if old in source:
            raise PatchError(f"unpatched source remains for {label}")
    try:
        compile(source, "adapter.py", "exec")
    except SyntaxError as exc:
        raise PatchError(f"patched adapter is not valid Python: {exc}") from exc


def patch_source(source: str, expected_sha256: str) -> str:
    if PATCH_MARKER in source:
        verify_patched_source(source)
        return source

    actual_sha256 = sha256_text(source)
    if actual_sha256 != expected_sha256:
        raise PatchError(
            "pinned adapter SHA mismatch: "
            f"expected {expected_sha256}, got {actual_sha256}"
        )

    patched = source
    for old, new, expected_count, label in PATCHES:
        actual_count = patched.count(old)
        if actual_count != expected_count:
            raise PatchError(
                f"source sentinel mismatch for {label}: "
                f"expected {expected_count}, got {actual_count}"
            )
        patched = patched.replace(old, new)
    verify_patched_source(patched)
    return patched


def patch_file(path: Path, expected_sha256: str = EXPECTED_ORIGINAL_SHA256) -> bool:
    source = path.read_text(encoding="utf-8")
    patched = patch_source(source, expected_sha256)
    if patched == source:
        return False

    stat = path.stat()
    temporary = path.with_name(path.name + ".qqbot-hk-audio.tmp")
    temporary.write_text(patched, encoding="utf-8", newline="\n")
    os.chmod(temporary, stat.st_mode)
    os.replace(temporary, path)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "path",
        nargs="?",
        default="/opt/hermes/gateway/platforms/qqbot/adapter.py",
    )
    args = parser.parse_args()
    try:
        changed = patch_file(Path(args.path))
    except (OSError, UnicodeError, PatchError) as exc:
        print(f"ERROR: Hermes QQ audio patch failed: {exc}", file=sys.stderr)
        return 1
    print(f"HERMES_QQ_AUDIO_PATCH={'applied' if changed else 'already-applied'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
