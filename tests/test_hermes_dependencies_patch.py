import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "patch-hermes-dependencies.py"
SPEC = importlib.util.spec_from_file_location("patch_hermes_dependencies", SCRIPT)
PATCH = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PATCH)


class HermesDependenciesPatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "hermes"
        self.root.mkdir()
        self.lockfile = Path(self.temp.name) / "safe-package-lock.json"
        self.fixture_lock = b'{"name":"safe-lock","lockfileVersion":3}\n'
        self.source_lock = b'{"name":"original-lock","lockfileVersion":3}\n'
        self.lockfile.write_bytes(self.fixture_lock)
        (self.root / "package-lock.json").write_bytes(self.source_lock)
        self.manifests = {
            "package.json": {
                "overrides": {
                    "brace-expansion": "5.0.9",
                    "undici@^6": "6.28.0",
                    "undici@^7": "7.29.0",
                }
            },
            "web/package.json": {"devDependencies": {"vitest": "4.1.10"}},
            "ui-tui/package.json": {
                "dependencies": {"undici": "6.28.0"},
                "devDependencies": {"vitest": "4.1.10"},
            },
            "tests-js/package.json": {"devDependencies": {"vitest": "4.1.10"}},
        }
        for name, manifest in self.manifests.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        PATCH.ORIGINAL_LOCK_SHA256 = self.sha(self.source_lock)
        PATCH.SAFE_LOCK_SHA256 = self.sha(self.fixture_lock)
        PATCH.MANIFESTS = {
            name: (self.sha((self.root / name).read_bytes()), edits)
            for name, (_original_hash, edits) in PATCH.MANIFESTS.items()
        }

    @staticmethod
    def sha(content):
        return hashlib.sha256(content).hexdigest()

    def snapshot(self):
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in sorted(self.root.rglob("*")) if path.is_file()
        }

    def test_applies_pins_and_matching_lockfile(self):
        original_package = json.loads((self.root / "package.json").read_text(encoding="utf-8"))
        self.assertNotIn("@xmldom/xmldom", original_package["overrides"])
        PATCH.patch_dependencies(self.root, self.lockfile)
        package = json.loads((self.root / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["overrides"], {
            "@xmldom/xmldom": "0.9.12",
            "brace-expansion": "5.0.12",
            "undici@^6": "6.28.1",
            "undici@^7": "7.29.1",
        })
        for name in ("web/package.json", "ui-tui/package.json", "tests-js/package.json"):
            manifest = json.loads((self.root / name).read_text(encoding="utf-8"))
            for section, key, old, new in PATCH.MANIFESTS[name][1]:
                self.assertEqual(manifest[section][key], new)
        generated_lock = (self.root / "package-lock.json").read_bytes()
        self.assertEqual(generated_lock, self.fixture_lock)
        self.assertEqual(self.sha(generated_lock), PATCH.SAFE_LOCK_SHA256)

    def test_any_manifest_or_lock_pollution_is_rejected_before_any_write(self):
        for polluted_name in (*PATCH.MANIFESTS, "package-lock.json", "safe-package-lock.json"):
            with self.subTest(target=polluted_name):
                self.setUp()
                target = (
                    self.root / polluted_name
                    if polluted_name != "safe-package-lock.json"
                    else self.lockfile
                )
                target.write_bytes(target.read_bytes() + b" ")
                before = self.snapshot()
                with self.assertRaises(PATCH.PatchError):
                    PATCH.patch_dependencies(self.root, self.lockfile)
                self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
