"""QQ group command parsing and deterministic local responses."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

_MENTION = re.compile(r"(?:<@!?[^>]+>|^@\S+)\s*")
_COMMAND = re.compile(r"^[／/]([A-Za-z]+|值日表)(?:\s+.*)?$")
_ALIASES = {"clear": "reset", "值日表": "duty_roster"}
_SUPPORTED = frozenset({"help", "reset", "status", "summary", "rules", "duty_roster"})


@dataclass(frozen=True)
class Command:
    name: str


def clean_text(value: Any) -> str:
    text = str(value or "").strip()
    previous = None
    while text != previous:
        previous = text
        text = _MENTION.sub("", text, count=1).strip()
    # QQ may append the bot mention after a slash command.
    text = re.sub(r"\s*<@!?[^>]+>\s*$", "", text).strip()
    return text


def parse_command(value: Any) -> Command | None:
    text = clean_text(value).replace("／", "/", 1)
    match = _COMMAND.fullmatch(text)
    if not match:
        return None
    name = _ALIASES.get(match.group(1).lower(), match.group(1).lower())
    return Command(name) if name in _SUPPORTED else None


def help_text() -> str:
    return (
        "【群聊助手】\n"
        "/help 功能说明\n/reset 或 /clear 重置本群上下文\n"
        "/status 运行状态\n/summary 本群近期互动摘要\n/rules 已启用规则\n"
        "/值日表 查看本周轮值安排"
    )


def status_text(*, model: str, reasoning: str, allowlisted: bool = True) -> str:
    return (
        "【运行状态】\n"
        f"模型：{model or '未配置'}\n推理：{reasoning or '默认'}\n"
        f"本群白名单：{'已启用' if allowlisted else '未启用'}"
    )


def rules_text(settings: Mapping[str, Any]) -> str:
    keyword_ids = [
        str(item.get("id")) for item in settings.get("keyword_replies", ())
        if isinstance(item, Mapping) and item.get("enabled", True) and item.get("id")
    ]
    static = settings.get("moderation", {}).get("static_rules", ())
    moderation_ids = [
        str(item.get("id")) for item in static
        if isinstance(item, Mapping) and item.get("enabled", True) and item.get("id")
    ]
    lines = ["【本群规则】"]
    lines.append("关键词：" + ("、".join(keyword_ids) if keyword_ids else "无"))
    lines.append("审核：" + ("、".join(moderation_ids) if moderation_ids else "无"))
    return "\n".join(lines)


__all__ = ["Command", "clean_text", "parse_command", "help_text", "status_text", "rules_text"]
