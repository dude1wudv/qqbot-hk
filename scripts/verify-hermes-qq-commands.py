#!/usr/bin/env python3
"""Behavior smoke for QQ native quick aliases and custom-first help."""

import argparse
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import yaml


# Import the shipped plugin, never a stand-in package. No connect() is called.
PLUGIN_ROOT = Path("/opt/hermes/qqbot-hk/plugins")
require_plugin = PLUGIN_ROOT / "smart_group_qq" / "__init__.py"
if not require_plugin.is_file():
    raise SystemExit(f"Image plugin is missing: {require_plugin}")
sys.path.insert(0, str(PLUGIN_ROOT))
from smart_group_qq import build_handler, qq_observer
from smart_group_qq.commands import help_text
from smart_group_qq.ingress import install_command_ingress, uninstall_command_ingress
from smart_group_qq.store import Store

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, SendResult
from gateway.platforms.qqbot.adapter import QQAdapter
from gateway.run_inbound import GatewayInboundMixin
from gateway.slash_commands import GatewaySlashCommandsMixin


EXPECTED = {
    "low": "/reasoning low",
    "medium": "/reasoning medium",
    "high": "/reasoning high",
    "xhigh": "/reasoning xhigh",
    "max": "/reasoning max",
    "deepseek": "/model deepseek/deepseek-v4.1-flash --session",
    "gemini": "/model gemini-3.8-flash-high --session",
    "mimo": "/model xiaomi/mimo-v2.6-flash --session",
    "muse": "/model meta/muse-spark-1.3-contributor --session",
}


class FakeEvent:
    def __init__(self, text: str):
        self.text = text

    def get_command(self):
        return self.text.lstrip("/").split(maxsplit=1)[0] if self.text.startswith("/") else None

    def get_command_args(self):
        parts = self.text.split(maxsplit=1)
        return parts[1] if len(parts) == 2 else ""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


class InboundHarness(GatewayInboundMixin):
    def __init__(self, config):
        self.config = config

    @staticmethod
    def _check_slash_access(source, command):
        return None

    async def _hm_command_hooks(self, event, source, quick_key, command, canonical):
        return False, None, None


async def verify_help() -> None:
    runner = object.__new__(GatewaySlashCommandsMixin)
    event = SimpleNamespace(source=SimpleNamespace(platform=Platform.QQBOT))
    reply = await runner._handle_help_command(event)
    require(reply.startswith(help_text()), "QQ actual custom help is not first")
    require("【Hermes 原生命令（折叠）】" in reply, "Hermes help fold is missing")
    require("/commands" in reply, "Hermes command expansion hint is missing")


async def verify_alias_resolution(config) -> None:
    runner = InboundHarness(config)
    source = SimpleNamespace(platform=Platform.QQBOT)
    for alias, target in EXPECTED.items():
        event = FakeEvent(f"/{alias}")
        handled, result, command, canonical = await runner._hm_resolve_command(
            event, source, "qqbot:dm:smoke"
        )
        expected_command = target.lstrip("/").split()[0]
        require(not handled and result is None, f"/{alias} was consumed before native dispatch")
        require(command == expected_command and canonical == expected_command, f"/{alias} was not recognized")
        require(event.text == target, f"/{alias} did not expand on the native command path")


