#!/usr/bin/env python3
"""Behavior smoke for the pinned QQ voice-input/output deny policy."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

import yaml

from gateway.platforms.base import PlatformConfig
from gateway.platforms.qqbot.adapter import QQAdapter, QQBOT_HK_AUDIO_PATCH


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def load_and_verify_config(path: Path) -> dict[str, Any]:
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    qq = (((config.get("platforms") or {}).get("qqbot") or {}).get("extra") or {})
    stt = qq.get("stt") or {}
    tools = ((config.get("platform_toolsets") or {}).get("qqbot") or [])

    require((config.get("voice") or {}).get("auto_tts") is False, "voice.auto_tts must be false")
    require(qq.get("voice_input_enabled") is False, "QQ voice input must be disabled")
    require(qq.get("voice_output_enabled") is False, "QQ voice output must be disabled")
    require(stt.get("enabled") is False, "QQ STT must be explicitly disabled")
    require(not config.get("tts"), "TTS provider config must be absent")
    require("tts" not in tools, "QQ tts tool must be disabled")
    require("terminal" in tools, "QQ terminal tool is not enabled")
    require("file" in tools, "QQ file tool is not enabled")
    require(not {"code", "computer"}.intersection(tools), "QQ toolset exposes an unintended execution tool")
    return config


async def verify_adapter_behavior(config: dict[str, Any]) -> None:
    qq_extra = (((config.get("platforms") or {}).get("qqbot") or {}).get("extra") or {})
    adapter = QQAdapter(PlatformConfig(enabled=True, extra=qq_extra))

    stt_calls = []

    async def forbidden_stt(*args: Any, **kwargs: Any) -> str:
        stt_calls.append((args, kwargs))
        return "must-not-run"

    adapter._stt_voice_attachment = forbidden_stt
    processed = await adapter._process_attachments([
        {
            "content_type": "audio/silk",
            "url": "https://qq.invalid/voice.silk",
            "filename": "voice.silk",
            "asr_refer_text": "must-not-be-used",
            "voice_wav_url": "https://qq.invalid/voice.wav",
        }
    ])
    require(stt_calls == [], "disabled voice input still invoked STT")
    require(processed.get("voice_transcripts") == [], "disabled voice input produced a transcript")
    require(processed.get("attachment_info") == "", "disabled voice input leaked into attachment text")

    media_calls = []

    async def forbidden_media(*args: Any, **kwargs: Any):
        media_calls.append((args, kwargs))
        raise AssertionError("disabled voice output reached media upload")

    adapter._send_media = forbidden_media
    result = await adapter.send_voice("group", "/tmp/voice.mp3", reply_to="message")
    require(getattr(result, "success", True) is False, "disabled voice output reported success")
    require("disabled" in str(getattr(result, "error", "")).lower(), "voice output denial lacks reason")
    require(media_calls == [], "disabled voice output uploaded media")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/opt/hermes/qqbot-hk/hermes-config.yaml")
    args = parser.parse_args()
    require(QQBOT_HK_AUDIO_PATCH == "v2", "Hermes QQ audio policy patch marker mismatch")
    config = load_and_verify_config(Path(args.config))
    asyncio.run(verify_adapter_behavior(config))
    print("HERMES_QQ_AUDIO_PATCH=passed POLICY=voice-input-disabled,voice-output-disabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
