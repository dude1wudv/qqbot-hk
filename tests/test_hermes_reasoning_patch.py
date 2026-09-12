import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "patch-hermes-reasoning.py"
SPEC = importlib.util.spec_from_file_location("patch_hermes_reasoning", SCRIPT)
PATCH = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PATCH)


def fixture_source() -> str:
    return (
        "def build(reasoning_config, kwargs, model, effective_max_tokens):\n"
        "    if reasoning_config and isinstance(reasoning_config, dict):\n"
        "        kwargs.update(_thinking_kwargs(reasoning_config, model, effective_max_tokens))\n"
        "    return kwargs\n"
    )


class HermesReasoningPatchTests(unittest.TestCase):
    def test_patch_is_exact_and_idempotent(self):
        source = fixture_source()
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        patched = PATCH.patch_source(source, digest)
        self.assertIn(PATCH.PATCH_MARKER, patched)
        self.assertIn('["reasoning_effort"] = effort', patched)
        self.assertIn('or "medium"', patched)
        self.assertEqual(patched, PATCH.patch_source(patched, "wrong-on-purpose"))

    def test_wrong_sha_fails_closed(self):
        with self.assertRaisesRegex(PATCH.PatchError, "SHA mismatch"):
            PATCH.patch_source(fixture_source(), "0" * 64)

    def test_missing_or_duplicate_sentinel_fails_closed(self):
        source = fixture_source().replace(PATCH.OLD, "", 1)
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        with self.assertRaisesRegex(PATCH.PatchError, "reasoning request path"):
            PATCH.patch_source(source, digest)

    def test_patch_file_preserves_an_already_patched_file(self):
        source = fixture_source()
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "anthropic_adapter.py"
            path.write_text(source, encoding="utf-8", newline="\n")
            self.assertTrue(PATCH.patch_file(path, digest))
            first = path.read_bytes()
            self.assertFalse(PATCH.patch_file(path, digest))
            self.assertEqual(first, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
