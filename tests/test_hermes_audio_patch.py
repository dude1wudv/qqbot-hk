import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "patch-hermes-audio.py"
SPEC = importlib.util.spec_from_file_location("patch_hermes_audio", SCRIPT)
PATCH = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PATCH)


def fixture_source() -> str:
    return (
        "from __future__ import annotations\n"
        "import logging\n\n"
        "logger = logging.getLogger(__name__)\n\n"
        "class SendResult:\n"
        "    def __init__(self, success, error=\"\"):\n"
        "        self.success = success\n"
        "        self.error = error\n\n"
        "class Adapter:\n"
        "    def __init__(self):\n"
        "        self.config = SimpleNamespace(extra={})\n"
        "        self._last_msg_id = {}\n"
        "        self._log_tag = \"test\"\n\n"
        "    def ingest(self):\n"
        "        event = dict(\n"
        "            message_type=self._detect_message_type(image_urls, image_media_types), raw_message=d,\n"
        "        )\n\n"
        "    def _is_voice_content_type(self, content_type, filename):\n"
        "        return str(content_type).startswith(\"audio/\")\n\n"
        "    def _opt_str(self, value):\n"
        "        return str(value) if value else None\n\n"
        "    async def _stt_voice_attachment(self, attachment, asr_refer, wav_url):\n"
        "        return \"transcribed\"\n\n"
        "    async def _process_attachments(self, attachments):\n"
        "        voice_transcripts = []\n"
        "        attachment_info = []\n"
        "        for att in attachments:\n"
        "            ct = att.get(\"content_type\")\n"
        "            filename = att.get(\"filename\")\n"
        "            if self._is_voice_content_type(ct, filename):\n"
        "                asr_refer, wav_url = (self._opt_str(att.get(k)) for k in (\"asr_refer_text\", \"voice_wav_url\"))\n"
        "                transcript = await self._stt_voice_attachment(att, asr_refer, wav_url)\n"
        "                if transcript:\n"
        "                    voice_transcripts.append(transcript)\n"
        "                    attachment_info.append(transcript)\n"
        "        return {\"voice_transcripts\": voice_transcripts, \"attachment_info\": \"\\n\".join(attachment_info)}\n\n"
        "    def stt(self, asr_refer_text):\n"
        "        if asr_refer_text:\n"
        "            return asr_refer_text\n"
        "        return None\n\n"
        "    def resolve(self, stt_cfg):\n"
        "        if stt_cfg:\n"
        "            api_key = stt_cfg.get(\"apiKey\") or stt_cfg.get(\"api_key\", \"\")\n"
        "            return api_key\n\n"
        "    async def send_voice(self, chat_id, audio_path, caption=None, reply_to=None, **kwargs) -> SendResult:\n"
        "        return await self._send_media(chat_id, audio_path, MEDIA_TYPE_VOICE, \"voice\", caption, reply_to)\n"
    )


class HermesAudioPatchTests(unittest.TestCase):
    def test_patch_is_exact_and_idempotent(self):
        source = fixture_source()
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        patched = PATCH.patch_source(source, digest)
        self.assertEqual(PATCH.PATCH_MARKER, 'QQBOT_HK_AUDIO_PATCH = "v2"')
        self.assertEqual(1, patched.count(PATCH.PATCH_MARKER))
        self.assertEqual(1, patched.count("MessageType.VOICE if voice_transcripts"))
        self.assertIn('"QQ_STT_PREFER_BUILTIN", "true"', patched)
        self.assertIn('or _resolve_qq_secret("QQ_STT_API_KEY", "")', patched)
        self.assertIn('get("voice_input_enabled") is False', patched)
        self.assertIn('get("voice_output_enabled") is False', patched)
        self.assertIn('error="QQ voice output disabled"', patched)
        self.assertIn("reply_to = reply_to or self._last_msg_id.get(chat_id)", patched)
        self.assertEqual(patched, PATCH.patch_source(patched, "wrong-on-purpose"))

    def test_wrong_sha_fails_closed(self):
        with self.assertRaisesRegex(PATCH.PatchError, "SHA mismatch"):
            PATCH.patch_source(fixture_source(), "0" * 64)

    def test_missing_or_duplicate_sentinel_fails_closed(self):
        source = fixture_source().replace(
            "            message_type=self._detect_message_type(image_urls, image_media_types), raw_message=d,\n",
            "",
            1,
        )
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        with self.assertRaisesRegex(PATCH.PatchError, "VOICE event preservation"):
            PATCH.patch_source(source, digest)

    def test_patch_file_preserves_an_already_patched_file(self):
        source = fixture_source()
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "adapter.py"
            path.write_text(source, encoding="utf-8", newline="\n")
            self.assertTrue(PATCH.patch_file(path, digest))
            first = path.read_bytes()
            self.assertFalse(PATCH.patch_file(path, digest))
            self.assertEqual(first, path.read_bytes())

    def _load_adapter(self):
        source = fixture_source()
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        namespace = {"SimpleNamespace": SimpleNamespace}
        patched = PATCH.patch_source(source, digest)
        exec(patched, namespace)
        return namespace["Adapter"]

    def test_disabled_voice_input_drops_attachment_before_stt(self):
        import asyncio

        adapter = self._load_adapter()()
        adapter.config = SimpleNamespace(extra={"voice_input_enabled": False})
        stt_calls = []

        async def forbidden_stt(*args, **kwargs):
            stt_calls.append((args, kwargs))
            return "must-not-run"

        adapter._stt_voice_attachment = forbidden_stt
        processed = asyncio.run(adapter._process_attachments([{
            "content_type": "audio/silk",
            "filename": "voice.silk",
            "asr_refer_text": "must-not-be-used",
            "voice_wav_url": "https://qq.invalid/voice.wav",
        }]))

        self.assertEqual(stt_calls, [])
        self.assertEqual(processed["voice_transcripts"], [])
        self.assertEqual(processed["attachment_info"], "")

    def test_disabled_voice_output_fails_before_media_upload(self):
        import asyncio

        adapter = self._load_adapter()()
        adapter.config = SimpleNamespace(extra={"voice_output_enabled": False})
        media_calls = []

        async def forbidden_media(*args, **kwargs):
            media_calls.append((args, kwargs))
            raise AssertionError("disabled voice output reached media upload")

        adapter._send_media = forbidden_media
        result = asyncio.run(adapter.send_voice("group", "/tmp/voice.mp3", reply_to="message"))

        self.assertFalse(result.success)
        self.assertIn("disabled", result.error.lower())
        self.assertEqual(media_calls, [])

    def test_config_contract_disables_both_qq_voice_directions(self):
        import yaml

        config = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "hermes-config.yaml").read_text(encoding="utf-8"))
        qq_extra = config["platforms"]["qqbot"]["extra"]
        self.assertFalse(qq_extra["voice_input_enabled"])
        self.assertFalse(qq_extra["voice_output_enabled"])
        self.assertFalse(qq_extra["stt"]["enabled"])
        self.assertFalse(config["voice"]["auto_tts"])
        self.assertNotIn("tts", config)
        self.assertNotIn("tts", config["platform_toolsets"]["qqbot"])


if __name__ == "__main__":
    unittest.main()
