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
                "/opt/hermes/agent/turn_finalizer.py": "77ca6c9e92cb7e887fd1d0d950da417e5540a865a6866af9ad0c23b784e7f6bf",
                "/opt/hermes/gateway/run_turn_runner.py": "216bdca083b7d2bfae481d07bd0791a76834674b3614fd4ee0aafac5c58f30db",
                "/opt/hermes/gateway/run_turn.py": "5bdee5c82c00e02f664516f9da0af99674e23493ec2a30eff06cb03afc6718a0",
                "/opt/hermes/agent/turn_context.py": "85f857dc3c317366d918516bc8b37a022f1020b34da0c3966a31bd289b2942e1",
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
            'if _qq_silence or self._is_intentional_silence\n'
            'turn_ctx.source.platform == Platform.QQBOT or is_machinery_display_kind(turn_ctx.persist_user_display_kind)"""\n'
        )
        self.assertIs(patcher.patch_source(Path("/opt/hermes/gateway/run_turn.py"), source, "wrong"), source)
    def test_each_patch_declares_exactly_one_marker_in_replacement(self):
        for filename, replacements in patcher.PATCHES.items():
            with self.subTest(filename=filename):
                patched_text = "\n".join(new for _old, new in replacements)
                self.assertEqual(patched_text.count(patcher.PATCH_MARKER), 1)


if __name__ == "__main__":
    unittest.main()
