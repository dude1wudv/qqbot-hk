"""Observe QQ group messages and selectively admit useful requests to Hermes.

Hermes v0.21.2 only routes ``GROUP_AT_MESSAGE_CREATE`` into the normal agent
pipeline. This shim ingests ``GROUP_MESSAGE_CREATE`` as ambient context first.
An optional ``should_reply`` callback can admit a message through the existing
group handler, preserving its ACL, duplicate cache and outbound send path.
Without that callback the observer remains read-only.
"""

from __future__ import annotations

import asyncio
import functools
import importlib
import inspect
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Optional, Union


Observer = Callable[[dict[str, Any]], Union[Awaitable[Any], Any]]

_STATE_ATTR = "_smart_group_qq_nonmention_observer_state"
_CALLBACK_ATTR = "_smart_group_qq_nonmention_observer_callback"
_LOGGER_ATTR = "_smart_group_qq_nonmention_observer_logger"
_SEEN_ATTR = "_smart_group_qq_nonmention_seen"
_SEEN_CLEANUP_ATTR = "_smart_group_qq_nonmention_seen_cleanup"
_QUEUE_ATTR = "_smart_group_qq_nonmention_queue"
_WORKER_ATTR = "_smart_group_qq_nonmention_worker"

# Keep this cache independent from QQAdapter._seen_messages.  The latter is
# used by normal agent-facing events and sharing it would cause an @ message
# with the same id to be dropped by Hermes.
_SEEN_WINDOW_SECONDS = 60 * 60
_SEEN_MAX_SIZE = 10_000
_SEEN_CLEANUP_INTERVAL_SECONDS = 60.0

# The observer must not make text ingestion wait on a slow image/file/voice
# processor.  One event-loop yield keeps the common fast path (and existing
# adapters that process small attachments synchronously) enriched, while a
# slow processor is completed in the background after the text record is
# already available to the callback.

# A callback failure releases the id and is retried in the same task.  Releasing
# the id is still important: a gateway redelivery after the task gives up must
# be able to ingest the event again.
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 0.05
_QUEUE_MAX_SIZE = 2000

_module_logger = logging.getLogger(__name__)


def _resolve_adapter_class() -> type[Any]:
    """Return the fixed Hermes QQ adapter without importing it at module load."""

    errors: list[Exception] = []
    for module_name in ("gateway.platforms.qqbot", "gateway.platforms.qqbot.adapter"):
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # pragma: no cover - exercised by non-Hermes hosts
            errors.append(exc)
            continue
        for class_name in ("QQAdapter", "QQBotAdapter"):
            adapter_class = getattr(module, class_name, None)
            if isinstance(adapter_class, type):
                return adapter_class
    detail = f": {errors[-1]}" if errors else ""
    raise ImportError("Hermes QQAdapter is unavailable" + detail)


def _logger(adapter: Any) -> logging.Logger:
    value = getattr(type(adapter), _LOGGER_ATTR, None)
    return value if value is not None else _module_logger


def _log(adapter: Any, level: str, message: str, *args: Any, exc_info: bool = False) -> None:
    """Log observer failures without allowing a custom logger to break dispatch."""

    try:
        method = getattr(_logger(adapter), level, None)
        if callable(method):
            method(message, *args, exc_info=exc_info)
    except Exception:
        # Logging is best effort.  In particular, a test/application logger may
        # have a reduced signature; observer isolation must still hold.
        return


def _message_id(payload: Mapping[str, Any]) -> str:
    data = payload.get("d")
    return str(data.get("id", "")).strip() if isinstance(data, Mapping) else ""


def _event_sequence(payload: Mapping[str, Any]) -> Optional[int]:
    sequence = payload.get("s")
    if isinstance(sequence, int) and not isinstance(sequence, bool):
        return sequence
    return None


def _advance_sequence(adapter: Any, payload: Mapping[str, Any]) -> Optional[int]:
    """Preserve the sequence bookkeeping done at the top of Hermes dispatch."""

    sequence = _event_sequence(payload)
    previous = getattr(adapter, "_last_seq", None)
    if sequence is not None and (previous is None or sequence > previous):
        setattr(adapter, "_last_seq", sequence)
    return sequence


