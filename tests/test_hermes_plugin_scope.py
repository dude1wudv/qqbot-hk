"""Synthetic regression checks for the pinned Hermes plugin-scope patch."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "tests" / "fixtures" / "plugin-scope-native"
PATCHER_PATH = ROOT / "scripts" / "patch-hermes-plugin-scope.py"
SECRET_NAME = "QQBOT_SYNTHETIC_PROFILE_SECRET"


def _load_patcher():
    spec = importlib.util.spec_from_file_location("qqbot_plugin_scope_patcher", PATCHER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class NativeSandbox:
    """Load the real pinned secret-scope and loader sources with synthetic host shims."""

    _MODULE_NAMES = (
        "hermes_constants", "registration_lifecycle", "utils", "utils.file_signature",
        "agent", "agent.secret_scope", "gateway", "gateway.config_loader",
        "gateway.config", "gateway.run",
        "hermes_cli", "hermes_cli.config", "hermes_cli.env_loader",
        "hermes_cli.plugins_discovery", "hermes_cli.plugins_manifest", "hermes_cli.plugins_state",
    )

    def __init__(self, root: Path):
        self.root = root
        self.saved_modules = {name: sys.modules.get(name) for name in self._MODULE_NAMES}
        self.original_source = (NATIVE / "plugins_loader.py").read_text(encoding="utf-8")
        self.patcher = _load_patcher()
        self.patched_source = self.patcher.patch_source(self.original_source)
        self.loader_path = root / "hermes_cli" / "plugins_loader.py"
        self.loader_path.parent.mkdir(parents=True)
        self.loader_path.write_text(self.patched_source, encoding="utf-8")
        self.process_home = root / "process-home"
        self.home_a = root / "profile-a"
        self.home_b = root / "profile-b"
        self.process_home.mkdir()
        self.home_a.mkdir()
        self.home_b.mkdir()
        (self.home_a / ".env").write_text(f"{SECRET_NAME}=profile-a-secret\n", encoding="utf-8")
        (self.home_b / ".env").write_text(f"{SECRET_NAME}=profile-b-secret\n", encoding="utf-8")
        self._install_shims()
        self.constants = sys.modules["hermes_constants"]
        secret_spec = importlib.util.spec_from_file_location("agent.secret_scope", NATIVE / "secret_scope.py")
        self.secret_scope = importlib.util.module_from_spec(secret_spec)
        assert secret_spec and secret_spec.loader
        sys.modules["agent.secret_scope"] = self.secret_scope
        secret_spec.loader.exec_module(self.secret_scope)
        self.loader = self._load_loader()
        os.environ["HERMES_HOME"] = str(self.process_home)
        self.secret_scope.set_multiplex_active(True)

    def _install_shims(self):
        home_override = ContextVar("synthetic_hermes_home_override", default=None)
        process_home = self.process_home
        constants = types.ModuleType("hermes_constants")
        constants.get_hermes_home_override = home_override.get
        constants.get_hermes_home = lambda: home_override.get() or Path(os.environ["HERMES_HOME"])
        constants.get_routing_process_hermes_home = lambda: process_home
        constants.get_process_hermes_home = lambda: process_home
        constants.set_hermes_home_override = lambda home: home_override.set(Path(home))
        constants.reset_hermes_home_override = lambda token: home_override.reset(token)
        constants.hermes_home_key = lambda home: str(Path(home).resolve()).casefold()
        constants.process_hermes_home_is_pinned = lambda: True
        constants.pin_process_hermes_home = lambda home: None
        sys.modules["hermes_constants"] = constants

        registration = types.ModuleType("registration_lifecycle")
        registration.replacement_coordinator = types.SimpleNamespace(transaction=contextlib.nullcontext)
        sys.modules["registration_lifecycle"] = registration

        utils = types.ModuleType("utils")
        utils.__path__ = []
        signature = types.ModuleType("utils.file_signature")
        signature.file_signature = lambda stat: (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        utils.file_signature = signature.file_signature
        utils.env_var_enabled = lambda name: bool(os.getenv(name))
        sys.modules["utils"] = utils
        sys.modules["utils.file_signature"] = signature

        agent = types.ModuleType("agent")
        agent.__path__ = [str(NATIVE)]
        sys.modules["agent"] = agent
        gateway = types.ModuleType("gateway")
        gateway.__path__ = []
        sys.modules["gateway"] = gateway
        gateway_config = types.ModuleType("gateway.config_loader")
        gateway_config.bridged_allow_all_users = lambda: None
        sys.modules["gateway.config_loader"] = gateway_config

        hermes_cli = types.ModuleType("hermes_cli")
        hermes_cli.__path__ = [str(NATIVE)]
        sys.modules["hermes_cli"] = hermes_cli
        config = types.ModuleType("hermes_cli.config")
        config.load_config_readonly = lambda: {}
        sys.modules["hermes_cli.config"] = config
        env_loader = types.ModuleType("hermes_cli.env_loader")
        env_loader.get_secret_source_values = lambda home: {}
        sys.modules["hermes_cli.env_loader"] = env_loader
        discovery = types.ModuleType("hermes_cli.plugins_discovery")
        discovery.ENTRY_POINTS_GROUP = "hermes_agent.plugins"
        discovery._select_entry_point_group = lambda *args, **kwargs: None
        sys.modules["hermes_cli.plugins_discovery"] = discovery
        manifest = types.ModuleType("hermes_cli.plugins_manifest")
        manifest.PluginManifest = type("PluginManifest", (), {})
        manifest.manifest_key = lambda value: str(value)
        manifest.portable_mcp_server_name = lambda value: str(value)
        manifest.validate_config_schema = lambda *args, **kwargs: []
        sys.modules["hermes_cli.plugins_manifest"] = manifest
        state = types.ModuleType("hermes_cli.plugins_state")
        state._plugin_settings_entry = lambda *args, **kwargs: {}
        sys.modules["hermes_cli.plugins_state"] = state

    def _load_loader(self):
        spec = importlib.util.spec_from_file_location("qqbot_patched_native_plugins_loader", self.loader_path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        return module

    def register_in_deadline(self, home: Path):
        observed = {}

        def register():
            observed["secret"] = self.secret_scope.get_secret(SECRET_NAME)
            observed["home"] = self.secret_scope.current_secret_scope_home()
            observed["worker"] = self.loader.in_plugin_load_worker()
            return observed["secret"]

        with self.loader._plugin_home_scope(home):
            value = self.loader.run_with_load_deadline(
                f"synthetic:{home.name}", types.SimpleNamespace(_abandon_load=lambda: None), register,
            )
        return value, observed

    def close(self):
        try:
            self.secret_scope.set_multiplex_active(False)
        except Exception:
            pass
        for name, previous in self.saved_modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


class HermesPluginScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot-plugin-scope-")
        self._old_hermes_home = os.environ.get("HERMES_HOME")
        self._old_secret = os.environ.get(SECRET_NAME)
        self.sandbox = NativeSandbox(Path(self.temp.name))
        os.environ["HERMES_HOME"] = str(self.sandbox.process_home)
        os.environ[SECRET_NAME] = "process-poison-must-never-win"

    def tearDown(self):
        self.sandbox.close()
        if self._old_hermes_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._old_hermes_home
        if self._old_secret is None:
            os.environ.pop(SECRET_NAME, None)
        else:
            os.environ[SECRET_NAME] = self._old_secret
        self.temp.cleanup()

    def test_pinned_patch_hash_idempotence_and_contamination_rejection(self):
        source = self.sandbox.original_source
        self.assertEqual(hashlib.sha256(source.encode()).hexdigest(), self.sandbox.patcher.EXPECTED_SHA256)
        patched = self.sandbox.patcher.patch_source(source)
        self.assertEqual(patched, self.sandbox.patcher.patch_source(patched))
        polluted = source.replace("yield\n    finally:", "yield\n    # unrecognized mutation\n    finally:", 1)
        with self.assertRaises(self.sandbox.patcher.PatchError):
            self.sandbox.patcher.patch_source(polluted)

    def test_profile_scopes_reach_registration_deadline_worker_and_restore(self):
        for home, expected in ((self.sandbox.home_a, "profile-a-secret"),
                               (self.sandbox.home_b, "profile-b-secret")):
            with self.subTest(profile=home.name):
                value, observed = self.sandbox.register_in_deadline(home)
                self.assertEqual(value, expected)
                self.assertEqual(observed["secret"], expected)
                self.assertTrue(observed["worker"])
                self.assertTrue(observed["home"].endswith(home.name))
                self.assertIsNone(self.sandbox.secret_scope.current_secret_scope())
                self.assertIsNone(self.sandbox.constants.get_hermes_home_override())
        with self.assertRaises(self.sandbox.secret_scope.UnscopedSecretError):
            self.sandbox.secret_scope.get_secret(SECRET_NAME)

    def test_exception_restores_home_and_secret_scope(self):
        original_home = self.sandbox.constants.get_hermes_home()
        with self.assertRaisesRegex(RuntimeError, "synthetic registration failure"):
            with self.sandbox.loader._plugin_home_scope(self.sandbox.home_a):
                self.assertEqual(self.sandbox.secret_scope.get_secret(SECRET_NAME), "profile-a-secret")
                raise RuntimeError("synthetic registration failure")
        self.assertEqual(self.sandbox.constants.get_hermes_home(), original_home)
        self.assertIsNone(self.sandbox.secret_scope.current_secret_scope())

    def test_matching_dynamic_scope_is_reused_and_foreign_scope_is_not_borrowed(self):
        dynamic = {SECRET_NAME: "same-home-dynamic"}
        token = self.sandbox.secret_scope.set_secret_scope(dynamic, profile_home=str(self.sandbox.home_a))
        try:
            with self.sandbox.loader._plugin_home_scope(self.sandbox.home_a):
                self.assertIs(self.sandbox.secret_scope.current_secret_scope(), dynamic)
                value, _ = self.sandbox.register_in_deadline(self.sandbox.home_a)
                self.assertEqual(value, "same-home-dynamic")
            self.assertIs(self.sandbox.secret_scope.current_secret_scope(), dynamic)
        finally:
            self.sandbox.secret_scope.reset_secret_scope(token)

        token = self.sandbox.secret_scope.set_secret_scope(dynamic, profile_home=str(self.sandbox.home_a))
        try:
            value, _ = self.sandbox.register_in_deadline(self.sandbox.home_b)
            self.assertEqual(value, "profile-b-secret")
            self.assertIs(self.sandbox.secret_scope.current_secret_scope(), dynamic)
        finally:
            self.sandbox.secret_scope.reset_secret_scope(token)

    def test_missing_profile_secret_does_not_fall_back_to_either_profile_or_environment(self):
        empty_home = self.sandbox.root / "profile-empty"
        empty_home.mkdir()
        with self.sandbox.loader._plugin_home_scope(empty_home):
            self.assertIsNone(self.sandbox.secret_scope.get_secret(SECRET_NAME))
            self.assertIsNone(self.sandbox.secret_scope.get_secret(SECRET_NAME, default=None))

    def test_parallel_profile_contexts_do_not_cross(self):
        barrier = threading.Barrier(2)

        def load(home):
            with self.sandbox.loader._plugin_home_scope(home):
                barrier.wait(timeout=3)
                return self.sandbox.loader.run_with_load_deadline(
                    f"parallel:{home.name}", types.SimpleNamespace(_abandon_load=lambda: None),
                    lambda: self.sandbox.secret_scope.get_secret(SECRET_NAME),
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(load, self.sandbox.home_a)
            b = pool.submit(load, self.sandbox.home_b)
            self.assertEqual((a.result(timeout=5), b.result(timeout=5)),
                             ("profile-a-secret", "profile-b-secret"))

    def test_synthetic_preflight_seam_uses_native_helper_without_storage_failure(self):
        """Exercise the preflight function with a synthetic eager-import seam, not CLI E2E."""
        self._install_cli_synthetic_modules()
        migration_path = NATIVE / "gateway_migrate.py"
        spec = importlib.util.spec_from_file_location("qqbot_native_gateway_migrate", migration_path)
        migration = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[spec.name] = migration
        try:
            spec.loader.exec_module(migration)
        finally:
            sys.modules.pop(spec.name, None)
        migration._profile_homes = lambda: [("default", self.sandbox.home_a)]
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture()
        logger = logging.getLogger("qqbot.synthetic.cli")
        logger.addHandler(handler)
        try:
            findings = migration.duplicate_credential_findings()
        finally:
            logger.removeHandler(handler)
        self.assertEqual(findings, [])
        self.assertNotIn("storage initialization failed", " ".join(records).lower())
        self.assertEqual(self.sandbox.secret_scope.is_multiplex_active(), True)

    def _install_cli_synthetic_modules(self):
        config = types.ModuleType("gateway.config")
        config.load_gateway_config = lambda: types.SimpleNamespace(platforms={})
        old_config = sys.modules.get("gateway.config")
        sys.modules["gateway.config"] = config
        old_gateway_run = sys.modules.get("gateway.run")

        # The first gateway.run import mirrors eager CLI module initialization: plugin discovery
        # runs before _profile_runtime_scope is entered. It exercises the patched native helper
        # and the real deadline worker using only a synthetic plugin that reads a .env secret.
        class Finder:
            def find_spec(inner_self, fullname, path=None, target=None):
                if fullname != "gateway.run":
                    return None
                return importlib.util.spec_from_loader(fullname, Loader())

        class Loader:
            def create_module(inner_self, spec):
                return None

            def exec_module(inner_self, module):
                try:
                    value, _ = self.sandbox.register_in_deadline(self.sandbox.home_a)
                    if value is None:
                        raise RuntimeError("profile storage secret unavailable")
                except Exception as exc:
                    logging.getLogger("qqbot.synthetic.cli").error("storage initialization failed: %s", exc)

                @contextlib.contextmanager
                def profile_runtime_scope(home):
                    with self.sandbox.loader._plugin_home_scope(home):
                        yield

                module._profile_runtime_scope = profile_runtime_scope
                module.GatewayRunner = type(
                    "GatewayRunner", (), {"_adapter_credential_fingerprint": staticmethod(lambda adapter: None)},
                )

        importer = Finder()
        sys.meta_path.insert(0, importer)
        sys.modules.pop("gateway.run", None)
        def restore_cli_modules():
            with contextlib.suppress(ValueError):
                sys.meta_path.remove(importer)
            if old_config is None:
                sys.modules.pop("gateway.config", None)
            else:
                sys.modules["gateway.config"] = old_config
            if old_gateway_run is None:
                sys.modules.pop("gateway.run", None)
            else:
                sys.modules["gateway.run"] = old_gateway_run
        self.addCleanup(restore_cli_modules)


if __name__ == "__main__":
    unittest.main(verbosity=2)
