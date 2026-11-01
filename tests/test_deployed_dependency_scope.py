import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "deployed_dependency_scope", ROOT / "scripts/verify-hermes-dependencies.py"
)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


class DeployedDependencyScopeTests(unittest.TestCase):
    def fixture(self, root, runtime=False):
        manifest = {"dependencies": {"fast-uri": "3.1.9"}} if runtime else {}
        (root / "package.json").write_text(json.dumps(manifest))
        return {"packages": {
            "": manifest,
            "apps/desktop": {"devDependencies": {"fast-uri": "3.1.9"}},
            "node_modules/fast-uri": {"version": "3.1.9"},
        }}

    def test_absent_desktop_only_pin_remains_locked_safe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = self.fixture(root)
            with patch.object(verifier, "PINS", {"fast-uri": {"3.1.9"}}):
                verifier.verify_installed_pins(lock, root)
                lock["packages"]["node_modules/fast-uri"]["version"] = "3.0.0"
                with self.assertRaisesRegex(SystemExit, "Unsafe locked"):
                    verifier.verify_installed_pins(lock, root)

    def test_missing_runtime_dependency_is_never_exempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = self.fixture(root, runtime=True)
            with patch.object(verifier, "PINS", {"fast-uri": {"3.1.9"}}):
                with self.assertRaisesRegex(SystemExit, "missing from installed"):
                    verifier.verify_installed_pins(lock, root)

    def test_present_desktop_requires_its_pinned_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = self.fixture(root)
            (root / "apps/desktop").mkdir(parents=True)
            (root / "apps/desktop/package.json").write_text("{}")
            with patch.object(verifier, "PINS", {"fast-uri": {"3.1.9"}}):
                with self.assertRaisesRegex(SystemExit, "missing from installed"):
                    verifier.verify_installed_pins(lock, root)


if __name__ == "__main__":
    unittest.main()
