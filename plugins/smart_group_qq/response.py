"""Fail-closed group reply envelopes and delivery correlation."""
from __future__ import annotations

import ast
import asyncio
import json
import re
import threading
import time
from dataclasses import dataclass, field
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Mapping

from .formatter import prepare_group_reply


SILENT_MARKER = "[SILENT]"
INVALID_REPLY_MESSAGE = "这次回复格式异常，请稍后重试。"
PROGRESS_MESSAGE = "还在处理，完成后会直接回复。"
# Read by the adapter wrapper; never forward this implementation detail to QQ.
OUTBOUND_KIND: ContextVar[str] = ContextVar("smart_group_qq_outbound_kind", default="final")
_FENCE = re.compile(r"^```(?:json|python|py)?\s*(.*?)\s*```$", re.IGNORECASE | re.DOTALL)


class _DuplicateFields(ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateFields("duplicate_fields")
        result[key] = value
    return result


def _literal_object(text):
    node = ast.parse(text, mode="eval")
    for item in ast.walk(node):
        if isinstance(item, ast.Dict):
            keys = [ast.literal_eval(key) for key in item.keys]
            if len(keys) != len(set(keys)):
                raise _DuplicateFields("duplicate_fields")
    return ast.literal_eval(node)


def _reply_object(response_text: str) -> dict[str, Any]:
    """Accept one envelope, rejecting duplicate keys and ambiguous multiple decisions."""
    candidate = str(response_text or "").strip()
    fenced = _FENCE.fullmatch(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    decoder = json.JSONDecoder(strict=False, object_pairs_hook=_unique_object)
    for loader in (decoder.decode, _literal_object):
        try:
            value = loader(candidate)
        except _DuplicateFields:
            raise
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            continue
        if not isinstance(value, dict):
            raise ValueError("not_object")
        return value
    objects = []
    index = 0
    while index < len(candidate):
        start = candidate.find("{", index)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(candidate[start:])
        except _DuplicateFields:
            raise
        except (ValueError, TypeError, RecursionError):
            index = start + 1
            continue
        index = start + end  # Never reinterpret JSON inside an envelope's message.
        if isinstance(value, dict) and ("action" in value or "message" in value):
            objects.append(value)
    if len(objects) != 1:
        raise ValueError("ambiguous_or_invalid_json")
    return objects[0]


def parse_reply_decision(response_text: str) -> tuple[str, str | None]:
    """Parse the only accepted group final-output envelope."""
    try:
        value = _reply_object(response_text)
    except ValueError as exc:
        raise ValueError("invalid_json") from exc
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
    return "reply", message


def render_reply_envelope(response_text: Any) -> str | None:
    """Return cleaned reply text, empty text for ignore, or None when this is ordinary text."""
    try:
        action, message = parse_reply_decision(str(response_text or ""))
    except ValueError:
        return None
    if action == "ignore":
        return ""
    return message


def _plain_text_fallback(response_text: Any) -> str | None:
    """Keep ordinary model prose deliverable while rejecting broken JSON envelopes."""
    text = str(response_text or "")
    broken_envelope = re.search(r'(?:["\']?(?:action|message)["\']?\s*[:：])', text, re.I)
    if not text.strip() or text.strip() == SILENT_MARKER or text.lstrip().startswith(("{", "[")) or broken_envelope:
        return None
    return text


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
    transport_chat_id: str = ""
    pending_message: str | None = None
    model: str = ""
    record_on_success: bool = True
    consumed: bool = False
    sent_chunks: list[str] = field(default_factory=list)
    sent_message_ids: list[str] = field(default_factory=list)
    send_lock: Any = field(default_factory=asyncio.Lock)
    last_send_result: Any = None
    sticker_adapter: Any = None
    sticker_guard: Callable[[], bool] | None = None
    progress_eligible: bool = False
    progress_attempted: bool = False
    phase: str = "pending"
    progress_task: Any = None
    progress_loop: Any = None
    delivery_state: str = "pending"
    delivery_callback: Callable[[Any, str], Any] | None = None
    execution_active: bool = False
    execution_guard: Callable[[], bool] | None = None
    idle_since: float | None = None


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
        try:
            current = record.execution_guard is None or bool(record.execution_guard())
        except Exception:
            current = False
        idle_since = record.created_at if record.idle_since is None else record.idle_since
        return (
            not record.cancelled
            and not record.consumed
            and record.delivery_state not in {"unknown", "cancelled", "sent"}
            and current
            and (record.execution_active or stamp - idle_since <= self.ttl_seconds)
            and self._epoch_getter(record.group_id) == record.epoch
        )

    def cleanup(self) -> None:
        now = time.monotonic()
        with self._lock:
            for key, value in list(self._records.items()):
                if self._valid_locked(value, now):
                    continue
                value.execution_active = False
                self.finish_progress(value, phase="cancelled")
                if value.pending_message is not None:
                    value.cancelled = True
                    self._blocked[(value.transport_chat_id or value.group_id, value.message_id)] = (value, now + 3600)
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

    def begin_execution(self, record: ReplyRequest, is_current: Callable[[], bool] | None = None) -> bool:
        """An actual task owner supersedes the orphan timeout, never its epoch."""
        with self._lock:
            if self._records.get(record.request_ref) is not record:
                return False
            record.execution_guard = is_current
            record.execution_active = True
            if not self._valid_locked(record):
                record.execution_active = False
                return False
            return True

    def finish_execution(self, record: ReplyRequest) -> None:
        """Release the task owner while allowing its final packet to be sent."""
        with self._lock:
            record.execution_active = False
            record.idle_since = time.monotonic()
            phase = record.phase if record.phase in {"finished", "cancelled"} else "ready"
        self.finish_progress(record, phase=phase)

    def start_progress(
        self, record: ReplyRequest, sender: Callable[[], Awaitable[Any]], *,
        delay_seconds: float = 10.0, is_current: Callable[[], bool] | None = None,
    ) -> asyncio.Task | None:
        """One disposable notice, tied to the same turn/epoch as its answer."""
        loop = asyncio.get_running_loop()
        with self._lock:
            if (not record.progress_eligible or not record.direct or record.progress_attempted
                    or self._records.get(record.request_ref) is not record
                    or not self._valid_locked(record)
                    or record.phase in {"ready", "finished", "cancelled"}
                    or (record.progress_task is not None and not record.progress_task.done())):
                return None
            record.phase = "running"
            record.progress_loop = loop

        async def notify() -> None:
            try:
                await asyncio.sleep(max(0.0, float(delay_seconds)))
                async with record.send_lock:
                    with self._lock:
                        try:
                            current = is_current is None or bool(is_current())
                        except Exception:
                            current = False
                        if (not current or not self._valid_locked(record)
                                or self._records.get(record.request_ref) is not record
                                or record.phase != "running" or record.progress_attempted):
                            return
                        # An uncertain send is still an attempt; never duplicate it.
                        record.progress_attempted = True
                    token = OUTBOUND_KIND.set("progress")
                    try:
                        await sender()
                    finally:
                        OUTBOUND_KIND.reset(token)
                    self._audit("progress_attempted", chat_id=record.group_id,
                                message_id=record.message_id, source=record.source_kind)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._audit("progress_failed", chat_id=record.group_id,
                            message_id=record.message_id, source=type(exc).__name__)
            finally:
                with self._lock:
                    if record.progress_task is asyncio.current_task():
                        record.progress_task = None

        with self._lock:
            # A finalizer thread may have completed the turn while the timer
            # closure was being prepared. Do not leave an ownerless timer.
            if record.phase != "running" or not self._valid_locked(record):
                return None
            task = loop.create_task(notify(), name="qq-task-wait-notice")
            record.progress_task = task
        return task

    def finish_progress(self, record: ReplyRequest, *, phase: str = "ready") -> None:
        """Safe from the finalizer worker thread as well as the gateway loop."""
        with self._lock:
            record.phase = phase
            task, loop = record.progress_task, record.progress_loop
            record.progress_task = None
        if task is not None and not task.done() and loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(task.cancel)

    def note_delivery(self, record: ReplyRequest, state: str) -> None:
        """Adapter hook for durable command receipts; no transcript side effects."""
        if state not in {"pending", "sending", "sent", "failed", "unknown", "cancelled"}:
            raise ValueError("invalid delivery state")
        with self._lock:
            record.delivery_state = state
            callback = record.delivery_callback
        if state != "pending":
            self.finish_progress(record, phase="cancelled" if state == "cancelled" else "ready")
        if callback is not None:
            callback(record, state)

    def cancel_group(self, group_id: str, *, ordinary_only: bool = False) -> None:
        with self._lock:
            for record in self._records.values():
                if record.group_id == str(group_id) and (
                    not ordinary_only or record.source_kind == "nonmention"
                ):
                    record.cancelled = True
                    record.execution_active = False
                    self.finish_progress(record, phase="cancelled")
                    self.note_delivery(record, "cancelled")
                    if record.pending_message is not None:
                        self._blocked[(record.transport_chat_id or record.group_id, record.message_id)] = (
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
            rendered = render_reply_envelope(response_text)
            return SILENT_MARKER if rendered == "" else (rendered if rendered is not None else str(response_text or ""))
        with self._lock:
            record = next((self._records.get(ref) for ref in refs if ref in self._records), None)
            if record is None or not self._valid_locked(record):
                return SILENT_MARKER
            self.finish_progress(record)
            try:
                if record.source_kind == "private":
                    rendered = render_reply_envelope(response_text)
                    message = str(response_text or "") if rendered is None else rendered
                    action = "reply" if message.strip() and message.strip() != SILENT_MARKER else "ignore"
                else:
                    action, message = parse_reply_decision(str(response_text or ""))
            except ValueError as exc:
                self._audit(
                    "output_invalid", chat_id=record.group_id, message_id=record.message_id,
                    source=str(exc),
                )
                fallback = _plain_text_fallback(response_text)
                if fallback is None and record.direct and str(response_text or "").strip() != SILENT_MARKER:
                    record.pending_message = INVALID_REPLY_MESSAGE
                    record.record_on_success = False
                    return INVALID_REPLY_MESSAGE
                if fallback is None:
                    record.consumed = True
                    self._records.pop(record.request_ref, None)
                    return SILENT_MARKER
                self._audit(
                    "output_fallback", chat_id=record.group_id, message_id=record.message_id,
                    source="plain_text",
                )
                action, message = "reply", fallback
            if action == "ignore":
                self._audit("output_ignore", chat_id=record.group_id, message_id=record.message_id, source=record.source_kind)
                record.consumed = True
                self._records.pop(record.request_ref, None)
                return SILENT_MARKER
            message = str(message)
            if record.source_kind != "private":
                message = prepare_group_reply(message, record.question)
            if not message:
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
                if (item.transport_chat_id or item.group_id) == group and item.message_id == anchor and item.pending_message is not None
            ]
            if candidates:
                return min(candidates, key=lambda item: item.created_at)
            blocked = self._blocked.get((group, anchor))
            return blocked[0] if blocked is not None else None

    def lookup(self, chat_id: Any, message_id: Any) -> ReplyRequest | None:
        """Find the accepted request before its model has produced final text."""
        group, anchor = str(chat_id or ""), str(message_id or "")
        with self._lock:
            candidates = [item for item in self._records.values()
                          if (item.transport_chat_id or item.group_id) == group
                          and item.message_id == anchor and self._valid_locked(item)]
            return min(candidates, key=lambda item: item.created_at) if candidates else None

    def send_allowed(self, record: ReplyRequest) -> bool:
        with self._lock:
            return self._records.get(record.request_ref) is record and self._valid_locked(record)

    def finish_send(self, record: ReplyRequest, result: Any, *, failure_state: str | None = None) -> None:
        success = bool(getattr(result, "success", False))
        callback = None
        with self._lock:
            if self._records.get(record.request_ref) is not record or record.consumed:
                return
            if not self._valid_locked(record):
                # A transport may complete just after reset/cancellation. Its
                # acknowledgement must not recreate memory or attention state.
                record.execution_active = False
                self.finish_progress(record, phase="cancelled")
                if record.delivery_state not in {"unknown", "cancelled"}:
                    self.note_delivery(record, "cancelled")
                record.cancelled = record.consumed = True
                self._records.pop(record.request_ref, None)
                if record.pending_message is not None:
                    self._blocked[(record.transport_chat_id or record.group_id, record.message_id)] = (
                        record, time.monotonic() + 3600)
                return
            if success:
                self.note_delivery(record, "sent")
                self.finish_progress(record, phase="finished")
                record.execution_active = False
                record.consumed = True
                self._records.pop(record.request_ref, None)
                callback = self._delivered
            else:
                # Only the sender can prove a failure is safe to repeat. Network
                # exceptions/timeouts and older adapters without that signal
                # must not be interpreted as an unsuccessful platform receipt.
                state = failure_state or getattr(result, "delivery_status", None) or "unknown"
                if state not in {"failed", "unknown", "cancelled"}:
                    raise ValueError("invalid failure state")
                self.note_delivery(record, state)
                record.execution_active = False
                self._audit("reply_failed", chat_id=record.group_id, message_id=record.message_id, source=record.source_kind)
        if callback is not None:
            callback(record, result)

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._records)


__all__ = [
    "INVALID_REPLY_MESSAGE", "PROGRESS_MESSAGE", "OUTBOUND_KIND", "ReplyRegistry", "ReplyRequest", "SILENT_MARKER",
    "message_text_parts", "parse_reply_decision",
]
