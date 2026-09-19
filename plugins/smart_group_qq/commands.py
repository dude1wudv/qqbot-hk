"""QQ group command parsing and deterministic local responses."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

_MENTION = re.compile(r"(?:<@!?[^>]+>|^@\S+)\s*")
_COMMAND = re.compile(r"^[／/]([A-Za-z]+|值日表)(?:\s+.*)?$")
_PROFILE_COMMAND = re.compile(
    r"^[／/](我的记忆|记住我|纠正记忆|忘记我|停止记忆)(?:\s*[:：]?\s*(.*))?$",
    re.DOTALL,
)
_MODEL_ALIAS_COMMAND = re.compile(r"^[／/]\s*(gemini|deepseek)\s*$", re.IGNORECASE)
_MODEL_ALIASES = {
    "gemini": "gemini-3.8-flash-high",
    "deepseek": "deepseek/deepseek-v4.1-flash",
}
_REASONING_ALIAS_COMMAND = re.compile(r"^[／/]\s*(low|medium|high|max)\s*$", re.IGNORECASE)
_ALIASES = {"clear": "reset", "值日表": "duty_roster"}
_SUPPORTED = frozenset({"help", "reset", "status", "summary", "rules", "duty_roster"})


@dataclass(frozen=True)
class Command:
    name: str


@dataclass(frozen=True)
class ProfileCommand:
    action: str
    argument: str = ""


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


def parse_profile_command(value: Any) -> ProfileCommand | None:
    text = clean_text(value).replace("／", "/", 1)
    match = _PROFILE_COMMAND.fullmatch(text)
    if not match:
        return None
    actions = {
        "我的记忆": "show",
        "记住我": "remember",
        "纠正记忆": "correct",
        "忘记我": "forget",
        "停止记忆": "opt_out",
    }
    return ProfileCommand(actions[match.group(1)], str(match.group(2) or "").strip())


def model_alias_rewrite(value: Any) -> str | None:
    """Translate friendly QQ aliases into Hermes' session model command."""

    match = _MODEL_ALIAS_COMMAND.fullmatch(clean_text(value))
    if not match:
        return None
    return f"/model {_MODEL_ALIASES[match.group(1).lower()]} --session"


def reasoning_alias_rewrite(value: Any) -> str | None:
    """Translate friendly QQ aliases into Hermes' session reasoning command."""

    match = _REASONING_ALIAS_COMMAND.fullmatch(clean_text(value))
    if not match:
        return None
    return f"/reasoning {match.group(1).lower()} --session"


def help_text() -> str:
    return (
        "【QQ 助手】\n"
        "/help 功能说明\n/reset、/clear 或 /new 重置当前会话\n"
        "/compress 立即重试上下文压缩（上下文过大时）\n"
        "/status 运行状态\n/summary 近期互动摘要\n/rules 已启用规则\n"
        "/gemini 切换当前会话到 Gemini\n/deepseek 切换当前会话到 DeepSeek\n"
        "/low /medium /high /max 切换当前会话推理强度\n"
        "/kb 知识库\n/我的记忆 查看个人记忆\n/记住我：内容 保存或更新个人信息\n"
        "/纠正记忆：字段=新内容 以本人确认更正旧记忆\n"
        "/忘记我 删除个人记忆\n/停止记忆 禁止继续建立个人记忆\n"
        "/值日表 查看本周轮值安排（仅群聊）"
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


__all__ = [
    "Command", "ProfileCommand", "clean_text", "parse_command", "parse_profile_command",
    "model_alias_rewrite", "reasoning_alias_rewrite",
    "help_text", "status_text", "rules_text",
]
