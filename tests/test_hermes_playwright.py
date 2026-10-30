import asyncio
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import zlib


SPEC = importlib.util.spec_from_file_location(
    "verify_hermes_playwright", Path(__file__).resolve().parents[1] / "scripts/verify-hermes-playwright.py"
)
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


def png(rgb=verify.EXPECTED_RGB, size=(640, 480), algorithm=0):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    row = bytes(rgb) * size[0]
    data = bytearray()
    previous = bytes(len(row))
    for _ in range(size[1]):
        data.append(algorithm)
        for x, value in enumerate(row):
            left = row[x - 3] if x >= 3 else 0
            up = previous[x]
            upper_left = previous[x - 3] if x >= 3 else 0
            prediction = 0
            if algorithm == 1:
                prediction = left
            elif algorithm == 2:
                prediction = up
            elif algorithm == 3:
                prediction = (left + up) // 2
            elif algorithm == 4:
                p = left + up - upper_left
                distances = (abs(p-left), abs(p-up), abs(p-upper_left))
                prediction = (left, up, upper_left)[distances.index(min(distances))]
            data.append((value - prediction) & 255)
        previous = row
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", *size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(data)) + chunk(b"IEND", b""))


class PlaywrightVerificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        runtime = patch.object(verify, "inspect_runtime", return_value={"uid": 10000, "playwright_version": "1.64.0"})
        runtime.start()
        self.addCleanup(runtime.stop)
        self.locator = SimpleNamespace(fill=AsyncMock(), click=AsyncMock(), inner_text=AsyncMock(return_value="Hello 小栖"))
        self.page = SimpleNamespace(set_content=AsyncMock(), locator=Mock(return_value=self.locator),
                                    screenshot=AsyncMock(return_value=png()), set_default_timeout=Mock(),
                                    set_default_navigation_timeout=Mock())
        self.context = SimpleNamespace(route=AsyncMock(), new_page=AsyncMock(return_value=self.page), close=AsyncMock())
        self.browser = SimpleNamespace(version=verify.CHROMIUM_VERSION, new_context=AsyncMock(return_value=self.context),
                                       close=AsyncMock(), is_connected=Mock(return_value=False))
        self.driver = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=self.browser)), stop=AsyncMock())
        self.manager = SimpleNamespace(start=AsyncMock(return_value=self.driver))
        self.factory = Mock(return_value=self.manager)

    async def test_real_api_sequence_requires_dom_render_and_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "fixture.png"
            result = await verify.verify_browser(factory=self.factory, screenshot=target)
            self.assertTrue(result["ok"])
            self.assertEqual(result["screenshot"]["sample_rgb"], list(verify.EXPECTED_RGB))
            self.assertEqual(target.read_bytes(), self.page.screenshot.return_value)
        self.assertEqual(result["chromium_version"], verify.CHROMIUM_VERSION)
        self.assertTrue(result["browser_closed"] and result["context_closed"] and result["driver_closed"])
        self.browser.new_context.assert_awaited_once_with(viewport=verify.VIEWPORT, device_scale_factor=1,
                                                        offline=True, service_workers="block", ignore_https_errors=False)
        self.locator.fill.assert_awaited_once_with("小栖")
        self.locator.click.assert_awaited_once()
        self.assertFalse(result["proxy_https_verified"])
        self.assertFalse(result["hermes_agent_browser_verified"])

    async def test_dom_failure_still_closes_every_resource(self):
        self.locator.inner_text.return_value = "wrong"
        result = await verify.verify_browser(factory=self.factory)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "dom_result_mismatch")
        self.page.screenshot.assert_not_awaited()
        self.context.close.assert_awaited_once()
        self.browser.close.assert_awaited_once()
        self.driver.stop.assert_awaited_once()

    async def test_chromium_version_must_match(self):
        self.browser.version = "155.0.0.0"
        result = await verify.verify_browser(factory=self.factory)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "unexpected_chromium_version")
        self.browser.new_context.assert_not_awaited()
        self.browser.close.assert_awaited_once()

    async def test_operation_timeout_and_close_failure_are_reported(self):
        async def slow(*args, **kwargs):
            await asyncio.sleep(10)
        self.page.set_content.side_effect = slow
        result = await verify.verify_browser(factory=self.factory, timeout_seconds=0.02)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "TimeoutError")
        self.assertTrue(result["browser_closed"])
        self.page.set_content.side_effect = None
        self.browser.close.side_effect = slow
        result = await verify.verify_browser(factory=self.factory, cleanup_timeout=0.01)
        self.assertFalse(result["ok"])
        self.assertFalse(result["browser_closed"])
        self.assertEqual(result["browser_close_error"], "TimeoutError")
        self.assertTrue(result["driver_closed"])

    async def test_unexpected_request_is_aborted_and_fails(self):
        route = SimpleNamespace(abort=AsyncMock())
        async def install_route(pattern, handler):
            self.assertEqual(pattern, "**/*")
            await handler(route)
        self.context.route.side_effect = install_route
        result = await verify.verify_browser(factory=self.factory)
        route.abort.assert_awaited_once()
        self.assertFalse(result["ok"])
        self.assertEqual(result["network_requests"], 1)

    async def test_partial_driver_start_is_also_closed(self):
        async def slow():
            await asyncio.sleep(10)
        self.manager.start.side_effect = slow
        self.manager.__aexit__ = AsyncMock()
        result = await verify.verify_browser(factory=self.factory, timeout_seconds=0.02)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "driver_start")
        self.assertTrue(result["driver_closed"])
        self.manager.__aexit__.assert_awaited_once_with(None, None, None)
        self.driver.chromium.launch.assert_not_awaited()


