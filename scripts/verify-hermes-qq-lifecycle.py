#!/usr/bin/env python3
"""Offline QQ upgrade smoke using real gateway authorization and reset boundaries."""
from __future__ import annotations

import asyncio
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import yaml


def require(condition, message):
    if not condition:
        raise AssertionError(message)


async def verify(home: Path) -> None:
    os.environ["HERMES_HOME"] = str(home)
    os.environ["SUB2API_DEEPSEEK_API_KEY"] = "offline-synthetic-not-a-key"
    os.environ["QQ_CLIENT_SECRET"] = "offline-synthetic-member-secret"
    config_path = Path("/opt/hermes/qqbot-hk/hermes-config.yaml")
    raw_config = yaml.safe_load(config_path.read_text())
    raw_config["providers"]["sub2api_deepseek"]["base_url"] = "http://127.0.0.1:9/v1"
    (home / "config.yaml").write_text(yaml.safe_dump(raw_config))
    sys.path.insert(0, "/opt/hermes/qqbot-hk/plugins")
    from smart_group_qq import build_handler
    from smart_group_qq.store import Store
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import MessageEvent, SendResult
    from gateway.platforms.qqbot.adapter import QQAdapter
    from gateway.run import GatewayRunner
    from gateway.session import SessionStore

    settings = raw_config["plugins"]["entries"]["smart_group_qq"]["settings"]

    class Context:
        def get_config(self, key, default=None):
            return settings.get(key, default)

    platform = PlatformConfig(enabled=True, extra={
        **raw_config["platforms"]["qqbot"]["extra"],
        "app_id": "offline-synthetic-not-an-app",
        "client_secret": "offline-synthetic-not-a-key",
    })
    config = GatewayConfig(platforms={Platform.QQBOT: platform},
                           sessions_dir=home / "sessions", group_sessions_per_user=False)
    runner = GatewayRunner(config)
    adapter = QQAdapter(platform)
    runner.adapters[Platform.QQBOT] = adapter
    sends = []

    async def send(chat_id, content, reply_to=None, **kwargs):
        sends.append((chat_id, content))
        return SendResult(success=True, message_id=f"offline-reply-{len(sends)}")

    adapter.send = send
    store = Store(":memory:", member_secret="offline-synthetic-member-secret")
    handler = build_handler(Context(), store)
    groups = [adapter.build_source(chat_id=f"offline-group-{i}", user_id="offline-member",
                                  chat_type="group") for i in range(2)]
    unknown_dm = adapter.build_source(chat_id="offline-unapproved-dm",
                                     user_id="offline-unapproved-member", chat_type="dm")
    try:
        require(all(runner._is_user_authorized_for_source(s) for s in groups),
                "Real central authorization did not allow configured groups")
        require(not runner._is_user_authorized_for_source(unknown_dm),
                "Real central authorization admitted an unapproved DM")
        denied = await handler(MessageEvent(text="/kb add denied | private", source=unknown_dm,
                                            message_id="denied-local"), runner)
        require(denied == {"action": "allow"} and not sends,
                "Unapproved DM executed a local command before pairing")
        untouched = runner.session_store.get_or_create_session(groups[1]).session_id
        source = groups[0]
        key = runner._session_key_for_source(source)
        entry = runner.session_store.get_or_create_session(source)
        # Real native model/reasoning handlers must persist per-session overrides.
        for command, method in [("/model xiaomi/mimo-v2.6-flash --session", runner._handle_model_command),
                                ("/reasoning high", runner._handle_reasoning_command)]:
            result = method(MessageEvent(text=command, source=source, message_id=command))
            if inspect.isawaitable(result):
                result = await result
            require(result is not None, "Native session command did not execute")
        route = runner.session_store.get_or_create_session(source)
        require("mimo-v2.6-flash" in json.dumps(route.to_dict()), "Model override not persisted")
        reasoning = runner._resolve_session_reasoning_config(source=source, session_key=key)
        require(reasoning == {"enabled": True, "effort": "high"},
                "Native current-session reasoning selection did not take effect")
        other_reasoning = runner._resolve_session_reasoning_config(source=groups[1])
        require(other_reasoning == {"enabled": True, "effort": "low"},
                "Reasoning selection crossed the group isolation boundary")
        reopened = SessionStore(config.sessions_dir, config)
        restored = reopened.get_or_create_session(source)
        require(restored.session_id == route.session_id,
                "Session route changed after reopening durable store")
        require(restored.to_dict().get("model_override") == route.to_dict().get("model_override"),
                "Session model override disappeared on reopen")
        handler.knowledge.add_document(source.chat_id, "retention", "synthetic-retention-marker")
        for index, command in enumerate(("/reset", "/纠正记忆：偏好=测试", "/停止记忆", "/忘记我")):
            before = runner.session_store.get_or_create_session(source).session_id
            generation = runner._begin_session_run_generation(key)
            result = await handler(MessageEvent(text=command, source=source,
                                                 message_id=f"privacy-{index}"), runner)
            require(result["action"] == "skip", "Privacy command was not handled")
            after = runner.session_store.get_or_create_session(source).session_id
            require(after != before, f"{command} did not rotate the actual durable route")
            require(not runner._is_session_run_current(key, generation),
                    f"{command} left an old in-flight generation deliverable")
            require(runner.session_store.get_or_create_session(groups[1]).session_id == untouched,
                    "Reset crossed the group isolation boundary")
            require(handler.knowledge.search(source.chat_id, "synthetic-retention-marker"),
                    "Reset/privacy command deleted the unrelated group knowledge base")
            await asyncio.sleep(0)
        require(len(sends) == 4, "Native reset/privacy confirmations not delivered once")
        print("QQ_NATIVE_LIFECYCLE=passed AUTH=real PAIRING=fail-closed RESET=real "
              "PRIVACY=retired-generation MODEL=durable REASONING=session GROUPS=isolated NETWORK=unused")
    finally:
        # Scheduled send-completion callbacks must finish before closing their Store.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        store.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="qq-native-upgrade-") as tmp:
        with patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")), \
                patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden")):
            asyncio.run(verify(Path(tmp)))


if __name__ == "__main__":
    main()
