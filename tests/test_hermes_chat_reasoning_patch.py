import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "patch-hermes-chat-reasoning.py"
SPEC = importlib.util.spec_from_file_location("patch_hermes_chat_reasoning", SCRIPT)
PATCH = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PATCH)


def fixture_source() -> str:
    return (
        "from agent.reasoning_effort import (\n"
        "    KIMI_K3_EFFORTS, KIMI_K3_OVERRIDES, OPENAI_COMPAT_WIRE_EFFORTS, TOKENHUB_EFFORTS, clamp_effort,\n"
        ")\n"
        "def build(reasoning_config, params, api_kwargs, model, supports_reasoning, is_lmstudio):\n"
        "        thinking_off = isinstance(reasoning_config, dict) and reasoning_config.get(\"enabled\") is False\n"
        "        _e = requested_effort(reasoning_config)\n"
        "        if supports_reasoning and not is_lmstudio:\n"
        "            pass\n"
    )


class HermesChatReasoningPatchTests(unittest.TestCase):
    def test_patch_is_exact_and_idempotent(self):
        source = fixture_source()
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        patched = PATCH.patch_source(source, digest)
        self.assertIn(PATCH.PATCH_MARKER, patched)
        self.assertIn('api_kwargs["reasoning_effort"] = clamp_effort(', patched)
        self.assertIn("and not is_sub2api_deepseek", patched)
        self.assertEqual(patched, PATCH.patch_source(patched, "wrong-on-purpose"))

    def test_wrong_sha_fails_closed(self):
        with self.assertRaisesRegex(PATCH.PatchError, "SHA mismatch"):
            PATCH.patch_source(fixture_source(), "0" * 64)

    def test_missing_sentinel_fails_closed(self):
        source = fixture_source().replace(PATCH.PATCHES[1][0], "", 1)
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        with self.assertRaisesRegex(PATCH.PatchError, "Sub2API top-level reasoning effort"):
            PATCH.patch_source(source, digest)

    def test_patch_file_preserves_an_already_patched_file(self):
        source = fixture_source()
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chat_completions.py"
            path.write_text(source, encoding="utf-8", newline="\n")
            self.assertTrue(PATCH.patch_file(path, digest))
            first = path.read_bytes()
            self.assertFalse(PATCH.patch_file(path, digest))
            self.assertEqual(first, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
