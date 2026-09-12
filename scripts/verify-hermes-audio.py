#!/usr/bin/env python3
"""Behavior smoke for the pinned, patched Hermes QQ audio integration."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from typing import Any

import yaml

from gateway.platforms.base import MessageType, PlatformConfig
from gateway.platforms.qqbot.adapter import QQAdapter, QQBOT_HK_AUDIO_PATCH
from gateway.platforms.qqbot.constants import MEDIA_TYPE_VOICE, MSG_TYPE_MEDIA


EXPECTED = {
    "stt_base_url": "http://sub2api:8080/v1",
    "stt_model": "qwen-audio-3.0-asr-flash",
    "tts_base_url": "http://sub2api:8080/v1",
    "tts_model": "qwen-audio-3.0-tts-plus",
    "tts_voice": "longanhuan_v3.6",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def load_and_verify_config(path: Path) -> dict[str, Any]:
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    qq = (((config.get("platforms") or {}).get("qqbot") or {}).get("extra") or {})
    stt = qq.get("stt") or {}
    tts = config.get("tts") or {}
    openai_tts = tts.get("openai") or {}
    tools = ((config.get("platform_toolsets") or {}).get("qqbot") or [])

    require((config.get("voice") or {}).get("auto_tts") is False, "voice.auto_tts mismatch")
    require(stt.get("provider") == "openai", "QQ STT provider mismatch")
    require(stt.get("baseUrl") == EXPECTED["stt_base_url"], "QQ STT baseUrl mismatch")
    require(stt.get("model") == EXPECTED["stt_model"], "QQ STT model mismatch")
    require(not stt.get("apiKey") and not stt.get("api_key"), "QQ STT secret must not be in YAML")
    require(tts.get("provider") == "openai", "TTS provider mismatch")
    require(openai_tts.get("base_url") == EXPECTED["tts_base_url"], "TTS base_url mismatch")
    require(openai_tts.get("model") == EXPECTED["tts_model"], "TTS model mismatch")
    require(openai_tts.get("voice") == EXPECTED["tts_voice"], "TTS voice mismatch")
    require("tts" in tools, "QQ tts tool is not enabled")
    require("terminal" in tools, "QQ terminal tool is not enabled")
    require("file" in tools, "QQ file tool is not enabled")
    forbidden = {"code", "computer"}
    require(not forbidden.intersection(tools), "QQ toolset exposes an unintended execution tool")
    return config


async def verify_adapter_behavior(config: dict[str, Any]) -> None:
    qq_extra = (((config.get("platforms") or {}).get("qqbot") or {}).get("extra") or {})
    old_env = {name: os.environ.get(name) for name in ("QQ_STT_API_KEY", "QQ_STT_PREFER_BUILTIN")}
    os.environ["QQ_STT_API_KEY"] = "smoke-only-placeholder"
    os.environ["QQ_STT_PREFER_BUILTIN"] = "false"
    try:
        adapter = QQAdapter(PlatformConfig(enabled=True, extra=qq_extra))
        resolved = adapter._resolve_stt_config()
        require(bool(resolved), "QQ STT config did not resolve")
        require(resolved["base_url"] == EXPECTED["stt_base_url"], "runtime STT base URL mismatch")
        require(resolved["model"] == EXPECTED["stt_model"], "runtime STT model mismatch")
        require(resolved["api_key"] == "smoke-only-placeholder", "runtime STT env-key fallback failed")

        class FakeResponse:
            content = b"RIFF" + b"\x00" * 40
            headers = {"content-type": "audio/wav"}

            def raise_for_status(self) -> None:
                return None

        class FakeHTTP:
            async def get(self, *args: Any, **kwargs: Any) -> FakeResponse:
                return FakeResponse()

        adapter._http_client = FakeHTTP()

        async def fake_call_stt(path: str) -> str:
            require(Path(path).suffix == ".wav", "external STT did not receive WAV")
            return "external-ok"

        adapter._call_stt = fake_call_stt
        import tools.url_safety as url_safety

        original_safe_url = url_safety.is_safe_url
        url_safety.is_safe_url = lambda _url: True
        try:
            transcript = await adapter._stt_voice_attachment(
                "https://qq.invalid/original.silk",
                "audio/silk",
                "voice.silk",
                asr_refer_text="builtin-must-not-win",
                voice_wav_url="https://qq.invalid/voice.wav",
            )
        finally:
            url_safety.is_safe_url = original_safe_url
        require(transcript == "external-ok", "QQ built-in ASR was not bypassed")

        async def fake_attachments(_attachments: Any) -> dict[str, Any]:
            return {
                "image_urls": [],
                "image_media_types": [],
                "voice_transcripts": ["[Voice] external-ok"],
                "attachment_info": "",
            }

        async def fake_quote(_message: Any) -> dict[str, Any]:
            return {"quote_block": "", "image_urls": [], "image_media_types": []}

        captured_events = []

        async def capture_event(event: Any) -> None:
            captured_events.append(event)

        adapter._process_attachments = fake_attachments
        adapter._process_quoted_context = fake_quote
        adapter.handle_message = capture_event
        await adapter._handle_c2c_message({}, "c2c-message", "", {"user_openid": "user-1"}, "")
        adapter._group_policy = "open"
        await adapter._handle_group_message(
            {"group_openid": "group-1"},
            "group-message",
            "",
            {"member_openid": "user-2"},
            "",
        )
        require(len(captured_events) == 2, "QQ voice event smoke did not capture both handlers")
        require(
            all(event.message_type == MessageType.VOICE for event in captured_events),
            "QQ voice event type was downgraded",
        )

        media_calls = []
        upload_calls = []

        class FakeWS:
            closed = False

        async def fake_upload_local_file(*args: Any, **kwargs: Any) -> Any:
            upload_calls.append((args, kwargs))
            return {"file_info": "smoke-file-token"}

        async def fake_post_message(path: str, body: dict[str, Any]) -> Any:
            media_calls.append((path, body))
            return {"id": "outbound-message"}

        adapter._running = True
        adapter._ws = FakeWS()
        adapter._chat_type_map["group-1"] = "group"
        adapter._upload_local_file = fake_upload_local_file
        adapter._post_message = fake_post_message
        adapter._last_msg_id["group-1"] = "inbound-anchor"
        await adapter.send_voice("group-1", "/tmp/voice.mp3")
        await adapter.send_voice("group-1", "/tmp/voice.mp3", reply_to="explicit-anchor")
        require(upload_calls[0][0][3] == MEDIA_TYPE_VOICE == 3, "MP3 did not use QQ native voice type")
        require(media_calls[0][0] == "/v2/groups/group-1/messages", "QQ group media path mismatch")
        require(media_calls[0][1].get("msg_type") == MSG_TYPE_MEDIA, "MP3 did not use QQ media message type")
        require(media_calls[0][1].get("media", {}).get("file_info") == "smoke-file-token", "MP3 media token missing")
        require(media_calls[0][1].get("msg_id") == "inbound-anchor", "QQ passive media anchor missing")
        require(media_calls[1][1].get("msg_id") == "explicit-anchor", "explicit QQ reply anchor lost")
    finally:
        for name, value in old_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default="/opt/hermes/qqbot-hk/hermes-config.yaml",
    )
    args = parser.parse_args()
    require(QQBOT_HK_AUDIO_PATCH == "v1", "Hermes QQ audio patch marker mismatch")
    config = load_and_verify_config(Path(args.config))
    asyncio.run(verify_adapter_behavior(config))
    print("HERMES_QQ_AUDIO_PATCH=passed")
    print("QQ_STT_CONFIG=passed")
    print("QQ_VOICE_EVENT=passed")
    print("QQ_NATIVE_MP3_REPLY_ANCHOR=passed")
    print("QQ_AUTO_TTS_TOOLSET=passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
