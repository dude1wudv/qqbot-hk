import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "patch-hermes-plugin-discovery.py"
SPEC = importlib.util.spec_from_file_location("patch_hermes_plugin_discovery", SCRIPT)
PATCH = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PATCH)


def fixture_source() -> str:
    return (
        "import logging\n\n"
        "logger = logging.getLogger(__name__)\n\n"
        "class GatewayRunner:\n"
        "    async def start(self):\n"
        + PATCH._DISCOVERY_BLOCK
    )


class HermesPluginDiscoveryPatchTests(unittest.TestCase):
    def test_patch_is_exact_and_idempotent(self):
        source = fixture_source()
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        patched = PATCH.patch_source(source, digest)
        self.assertIn(PATCH.PATCH_MARKER, patched)
        self.assertIn("discover_plugins(force=True)", patched)
        self.assertEqual(patched, PATCH.patch_source(patched, "wrong-on-purpose"))

    def test_wrong_sha_fails_closed(self):
        with self.assertRaisesRegex(PATCH.PatchError, "SHA mismatch"):
            PATCH.patch_source(fixture_source(), "0" * 64)

    def test_missing_discovery_sentinel_fails_closed(self):
        source = fixture_source().replace(PATCH._DISCOVERY_BLOCK, "")
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        with self.assertRaisesRegex(PATCH.PatchError, "discovery block sentinel"):
            PATCH.patch_source(source, digest)

    def test_patch_file_preserves_an_already_patched_file(self):
        source = fixture_source()
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.py"
            path.write_text(source, encoding="utf-8", newline="\n")
            self.assertTrue(PATCH.patch_file(path, digest))
            first = path.read_bytes()
            self.assertFalse(PATCH.patch_file(path, digest))
            self.assertEqual(first, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
