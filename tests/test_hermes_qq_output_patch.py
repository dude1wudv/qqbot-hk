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
            {'/opt/hermes/agent/turn_finalizer.py': '4c1e83c6a23bcb7fc10f942306459b54b130975fdce3d76d8fd7f2432698632e',
 '/opt/hermes/gateway/run_turn_runner.py': '04f76f4857a8ba251b014d615b814f3920fef8212ad24e69b28fbbc4fdf2df39',
 '/opt/hermes/gateway/run_turn.py': 'd26c177a4c0edcdd8f901d4b1b7db789c1aa534d72177645afdab32af5509b7d',
 '/opt/hermes/agent/turn_context.py': '09a6159b0301da271f1a6bdcf15d924a05dee8d7d6b9e2f4ed46e406aac0aa4e',
 '/opt/hermes/gateway/run_turn_persistence.py': '31d1d1869b5c9d9d08b8f57bd3ccde4dd9c2844c4a2f74fae991d28842363880'},
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
            'if _qq_silence or self._is_intentional_silence\n'
            'turn_ctx.source.platform == Platform.QQBOT or silence_allowed(turn_ctx.persist_user_display_kind, turn_ctx.reply_expected)"""\n'
        )
        self.assertIs(patcher.patch_source(Path("/opt/hermes/gateway/run_turn.py"), source, "wrong"), source)
    def test_each_patch_declares_exactly_one_marker_in_replacement(self):
        for filename, replacements in patcher.PATCHES.items():
            with self.subTest(filename=filename):
                patched_text = "\n".join(new for _old, new in replacements)
                self.assertEqual(patched_text.count(patcher.PATCH_MARKER), 1)


if __name__ == "__main__":
    unittest.main()
