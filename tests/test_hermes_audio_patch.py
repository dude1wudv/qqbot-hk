import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "patch-hermes-audio.py"
SPEC = importlib.util.spec_from_file_location("patch_hermes_audio", SCRIPT)
PATCH = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PATCH)


def fixture_source() -> str:
    voice_assignment = (
        "            message_type=self._detect_message_type(image_urls, image_media_types),\n"
    )
    handlers = "".join(
        f"    def handler_{index}(self):\n"
        "        event = dict(\n"
        f"{voice_assignment}"
        "        )\n"
        for index in range(4)
    )
    return (
        "import logging\n\n"
        "logger = logging.getLogger(__name__)\n\n"
        "class Adapter:\n"
        + handlers
        + "    def stt(self, asr_refer_text):\n"
        "        # 1. Use QQ's built-in ASR text if available\n"
        "        if asr_refer_text:\n"
        "            return asr_refer_text\n"
        "        return None\n\n"
        "    def resolve(self, stt_cfg):\n"
        "        if stt_cfg:\n"
        "            api_key = stt_cfg.get(\"apiKey\") or stt_cfg.get(\"api_key\", \"\")\n"
        "            return api_key\n\n"
        "    async def send_voice(\n"
        "            self,\n"
        "            chat_id,\n"
        "            audio_path,\n"
        "            caption=None,\n"
        "            reply_to=None,\n"
        "            **kwargs,\n"
        "    ):\n"
        "        \"\"\"Send a voice message natively.\"\"\"\n"
        "        del kwargs\n"
        "        return await self._send_media(\n"
        "            chat_id, audio_path, MEDIA_TYPE_VOICE, \"voice\", caption, reply_to\n"
        "        )\n"
    )


class HermesAudioPatchTests(unittest.TestCase):
    def test_patch_is_exact_and_idempotent(self):
        source = fixture_source()
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        patched = PATCH.patch_source(source, digest)
        self.assertIn(PATCH.PATCH_MARKER, patched)
        self.assertEqual(4, patched.count("MessageType.VOICE if voice_transcripts"))
        self.assertIn('"QQ_STT_PREFER_BUILTIN", "true"', patched)
        self.assertIn('or _resolve_qq_secret("QQ_STT_API_KEY", "")', patched)
        self.assertIn("reply_to = reply_to or self._last_msg_id.get(chat_id)", patched)
        self.assertEqual(patched, PATCH.patch_source(patched, "wrong-on-purpose"))

    def test_wrong_sha_fails_closed(self):
        with self.assertRaisesRegex(PATCH.PatchError, "SHA mismatch"):
            PATCH.patch_source(fixture_source(), "0" * 64)

    def test_missing_or_duplicate_sentinel_fails_closed(self):
        source = fixture_source().replace(
            "            message_type=self._detect_message_type(image_urls, image_media_types),\n",
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


if __name__ == "__main__":
    unittest.main()
