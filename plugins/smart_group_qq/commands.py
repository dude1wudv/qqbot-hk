"""QQ group command parsing and deterministic local responses."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

_MENTION = re.compile(r"^(?:<@!?[^>]+>|@[^\s/／]+)\s*")
_COMMAND = re.compile(r"^[／/]([A-Za-z]+|值日表)(?:\s+(.*))?$", re.DOTALL)
_PROFILE_COMMAND = re.compile(
    r"^/(我的记忆|记住我|纠正记忆|忘记我|停止记忆)(?:\s+(.*))?$",
    re.DOTALL,
)
_MODEL_ALIAS_COMMAND = re.compile(
    r"^[／/]\s*(deepseek|deepseekfast|自动模型)\s*$", re.IGNORECASE
)
_MODEL_ALIASES = {
    "deepseek": "deepseek/deepseek-v4.1-flash",
    "deepseekfast": "deepseek/deepseek-v4.1-flash-fast",
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
    argument: str = ""


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
    return Command(name, str(match.group(2) or "").strip() if name == "summary" else "") if name in _SUPPORTED else None


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
    if match.group(1) in {"忘记我", "停止记忆"} and argument:
        return None
    return ProfileCommand(actions[match.group(1)], argument)


def model_alias_rewrite(value: Any) -> str | None:
    """Translate friendly QQ aliases into Hermes' session model command."""

    match = _MODEL_ALIAS_COMMAND.fullmatch(normalize_command_text(value))
    if not match:
        return None
    if match.group(1) == "自动模型":
        return "/model auto"
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
        "/表达库、/学表达 场景 | 短表达、/忘表达 ID（仅相关场景使用）\n"
        "/待续 话题内容、/话题、/结束话题 ID、/忘话题 ID（不定时催问）\n"
        "/目标 内容、/完成目标 ID、/取消目标 ID\n"
        "/群投票 创建 题目 | 选项A | 选项B；/群投票 查看或选择/结束投票\n"
        "/宠物、/喂食、/摸摸、/宠物取名 名字\n/剧情 设定、/投票 1或2、/结束剧情\n/表情库 查看本群分类；/表情 心情 发一张\n"
        "/help 功能说明\n/reset、/clear 或 /new 重置当前会话\n"
        "/compress 立即重试上下文压缩（上下文过大时）\n工具执行无需额外审批。\n"
        "/status 运行状态\n/summary 查看或更新近期摘要（无新消息时用缓存）\n"
        "/summary status 查看整理进度；/summary retry 显式重试失败任务\n/rules 已启用规则\n"
        "/deepseekfast 使用 Fast；/deepseek 使用原 Flash；/model auto 或 /自动模型 恢复自动路由\n"
        "/low /medium /high /xhigh /max 切换当前会话推理强度（DeepSeek 的 xhigh 实际按 max 请求）\n"
        "/kb 知识库\n/我的记忆 [关键词] 查看或搜索个人记忆，例如 /我的记忆 项目\n/记住我：内容 保存或更新个人信息\n"
        "/纠正记忆：字段=新内容 以本人确认更正旧记忆\n"
        "/忘记我 删除个人记忆\n/停止记忆 禁止继续建立个人记忆\n"
        "可以直接说：回答短一点、别叫我某称呼、对我别用某个梗。\n"
        "/值日表 查看本周轮值安排（仅群聊）"
    )


def status_text(*, model: str, reasoning: str, allowlisted: bool = True,
                mode: str = "自动", reasoning_manual: bool = False) -> str:
    return (
        "【运行状态】\n"
        f"模型模式：{mode}\n主回复：{model or '未配置'}\n"
        f"主回复推理：{reasoning or '默认'}{'（手动）' if reasoning_manual else ''}\n"
        "后台任务：deepseek/deepseek-v4.1-flash / low\n"
        "自动模式可在首个回复或工具前回退一次；/model auto 恢复自动模型并保留聊天历史。\n"
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
