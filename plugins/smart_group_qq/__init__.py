"""Hermes-native QQ group governance plugin."""
from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Mapping

from .commands import clean_text, help_text, parse_command, rules_text, status_text
from .formatter import format_for_qq, split_message
from .memory import GroupMemory, normalize_member_message
from .policy import PolicyEngine
from .store import Store

logger = logging.getLogger(__name__)
PLUGIN_ID = "smart_group_qq"


def _platform_name(source: Any) -> str:
    platform = getattr(source, "platform", "")
    return str(getattr(platform, "value", platform)).lower()


def _adapter(gateway: Any, source: Any) -> Any:
    adapters = getattr(gateway, "adapters", {}) or {}
    return adapters.get(getattr(source, "platform", None)) or adapters.get("qqbot")

def _configure_adapter(adapter: Any) -> None:
    if adapter is None or getattr(adapter, "_smart_group_qq_formatting", False):
        return
    markdown = bool(getattr(adapter, "_markdown_support", False))
    adapter.format_message = lambda content: format_for_qq(content, markdown_support=markdown)
    adapter.MAX_MESSAGE_LENGTH = 1500
    adapter._smart_group_qq_formatting = True


def _schedule_reply(adapter: Any, chat_id: str, reply_to: str | None, text: str, store: Store, claim: tuple[str, str, str]) -> bool:
    if adapter is None or not callable(getattr(adapter, "send", None)):
        return False

    async def send_all() -> bool:
        for chunk in split_message(format_for_qq(text))[:5]:
            result = await adapter.send(chat_id, chunk, reply_to=reply_to)
            if not bool(getattr(result, "success", False)):
                return False
        return True

    try:
        task = asyncio.create_task(send_all())
    except RuntimeError:
        store.finish_claim(*claim, success=False)
        return False

    def finished(done: asyncio.Task) -> None:
        try:
            store.finish_claim(*claim, success=bool(done.result()))
        except Exception:
            store.finish_claim(*claim, success=False)
            logger.warning("smart_group_qq reply delivery failed", exc_info=True)

    task.add_done_callback(finished)
    return True


def _reset_gateway_session(gateway: Any, session_store: Any, source: Any) -> None:
    if session_store is None:
        return
    key_factory = getattr(gateway, "_session_key_for_source", None)
    reset = getattr(session_store, "reset_session", None)
    if not callable(key_factory) or not callable(reset):
        return
    result = reset(key_factory(source), display_name=getattr(source, "chat_name", None))
    if inspect.isawaitable(result):
        asyncio.create_task(result)


def build_handler(ctx: Any, store: Store):
    settings = {
        "keyword_replies": ctx.get_config("keyword_replies", []),
        "moderation": ctx.get_config("moderation", {}),
        "memory": ctx.get_config("memory", {}),
    }
    policy = PolicyEngine(settings, logger=logger)
    memory_cfg = settings.get("memory") if isinstance(settings.get("memory"), Mapping) else {}
    memory = GroupMemory(
        store,
        window_size=int(memory_cfg.get("window_size", 20)),
        idle_seconds=int(memory_cfg.get("idle_seconds", 1800)),
        summary_chars=int(memory_cfg.get("summary_chars", 200)),
    )

    def handle(event: Any = None, gateway: Any = None, session_store: Any = None, **_: Any):
        source = getattr(event, "source", None)
        if source is None or _platform_name(source) != "qqbot" or getattr(source, "chat_type", "") != "group":
            return {"action": "allow"}
        adapter = _adapter(gateway, source)
        _configure_adapter(adapter)
        group_id = str(getattr(source, "chat_id", "") or "")
        member_id = str(getattr(source, "user_id", "") or "")
        message_id = str(getattr(event, "message_id", "") or "")
        text = clean_text(getattr(event, "text", ""))
        if not group_id or not text:
            return {"action": "allow"}

        command = parse_command(text)
        reply = None
        claim_action = ""
        if command:
            claim_action = "command:" + command.name
            if command.name == "help":
                reply = help_text()
            elif command.name == "reset":
                memory.reset(group_id)
                _reset_gateway_session(gateway, session_store, source)
                reply = "本群机器人上下文已重置。"
            elif command.name == "status":
                reply = status_text(
                    model=str(ctx.get_config("status_model", "deepseek-v4-flash-0731")),
                    reasoning=str(ctx.get_config("status_reasoning", "low")),
                )
            elif command.name == "summary":
                reply = "【本群互动摘要】\n" + memory.summary(group_id)
            elif command.name == "rules":
                reply = rules_text(settings)
        else:
            decision = policy.static(text)
            if decision.blocked:
                claim_action = "moderation:" + str(decision.rule_id or "static")
                reply = decision.notice or "此消息未能通过群聊安全审核。"
            else:
                decision = policy.keyword(text)
                if decision.replied:
                    claim_action = "keyword:" + str(decision.rule_id or "reply")
                    reply = decision.notice

        if reply is not None:
            claim = ("qqbot", message_id, claim_action)
            if not store.claim_message(*claim):
                return {"action": "skip", "reason": "duplicate"}
            scheduled = _schedule_reply(adapter, group_id, message_id or None, reply, store, claim)
            store.record_audit(
                "rule_dispatch", chat_id=group_id, message_id=message_id, action=claim_action,
                rule_id=claim_action.partition(":")[2], source="command" if command else "policy",
            )
            if not scheduled:
                # Deterministic keyword replies fail open; moderation remains
                # blocked so unsafe text never reaches the agent.
                store.finish_claim(*claim, success=False)
                if not (not command and policy.static(text).blocked):
                    return {"action": "allow"}
            return {"action": "skip", "reason": "command_or_policy_handled"}

        memory.record(group_id, member_id, text, message_id or None)
        normalized = normalize_member_message(member_id, text)
        background = memory.background(group_id)
        if background:
            normalized = f"[群背景摘要]: {background}\n{normalized}"
        return {"action": "rewrite", "text": normalized}

    return handle


def register(ctx: Any) -> None:
    try:
        from plugins.plugin_storage import plugin_db
        store = Store(plugin_db(ctx.plugin_id))
    except Exception:
        logger.exception("smart_group_qq storage initialization failed")
        return
    ctx.register_hook("pre_gateway_dispatch", build_handler(ctx, store))


__all__ = ["PLUGIN_ID", "build_handler", "register"]
