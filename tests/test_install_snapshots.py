import os
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from types import ModuleType
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts/install-server.sh").read_text(encoding="utf-8")
BLOCKS = re.findall(r"<<'PY'\n(.*?)\nPY", SCRIPT, re.DOTALL)
ENV_BLOCK = next(block for block in BLOCKS if "qq_source, deepseek_key_source" in block)
SNAPSHOT_BLOCK = next(block for block in BLOCKS if "data_dir, backup_dir =" in block)
MIGRATION_BLOCK = next(block for block in BLOCKS if "from smart_group_qq.store import Store" in block)


class InstallSnapshotTests(unittest.TestCase):
    def test_empty_or_changed_member_secret_does_not_publish_runtime_env(self):
        for secret, existing in (("", None), ("   ", None), ("new-synthetic-secret", "old-synthetic-secret")):
            with self.subTest(secret=secret), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                qq = root / "qq.env"
                qq.write_text(
                    f"QQ_APP_ID=synthetic-app\nQQ_CLIENT_SECRET={secret}\nQQ_SCHEDULE_GROUPS=synthetic-group\n",
                    encoding="utf-8",
                )
                key = root / "model-key"
                key.write_text("offline-synthetic-api-key", encoding="utf-8")
                runtime = root / "runtime.env"
                if existing is not None:
                    runtime.write_text(f"QQ_CLIENT_SECRET={existing}\n", encoding="utf-8")
                output = root / "new.env"
                result = subprocess.run(
                    [sys.executable, "-c", ENV_BLOCK, str(qq), str(key),
                     str(ROOT / "config/hermes-config.yaml"), str(output), str(root / "config.yaml"), str(runtime)],
                    capture_output=True, text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(output.exists())
                self.assertNotIn("synthetic-secret", result.stdout + result.stderr)

    def test_paired_snapshot_preserves_schema4_plugin_and_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data, backup = root / "data", root / "backup"
            backup.mkdir()
            for relative in ("state.db", "plugin-data/smart_group_qq/data.db"):
                path = data / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(path)) as db, db:
                    db.executescript("CREATE TABLE retained(value TEXT); INSERT INTO retained VALUES ('synthetic-retained'); PRAGMA user_version=4;")
            result = subprocess.run(
                [sys.executable, "-c", SNAPSHOT_BLOCK, str(data), str(backup), "synthetic-stamp"],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in ("state.db", "plugin-data.db"):
                path = backup / f"{name}.synthetic-stamp"
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
                    self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(db.execute("SELECT value FROM retained").fetchone()[0], "synthetic-retained")
                if os.name != "nt":
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_build_and_paired_backup_precede_live_config_and_plugin_replacement(self):
        build = SCRIPT.index('build "$service"')
        stop = SCRIPT.index('stop "$service"')
        snapshot = SCRIPT.index('python3 - "$data_dir" "$session_backup_dir"')
        config_backup = SCRIPT.index('config_backup_dir="$session_backup_dir/config.')
        replacement = SCRIPT.index('mv -f "$data_dir/.config.yaml.new"')
        plugin_replacement = SCRIPT.index('mv "$stage_dir/smart_group_qq"')
        start = SCRIPT.index('up -d --force-recreate --no-deps "$service"')
        self.assertEqual(sorted((build, stop, snapshot, config_backup, replacement, plugin_replacement, start)),
                         [build, stop, snapshot, config_backup, replacement, plugin_replacement, start])

    def test_explicit_migration_loads_runtime_env_before_fail_closed_store(self):
        calls = []
        loader = ModuleType("hermes_cli.env_loader")
        def load_runtime(*, hermes_home, project_env):
            self.assertEqual(hermes_home, Path("/opt/data"))
            self.assertEqual(project_env, Path("/opt/hermes/.env"))
            os.environ["QQ_CLIENT_SECRET"] = "offline-synthetic-member-secret"
            calls.append("load")
        loader.load_hermes_dotenv = load_runtime
        store_module = ModuleType("smart_group_qq.store")
        class FakeStore:
            def __init__(self, path, *, member_secret):
                self_secret = member_secret
                if self_secret != "offline-synthetic-member-secret":
                    raise ValueError("missing runtime secret")
                calls.append("store")
            def close(self):
                calls.append("close")
        store_module.Store = FakeStore
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "path", list(sys.path)):
            with patch.dict(sys.modules, {"hermes_cli.env_loader": loader, "smart_group_qq.store": store_module}):
                exec(compile(MIGRATION_BLOCK, "explicit-migration", "exec"), {})
        self.assertEqual(calls, ["load", "store", "close"])


if __name__ == "__main__":
    unittest.main()
