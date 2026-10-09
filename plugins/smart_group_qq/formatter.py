"""QQ-safe plain-text formatting and passive-reply chunking."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


DEFAULT_MAX_CHARS = 1500
_QUOTE_PAIRS = {"“": "”", "‘": "’", "「": "」", "『": "』", "（": "）", "(": ")"}


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
    if markdown_support:
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
    value = "\n".join(output).strip()
    return re.sub(r"\n{3,}", "\n\n", value)


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


def prepare_group_reply(text: str, question: str) -> str:
    """Bound casual replies only; preserve requested task output before formatting."""
    if _DETAIL_REQUEST.search(question) or "```" in text or re.search(r"https?://|^\s*[\[{]", text):
        return text.strip()
    value = trim_chat_followup(text.strip())
    # Preserve a short literal/code answer: never cut inside structured content.
    if len(value) <= 180:
        return value
    # Reuse quote-aware boundaries so Chinese quotes are never left hanging.
    sentences = _sentence_parts(format_for_qq(value))
    selected: list[str] = []
    for sentence in sentences:
        if len(selected) >= 2 or len("".join(selected)) + len(sentence) > 180:
            break
        selected.append(sentence)
    if selected:
        return "".join(selected)
    # Prefer a complete clause when the model ignored the requested length.
    clauses = _sentence_parts(format_for_qq(value), clauses=True)
    if clauses and len(clauses[0]) <= 179:
        return clauses[0].rstrip("，,；;：:") + "。"
    # Avoid inventing a replacement answer or a canned follow-up question.
    closing: list[str] = []
    excerpt = ""
    for char in value:
        next_closing = closing.copy()
        if next_closing and char == next_closing[-1]:
            next_closing.pop()
        elif char in _QUOTE_PAIRS or char == '"':
            next_closing.append(_QUOTE_PAIRS.get(char, char))
        if len(excerpt) + 1 + len(next_closing) + 1 > 180:
            break
        excerpt += char
        closing = next_closing
    return excerpt.rstrip() + "…" + "".join(reversed(closing))


def split_group_reply(text: Any, *, direct: bool = False) -> list[str]:
    """Keep task output intact and bound short replies to at most two bubbles."""
    value = format_for_qq(text)
    if len(value) > 240 or "```" in str(text):
        return split_message(value, max_chars=1500)
    parts = _sentence_parts(value)
    # Bound pushes per short turn, even when the model emits many tiny sentences.
    return parts if len(parts) <= 2 else [parts[0], "\n".join(parts[1:])]


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
            part = value[start:index + 1].strip()
            if part:
                parts.append(part)
            start = index + 1
    remainder = value[start:].strip()
    if remainder:
        parts.append(remainder)
    return parts or [value]


def split_message(text: Any, *, max_chars: int = DEFAULT_MAX_CHARS) -> list[str]:
    """Split by paragraphs/lines before using a hard character boundary."""
    value = "" if text is None else str(text)
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if not value:
        return [""]
    if len(value) <= max_chars:
        return [value]
    chunks: list[str] = []
    current = ""

    def append_piece(piece: str) -> None:
        nonlocal current
        if not piece:
            return
        if len(piece) <= max_chars - len(current):
            current += piece
            return
        if current:
            chunks.append(current.rstrip("\n"))
            current = ""
        remaining = piece
        while len(remaining) > max_chars:
            # Prefer the last whitespace in this segment, preserving readable
            # words without ever exceeding the protocol limit.
            cut = max(remaining.rfind(" ", 0, max_chars + 1), remaining.rfind("\n", 0, max_chars + 1))
            if cut <= 0:
                cut = max_chars
            chunks.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip(" \n")
        current = remaining

    paragraphs = re.split(r"(\n\s*\n)", value)
    for part in paragraphs:
        if not part:
            continue
        if re.fullmatch(r"\n\s*\n", part or ""):
            if current and len(current) + len(part) <= max_chars:
                current += part
            elif current:
                chunks.append(current.rstrip("\n"))
                current = ""
            continue
        pieces = part.splitlines(keepends=True)
        for piece in pieces:
            append_piece(piece)
    if current:
        chunks.append(current.rstrip("\n"))
    return chunks or [""]


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
]
