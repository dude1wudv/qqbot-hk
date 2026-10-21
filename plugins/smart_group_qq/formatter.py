"""QQ-safe plain-text formatting and passive-reply chunking."""
from __future__ import annotations

import re
import json
from array import array
from collections import deque
from dataclasses import dataclass
from typing import Any


DEFAULT_MAX_CHARS = 1500
_QUOTE_PAIRS = {"“": "”", "‘": "’", "「": "」", "『": "』", "（": "）", "(": ")"}


def _json_payload(value: str) -> bool:
    if not value.lstrip().startswith(("{", "[")):
        return False
    try:
        return isinstance(json.loads(value), (dict, list))
    except (ValueError, TypeError, RecursionError):
        return False


@dataclass(frozen=True)
class MessageChunk:
    text: str
    msg_id: str | None = None
    msg_seq: int | None = None

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"text": self.text}
        if self.msg_id is not None:
            data["msg_id"] = self.msg_id
        if self.msg_seq is not None:
            data["msg_seq"] = self.msg_seq
        return data


def format_for_qq(text: Any, *, markdown_support: bool = False) -> str:
    """Turn common Markdown into quiet, readable QQ plain text."""
    value = "" if text is None else str(text)
    if markdown_support or _json_payload(value):
        return value
    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    output: list[str] = []
    in_code = False
    language = ""
    code_lines: list[str] = []

    def flush_code() -> None:
        nonlocal code_lines, language
        if language:
            output.append(f"代码（{language}）：")
        output.extend(code_lines)
        code_lines = []
        language = ""

    for line in lines:
        fence = re.match(r"^\s*```\s*([\w.+-]*)\s*$", line)
        if fence:
            if in_code:
                flush_code()
                in_code = False
            else:
                in_code = True
                language = fence.group(1)
            continue
        if in_code:
            code_lines.append(line)
            continue
        # Keep transformations line-oriented so Markdown inside code remains
        # untouched. QQ output deliberately avoids decorative replacements.
        if re.match(r"^\s*(?:[-*_]\s*){3,}$", line):
            continue
        if re.match(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*$", line):
            continue
        line = re.sub(r"^\s*#{1,6}\s+(.+?)\s*#*\s*$", r"\1", line)
        line = re.sub(r"^\s*>+\s?", "", line)
        line = re.sub(r"^\s*[-*+]\s+\[x\]\s+", "已完成：", line, flags=re.IGNORECASE)
        line = re.sub(r"^\s*[-*+]\s+\[\s\]\s+", "待办：", line)
        line = re.sub(r"^\s*[-*+]\s+", "· ", line)
        line = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"图片：\1 (\2)", line)
        line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", line)
        line = re.sub(r"\*\*([^*]+?)\*\*", r"\1", line)
        line = re.sub(r"__([^_]+?)__", r"\1", line)
        line = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"\1", line)
        line = re.sub(r"(?<!_)_([^_\n]+?)_(?!_)", r"\1", line)
        line = re.sub(r"~~([^~]+?)~~", r"\1", line)
        line = re.sub(r"`([^`\n]+)`", r"\1", line)
        if line.strip().startswith("|") and line.strip().endswith("|"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            line = "；".join(cell for cell in cells if cell)
        output.append(line)
    if in_code:
        # An unfinished fence is still rendered as plain code, rather than
        # leaking Markdown syntax into clients.
        flush_code()
    # Spaces and blank lines can be significant in code. Presentation may
    # remove surrounding empty lines, but must not rewrite the code payload.
    return "\n".join(output).strip("\n")


format_text = format_for_qq


def trim_chat_followup(text: str) -> str:
    """Drop a standalone canned closing invitation, keeping real questions."""
    return re.sub(
        r"(?:^|(?<=[。！？!?\n]))[ \t]*(?:还需要我|要不要我|需要我再|你呢[？?]|你怎么看[？?])"
        r"[^\n。！？!?]*[？?]?[ \t]*$", "", text,
    ).rstrip()


# Explicit tasks keep their payload intact. A mention alone is not a detail request.
_DETAIL_REQUEST = re.compile(
    r"详细|展开|完整|长文|长篇|逐[步条行]|步骤|教程|代码|脚本|程序|函数|SQL|JSON|配置|"
    r"日志|报错|排查|调试|修复|翻译|原文|全文|写[一个份段篇]|列[出个]|对比|比较|"
    r"总结|分析|解释|证明|推导|计算|清单|计划|攻略|表格|报告|讲解|解决|"
    r"\b(?:code|debug|explain|translate|detailed|steps)\b", re.IGNORECASE,
)

_ARTIFACT_REQUEST = re.compile(
    r"(?:^|[，,。！？!?；;\n])\s*(?:请|再|重新|继续|直接)?\s*"
    r"(?:把[^，,。！？!?；;\n]{1,40})?"
    r"(?:"
    r"画(?:一[张幅个]|[张幅个]|出|一下)|绘制|设计|做成|"
    r"(?:发(?:送)?|传|提供|导出|保存|打包)(?:给我|我)?\s*(?:成|为)?\s*"
    r"(?:一[个份]|[个份])?\s*(?:文件|附件|(?:html|svg|pdf)(?![a-z0-9_])|网页)"
    r")",
    re.IGNORECASE,
)


def is_explicit_request(text: str) -> bool:
    """A local hint for one slow-task notice, never a content truncation rule."""
    return bool(_DETAIL_REQUEST.search(text) or _ARTIFACT_REQUEST.search(text) or re.search(
        r"帮我|请(?:帮|查|找|写|做|给|生成|推荐)|能否|能不能|麻烦|"
        r"写(?:封|首|句|则|条|信|诗)|推荐|查询|搜索|查找|生成|整理|制作|"
        r"为什么|怎么办|怎么做|如何|(?i:\b(?:please|help|write|find|search|summarize)\b)",
        str(text or ""),
    ))


def prepare_group_reply(text: str, question: str) -> str:
    """Keep the answer complete; concise conversation is a prompting policy."""
    return text


def split_group_reply(text: Any, *, direct: bool = False) -> list[str]:
    """Keep task output intact and bound short replies to at most two bubbles."""
    value = format_for_qq(text)
    if len(value) > 240 or "```" in str(text) or _json_payload(value):
        return split_message(value, max_chars=1500)
    parts = _sentence_parts(value)
    # Bound pushes per short turn, even when the model emits many tiny sentences.
    return parts if len(parts) <= 2 else [parts[0], "".join(parts[1:])]


def _sentence_parts(value: str, *, clauses: bool = False) -> list[str]:
    # A question inside a quote is not a bubble boundary: splitting there
    # strands the closing quote and the rest of the sentence in another reply.
    pairs = _QUOTE_PAIRS
    closing: list[str] = []
    parts: list[str] = []
    start = 0
    for index, char in enumerate(value):
        if closing and char == closing[-1]:
            closing.pop()
        elif char in pairs:
            closing.append(pairs[char])
        elif char == '"':
            closing.append(char)
        if closing:
            continue
        following = value[index + 1:index + 2]
        boundary = (clauses and char in "，,；;：:") or char == "\n" or char in "。！？" or (
            char in "!?" and (not following or following.isspace())
        )
        if boundary and following and following in "。！？!?":
            continue
        if boundary:
            part = value[start:index + 1]
            if part.strip():
                parts.append(part)
                start = index + 1
            elif parts:
                parts[-1] += part
                start = index + 1
    remainder = value[start:]
    if remainder.strip():
        parts.append(remainder)
    elif parts:
        parts[-1] += remainder
    return parts


def _nonblank_packets(value: str, max_chars: int) -> list[str]:
    """Redistribute whitespace without altering the payload or emitting blanks.

    Each reachable prefix ends with a packet containing a non-whitespace
    character. The sliding window finds such a preceding boundary in O(n).
    This path is only needed when preferred word/line cuts left a blank packet.
    """
    predecessors = array("q", [-1]) * (len(value) + 1)
    reachable = deque([0])
    last_visible = -1
    for end, character in enumerate(value, 1):
        if not character.isspace():
            last_visible = end - 1
        while reachable and reachable[0] < end - max_chars:
            reachable.popleft()
        if reachable and reachable[0] <= last_visible:
            predecessors[end] = reachable[0]
            reachable.append(end)
    if predecessors[-1] < 0:
        # For example, one visible character followed by >= max_chars spaces
        # cannot meet all three constraints. Never silently discard its spaces.
        raise ValueError("Message cannot be split losslessly into non-blank packets within max_chars")
    result = []
    end = len(value)
    while end:
        start = predecessors[end]
        result.append(value[start:end])
        end = start
    return list(reversed(result))


def split_message(text: Any, *, max_chars: int = DEFAULT_MAX_CHARS) -> list[str]:
    """Lossless non-blank packets; pure whitespace has no deliverable content.

    Raise ValueError when mixed content cannot fit the packet limit without a
    whitespace-only packet. This is preferable to silently damaging code/data.
    """
    value = "" if text is None else str(text)
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if not value.strip():
        return []
    if len(value) <= max_chars:
        return [value]
    chunks: list[str] = []
    offset = 0
    while len(value) - offset > max_chars:
        segment = value[offset:offset + max_chars]
        # Include the separator in the preceding packet, so concatenation is
        # exact and indentation on the following line remains untouched.
        cut = segment.rfind("\n") + 1
        if not cut:
            cut = segment.rfind(" ") + 1
        if not cut or not segment[:cut].strip():
            cut = max_chars
        chunks.append(value[offset:offset + cut])
        offset += cut
    if offset < len(value):
        chunks.append(value[offset:])
    return _nonblank_packets(value, max_chars) if any(not chunk.strip() for chunk in chunks) else chunks


def chunk_messages(
    text: Any,
    *,
    msg_id: str | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    start_seq: int = 1,
) -> list[MessageChunk]:
    return [
        MessageChunk(piece, msg_id=msg_id, msg_seq=start_seq + index if msg_id is not None else None)
        for index, piece in enumerate(split_message(text, max_chars=max_chars))
    ]


def format_and_chunk(
    text: Any,
    *,
    markdown_support: bool = False,
    msg_id: str | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    start_seq: int = 1,
) -> list[MessageChunk]:
    return chunk_messages(
        format_for_qq(text, markdown_support=markdown_support),
        msg_id=msg_id,
        max_chars=max_chars,
        start_seq=start_seq,
    )


__all__ = [
    "MessageChunk", "format_for_qq", "format_text", "split_message", "chunk_messages", "format_and_chunk",
    "is_explicit_request", "prepare_group_reply", "split_group_reply",
]