class PngAndRuntimeTests(unittest.TestCase):
    def test_png_filters_and_bad_render(self):
        for algorithm in range(5):
            result = verify.inspect_png(png(size=(2, 2), algorithm=algorithm), size=(2, 2), sample=(1, 1))
            self.assertEqual(result["sample_rgb"], list(verify.EXPECTED_RGB))
        with self.assertRaisesRegex(ValueError, "rendered_pixel"):
            verify.inspect_png(png(rgb=(255, 0, 0)))
        with self.assertRaises(ValueError):
            verify.inspect_png(png()[:-20])
        data = bytearray(png())
        data[30] ^= 1
        with self.assertRaisesRegex(ValueError, "crc"):
            verify.inspect_png(bytes(data))

    def test_uid_and_package_version_are_enforced(self):
        with patch.object(verify.os, "getuid", return_value=0, create=True):
            with self.assertRaisesRegex(ValueError, "runtime_uid"):
                verify.inspect_runtime(10000)
        with patch.object(verify.os, "getuid", return_value=10000, create=True), patch.object(verify.importlib.metadata, "version", return_value="1.63.0"):
            with self.assertRaisesRegex(ValueError, "playwright_version"):
                verify.inspect_runtime(10000)

    def test_no_home_cache_or_bundled_node_fallback(self):
        with patch.object(verify.os, "getuid", return_value=10000, create=True), patch.object(verify.importlib.metadata, "version", return_value="1.64.0"), patch.dict(verify.os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "/root/.cache/ms-playwright"}, clear=True):
            with self.assertRaisesRegex(ValueError, "browser_cache"):
                verify.inspect_runtime(10000)
        with patch.object(verify.os, "getuid", return_value=10000, create=True), patch.object(verify.importlib.metadata, "version", return_value="1.64.0"), patch.dict(verify.os.environ, {"PLAYWRIGHT_BROWSERS_PATH": str(verify.BROWSERS_PATH)}, clear=True), patch.object(Path, "is_dir", return_value=True), patch.object(Path, "is_file", return_value=False), patch.object(verify.os, "access", return_value=True):
            with self.assertRaisesRegex(ValueError, "node_path"):
                verify.inspect_runtime(10000)

    def test_cli_nonzero_and_json_without_raw_exception(self):
        with patch.object(verify, "verify_browser", new=AsyncMock(return_value={"ok": False, "error_type": "TimeoutError"})), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(verify.main([]), 1)
            self.assertEqual(json.loads(output.getvalue())["error_type"], "TimeoutError")

    def test_actual_node_resolution_is_required_with_or_without_override(self):
        modules = {name: ModuleType(name) for name in ("playwright", "playwright._impl", "playwright._impl._driver")}
        driver = modules["playwright._impl._driver"]
        driver.compute_driver_executable = Mock(return_value=(str(verify.NODE_PATH), "synthetic-cli.js"))
        with patch.dict(sys.modules, modules), patch.object(verify.os, "getuid", return_value=10000, create=True), patch.object(verify.importlib.metadata, "version", return_value="1.64.0"), patch.dict(verify.os.environ, {"PLAYWRIGHT_BROWSERS_PATH": str(verify.BROWSERS_PATH)}, clear=True), patch.object(Path, "is_dir", return_value=True), patch.object(Path, "is_file", return_value=True), patch.object(verify.os, "access", return_value=True):
            report = verify.inspect_runtime(10000)
            self.assertFalse(report["node_override_present"])
            self.assertEqual(report["driver_node_resolved"], str(verify.NODE_PATH.resolve()))
            driver.compute_driver_executable.return_value = ("/unsafe/wheel/driver/node", "synthetic-cli.js")
            with self.assertRaisesRegex(ValueError, "driver_node_mismatch"):
                verify.inspect_runtime(10000)


if __name__ == "__main__":
    unittest.main()
