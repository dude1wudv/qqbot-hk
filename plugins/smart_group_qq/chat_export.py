"""Render a bounded, privacy-safe QQ conversation history image."""
from __future__ import annotations

import asyncio
import re
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

MAX_ROWS = 80
MAX_TEXT_CHARS = 6000
MAX_TOTAL_CHARS = 24000
MAX_IMAGE_BYTES = 8 * 1024 * 1024
WIDTH = 1080
PAD = 42
_BG = "#f6f7fb"
_CARD = "#ffffff"
_INK = "#1f2937"
_MUTED = "#6b7280"
_ACCENT = "#3b82f6"
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{12,}\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+"),
)


def _value(row: Any, key: str, default: Any = None) -> Any:
    """Read both sqlite3.Row and ordinary mappings without exposing internals."""
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _safe_text(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    text = str(value or "").replace("\x00", "").strip()[:limit]
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[已隐藏敏感信息]", text)
    return text


def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/noto/NotoSansCJK-Regular.ttc",
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
    ]
    for path in candidates:
        try:
            if Path(path).is_file():
                return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(draw, text: str, font, width: int) -> list[str]:
    text = _safe_text(text) or "（空消息）"
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        current = ""
        for char in paragraph:
            candidate = current + char
            if current and draw.textlength(candidate, font=font) > width:
                lines.append(current)
                current = char
            else:
                current = candidate
        lines.append(current or " ")
    return lines or [" "]


def _display_name(row: Mapping[str, Any], names: dict[str, str]) -> str:
    if str(_value(row, "role") or "") == "assistant":
        return "小栖"
    raw = str(_value(row, "member_id") or "成员")
    if raw not in names:
        names[raw] = f"成员{len(names) + 1}"
    return names[raw]


def render_chat_history(
    rows: Iterable[Mapping[str, Any]],
    output_dir: str = "/opt/data/cache/chat-exports",
    *,
    title: str = "聊天记录",
) -> str | None:
    """Render newest bounded rows to a PNG and return its path.

    Message IDs, OpenIDs, attachment URLs and internal metadata are never drawn.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    items = list(rows)[-MAX_ROWS:]
    if not items:
        return None
    title = _safe_text(title, 80) or "聊天记录"
    body_font = _font(30)
    meta_font = _font(22)
    title_font = _font(38, bold=True)
    probe = Image.new("RGB", (WIDTH, 10), _BG)
    probe_draw = ImageDraw.Draw(probe)
    names: dict[str, str] = {}
    prepared = []
    total_height = PAD + 72
    for row in items:
        text = _safe_text(_value(row, "text"), MAX_TEXT_CHARS)
        media = _value(row, "media_json") or _value(row, "media") or ""
        if media and not text:
            text = "[附件]"
        lines = _wrap(probe_draw, text, body_font, WIDTH - 2 * PAD - 48)
        stamp = _value(row, "created_at")
        try:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(stamp))) if stamp else ""
        except (TypeError, ValueError, OverflowError):
            when = ""
        name = _display_name(row, names)
        prepared.append((name, when, lines))
        total_height += 116 + max(1, len(lines)) * 40
    total_height = min(max(total_height, 240), 120000)
    image = Image.new("RGB", (WIDTH, total_height), _BG)
    draw = ImageDraw.Draw(image)
    draw.text((PAD, PAD), title, font=title_font, fill=_INK)
    y = PAD + 78
    for name, when, lines in prepared:
        card_h = 86 + len(lines) * 40
        draw.rounded_rectangle((PAD, y, WIDTH - PAD, y + card_h), radius=18, fill=_CARD)
        is_bot = name == "小栖"
        draw.text((PAD + 24, y + 18), name, font=meta_font, fill=_ACCENT if is_bot else _INK)
        if when:
            tw = draw.textlength(when, font=meta_font)
            draw.text((WIDTH - PAD - 24 - tw, y + 18), when, font=meta_font, fill=_MUTED)
        body_y = y + 54
        for line in lines:
            draw.text((PAD + 24, body_y), line, font=body_font, fill=_INK)
            body_y += 40
        y += card_h + 18
    root = Path(output_dir)
    try:
        root.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix="chat-export-", suffix=".png", dir=root)
        os.close(fd)
        Path(path).unlink(missing_ok=True)
        image.save(path, format="PNG", optimize=True)
        if Path(path).stat().st_size > MAX_IMAGE_BYTES:
            Path(path).unlink(missing_ok=True)
            return None
        return path
    except OSError:
        return None


def export_from_store(
    store: Any,
    scope: str,
    *,
    output_dir: str = "/opt/data/cache/chat-exports",
    limit: int = MAX_ROWS,
) -> str | None:
    rows = store.get_history(scope, limit=max(1, min(MAX_ROWS, int(limit))))
    # Let the bot choose a readable window: keep more short messages and fewer
    # long task replies while always retaining the newest exchange.
    selected = []
    total = 0
    for row in reversed(rows):
        text = _safe_text(_value(row, "text"), MAX_TEXT_CHARS)
        cost = max(1, len(text))
        if selected and total + cost > MAX_TOTAL_CHARS:
            break
        selected.append(row)
        total += cost
    rows = list(reversed(selected))
    return render_chat_history(rows, output_dir, title="聊天记录 · 小栖")


async def send_chat_export(
    store: Any,
    scope: str,
    adapter: Any,
    chat_id: str,
    *,
    reply_to: str | None = None,
    output_dir: str = "/opt/data/cache/chat-exports",
    limit: int = MAX_ROWS,
) -> bool:
    """Render and upload one export through the adapter's existing image API."""
    path = await asyncio.to_thread(export_from_store, store, scope, output_dir=output_dir, limit=limit)
    sender = getattr(adapter, "send_image_file", None)
    if not path or not callable(sender):
        return False
    try:
        result = await sender(chat_id, path, reply_to=reply_to)
        return bool(getattr(result, "success", False))
    except Exception:
        return False
    finally:
        # The adapter has uploaded the file by the time send_image_file returns;
        # avoid retaining private conversation screenshots on disk.
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass


__all__ = ["MAX_ROWS", "MAX_TOTAL_CHARS", "render_chat_history", "export_from_store", "send_chat_export"]
