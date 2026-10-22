#!/usr/bin/env python3
"""Offline checks of QQ routing hooks against the installed, patched Hermes runtime."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
from copy import deepcopy
import inspect
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def verify_native_hooks(routing, runner, source, key):
    import httpx
    from agent import chat_completion_helpers as native
    from agent import agent_runtime_helpers as runtime
    from agent.agent_init import _snapshot_primary_runtime
    from agent.error_classifier import FailoverReason, classify_api_error

    root = Path(native.__file__).resolve().parents[1]
    marker = '# QQBOT_HK_MODEL_ROUTING_PATCH = "v1"'
    for relative in ("gateway/run_turn_runner.py", "agent/chat_completion_helpers.py", "agent/conversation_loop.py"):
        require((root / relative).read_text().count(marker) == 1, f"Missing/duplicate routing patch: {relative}")
    require(routing.RETRYABLE_REASONS <= {reason.value for reason in FailoverReason},
            "Retryable reason whitelist does not match the installed native enum")
    for error in (httpx.ConnectError("synthetic connect error"), httpx.ReadTimeout("synthetic read timeout"),
                  httpx.RemoteProtocolError("synthetic incomplete chunked read")):
        classified = classify_api_error(error, provider=routing.PROVIDER, model=routing.FAST_MODEL,
                                        base_url=routing.BASE_URL)
        require(classified.reason is FailoverReason.timeout and classified.retryable,
                "Native connection failure no longer maps to retryable timeout")

    def make_agent():
        value = SimpleNamespace(platform="qqbot", model=routing.FAST_MODEL, provider=routing.PROVIDER,
                                requested_provider=routing.PROVIDER, base_url=routing.BASE_URL,
                                api_mode="chat_completions", reasoning_config={"enabled": True, "effort": "low"},
                                _primary_runtime={"reasoning_config": {"enabled": True, "effort": "high"}},
                                _interrupt_requested=False, _qq_deadline_exceeded=False,
                                _transport_cache={}, _buffer_diagnostic_status=Mock(),
                                api_key="offline-synthetic-not-a-key", _client_kwargs={},
                                _use_prompt_caching=False, _use_native_cache_layout=False,
                                context_compressor=SimpleNamespace(model=routing.FAST_MODEL, base_url=routing.BASE_URL,
                                    api_key="offline-synthetic-not-a-key", provider=routing.PROVIDER,
                                    context_length=1000000, threshold_tokens=80000, update_model=Mock()),
                                _anthropic_prompt_cache_policy=lambda **kw: (False, False),
                                _ensure_lmstudio_runtime_loaded=lambda: None)
        _snapshot_primary_runtime(value)
        routing.configure_turn(value, runner, source=source, session_key=key)
        return value

    client = SimpleNamespace(base_url=routing.BASE_URL)
    swaps = []

    def swap(value, new_client, *args):
        swaps.append(args)
        value.client = new_client

    # Stub client creation and unrelated provider probes, leaving native candidate
    # iteration, route mutation, reasoning hook and the public guard executable.
    with ExitStack() as stack:
        for name in ("_rebind_fallback_credential_pool", "_update_fallback_context_compressor",
                     "_rescope_fallback_extra_body", "rewrite_prompt_model_identity", "_reset_stale_streak"):
            stack.enter_context(patch.object(native, name))
        stack.enter_context(patch.object(native, "_should_skip_fallback_candidate", return_value=False))
        stack.enter_context(patch("agent.fallback_cooldown.switch_deferred_by_reset", return_value=False))
        stack.enter_context(patch("agent.fallback_cooldown._arm_rate_limit_cooldown", return_value=None))
        resolver = stack.enter_context(patch("agent.auxiliary_client.resolve_provider_client",
                                             return_value=(client, routing.FLASH_MODEL)))
        stack.enter_context(patch("hermes_cli.fallback_config.resolve_entry_api_key", return_value=None))
        stack.enter_context(patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda model, *_: model))
        stack.enter_context(patch("agent.client_lifecycle._swap_fallback_clients", side_effect=swap))
        stack.enter_context(patch("agent.agent_runtime_helpers.sync_credential_pool_entry_id"))
        stack.enter_context(patch("agent.native_compaction.resolve_native_compaction_capabilities", return_value={}))

        value = make_agent()
        require(native.try_activate_fallback(value, FailoverReason.server_error), "Eligible native fallback failed")
        require(value.model == routing.FLASH_MODEL and value.client is client and len(swaps) == 1,
                "Native fallback did not swap exactly one same-provider route")
        require(value.reasoning_config == {"enabled": True, "effort": "low"}, "Fallback replaced manual reasoning")
        require(not native.try_activate_fallback(value, FailoverReason.timeout), "Native fallback repeated")
        require(resolver.call_count == 1 and len(swaps) == 1, "Retry constructed a second fallback client")

        # Reused agents receive the current /reasoning override before native
        # turn-context initialization restores the previous primary snapshot.
        for effort in ({"enabled": True, "effort": "low"}, {"enabled": False}):
            value.model = routing.FLASH_MODEL
            value._fallback_activated = True
            value.reasoning_config = deepcopy(effort)
            routing.configure_turn(value, runner, source=source, session_key=key)
            with patch("agent.fallback_cooldown._is_entitlement_rejected", return_value=False), \
                    patch.object(runtime, "_primary_reset_gate_blocks", return_value=(False, None, False)), \
                    patch.object(runtime, "_rebind_primary_credential_pool"), \
                    patch.object(runtime, "_rebuild_primary_client", side_effect=lambda a, *_args, **_kw: setattr(a, "client", client)):
                require(runtime.restore_primary_runtime(value), "Native next-turn primary restoration failed")
            require(value.model == routing.FAST_MODEL and not value._fallback_activated,
                    "Native cached agent stayed on fallback after recovery")
            require(value.reasoning_config == effort, "Native primary restoration replaced current manual reasoning")

        # The real raw-chunk interceptor sees reasoning even without a QQ stream callback.
        for field, text in (("content", "synthetic text"), ("reasoning_content", "synthetic thought"),
                            ("tool_calls", [SimpleNamespace(id="synthetic-tool")])):
            value = make_agent()
            stream = object.__new__(native._StreamingCall)
            stream.agent = value
            stream._qq_model_route_state = routing.capture_route_state(value)
            stream.provider_tool_in_flight = {"yes": False}
            stream.last_chunk_time = {"t": 0}
            stream._stream_attempt_is_active = lambda _: True
            stream._writer_still_current = lambda _: True
            chunk = SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(**{field: text}), finish_reason=None)])
            require(stream._accept_chat_chunk(1, chunk), "Synthetic current native stream chunk was rejected")
            require(not native.try_activate_fallback(value, FailoverReason.timeout), f"Native fallback after {field}")

        value = make_agent()
        response = SimpleNamespace(choices=[])
        completion = Mock(return_value=response)
        request_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion)))
        actual = native._dispatch_nonstreaming_api_request(value, {"model": routing.FAST_MODEL},
                                                          make_client=lambda *_args, **_kw: request_client)
        require(actual is response and completion.call_count == 1, "Native nonstream response hook changed dispatch")
        require(not native.try_activate_fallback(value, FailoverReason.server_error), "Native fallback after successful response")

        # This is the exact extra boundary used immediately before native run_tool_round;
        # no full conversation restart is invoked by this helper or native fallback.
        value = make_agent()
        tools = Mock(return_value="synthetic side effect")
        routing.note_progress(value, "tools")
        tools()
        require(not native.try_activate_fallback(value, FailoverReason.timeout), "Native fallback after tool execution")
        require(tools.call_count == 1 and len(swaps) == 1, "Tool round or model swap was repeated")

        for pin in (routing.FLASH_MODEL, routing.FAST_MODEL):
            runner._session_state(key).conversation.model_override = {"model": pin}
            value = make_agent()
            require(not native.try_activate_fallback(value, FailoverReason.server_error), "Manual pin allowed auto fallback")
        runner._session_state(key).conversation.model_override = None
        require(resolver.call_count == 1, "Blocked fallback still resolved an extra model/client")


async def verify(home: Path):
    os.environ["HERMES_HOME"] = str(home)
    os.environ["SUB2API_DEEPSEEK_API_KEY"] = "offline-synthetic-not-a-key"
    os.environ["QQ_CLIENT_SECRET"] = "offline-synthetic-member-secret"
    raw = yaml.safe_load(Path("/opt/hermes/qqbot-hk/hermes-config.yaml").read_text())
    (home / "config.yaml").write_text(yaml.safe_dump(raw))
    sys.path.insert(0, "/opt/hermes/qqbot-hk/plugins")
    import qqbot_model_routing as routing
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import MessageEvent, SendResult
    from gateway.platforms.qqbot.adapter import QQAdapter
    from gateway.run import GatewayRunner
    from gateway.session import SessionStore
    from smart_group_qq import build_handler
    from smart_group_qq.store import Store

    platform = PlatformConfig(enabled=True, extra={**raw["platforms"]["qqbot"]["extra"],
                              "app_id": "offline-synthetic-app", "client_secret": "offline-synthetic-not-a-key"})
    config = GatewayConfig(platforms={Platform.QQBOT: platform}, sessions_dir=home / "sessions",
                           group_sessions_per_user=False)
    runner, adapter = GatewayRunner(config), QQAdapter(platform)
    runner.adapters[Platform.QQBOT] = adapter
    source = adapter.build_source(chat_id="offline-route-group", user_id="offline-route-member", chat_type="group")
    other = adapter.build_source(chat_id="offline-other-group", user_id="offline-other-member", chat_type="group")
    key = runner._session_key_for_source(source)
    entry = runner.session_store.get_or_create_session(source)
    other_id = runner.session_store.get_or_create_session(other).session_id
    db = runner.session_store._db
    require(db is not None, "Real isolated SessionDB is unavailable")
    if db.get_session(entry.session_id) is None:
        db.create_session(entry.session_id, source="qqbot", model=routing.FAST_MODEL, session_key=key,
                          user_id=source.user_id, chat_id=source.chat_id, chat_type="group")
    db.append_message(entry.session_id, "user", "synthetic durable user message")
    db.append_message(entry.session_id, "assistant", "synthetic durable assistant reply")
    history = deepcopy(runner.session_store.load_transcript(entry.session_id))
    require(len(history) == 2, "Synthetic native transcript was not persisted")
    default = routing.session_route_status(runner, source)
    require(default["mode"] == "auto" and default["model"] == routing.FAST_MODEL
            and default["reasoning"] == "high" and not default["reasoning_manual"], "New session route is not Fast/high")

    async def native_command(text, method):
        result = method(MessageEvent(text=text, source=source, message_id="offline-command"))
        if inspect.isawaitable(result):
            result = await result
        require(result is not None, "Native session command did not execute")

    await native_command(f"/model {routing.FLASH_MODEL} --session", runner._handle_model_command)
    await native_command("/reasoning low", runner._handle_reasoning_command)
    manual = routing.session_route_status(runner, source)
    require(manual["mode"] == "manual" and manual["model"] == routing.FLASH_MODEL
            and manual["reasoning"] == "low" and manual["reasoning_manual"], "Native manual settings did not win")

    settings = raw["plugins"]["entries"]["smart_group_qq"]["settings"]
    context = SimpleNamespace(get_config=lambda name, default=None: settings.get(name, default))
    memory = Store(":memory:", member_secret="offline-synthetic-member-secret")
    handler = build_handler(context, memory)
    sent = []

    async def send(chat_id, content, reply_to=None, **kwargs):
        sent.append(content)
        return SendResult(success=True, message_id=f"offline-ack-{len(sent)}")

    adapter.send = send
    try:
        for index, command in enumerate(("/model auto", "/自动模型")):
            await native_command(f"/model {routing.FLASH_MODEL} --session", runner._handle_model_command)
            runner._session_state(key).conversation.one_turn_restore = {"had_override": True, "override": {"model": routing.FLASH_MODEL}}
            result = await handler(MessageEvent(text=command, source=source, message_id=f"offline-auto-{index}"), runner)
            if handler.local_replies.tasks:
                await asyncio.gather(*tuple(handler.local_replies.tasks.values()))
            require(result["action"] == "skip", "QQ auto command escaped the predispatch control")
            status = routing.session_route_status(runner, source)
            require(status["mode"] == "auto" and status["model"] == routing.FAST_MODEL
                    and status["reasoning"] == "low" and status["reasoning_manual"], "Auto changed reasoning or retained model pin")
            require(runner._session_state(key).conversation.one_turn_restore is None, "Auto left a one-turn pin resurrection")
            require(runner.session_store.get_or_create_session(source).session_id == entry.session_id,
                    "Auto rotated the native conversation")
            require(runner.session_store.load_transcript(entry.session_id) == history, "Auto rewrote native transcript")
            require(runner.session_store.get_or_create_session(other).session_id == other_id, "Auto crossed group boundary")
            require(SessionStore(config.sessions_dir, config).get_model_override(key) is None, "Cleared model pin did not persist")
        require(len(sent) == 2, "Auto control did not send exactly one local receipt per command")
        verify_native_hooks(routing, runner, source, key)
    finally:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        memory.close()
    print("QQ_NATIVE_MODEL_ROUTING=passed DEFAULT=Fast/high BACKGROUND=Flash/low "
          "MANUAL=native AUTO=plugin+native-store HISTORY=preserved FALLBACK=once-before-progress "
          "THINKING=blocks-fallback TOOLS=no-replay NETWORK=blocked")


def main():
    with tempfile.TemporaryDirectory(prefix="qq-native-model-route-") as tmp:
        with patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")), \
                patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden")):
            asyncio.run(verify(Path(tmp)))


if __name__ == "__main__":
    main()
