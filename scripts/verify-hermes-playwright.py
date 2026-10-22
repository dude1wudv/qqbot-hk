#!/usr/bin/env python3
"""Verify the pinned Python Playwright runtime using only a synthetic offline page.

Run as the production UID, normally:
  /opt/hermes/.venv/bin/python /opt/hermes/verify-hermes-playwright.py
No package installation, external URL, credentials or existing browser session
is used. Proxy/HTTPS connectivity and Hermes agent-browser/CDP are separate checks.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import struct
import time
import zlib


PLAYWRIGHT_VERSION = "1.64.0"
CHROMIUM_VERSION = "156.0.8078.4"
BROWSERS_PATH = Path("/opt/hermes/.playwright")
NODE_PATH = Path("/usr/local/bin/node")
VIEWPORT = {"width": 640, "height": 480}
EXPECTED_RGB = (37, 99, 235)
HTML = """<!doctype html><html><head><meta charset="utf-8"><title>QQ browser fixture</title>
<style>body{margin:0;background:white;font:16px sans-serif}main{margin:96px 24px}
#swatch{position:absolute;left:0;top:0;width:64px;height:64px;background:#dc2626}</style>
</head><body><div id="swatch"></div><main><label for="name">Name</label>
<input id="name"><button id="apply">Apply</button><p id="result">Waiting</p></main>
<script>document.querySelector('#apply').onclick=()=>{
document.querySelector('#result').textContent='Hello '+document.querySelector('#name').value;
document.querySelector('#swatch').style.backgroundColor='#2563eb';};</script></body></html>"""


def inspect_png(data: bytes, *, size: tuple[int, int] = (640, 480),
                sample: tuple[int, int] = (32, 32)) -> dict:
    """Validate a Chromium PNG and decode one rendered RGB pixel using stdlib."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("invalid_png_signature")
    offset, compressed, header, ended = 8, bytearray(), None, False
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise ValueError("truncated_png")
        payload = data[offset + 8:offset + 8 + length]
        crc = struct.unpack(">I", data[offset + 8 + length:end])[0]
        if zlib.crc32(kind + payload) != crc:
            raise ValueError("invalid_png_crc")
        if kind == b"IHDR":
            if header is not None or length != 13:
                raise ValueError("invalid_png_header")
            header = struct.unpack(">IIBBBBB", payload)
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            ended = True
            break
        offset = end
    if not ended or header is None:
        raise ValueError("incomplete_png")
    width, height, depth, color, compression, filtering, interlace = header
    if (width, height) != size or depth != 8 or color not in (2, 6) or any((compression, filtering, interlace)):
        raise ValueError("unexpected_png_format")
    channels = 3 if color == 2 else 4
    row_size = width * channels
    expected_length = (row_size + 1) * height
    decoder = zlib.decompressobj()
    pixels = decoder.decompress(compressed, expected_length + 1)
    if len(pixels) != expected_length or not decoder.eof or decoder.unconsumed_tail:
        raise ValueError("invalid_png_pixels")
    previous = bytearray(row_size)
    sampled = None
    for y in range(height):
        row_start = y * (row_size + 1)
        algorithm = pixels[row_start]
        row = bytearray(pixels[row_start + 1:row_start + 1 + row_size])
        if algorithm not in range(5):
            raise ValueError("invalid_png_filter")
        for x in range(row_size):
            left = row[x - channels] if x >= channels else 0
            up = previous[x]
            upper_left = previous[x - channels] if x >= channels else 0
            prediction = 0
            if algorithm == 1:
                prediction = left
            elif algorithm == 2:
                prediction = up
            elif algorithm == 3:
                prediction = (left + up) // 2
            elif algorithm == 4:
                p = left + up - upper_left
                distances = (abs(p - left), abs(p - up), abs(p - upper_left))
                prediction = (left, up, upper_left)[distances.index(min(distances))]
            row[x] = (row[x] + prediction) & 255
        if y == sample[1]:
            sampled = tuple(row[sample[0] * channels:sample[0] * channels + 3])
        previous = row
    if sampled != EXPECTED_RGB:
        raise ValueError("rendered_pixel_mismatch")
    return {"width": width, "height": height, "sample_rgb": list(sampled),
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def inspect_runtime(expected_uid: int) -> dict:
    uid = os.getuid() if hasattr(os, "getuid") else None
    if uid != expected_uid:
        raise ValueError("unexpected_runtime_uid")
    version = importlib.metadata.version("playwright")
    if version != PLAYWRIGHT_VERSION:
        raise ValueError("unexpected_playwright_version")
    configured_cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if configured_cache != str(BROWSERS_PATH) or not BROWSERS_PATH.is_dir():
        raise ValueError("unexpected_browser_cache")
    if not os.access(BROWSERS_PATH, os.R_OK | os.X_OK):
        raise ValueError("browser_cache_not_accessible")
    configured_node = os.environ.get("PLAYWRIGHT_NODEJS_PATH", "")
    if not NODE_PATH.is_file() or not os.access(NODE_PATH, os.X_OK):
        raise ValueError("unexpected_node_path")
    if configured_node and Path(configured_node).resolve() != NODE_PATH.resolve():
        raise ValueError("unexpected_node_path")
    from playwright._impl._driver import compute_driver_executable

    driver_node, _ = compute_driver_executable()
    if Path(driver_node).resolve() != NODE_PATH.resolve():
        raise ValueError("playwright_driver_node_mismatch")
    return {"uid": uid, "playwright_version": version, "browser_cache": configured_cache,
            "driver_node": driver_node, "driver_node_resolved": str(NODE_PATH.resolve()),
            "node_override_present": bool(configured_node)}


async def verify_browser(*, expected_uid: int = 10000, timeout_seconds: float = 30,
                         screenshot: Path | None = None, factory=None,
                         cleanup_timeout: float = 5) -> dict:
    started = time.monotonic()
    report = {"ok": False, "scope": "synthetic_offline_browser", "stage": "runtime",
              "timeout_seconds": timeout_seconds, "cleanup_timeout_seconds": cleanup_timeout,
              "browser_started": False, "browser_closed": None, "context_closed": None,
              "driver_closed": None, "network_requests": 0,
              "proxy_https_verified": False, "hermes_agent_browser_verified": False}
    manager = driver = browser = context = None

    async def steps():
        nonlocal manager, driver, browser, context
        report.update(inspect_runtime(expected_uid))
        create = factory
        if create is None:
            from playwright.async_api import async_playwright
            create = async_playwright
        report["stage"] = "driver_start"
        manager = create()
        driver = await manager.start()
        report["stage"] = "browser_launch"
        # Match Hermes' existing container sandbox/dev-shm policy. No browser
        # binary override: launch must locate the pinned Playwright build.
        browser = await driver.chromium.launch(
            headless=True, timeout=min(timeout_seconds * 1000, 15000),
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-background-networking"],
        )
        report["browser_started"] = True
        report["chromium_version"] = browser.version
        if browser.version != CHROMIUM_VERSION:
            raise ValueError("unexpected_chromium_version")
        report["stage"] = "context"
        context = await browser.new_context(viewport=VIEWPORT, device_scale_factor=1,
                                            offline=True, service_workers="block", ignore_https_errors=False)

        async def block_request(route):
            report["network_requests"] += 1
            await route.abort()

        await context.route("**/*", block_request)
        page = await context.new_page()
        page.set_default_timeout(5000)
        page.set_default_navigation_timeout(5000)
        report["stage"] = "dom"
        await page.set_content(HTML, wait_until="load", timeout=5000)
        await page.locator("#name").fill("小栖")
        await page.locator("#apply").click()
        text = await page.locator("#result").inner_text()
        if text != "Hello 小栖":
            raise ValueError("dom_result_mismatch")
        report["dom_result"] = text
        report["stage"] = "screenshot"
        png = await page.screenshot(type="png", animations="disabled", timeout=5000)
        report["screenshot"] = inspect_png(png)
        if screenshot is not None:
            screenshot.write_bytes(png)
            report["screenshot"]["path"] = str(screenshot)
        if report["network_requests"]:
            raise ValueError("unexpected_network_request")
        report["stage"] = "completed"
        report["ok"] = True

    try:
        await asyncio.wait_for(steps(), timeout=timeout_seconds)
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        # Our own finite reason codes contain no provider output, env or URL.
        if isinstance(exc, ValueError) and str(exc).replace("_", "").isalnum():
            report["error"] = str(exc)
    finally:
        # __aenter__/start may be interrupted after launching the driver but
        # before returning its public handle. The context manager still owns
        # that partial startup and can close it through its public exit API.
        if driver is None and manager is not None:
            try:
                await asyncio.wait_for(manager.__aexit__(None, None, None), timeout=cleanup_timeout)
                report["driver_closed"] = True
            except Exception as exc:
                report["driver_closed"] = False
                report["driver_close_error"] = type(exc).__name__
        for name, resource in (("context", context), ("browser", browser), ("driver", driver)):
            if resource is None:
                continue
            try:
                close = resource.stop if name == "driver" else resource.close
                await asyncio.wait_for(close(), timeout=cleanup_timeout)
                report[f"{name}_closed"] = name != "browser" or not browser.is_connected()
            except Exception as exc:
                report[f"{name}_closed"] = False
                report[f"{name}_close_error"] = type(exc).__name__
            if not report[f"{name}_closed"]:
                report["ok"] = False
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-uid", type=int, default=10000)
    parser.add_argument("--timeout-seconds", type=float, default=30)
    parser.add_argument("--screenshot", type=Path, help="Optional writable path for the synthetic PNG.")
    parser.add_argument("--output", type=Path, help="Optional writable path for the JSON report.")
    args = parser.parse_args(argv)
    if not 0 < args.timeout_seconds <= 120:
        parser.error("--timeout-seconds must be in (0, 120]")
    report = asyncio.run(verify_browser(expected_uid=args.expected_uid,
                                        timeout_seconds=args.timeout_seconds, screenshot=args.screenshot))
    if args.output:
        try:
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            report["ok"] = False
            report["report_write_error"] = "OSError"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
