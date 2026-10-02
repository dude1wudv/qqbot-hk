import ast
import contextlib
import hashlib
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / "tests" / "fixtures" / "hermes-doctor-config.py"
SCRIPT = ROOT / "scripts" / "patch-hermes-doctor.py"
SPEC = importlib.util.spec_from_file_location("patch_hermes_doctor", SCRIPT)
PATCH = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PATCH)


def validate_function(source):
    tree = ast.parse(source)
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_validate_model_config"
    )
    module = ast.Module(body=[function], type_ignores=[])
    namespace = {
        "_VENDOR_SLUG_PROVIDERS": {
            "openrouter", "auto", "ai-gateway", "kilocode", "opencode-zen",
            "huggingface", "lmstudio", "nous", "nvidia", "fireworks", "deepinfra",
        },
        "_known_provider_ids": lambda cfg: (
            cfg["known"], cfg.get("custom", []), cfg.get("resolve_auth"),
            cfg.get("normalize"), cfg.get("resolve_full"),
        ),
        "_provider_has_credentials": lambda provider: True,
        "_fail_and_issue": lambda _title, _detail, issue, issues: issues.append(issue),
        "check_warn": lambda *_args: None,
        "warn_on_error": lambda _message: contextlib.nullcontext(),
    }
    exec(compile(module, str(UPSTREAM), "exec"), namespace)
    return namespace["_validate_model_config"]


def config_modules():
    package = types.ModuleType("hermes_cli")
    package.__path__ = []
    config_module = types.ModuleType("hermes_cli.config")
    config_module.read_user_config_raw = lambda config: config
    doctor_module = types.ModuleType("hermes_cli.doctor")
    doctor_module._DHH = "~/.hermes"
    return {
        "hermes_cli": package,
        "hermes_cli.config": config_module,
        "hermes_cli.doctor": doctor_module,
    }


def config_for(
    provider, model, *, catalog_id=None, catalog_source=None, is_aggregator=None,
    known=None, credentials=True,
):
    cfg = {
        "model": {"provider": provider, "default": model},
        "known": set(known if known is not None else {provider}),
        "has_credentials": credentials,
    }
    if catalog_id is not None:
        cfg["resolve_auth"] = lambda name: name
        cfg["resolve_full"] = lambda *_args: types.SimpleNamespace(
            id=catalog_id, source=catalog_source, is_aggregator=is_aggregator,
        )
    else:
        cfg["resolve_full"] = lambda *_args: None
    return cfg


class HermesDoctorPatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = UPSTREAM.read_text(encoding="utf-8")
        cls.patched = PATCH.patch_source(cls.original)

    def validate(self, source, cfg, *, credentials=True):
        function = validate_function(source)
        function.__globals__["_provider_has_credentials"] = lambda _provider: credentials
        issues = []
        with patch.dict(sys.modules, config_modules()):
            function(cfg, issues)
        return issues

    def test_registered_custom_provider_vendor_model_regression(self):
        cfg = config_for(
            "sub2api_deepseek", "deepseek/deepseek-v3.2",
            catalog_id="custom:sub2api_deepseek",
        )
        baseline_issues = self.validate(self.original, cfg)
        self.assertEqual(1, len(baseline_issues), baseline_issues)
        self.assertIn("vendor-prefixed", baseline_issues[0])

        patched_issues = self.validate(self.patched, cfg)
        self.assertEqual([], patched_issues)

    def test_user_config_provider_alias_allows_vendor_model(self):
        cfg = config_for(
            "sub2api_deepseek", "deepseek/deepseek-v3.2",
            catalog_id="sub2api_deepseek", catalog_source="user-config",
            is_aggregator=False,
        )
        issues = self.validate(self.patched, cfg)
        self.assertEqual([], issues)

    def test_non_user_catalog_alias_does_not_allow_vendor_model(self):
        for source in ("hermes", "models.dev"):
            with self.subTest(source=source):
                cfg = config_for(
                    "sub2api_deepseek", "deepseek/deepseek-v3.2",
                    catalog_id="sub2api_deepseek", catalog_source=source,
                    is_aggregator=False,
                )
                issues = self.validate(self.patched, cfg)
                self.assertEqual(1, len(issues), issues)
                self.assertIn("vendor-prefixed", issues[0])

    def test_native_provider_still_rejects_vendor_slug(self):
        cfg = config_for("anthropic", "other/vendor-model", catalog_id="anthropic")
        issues = self.validate(self.patched, cfg)
        self.assertEqual(1, len(issues), issues)
        self.assertIn("vendor-prefixed", issues[0])

    def test_unregistered_provider_and_missing_credentials_are_rejected(self):
        unknown = config_for("not-registered", "model-name", known={"anthropic"})
        unknown_issues = self.validate(self.patched, unknown)
        self.assertTrue(any("is unknown. Valid providers" in issue for issue in unknown_issues))

        no_credentials = config_for("anthropic", "claude-sonnet", catalog_id="anthropic")
        credential_issues = self.validate(self.patched, no_credentials, credentials=False)
        self.assertTrue(any("No credentials found" in issue for issue in credential_issues))

    def test_patch_integrity_and_idempotence(self):
        with self.assertRaises(PATCH.PatchError):
            PATCH.patch_source(self.original, "0" * 64)

        duplicate_marker = self.patched + "\n" + PATCH.MARKER + "\n"
        with self.assertRaises(PATCH.PatchError):
            PATCH.patch_source(duplicate_marker)

        damaged = self.patched.replace(PATCH.NEW, PATCH.NEW.replace("catalog_provider", "broken"), 1)
        with self.assertRaises(PATCH.PatchError):
            PATCH.patch_source(damaged)

        self.assertEqual(self.patched, PATCH.patch_source(self.patched, "wrong-on-purpose"))
        self.assertEqual(
            PATCH.EXPECTED_SHA256,
            hashlib.sha256(self.original.encode("utf-8")).hexdigest(),
        )



if __name__ == "__main__":
    unittest.main()
