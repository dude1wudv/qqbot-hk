#!/usr/bin/env python3
"""Behavior smoke for QQ native quick aliases and custom-first help."""

import argparse
import asyncio
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import yaml


commands_module = ModuleType("smart_group_qq.commands")
commands_module.help_text = lambda: "【QQ 助手】\n/low /medium /high /max 切换当前会话推理强度"
package_module = ModuleType("smart_group_qq")
package_module.commands = commands_module
sys.modules["smart_group_qq"] = package_module
sys.modules["smart_group_qq.commands"] = commands_module

from gateway.config import Platform
from gateway.run_inbound import GatewayInboundMixin
from gateway.slash_commands import GatewaySlashCommandsMixin


EXPECTED = {
    "low": "/reasoning low --session",
    "medium": "/reasoning medium --session",
    "high": "/reasoning high --session",
    "max": "/reasoning max --session",
    "deepseek": "/model deepseek/deepseek-v4.1-flash --session",
    "gemini": "/model gemini-3.8-flash-high --session",
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
    require(reply.startswith("【QQ 助手】"), "QQ custom help is not first")
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
    print("QQ_NATIVE_COMMANDS=passed HELP=custom-first HERMES=folded ALIASES=low,medium,high,max,deepseek,gemini")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/opt/hermes/qqbot-hk/hermes-config.yaml"),
    )
    main(parser.parse_args().config)
