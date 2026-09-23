"""QQ group command parsing and deterministic local responses."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

_MENTION = re.compile(r"^(?:<@!?[^>]+>|@[^\s/／]+)\s*")
_COMMAND = re.compile(r"^[／/]([A-Za-z]+|值日表)(?:\s+.*)?$")
_PROFILE_COMMAND = re.compile(
    r"^/(我的记忆|记住我|纠正记忆|忘记我|停止记忆)(?:\s+(.*))?$",
    re.DOTALL,
)
_MODEL_ALIAS_COMMAND = re.compile(
    r"^[／/]\s*(deepseek)\s*$", re.IGNORECASE
)
_MODEL_ALIASES = {
    "deepseek": "deepseek/deepseek-v4.1-flash",
}
_REASONING_ALIAS_COMMAND = re.compile(
    r"^[／/]\s*(low|medium|high|xhigh|max)\s*$", re.IGNORECASE
)
_ALIASES = {"clear": "reset", "值日表": "duty_roster"}
_SUPPORTED = frozenset({"help", "reset", "status", "summary", "rules", "duty_roster"})
_NATIVE_GROUP_PASSTHROUGH = frozenset({"commands", "compress", "new"})


@dataclass(frozen=True)
class Command:
    name: str


@dataclass(frozen=True)
class ProfileCommand:
    action: str
    argument: str = ""


def clean_text(value: Any) -> str:
    text = re.sub(r"^[\s\u200b\ufeff]+", "", str(value or "")).rstrip()
    previous = None
    while text != previous:
        previous = text
        text = re.sub(r"^[\s\u200b\ufeff]+", "", _MENTION.sub("", text, count=1)).rstrip()
    # QQ may append the bot mention after a slash command.
    text = re.sub(r"\s*<@!?[^>]+>\s*$", "", text).strip()
    return text


def normalize_command_text(value: Any) -> str:
    """Normalize only the command token; preserve user payload bytes and casing."""
    text = clean_text(value)
    if text in {"/", "／"}:
        return "/help"
    match = re.match(r"^[／/]\s*([^\s:：=＝]+)(?:\s*[:：=＝]\s*|\s+)?(.*)$", text, re.S)
    if not match:
        return text
    head = unicodedata.normalize("NFKC", match[1]).casefold()
    argument = match[2].strip()
    if not argument:
        head = head.rstrip("。！!？?")
    return "/" + head + (" " + argument if argument else "")


def parse_command(value: Any) -> Command | None:
    text = normalize_command_text(value)
    match = _COMMAND.fullmatch(text)
    if not match:
        return None
    name = _ALIASES.get(match.group(1).lower(), match.group(1).lower())
    return Command(name) if name in _SUPPORTED else None


def parse_profile_command(value: Any) -> ProfileCommand | None:
    text = normalize_command_text(value)
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
    argument = str(match.group(2) or "").strip()
    if match.group(1) in {"我的记忆", "忘记我", "停止记忆"} and argument:
        return None
    return ProfileCommand(actions[match.group(1)], argument)


def model_alias_rewrite(value: Any) -> str | None:
    """Translate friendly QQ aliases into Hermes' session model command."""

    match = _MODEL_ALIAS_COMMAND.fullmatch(normalize_command_text(value))
    if not match:
        return None
    return f"/model {_MODEL_ALIASES[match.group(1).lower()]} --session"


def reasoning_alias_rewrite(value: Any) -> str | None:
    """Translate friendly QQ aliases into Hermes' current-session reasoning command."""
    match = _REASONING_ALIAS_COMMAND.fullmatch(normalize_command_text(value))
    if not match:
        return None
    # Hermes applies /reasoning to the active conversation; its pinned command
    # parser does not accept the model command's ``--session`` option here.
    return f"/reasoning {match.group(1).lower()}"


def native_group_command_rewrite(value: Any) -> str | None:
    """Return the small, explicitly safe set of native commands exposed in QQ groups."""

    text = normalize_command_text(value)
    if not text.startswith("/"):
        return None
    command = text[1:].partition(" ")[0].lower()
    return text if command in _NATIVE_GROUP_PASSTHROUGH else None


def help_text() -> str:
    return (
        "【小栖 · 常驻 AI 角色】\n"
        "可以直接说：切到 DeepSeek、推理调高、安静10分钟、给宠物喂点东西。\n"
        "完成目标可填 ID 或名称；指代不清时会请你补充。\n"
        "/配置 查看配置写法；/model、/reasoning 查看当前会话设置\n"
        "/角色 角色状态与自然语言控制\n/经历 共同经历\n/梗簿、/记梗 内容、/忘梗 ID\n"
        "/目标 内容、/完成目标 ID、/取消目标 ID\n/探索 查看有无新发现\n"
        "/宠物、/喂食、/摸摸、/宠物取名 名字\n/剧情 设定、/投票 1或2、/结束剧情\n/表情 心情\n"
        "/help 功能说明\n/reset、/clear 或 /new 重置当前会话\n"
        "/compress 立即重试上下文压缩（上下文过大时）\n/approve /cancel /deny 仅私聊可处理工具确认提示\n"
        "/status 运行状态\n/summary 近期互动摘要\n/rules 已启用规则\n"
        "/deepseek 切回当前会话的 DeepSeek\n"
        "/low /medium /high /xhigh /max 切换当前会话推理强度（DeepSeek 的 xhigh 实际按 max 请求）\n"
        "/kb 知识库\n/我的记忆 查看个人记忆\n/记住我：内容 保存或更新个人信息\n"
        "/纠正记忆：字段=新内容 以本人确认更正旧记忆\n"
        "/忘记我 删除个人记忆\n/停止记忆 禁止继续建立个人记忆\n"
        "/值日表 查看本周轮值安排（仅群聊）"
    )


def status_text(*, model: str, reasoning: str, allowlisted: bool = True) -> str:
    return (
        "【运行状态】\n"
        f"配置默认模型：{model or '未配置'}\n配置默认推理：{reasoning or '默认'}\n"
        "当前会话可能已有覆写，发送 /model 或 /reasoning 查看原生状态。\n"
        f"本群白名单：{'已启用' if allowlisted else '未启用'}"
    )


def rules_text(settings: Mapping[str, Any]) -> str:
    keyword_ids = [
        str(item.get("id"))
        for item in settings.get("keyword_replies", ())
        if isinstance(item, Mapping) and item.get("enabled", True) and item.get("id")
    ]
    static = settings.get("moderation", {}).get("static_rules", ())
    moderation_ids = [
        str(item.get("id"))
        for item in static
        if isinstance(item, Mapping) and item.get("enabled", True) and item.get("id")
    ]
    lines = ["【本群规则】"]
    lines.append("关键词：" + ("、".join(keyword_ids) if keyword_ids else "无"))
    lines.append("审核：" + ("、".join(moderation_ids) if moderation_ids else "无"))
    return "\n".join(lines)


__all__ = [
    "Command",
    "ProfileCommand",
    "clean_text",
    "parse_command",
    "parse_profile_command",
    "model_alias_rewrite",
    "reasoning_alias_rewrite",
    "native_group_command_rewrite",
    "help_text",
    "status_text",
    "rules_text",
]
