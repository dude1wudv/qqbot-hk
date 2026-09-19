#!/usr/bin/env python3
"""Fail-closed QQ audio policy patch for the pinned Hermes adapter."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys


EXPECTED_ORIGINAL_SHA256 = "603a00c3c72f7e8e9101056d698d97e5719db44f6d0ab3fc25d9b26c00f07599"
PATCH_MARKER = 'QQBOT_HK_AUDIO_PATCH = "v2"'


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
        "            message_type=self._detect_message_type(image_urls, image_media_types), raw_message=d,",
        "            message_type=(\n"
        "                MessageType.VOICE if voice_transcripts\n"
        "                else self._detect_message_type(image_urls, image_media_types)\n"
        "            ), raw_message=d,",
        1,
        "VOICE event preservation",
    ),
    (
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
        "            if self._is_voice_content_type(ct, filename):\n"
        "                asr_refer, wav_url = (self._opt_str(att.get(k)) for k in (\"asr_refer_text\", \"voice_wav_url\"))\n",
        "            if self._is_voice_content_type(ct, filename):\n"
        "                if (self.config.extra or {}).get(\"voice_input_enabled\") is False:\n"
        "                    logger.info(\"[%s] QQ voice input disabled; attachment ignored\", self._log_tag)\n"
        "                    continue\n"
        "                asr_refer, wav_url = (self._opt_str(att.get(k)) for k in (\"asr_refer_text\", \"voice_wav_url\"))\n",
        1,
        "QQ voice input policy",
    ),
    (
        "    async def send_voice(self, chat_id, audio_path, caption=None, reply_to=None, **kwargs) -> SendResult:\n"
        "        return await self._send_media(chat_id, audio_path, MEDIA_TYPE_VOICE, \"voice\", caption, reply_to)\n",
        "    async def send_voice(self, chat_id, audio_path, caption=None, reply_to=None, **kwargs) -> SendResult:\n"
        "        if (self.config.extra or {}).get(\"voice_output_enabled\") is False:\n"
        "            return SendResult(success=False, error=\"QQ voice output disabled\")\n"
        "        reply_to = reply_to or self._last_msg_id.get(chat_id)\n"
        "        return await self._send_media(chat_id, audio_path, MEDIA_TYPE_VOICE, \"voice\", caption, reply_to)\n",
        1,
        "QQ media reply anchor",
    ),
)


def sha256_text(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def verify_patched_source(source: str) -> None:
    required = {
        PATCH_MARKER: 1,
        "MessageType.VOICE if voice_transcripts": 1,
        '"QQ_STT_PREFER_BUILTIN", "true"': 1,
        'or _resolve_qq_secret("QQ_STT_API_KEY", "")': 1,
        'get("voice_input_enabled") is False': 1,
        'get("voice_output_enabled") is False': 1,
        'error="QQ voice output disabled"': 1,
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