def _cleanup_seen(adapter: Any, now: Optional[float] = None) -> None:
    """Periodically expire observed ids and enforce the hard cache bound."""

    stamp = time.monotonic() if now is None else float(now)
    seen = getattr(adapter, _SEEN_ATTR, None)
    if not isinstance(seen, dict):
        return
    last_cleanup = getattr(adapter, _SEEN_CLEANUP_ATTR, 0.0)
    if (
        len(seen) <= _SEEN_MAX_SIZE
        and stamp - float(last_cleanup or 0.0) < _SEEN_CLEANUP_INTERVAL_SECONDS
    ):
        return

    cutoff = stamp - _SEEN_WINDOW_SECONDS
    for key, timestamp in list(seen.items()):
        try:
            expired = float(timestamp) <= cutoff
        except (TypeError, ValueError):
            expired = True
        if expired:
            seen.pop(key, None)

    # A burst of unique events should not make the private cache grow without
    # bound even when all entries are still within the time window.
    if len(seen) > _SEEN_MAX_SIZE:
        oldest = sorted(
            seen,
            key=lambda key: float(seen.get(key, stamp)),
        )[: len(seen) - _SEEN_MAX_SIZE]
        for key in oldest:
            seen.pop(key, None)
    setattr(adapter, _SEEN_CLEANUP_ATTR, stamp)


def _claim_message_id(adapter: Any, message_id: str) -> bool:
    """Atomically claim an observed id in a cache private to this shim."""

    now = time.monotonic()
    seen = getattr(adapter, _SEEN_ATTR, None)
    if not isinstance(seen, dict):
        seen = {}
        setattr(adapter, _SEEN_ATTR, seen)

    _cleanup_seen(adapter, now)

    if message_id in seen:
        return False
    seen[message_id] = now
    # Enforce the bound after insertion as well as before it; this keeps the
    # cache at or below the limit even when a burst arrives between cleanup
    # intervals.
    _cleanup_seen(adapter, now)
    return True


def _release_message_id(adapter: Any, message_id: str) -> None:
    """Allow a failed callback or a redelivery to retry this event."""

    seen = getattr(adapter, _SEEN_ATTR, None)
    if isinstance(seen, dict):
        seen.pop(str(message_id), None)


def _minimal_raw(
    data: Mapping[str, Any],
    group_id: str,
    member_id: str,
    message_id: str,
    *,
    sequence: Optional[int] = None,
    display_name: str = "",
    username: str = "",
) -> dict[str, Any]:
    """Keep only useful, non-URL event metadata in the callback record."""

    author = {"member_openid": member_id} if member_id else {}
    if display_name:
        author["display_name"] = display_name
    if username:
        author["username"] = username
    raw: dict[str, Any] = {
        "id": message_id,
        "sequence": sequence,
        "timestamp": str(data.get("timestamp", "") or ""),
        "content": str(data.get("content", "") or ""),
        "group_openid": group_id,
        "author": author,
    }

    # Attachment URLs are signed credentials and are not needed by consumers:
    # _process_attachments already returns local paths.  Keep only descriptive
    # metadata for audit/indexing purposes.
    attachments = data.get("attachments")
    if isinstance(attachments, list):
        metadata: list[dict[str, Any]] = []
        for item in attachments:
            if not isinstance(item, Mapping):
                continue
            entry = {
                key: item[key]
                for key in ("content_type", "filename", "size")
                if key in item and item[key] is not None
            }
            if entry:
                metadata.append(entry)
        if metadata:
            raw["attachments"] = metadata
    return raw


