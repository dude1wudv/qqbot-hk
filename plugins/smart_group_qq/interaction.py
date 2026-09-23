"""Conservative natural-language intents sharing the existing command executors.

Only complete requests match. Quoted examples, hypothetical questions and multiple
operations remain chat; unknown explicit configuration receives a useful error.
No model output can invent a configuration key or bypass command permissions.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Sequence

from .commands import clean_text, normalize_command_text

MODELS = {
    "deepseek": "deepseek",
    "deepseek/deepseek-v4.1-flash": "deepseek",
    "deepseek-v4.1-flash": "deepseek",
    "深度求索": "deepseek",
}
EFFORTS = {
    "low": "low",
    "低": "low",
    "低档": "low",
    "medium": "medium",
    "中": "medium",
    "中等": "medium",
    "high": "high",
    "高": "high",
    "高档": "high",
    "xhigh": "xhigh",
    "超高": "xhigh",
    "max": "max",
    "最高": "max",
    "最大": "max",
}
NO_ARGS = {
    "help",
    "reset",
    "clear",
    "status",
    "summary",
    "rules",
    "值日表",
    "角色",
    "经历",
    "梗簿",
    "探索",
    "宠物",
    "喂食",
    "摸摸",
    "结束剧情",
    "我的记忆",
    "忘记我",
    "停止记忆",
}
REQUIRES_ARG = {
    "记梗",
    "忘梗",
    "完成目标",
    "取消目标",
    "宠物取名",
    "投票",
    "记住我",
    "纠正记忆",
    "安静",
}
CONFIG_ERROR = "当前只使用 DeepSeek V4.1 Flash。推理强度支持 low、medium、high、xhigh、max（低、中、高、超高、最高）；DeepSeek 的 xhigh 实际按 max 请求。例如：切换到 DeepSeek、推理强度调到低，或 /配置 模型 DeepSeek。配置只作用于当前会话。"


@dataclass(frozen=True)
class Interaction:
    text: str
    natural: bool = False
    error: str = ""
    native: bool = False


def _key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def strip_address(text: str, wake_words: Sequence[str]) -> str:
    value = clean_text(text)
    for word in sorted(wake_words, key=len, reverse=True):
        if value.casefold().startswith(word.casefold()):
            rest = value[len(word) :]
            # ASCII names need a boundary: "botany" is not a call to "bot".
            if word.isascii() and rest and rest[0].isalnum():
                continue
            return rest.lstrip(" ，,：:、")
    return value


def _config(kind: str, arg: str, *, natural=False, private=False) -> Interaction:
    value = re.sub(r"\s+--session\s*$", "", arg, flags=re.I).strip()
    if not value:
        return Interaction(
            "/model" if kind == "model" else "/reasoning", natural, native=True
        )
    mapping = MODELS if kind == "model" else EFFORTS
    alias = mapping.get(_key(value))
    if alias:
        return Interaction("/" + alias, natural)
    if private and not natural and kind in ("model", "reasoning"):
        # Explicit private native syntax (custom providers etc.) stays Hermes-owned.
        return Interaction("/" + kind + " " + arg, native=True)
    return Interaction("", natural, CONFIG_ERROR)


def resolve_interaction(
    text: str,
    *,
    wake_words: Sequence[str] = (),
    pet_name="小电团",
    pet_focused=False,
    private=False,
) -> Interaction | None:
    raw = strip_address(text, wake_words)
    value = normalize_command_text(raw)
    if value.startswith("/"):
        head, _, arg = value[1:].partition(" ")
        if head in ("配置", "设置", "config"):
            if not arg:
                return Interaction("/配置")
            match = re.fullmatch(
                r"(模型|model|推理强度|推理|reasoning)(?:\s*[:：=＝]\s*|\s+)?(.*)",
                arg,
                re.I,
            )
            if not match:
                return Interaction("", error=CONFIG_ERROR)
            return _config(
                "model" if _key(match[1]) in ("模型", "model") else "reasoning",
                match[2],
            )
        if head in ("模型", "model", "推理", "推理强度", "reasoning"):
            return _config(
                "model" if head in ("模型", "model") else "reasoning",
                arg,
                private=private and head in ("model", "reasoning"),
            )
        if head in MODELS or head in EFFORTS:
            if arg:
                return Interaction(
                    "", error="这条快捷命令不接受参数，请单独发送，或使用 /配置。"
                )
            return Interaction("/" + (MODELS.get(head) or EFFORTS[head]))
        if head in NO_ARGS and arg:
            return Interaction(
                "",
                error=f"/{head} 不接受额外内容；如要讨论命令用法，请去掉开头的斜杠。",
            )
        if head in REQUIRES_ARG and not arg:
            return Interaction(
                "", error=f"请补充 /{head} 后面的内容。可发送 /help 查看示例。"
            )
        return Interaction(value)

    # Matching a short whole utterance prevents instruction text inside messages
    # or knowledge snippets from becoming a configuration operation.
    if (
        not raw
        or "\n" in raw
        or len(raw) > 1500
        or raw.startswith(("“", "「", "『", '"', "'", "`"))
    ):
        return None
    value = re.sub(
        r"^(?:请帮我|麻烦你帮我|麻烦帮我|能不能帮我|可以帮我|请你|请|麻烦你|麻烦|帮我)\s*",
        "",
        raw,
    )
    value = re.sub(r"(?:好吗|好不好)?[。！!？?]*$", "", value).strip()
    fixed = {
        "安静一会儿": "安静一会儿",
        "安静一下": "安静一下",
        "先别插话": "安静一会儿",
        "先别插话，我俩讨论一下": "安静一会儿",
        "先别插话,我俩讨论一下": "安静一会儿",
        "活跃一点": "活跃一点",
        "多聊一点": "活跃一点",
        "自由聊天": "自由聊天",
        "恢复聊天": "恢复聊天",
        "可以说话了": "恢复聊天",
        "少说一点": "少说一点",
        "停止主动分享": "停止主动分享",
        "别主动推送了": "停止主动分享",
        "关闭主动分享": "停止主动分享",
        "开启主动分享": "开启主动分享",
        "恢复主动分享": "开启主动分享",
        "看看宠物": "/宠物",
        "看看小电团": "/宠物",
        "查看我的目标": "/目标",
        "看看我的目标": "/目标",
        "看看目标进度": "/目标",
        "看看我们之前聊过什么": "/经历",
        "看看梗簿": "/梗簿",
        "看看剧情进度": "/剧情",
        "结束这段剧情": "/结束剧情",
        "结束剧情": "/结束剧情",
        "查看我的记忆": "/我的记忆",
        "看看你记住了我什么": "/我的记忆",
        "停止记住我": "/停止记忆",
        "别再记住我的信息": "/停止记忆",
        "删除我的记忆": "/忘记我",
        "忘掉我的个人记忆": "/忘记我",
        "查看配置": "/配置",
        "看看配置": "/配置",
        "查看当前模型": "/model",
        "现在用的什么模型": "/model",
        "当前推理强度是多少": "/reasoning",
        "看看你有什么新发现": "/探索",
        "去看看有没有新发现": "/探索",
    }
    if value in fixed:
        canonical = fixed[value]
        return Interaction(
            canonical, True, native=canonical in ("/model", "/reasoning")
        )
    match = re.fullmatch(
        r"(?:让你|请你)?(?:安静|别插话|暂停插话)\s*(半小时|一小时|\d+\s*(?:分钟|小时))",
        value,
    )
    if match:
        return Interaction("/安静 " + match[1], True)
    match = re.fullmatch(
        r"(?:把)?(?:当前)?(?:模型)?\s*(?:切换到|切到|切换为|切换成|换成|换到|改成|改为|用)\s*([\w./-]+?)(?:吧)?",
        value,
        re.I,
    )
    if match:
        result = _config("model", match[1], natural=True)
        return result if not result.error or "模型" in value else None
    match = re.fullmatch(
        r"(?:把)?(?:推理强度|推理档位|思考强度|思考档位|推理)\s*(?:设置为|设为|调到|调成|改成|改为|切到|用|调)\s*(low|medium|high|max|低档|低|中等|中|高档|高|最高|最大)(?:吧)?",
        value,
        re.I,
    )
    if match:
        return _config("reasoning", match[1], natural=True)
    pet = re.escape(pet_name)
    if re.fullmatch(
        r"(?:给)?(?:宠物|小电团|"
        + pet
        + r")\s*(?:喂食|喂点(?:东西|吃的)|喂点食物|喂点东西吃)(?:吧)?",
        value,
    ):
        return Interaction("/喂食", True)
    if re.fullmatch(
        r"(?:摸摸|摸一下|摸一摸)(?:宠物|小电团|" + pet + r")(?:吧)?", value
    ):
        return Interaction("/摸摸", True)
    match = re.fullmatch(
        r"(?:把|给)?(?:宠物|小电团|"
        + pet
        + r")(?:改名为|改名叫|取名为|取名叫|起名叫|改名成)\s*(.+?)(?:吧)?",
        value,
    )
    if match:
        return Interaction("/宠物取名 " + _unquote(match[1]), True)
    match = re.fullmatch(r"(?:以后)?(?:叫它|给它取名|把它改名为)\s*(.+?)(?:吧)?", value)
    if match:
        if not pet_focused:
            return Interaction(
                "", True, "你是想给宠物改名吗？请说“把宠物改名为 名字”，避免改错对象。"
            )
        return Interaction("/宠物取名 " + _unquote(match[1]), True)
    if re.fullmatch(r"(?:给它喂点(?:东西|吃的)|喂它|摸摸它)(?:吧)?", value):
        if not pet_focused:
            return Interaction(
                "", True, "你指的是宠物吗？可以说“给宠物喂点东西”或“摸摸宠物”。"
            )
        return Interaction("/摸摸" if value.startswith("摸") else "/喂食", True)
    patterns = [
        (r"(?:记住这个梗|把这个梗记下来|记个梗)\s*[:：]\s*(.+)", "/记梗 "),
        (r"(?:新增目标|添加目标|记个目标|加个目标)(?:\s*[:：]\s*|\s+)(.+)", "/目标 "),
        (r"(?:记住我|记住我的信息)\s*[:：]\s*(.+)", "/记住我 "),
        (r"(?:纠正记忆|更正我的记忆)\s*[:：]\s*(.+)", "/纠正记忆 "),
        (r"(?:开始一段剧情|开始冒险|开个剧情)\s*[:：]\s*(.+)", "/剧情 "),
        (r"(?:完成目标|把目标标记为完成)(?:\s*[:：]\s*|\s+)(.+)", "/完成目标 "),
        (r"(?:取消目标|删掉目标)(?:\s*[:：]\s*|\s+)(.+)", "/取消目标 "),
        (r"(?:忘掉这个梗|删除梗)(?:\s*[:：]\s*|\s+)(.+)", "/忘梗 "),
    ]
    for pattern, prefix in patterns:
        match = re.fullmatch(pattern, value)
        if match:
            return Interaction(prefix + match[1].strip(), True)
    match = re.fullmatch(
        r"(?:把)?(.+?)(?:这个|那个)?(?:目标|任务)(?:已经)?(?:搞定了|做完了|完成了|标记为完成)",
        value,
    )
    if match:
        return Interaction("/完成目标 " + match[1].strip(), True)
    if value in ("这个梗你可得记着", "记住这个梗", "把这个梗记下来"):
        return Interaction(
            "",
            True,
            "把要记的梗内容发给我吧，例如“记住这个梗：电子土豆”。我不会猜测你指的是哪条消息。",
        )
    match = re.fullmatch(
        r"(?:我)?(?:选|投|投票给)(?:第)?([12一二])(?:个|项|号)?", value
    )
    if match:
        return Interaction(
            "/投票 " + {"一": "1", "二": "2"}.get(match[1], match[1]), True
        )
    match = re.fullmatch(
        r"(?:发|来)(?:个|一个)(开心|疑惑|无语|鼓励|晚安)(?:的)?表情(?:包)?", value
    )
    if match:
        return Interaction("/表情 " + match[1], True)
    return None


def _unquote(value: str) -> str:
    value = value.strip()
    for left, right in (("“", "”"), ("「", "」"), ('"', '"'), ("'", "'")):
        if value.startswith(left) and value.endswith(right):
            return value[1:-1]
    return value
