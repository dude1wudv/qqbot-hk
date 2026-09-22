import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))
import smart_group_qq as plugin

spec = importlib.util.spec_from_file_location("qq_startup_hook", ROOT / "hooks/smart_group_qq/handler.py")
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


class MaintenanceTests(unittest.TestCase):
    def test_sync_discovery_then_gateway_start_runs_once_and_recovers(self):
        ctx = SimpleNamespace(
            plugin_id="smart_group_qq", get_config=lambda key, default=None: default,
            register_hook=MagicMock(), subscribe=MagicMock(), spawn_task=MagicMock(),
        )
        store = MagicMock()
        store.list_memory_backlog_groups.return_value = []
        handler = MagicMock()
        handler.character.tick = AsyncMock(side_effect=[RuntimeError("temporary"), None])
        storage = ModuleType("plugins.plugin_storage")
        storage.plugin_db = MagicMock()
        with patch.dict(sys.modules, {"plugins.plugin_storage": storage}), \
                patch.object(plugin, "Store", return_value=store), \
                patch.object(plugin, "build_handler", return_value=handler), \
                patch.object(plugin, "install_nonmention_observer"):
            plugin.register(ctx)  # no running asyncio loop, as in production
        ctx.spawn_task.assert_not_called()
        event, start = ctx.subscribe.call_args.args
        self.assertEqual(event, "smart_group_qq:gateway_startup")
        api = ModuleType("hermes_cli.plugins")
        api.get_plugin_subscriptions = lambda: {event: [start]}

        async def run():
            real_sleep = asyncio.sleep
            cycles = 0
            ready = asyncio.Event()
            hold = asyncio.Event()

            async def sleep(_seconds):
                nonlocal cycles
                cycles += 1
                if cycles > 2:
                    ready.set()
                    await hold.wait()
                await real_sleep(0)

            ctx.spawn_task.side_effect = lambda coro, **kwargs: asyncio.create_task(coro, **kwargs)
            with patch.dict(sys.modules, {"hermes_cli.plugins": api}), \
                    patch.object(plugin, "asyncio", SimpleNamespace(
                        Task=asyncio.Task, CancelledError=asyncio.CancelledError,
                        get_running_loop=asyncio.get_running_loop, sleep=sleep)):
                await hook.handle("gateway:startup", {})
                task = ctx._smart_group_qq_maintenance_task
                await hook.handle("gateway:startup", {})
                self.assertIs(ctx._smart_group_qq_maintenance_task, task)
                ctx.spawn_task.assert_called_once()
                await asyncio.wait_for(ready.wait(), 2)
                self.assertEqual(handler.character.tick.await_count, 2)
                store.record_audit.assert_called_once_with("maintenance_ready", source="gateway_startup")
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        with self.assertLogs(plugin.logger, level="WARNING") as logs:
            asyncio.run(run())
        self.assertTrue(any("maintenance cycle failed" in line for line in logs.output))

    def test_missing_subscription_is_visible(self):
        api = ModuleType("hermes_cli.plugins")
        api.get_plugin_subscriptions = lambda: {}
        with patch.dict(sys.modules, {"hermes_cli.plugins": api}):
            with self.assertRaisesRegex(RuntimeError, "subscription unavailable"):
                asyncio.run(hook.handle("gateway:startup", {}))


if __name__ == "__main__":
    unittest.main()
