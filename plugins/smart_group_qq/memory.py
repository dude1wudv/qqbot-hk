"""Bounded, group-isolated memory helpers."""
from __future__ import annotations

import time
from typing import Any


def member_label(member_id: Any) -> str:
    value = str(member_id or "unknown")
    return value[:6] if value else "unknown"


def normalize_member_message(member_id: Any, text: Any) -> str:
    return f"[群成员:{member_label(member_id)}]: {str(text or '').strip()}"


class GroupMemory:
    def __init__(self, store: Any, *, window_size: int = 20, idle_seconds: int = 1800, summary_chars: int = 200):
        self.store = store
        self.window_size = max(2, int(window_size))
        self.idle_seconds = max(1, int(idle_seconds))
        self.summary_chars = max(40, int(summary_chars))

    def record(self, group_id: str, member_id: str, text: str, message_id: str | None = None) -> None:
        history = self.store.get_history(group_id)
        idle = bool(history and time.time() - float(history[-1]["created_at"]) >= self.idle_seconds)
        self.store.append_history(
            group_id, role="user", member_id=member_label(member_id), text=str(text), message_id=message_id
        )
        if idle or self.store.count_history(group_id) > self.window_size:
            self.compact(group_id)

    def compact(self, group_id: str) -> str:
        rows = self.store.get_history(group_id, self.window_size)
        pieces = [str(row["text"]).strip().replace("\n", " ") for row in rows if str(row["text"]).strip()]
        summary = "；".join(pieces)[-self.summary_chars:]
        self.store.set_memory(group_id, summary, window_size=self.window_size)
        self.store.trim_history(group_id, self.window_size)
        return summary

    def summary(self, group_id: str) -> str:
        memory = self.store.get_memory(group_id)
        stored = str(memory["summary"]).strip() if memory is not None else ""
        recent = [str(row["text"]).strip() for row in self.store.get_history(group_id, min(10, self.window_size))]
        body = "；".join(item for item in recent if item)
        combined = "；".join(item for item in (stored, body) if item)
        return combined[-self.summary_chars:] or "本群尚无可整理的机器人互动。"

    def background(self, group_id: str) -> str:
        memory = self.store.get_memory(group_id)
        return str(memory["summary"]).strip() if memory is not None else ""

    def reset(self, group_id: str) -> None:
        self.store.clear_group(group_id)


__all__ = ["GroupMemory", "member_label", "normalize_member_message"]
