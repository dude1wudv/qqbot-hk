import hashlib
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "patch-hermes-qq-output.py"
spec = importlib.util.spec_from_file_location("patch_hermes_qq_output", SCRIPT)
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)


class HermesQQOutputPatchTests(unittest.TestCase):
    def test_targets_are_the_verified_fixed_image_sha256_values(self):
        self.assertEqual(
            patcher.TARGETS,
            {
                "/opt/hermes/agent/turn_finalizer.py": "3e9658908b7421d3ee2da6516f2ac511541f7e134fcacaace8c807c996747fb1",
                "/opt/hermes/gateway/run_turn_runner.py": "450318a4123b71fb79ac42b10c0558364341ff26a36f98f0f8c39fe76866bf90",
                "/opt/hermes/gateway/run_turn.py": "d8456aa1246fab33dbfa86e60f46a6d1992731dbda6e7be7e223c3dda5c086d0",
            },
        )

    def test_hash_helper_matches_standard_sha256(self):
        self.assertEqual(patcher._sha("Hermes QQ"), hashlib.sha256(b"Hermes QQ").hexdigest())

    def test_source_mismatch_fails_closed_before_replacement(self):
        for path, expected in patcher.TARGETS.items():
            with self.subTest(path=path):
                with self.assertRaises(patcher.PatchError):
                    patcher.patch_source(Path(path), "def changed():\n    return 1\n", expected)

    def test_already_marked_source_is_idempotent_and_compiles(self):
        source = (
            '"""# QQBOT_HK_OUTPUT_PATCH = "v1"\n'
            '        user_message=original_user_message,\n'
            'source.platform == Platform.QQBOT and is_intentional_silence_response(response)\n'
            'if source.platform == Platform.QQBOT:\n                return None\n'
            'if _qq_silence or self._is_intentional_silence"""\n'
        )
        self.assertIs(patcher.patch_source(Path("/opt/hermes/gateway/run_turn.py"), source, "wrong"), source)
    def test_each_patch_declares_exactly_one_marker_in_replacement(self):
        for filename, replacements in patcher.PATCHES.items():
            with self.subTest(filename=filename):
                patched_text = "\n".join(new for _old, new in replacements)
                self.assertEqual(patched_text.count(patcher.PATCH_MARKER), 1)


if __name__ == "__main__":
    unittest.main()