async def verify_real_ingress(config) -> None:
    """Exercise real QQ decode/ingest, base busy queue and runner pre-dispatch.

    Native commands stop at resolution, not model/provider execution. The harness
    is deliberately not a claim of full GatewayRunner busy-policy coverage.
    """
    class Context:
        llm = SimpleNamespace(acomplete_structured=AsyncMock(
            side_effect=AssertionError("Local smoke must never invoke an LLM")))

        @staticmethod
        def get_config(key, default=None):
            return {"ambient": {"enabled": False, "participation": {
                "enabled": False, "wake_words": ["小分队机器人"]}}}.get(key, default)

    store = Store(":memory:")
    handler = build_handler(Context(), store)
    runner = InboundHarness(config)
    runner._is_user_authorized_for_source = lambda source: True
    # Non-secret sentinels prevent constructor fallback to ambient credentials.
    adapter = QQAdapter(PlatformConfig(extra={
        "app_id": "offline-smoke-not-a-credential",
        "client_secret": "offline-smoke-not-a-credential",
        "group_policy": "open",
    }))
    runner.adapters = {Platform.QQBOT: adapter}
    runner._session_key_for_source = lambda source: adapter._event_session_key(
        MessageEvent(text="", source=source))
    sends, prehook_texts, native_texts, hooks = [], [], [], []

    async def send(chat_id, content, reply_to=None, **kwargs):
        sends.append((chat_id, content, reply_to))
        return SendResult(success=True, message_id="offline-reply")

    def invoke_hook(name, **kwargs):
        require(name == "pre_gateway_dispatch", f"Unexpected lifecycle hook: {name}")
        hooks.append(kwargs["event"].message_id)
        return [handler(**kwargs)]

    async def inbound(item):
        require(isinstance(item, MessageEvent), "QQ ingest did not build a real MessageEvent")
        prehook_texts.append(item.text)
        result = runner._hm_pre_gateway_dispatch_hook(item, item.source)
        if result is not None:
            require(runner._is_user_authorized_for_source(result.source), "Unauthorized native entry")
            resolved = await runner._hm_resolve_command(
                result, result.source, adapter._event_session_key(result))
            require(not resolved[0], "Native command was unexpectedly consumed")
            native_texts.append(result.text)
        return None

    adapter.send = send
    adapter._message_handler = inbound
    adapter._busy_text_mode = "queue"
    source = adapter.build_source(chat_id="offline-smoke-group",
                                  user_id="offline-smoke-member", chat_type="group")
    key = adapter._event_session_key(MessageEvent(text="", source=source))
    adapter._active_sessions[key] = asyncio.Event()

    def payload(index, text, kind="GROUP_AT_MESSAGE_CREATE", mentions=None):
        data = {"id": f"offline-{index}", "content": text,
                "timestamp": "2026-09-22T00:00:00+00:00",
                "group_openid": source.chat_id,
                "author": {"member_openid": source.user_id}, "attachments": []}
        if mentions is not None:
            data["mentions"] = mentions
        return {"op": 0, "t": kind, "d": data}

    async def settle():
        for queue in getattr(adapter, "_smart_group_qq_nonmention_queue", {}).values():
            await asyncio.wait_for(queue.join(), 3)
        await asyncio.sleep(0)

    try:
        with patch("hermes_cli.lifecycle.invoke_hook", side_effect=invoke_hook), \
                patch.object(adapter, "_is_group_allowed", return_value=True), \
                patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")), \
                patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden")):
            # Reproduce the unpatched queue loss with real BasePlatformAdapter.
            for index, text in enumerate(("/值日表", "/all")):
                data = payload(f"before-{index}", text)["d"]
                await adapter._on_message("GROUP_AT_MESSAGE_CREATE", data)
                require(key in adapter._pending_messages, "Original busy path did not queue")
                require(not hooks and not sends, "Original local command unexpectedly reached handler")
                adapter._pending_messages.clear()
            install_command_ingress(QQAdapter)
            patched = QQAdapter.handle_message
            install_command_ingress(QQAdapter)
            require(QQAdapter.handle_message is patched, "Ingress install is not idempotent")
            # Same content, new transport IDs (QQ correctly deduplicates old IDs).
            cases = (("/值日表", "/值日表"), ("/all", "/all"),
                     ("/muse", EXPECTED["muse"]), ("/mimo", EXPECTED["mimo"]),
                     ("/xhigh", EXPECTED["xhigh"]))
            for index, (text, normalized) in enumerate(cases):
                data = payload(f"after-{index}", "@小分队机器人" + text)["d"]
                before = len(hooks)
                await adapter._on_message("GROUP_AT_MESSAGE_CREATE", data)
                await settle()
                require(len(hooks) == before + 1, f"{text} did not reach real pre-dispatch")
                require(prehook_texts[-1] == text, f"{text} was not normalized before pre-dispatch")
                if text in ("/值日表", "/all"):
                    expected = "本周值日表" if text == "/值日表" else "已恢复群聊自动参与"
                    require(expected in sends[-1][1], f"{text} did not produce its Chinese local reply")
                else:
                    require(native_texts[-1] == normalized, f"{text} native normalization failed")
                require(key not in adapter._pending_messages, f"{text} leaked into busy queue")
            # Full-group observer -> real GROUP_AT -> real ingest -> busy inline -> prehook.
            qq_observer.install_nonmention_observer(handler.observe_nonmention)
            for index, (text, mentions, expected) in enumerate((
                ("@小分队机器人 /all", None, "已恢复群聊自动参与"),
                ("/值日表", [{"bot": True}], "本周值日表"),
            )):
                item = payload(f"observer-{index}", text, "GROUP_MESSAGE_CREATE", mentions)
                before = len(sends)
                adapter._dispatch_payload(item)
                await settle()
                require(len(sends) == before + 1 and expected in sends[-1][1],
                        f"Observer did not deliver {text} locally")
                adapter._dispatch_payload(item)
                await settle()
                await adapter._on_message("GROUP_AT_MESSAGE_CREATE", item["d"])
                require(len(sends) == before + 1, "Cross-event duplicate executed twice")
            before = len(hooks)
            for index, text in enumerate(("/all", "@other /all", "普通消息")):
                adapter._dispatch_payload(payload(f"silent-{index}", text, "GROUP_MESSAGE_CREATE"))
            await settle()
            require(len(hooks) == before, "Unaddressed ambient content reached gateway")
            Context.llm.acomplete_structured.assert_not_called()
    finally:
        qq_observer.uninstall_nonmention_observer()
        uninstall_command_ingress(QQAdapter)
        store.close()
    print("QQ_REAL_INGEST=passed BASE_BUSY_QUEUE=reproduced PRE_DISPATCH=real OBSERVER=passed LLM=unused")


def main(config_path: Path) -> None:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    runner = object.__new__(GatewayInboundMixin)
    runner.config = config
    quick_commands = runner._hm_quick_commands()
    for alias, target in EXPECTED.items():
        require(quick_commands.get(alias) == {"type": "alias", "target": target}, f"/{alias} alias missing")
        event = FakeEvent("")
        command = runner._hm_expand_alias_quick_command(event, quick_commands[alias])
        require(command == target.lstrip("/").split()[0], f"/{alias} resolved to wrong command")
        require(event.text == target, f"/{alias} expanded to wrong target")
    asyncio.run(verify_help())
    asyncio.run(verify_alias_resolution(config))
    asyncio.run(verify_real_ingress(config))
    print("QQ_NATIVE_COMMANDS=passed HELP=custom-first HERMES=folded ALIASES=low,medium,high,xhigh,max,deepseek,gemini,mimo,muse")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/opt/hermes/qqbot-hk/hermes-config.yaml"),
    )
    main(parser.parse_args().config)