def _attachment_value(result: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = result.get(key)
        if value is not None:
            return value
    return []


async def _observe_message(adapter: Any, payload: Mapping[str, Any], callback: Observer) -> bool | None:
    data = payload.get("d")
    if not isinstance(data, Mapping):
        return

    group_id = str(data.get("group_openid", "") or "").strip()
    author = data.get("author")
    author_map = author if isinstance(author, Mapping) else {}
    member_id = str(
        author_map.get("member_openid") or data.get("member_openid") or ""
    ).strip()
    message_id = str(data.get("id", "") or "").strip()
    if not group_id or not message_id:
        return

    # The adapter remains the authority for group access.  Failing closed here
    # avoids indexing events from an adapter whose ACL implementation is absent
    # or malfunctioning.
    allowed = getattr(adapter, "_is_group_allowed", None)
    try:
        if not callable(allowed) or not bool(allowed(group_id, member_id)):
            return
    except Exception:
        _log(adapter, "warning", "QQ non-mention observer ACL check failed", exc_info=True)
        return

    if author_map.get("bot") is True:
        return

    if not _claim_message_id(adapter, message_id):
        return

    def first_text(*keys: str) -> str:
        candidates = (author_map, data.get("member"), data)
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            for key in keys:
                value = str(candidate.get(key, "") or "").strip()
                if value:
                    return value
        return ""

    display_name = first_text(
        "display_name", "nickname", "nick", "member_name", "name"
    )
    username = first_text("username", "user_name", "screen_name")
    sequence = _event_sequence(payload)
    raw_content = str(data.get("content", "") or "").strip()
    attachments = data.get("attachments")
    has_attachments = isinstance(attachments, list) and bool(attachments)
    if not raw_content and not has_attachments:
        _release_message_id(adapter, message_id)
        return

    timestamp_raw = str(data.get("timestamp", "") or "")
    parser = getattr(adapter, "_parse_qq_timestamp", None)
    try:
        timestamp = parser(timestamp_raw) if callable(parser) else datetime.now(tz=timezone.utc)
    except Exception:
        _log(adapter, "warning", "QQ non-mention timestamp parsing failed", exc_info=True)
        timestamp = datetime.now(tz=timezone.utc)

    # The fast record is intentionally useful without waiting for media.  It
    # gives durable-ingestion consumers a stable event key, sequence, event
    # timestamp and member identity even when QQ attachment processing stalls.
    record: dict[str, Any] = {
        "group_id": group_id,
        "member_id": member_id,
        "message_id": message_id,
        "display_name": display_name,
        "username": username,
        "sequence": sequence,
        "gateway_sequence": sequence,
        "timestamp": timestamp,
        "event_timestamp": timestamp,
        "event_timestamp_raw": timestamp_raw,
        "received_at": datetime.now(tz=timezone.utc),
        "text": raw_content,
        "image_paths": [],
        "media_types": [],
        "attachment_info": "",
        "attachment_status": "pending" if has_attachments else "none",
        "attachments_pending": has_attachments,
        "raw": _minimal_raw(
            data,
            group_id,
            member_id,
            message_id,
            sequence=sequence,
            display_name=display_name,
            username=username,
        ),
        "fast_ingested": bool(payload.get("_smart_group_qq_fast_ingested")),
    }

    async def process_attachments() -> dict[str, Any]:
        processor = getattr(adapter, "_process_attachments", None)
        if not callable(processor):
            return {"status": "unavailable"}
        try:
            result = await processor(attachments)
            if not isinstance(result, Mapping):
                return {"status": "ready"}
            image_paths = list(_attachment_value(result, "image_paths", "image_urls") or [])
            media_types = list(
                _attachment_value(result, "media_types", "image_media_types") or []
            )
            voices = list(result.get("voice_transcripts") or [])
            attachment_info = str(result.get("attachment_info", "") or "").strip()
            extra_parts = [str(item).strip() for item in voices if str(item).strip()]
            if attachment_info:
                extra_parts.append(attachment_info)
            return {
                "status": "ready",
                "image_paths": image_paths,
                "media_types": media_types,
                "attachment_info": attachment_info,
                "extra_text": "\n\n".join(
                    part for part in [raw_content, *extra_parts] if part
                ).strip(),
            }
        except asyncio.CancelledError:
            raise
        except Exception:
            return {"status": "failed"}

    def apply_attachments(result: Mapping[str, Any]) -> None:
        record["image_paths"] = list(result.get("image_paths") or [])
        record["media_types"] = list(result.get("media_types") or [])
        record["attachment_info"] = str(result.get("attachment_info", "") or "").strip()
        extra_text = str(result.get("extra_text", "") or "").strip()
        if extra_text:
            record["text"] = extra_text
        record["attachment_status"] = str(result.get("status") or "ready")
        record["attachments_pending"] = False

    attachment_task: Optional[asyncio.Task[dict[str, Any]]] = None
    attachment_result: Optional[dict[str, Any]] = None
    if has_attachments:
        attachment_task = asyncio.create_task(process_attachments())
        try:
            # Give a fast adapter one scheduling turn, but never wait for a
            # network/model-bound processor.  The task remains alive and its
            # optional on_media_ready hook is handled below.
            await asyncio.sleep(0)
            if attachment_task.done():
                attachment_result = attachment_task.result()
        except asyncio.CancelledError:
            attachment_task.cancel()
            raise

    if attachment_result is not None:
        apply_attachments(attachment_result)

    try:
        result = callback(record)
        if inspect.isawaitable(result):
            result = await result
        if result is False:
            raise RuntimeError("observer callback rejected record")
    except asyncio.CancelledError:
        _release_message_id(adapter, message_id)
        if attachment_task is not None and not attachment_task.done():
            attachment_task.cancel()
        raise
    except Exception:
        # Observer consumers are intentionally outside Hermes' normal message
        # path.  Their failures must never surface as gateway dispatch errors.
        _release_message_id(adapter, message_id)
        if attachment_task is not None and not attachment_task.done():
            attachment_task.cancel()
        return False

    should_reply = getattr(callback, "should_reply", None)
    dispatch_message = getattr(adapter, "_on_message", None)
    if callable(should_reply) and callable(dispatch_message):
        try:
            if await should_reply(record):
                # Reuse Hermes' group normalization, deduplication and session
                # dispatch; the raw marker preserves that this was NOT an @.
                # Never route the whole ambient stream to the agent.
                addressed = dict(data)
                addressed["_smart_group_qq_nonmention"] = True
                await dispatch_message("GROUP_AT_MESSAGE_CREATE", addressed)
        except Exception:
            # Dispatch may already have produced a reply. Do not release the
            # observer claim or retry a side effect after an uncertain failure.
            _log(adapter, "warning", "QQ non-mention participation dispatch failed")

    if attachment_task is not None and attachment_result is None:
        async def finish_media() -> None:
            try:
                result = await attachment_task
                apply_attachments(result)
                # Existing plugin callbacks ingest the fast text record and do
                # not need a second event.  Future durable consumers can opt in
                # to the enriched phase without changing __init__.py.
                media_ready = getattr(callback, "on_media_ready", None)
                if callable(media_ready):
                    enriched = media_ready(record)
                    if inspect.isawaitable(enriched):
                        await enriched
            except asyncio.CancelledError:
                raise
            except Exception:
                _log(adapter, "warning", "QQ non-mention attachment completion failed", exc_info=True)

        task = asyncio.create_task(finish_media())
        task.add_done_callback(_consume_task_exception)
    return True


async def _observe_with_retries(
    adapter: Any, payload: Mapping[str, Any], callback: Observer
) -> None:
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            succeeded = await _observe_message(adapter, payload, callback)
        except asyncio.CancelledError:
            raise
        if succeeded is not False:
            return
        if attempt + 1 < _RETRY_ATTEMPTS:
            await asyncio.sleep(_RETRY_BACKOFF_SECONDS * (2**attempt))

    _log(adapter, "warning", "QQ non-mention observer callback failed after retries")


def _schedule(adapter: Any, payload: Mapping[str, Any], callback: Observer) -> None:
    """Persist the fast path, then process enrichment through one bounded queue."""

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    queued_payload = dict(payload)
    fast_ingest = getattr(callback, "fast_ingest", None)
    if callable(fast_ingest):
        try:
            if fast_ingest(adapter, payload) is False:
                return
            queued_payload["_smart_group_qq_fast_ingested"] = True
        except Exception:
            _log(adapter, "warning", "QQ non-mention fast ingest failed", exc_info=True)
            return

    configured_max = getattr(callback, "queue_max_size", _QUEUE_MAX_SIZE)
    try:
        max_size = max(1, int(configured_max))
    except (TypeError, ValueError):
        max_size = _QUEUE_MAX_SIZE
    data = payload.get("d") if isinstance(payload.get("d"), Mapping) else {}
    group_key = str(data.get("group_openid") or "unknown")
    queues = getattr(adapter, _QUEUE_ATTR, None)
    if not isinstance(queues, dict):
        queues = {}
        setattr(adapter, _QUEUE_ATTR, queues)
    queue = queues.get(group_key)
    if not isinstance(queue, asyncio.Queue):
        queue = asyncio.Queue(maxsize=max_size)
        queues[group_key] = queue

    try:
        queue.put_nowait((queued_payload, callback))
    except asyncio.QueueFull:
        # Base text has already been persisted. Dropping enrichment keeps an
        # attachment burst from delaying addressed messages without bound.
        _log(adapter, "warning", "QQ non-mention enrichment queue is full")
        return

    workers = getattr(adapter, _WORKER_ATTR, None)
    if not isinstance(workers, dict):
        workers = {}
        setattr(adapter, _WORKER_ATTR, workers)
    worker = workers.get(group_key)
    if isinstance(worker, asyncio.Task) and not worker.done():
        return

    async def drain() -> None:
        while True:
            item_payload, item_callback = await queue.get()
            try:
                await _observe_with_retries(adapter, item_payload, item_callback)
            finally:
                queue.task_done()
            if queue.empty():
                workers.pop(group_key, None)
                return

    try:
        worker = loop.create_task(drain())
        worker.add_done_callback(_consume_task_exception)
        workers[group_key] = worker
    except Exception:
        _log(adapter, "warning", "QQ non-mention observer scheduling failed", exc_info=True)


def _consume_task_exception(task: asyncio.Future[Any]) -> None:
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        return


def install_nonmention_observer(callback: Observer, logger: Optional[logging.Logger] = None) -> type[Any]:
    """Install or update the QQ non-mention observer monkey-patch.

    The callback receives one normalized dictionary per allowlisted
    ``GROUP_MESSAGE_CREATE`` event.  Installation is idempotent: subsequent
    calls update the callback/logger on the existing wrapper without nesting
    another wrapper around ``_dispatch_payload``.  The adapter class is
    returned to make integration/testing explicit.
    """

    if not callable(callback):
        raise TypeError("callback must be callable")
    adapter_class = _resolve_adapter_class()
    dispatch = getattr(adapter_class, "_dispatch_payload", None)
    if not callable(dispatch):
        raise AttributeError("QQAdapter._dispatch_payload is unavailable")

    state = getattr(adapter_class, _STATE_ATTR, None)
    if isinstance(state, dict) and state.get("wrapper") is dispatch:
        setattr(adapter_class, _CALLBACK_ATTR, callback)
        setattr(adapter_class, _LOGGER_ATTR, logger or _module_logger)
        return adapter_class

    original = state.get("original") if isinstance(state, dict) else dispatch
    if not callable(original):
        original = dispatch

    @functools.wraps(original)
    def wrapped(self: Any, payload: Any) -> Any:
        if (
            isinstance(payload, Mapping) and payload.get("op") == 0
            and payload.get("t") in {"GROUP_AT_MESSAGE_CREATE", "GROUP_ADD_ROBOT", "GROUP_MSG_RECEIVE"}
        ):
            data = payload.get("d")
            if isinstance(data, Mapping):
                group_id = str(data.get("group_openid") or "").strip()
                author = data.get("author")
                member_id = str(author.get("member_openid") or "") if isinstance(author, Mapping) else ""
                allowed = getattr(self, "_is_group_allowed", None)
                callback = getattr(type(self), _CALLBACK_ATTR, None)
                discover = getattr(callback, "discover_group", None)
                try:
                    if group_id and callable(allowed) and not allowed(group_id, member_id) and callable(discover):
                        discover(group_id, str(payload["t"]))
                except Exception:
                    _log(self, "warning", "QQ group discovery failed")
        is_nonmention = (
            isinstance(payload, Mapping)
            and payload.get("op") == 0
            and payload.get("t") == "GROUP_MESSAGE_CREATE"
        )
        if is_nonmention:
            _advance_sequence(self, payload)
            current_callback = getattr(type(self), _CALLBACK_ATTR, None)
            if callable(current_callback):
                _schedule(self, payload, current_callback)
            # The observer owns selective admission; never let a future native
            # handler route the entire ambient stream to the agent.
            return None
        return original(self, payload)

    setattr(adapter_class, _CALLBACK_ATTR, callback)
    setattr(adapter_class, _LOGGER_ATTR, logger or _module_logger)
    setattr(adapter_class, _STATE_ATTR, {"original": original, "wrapper": wrapped})
    setattr(adapter_class, "_dispatch_payload", wrapped)
    return adapter_class


def uninstall_nonmention_observer() -> bool:
    """Restore the original adapter dispatch method if this shim is installed."""

    try:
        adapter_class = _resolve_adapter_class()
    except ImportError:
        return False
    state = getattr(adapter_class, _STATE_ATTR, None)
    if not isinstance(state, dict) or not callable(state.get("original")):
        return False
    setattr(adapter_class, "_dispatch_payload", state["original"])
    for attr in (_STATE_ATTR, _CALLBACK_ATTR, _LOGGER_ATTR):
        try:
            delattr(adapter_class, attr)
        except AttributeError:
            pass
    return True


__all__ = ["install_nonmention_observer", "uninstall_nonmention_observer"]
