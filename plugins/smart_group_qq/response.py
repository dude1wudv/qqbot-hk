"""Fail-closed group reply envelopes and delivery correlation."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

SILENT_MARKER = "[SILENT]"
INVALID_REPLY_MESSAGE = "这次回复格式异常，请稍后重试。"


def parse_reply_decision(response_text: str) -> tuple[str, str | None]:
    """Parse the only accepted group final-output envelope."""
    try:
        value = json.loads(str(response_text))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_json") from exc
    if not isinstance(value, dict):
        raise ValueError("not_object")
    if set(value) != {"action", "message"}:
        raise ValueError("invalid_fields")
    action = value.get("action")
    message = value.get("message")
    if action == "ignore":
        if message is not None and not isinstance(message, str):
            raise ValueError("invalid_ignore_message")
        return "ignore", None
    if action != "reply":
        raise ValueError("invalid_action")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("invalid_reply_message")
    return "reply", message.strip()


def message_text_parts(user_message: Any) -> tuple[str, ...]:
    if isinstance(user_message, str):
        return (user_message,)
    if isinstance(user_message, list):
        return tuple(
            str(part["text"])
            for part in user_message
            if isinstance(part, Mapping) and isinstance(part.get("text"), str)
        )
    return ()


@dataclass
class ReplyRequest:
    request_ref: str
    group_id: str
    member_ref: str
    message_id: str
    epoch: int
    source_kind: str
    merged_ids: tuple[str, ...]
    question: str
    direct: bool
    created_at: float = field(default_factory=time.monotonic)
    cancelled: bool = False
    pending_message: str | None = None
    model: str = ""
    record_on_success: bool = True
    consumed: bool = False


class ReplyRegistry:
    """Thread-safe request state shared by finalizer workers and the event loop."""

    def __init__(
        self,
        epoch_getter: Callable[[str], int],
        audit: Callable[..., Any],
        delivered: Callable[[ReplyRequest, Any], None],
        *,
        max_pending: int = 256,
        ttl_seconds: float = 900.0,
    ) -> None:
        self._epoch_getter = epoch_getter
        self._audit = audit
        self._delivered = delivered
        self.max_pending = max(1, int(max_pending))
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self._lock = threading.RLock()
        self._records: dict[str, ReplyRequest] = {}

        self._blocked: dict[tuple[str, str], tuple[ReplyRequest, float]] = {}

    def _valid_locked(self, record: ReplyRequest, now: float | None = None) -> bool:
        stamp = time.monotonic() if now is None else now
        return (
            not record.cancelled
            and not record.consumed
            and stamp - record.created_at <= self.ttl_seconds
            and self._epoch_getter(record.group_id) == record.epoch
        )

    def cleanup(self) -> None:
        now = time.monotonic()
        with self._lock:
            for key, value in list(self._records.items()):
                if self._valid_locked(value, now):
                    continue
                if value.pending_message is not None:
                    value.cancelled = True
                    self._blocked[(value.group_id, value.message_id)] = (value, now + 3600)
                self._records.pop(key, None)
            for key, (_, expires) in list(self._blocked.items()):
                if expires <= now:
                    self._blocked.pop(key, None)
            while len(self._blocked) > self.max_pending * 2:
                self._blocked.pop(next(iter(self._blocked)))

    def register(self, record: ReplyRequest, *, official: bool = False) -> bool:
        self.cleanup()
        with self._lock:
            if not official and len(self._records) >= self.max_pending:
                return False
            self._records[record.request_ref] = record
            return True

    def cancel_group(self, group_id: str, *, ordinary_only: bool = False) -> None:
        with self._lock:
            for record in self._records.values():
                if record.group_id == str(group_id) and (
                    not ordinary_only or record.source_kind == "nonmention"
                ):
                    record.cancelled = True
                    if record.pending_message is not None:
                        self._blocked[(record.group_id, record.message_id)] = (
                            record, time.monotonic() + 3600
                        )

    def transform(self, response_text: Any, user_message: Any) -> str:
        parts = message_text_parts(user_message)
        refs: list[str] = []
        for part in parts:
            start = 0
            while True:
                prefix = part.find("[群对话标记:", start)
                if prefix < 0:
                    break
                end = part.find("]", prefix)
                if end < 0:
                    break
                value = part[prefix + len("[群对话标记:"):end]
                if len(value) == 32 and all(ch in "0123456789abcdef" for ch in value):
                    refs.append(value)
                start = end + 1
        if not refs:
            return str(response_text or "")
        with self._lock:
            record = next((self._records.get(ref) for ref in refs if ref in self._records), None)
            if record is None or not self._valid_locked(record):
                return SILENT_MARKER
            try:
                action, message = parse_reply_decision(str(response_text or ""))
            except ValueError as exc:
                self._audit(
                    "output_invalid", chat_id=record.group_id, message_id=record.message_id,
                    source=str(exc),
                )
                if record.direct:
                    record.pending_message = INVALID_REPLY_MESSAGE
                    record.record_on_success = False
                    return INVALID_REPLY_MESSAGE
                record.consumed = True
                self._records.pop(record.request_ref, None)
                return SILENT_MARKER
            if action == "ignore":
                self._audit("output_ignore", chat_id=record.group_id, message_id=record.message_id, source=record.source_kind)
                record.consumed = True
                self._records.pop(record.request_ref, None)
                return SILENT_MARKER
            record.pending_message = message
            return str(message)

    def note_model(self, user_message: Any, model: Any) -> None:
        parts = message_text_parts(user_message)
        with self._lock:
            for record in self._records.values():
                marker = f"[群对话标记:{record.request_ref}]"
                if any(marker in part for part in parts):
                    record.model = str(model or "")
                    return

    def pending_for_send(self, chat_id: Any, reply_to: Any) -> ReplyRequest | None:
        group = str(chat_id or "")
        anchor = str(reply_to or "")
        with self._lock:
            candidates = [
                item for item in self._records.values()
                if item.group_id == group and item.message_id == anchor and item.pending_message is not None
            ]
            if candidates:
                return min(candidates, key=lambda item: item.created_at)
            blocked = self._blocked.get((group, anchor))
            return blocked[0] if blocked is not None else None

    def send_allowed(self, record: ReplyRequest) -> bool:
        with self._lock:
            return self._records.get(record.request_ref) is record and self._valid_locked(record)

    def finish_send(self, record: ReplyRequest, result: Any) -> None:
        success = bool(getattr(result, "success", False))
        callback = None
        with self._lock:
            if self._records.get(record.request_ref) is not record or record.consumed:
                return
            if success:
                record.consumed = True
                self._records.pop(record.request_ref, None)
                callback = self._delivered
            else:
                self._audit("reply_failed", chat_id=record.group_id, message_id=record.message_id, source=record.source_kind)
        if callback is not None:
            callback(record, result)

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._records)


__all__ = [
    "INVALID_REPLY_MESSAGE", "ReplyRegistry", "ReplyRequest", "SILENT_MARKER",
    "message_text_parts", "parse_reply_decision",
]
