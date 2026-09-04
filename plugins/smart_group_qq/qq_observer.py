"""Read-only observation of QQ group messages without an @ mention.

Hermes v0.21.0 only routes ``GROUP_AT_MESSAGE_CREATE`` into the normal agent
pipeline.  This module adds a deliberately narrow compatibility shim for the
gateway dispatch method so a caller can build a group index from
``GROUP_MESSAGE_CREATE`` events without sending them to the agent.

The shim is intentionally self-contained.  It does not alter the adapter's
normal duplicate cache, message handler, or outbound send path.
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

# Keep this cache independent from QQAdapter._seen_messages.  The latter is
# used by normal agent-facing events and sharing it would cause an @ message
# with the same id to be dropped by Hermes.
_SEEN_WINDOW_SECONDS = 60 * 60
_SEEN_MAX_SIZE = 10_000

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


def _advance_sequence(adapter: Any, payload: Mapping[str, Any]) -> None:
    """Preserve the sequence bookkeeping done at the top of Hermes dispatch."""

    sequence = payload.get("s")
    previous = getattr(adapter, "_last_seq", None)
    if isinstance(sequence, int) and (previous is None or sequence > previous):
        setattr(adapter, "_last_seq", sequence)


def _claim_message_id(adapter: Any, message_id: str) -> bool:
    """Atomically claim an observed id in a cache private to this shim."""

    now = time.monotonic()
    seen = getattr(adapter, _SEEN_ATTR, None)
    if not isinstance(seen, dict):
        seen = {}
        setattr(adapter, _SEEN_ATTR, seen)

    if len(seen) > _SEEN_MAX_SIZE:
        cutoff = now - _SEEN_WINDOW_SECONDS
        for key, timestamp in list(seen.items()):
            if timestamp <= cutoff:
                seen.pop(key, None)
        # A burst of unique events should not make the private cache grow
        # without bound even when all entries are still within the time window.
        if len(seen) > _SEEN_MAX_SIZE:
            oldest = sorted(seen, key=seen.get)[: len(seen) - _SEEN_MAX_SIZE]
            for key in oldest:
                seen.pop(key, None)

    if message_id in seen:
        return False
    seen[message_id] = now
    return True


def _minimal_raw(data: Mapping[str, Any], group_id: str, member_id: str, message_id: str) -> dict[str, Any]:
    """Keep only useful, non-URL event metadata in the callback record."""

    author = {"member_openid": member_id} if member_id else {}
    raw: dict[str, Any] = {
        "id": message_id,
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


async def _observe_message(adapter: Any, payload: Mapping[str, Any], callback: Observer) -> None:
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

    if not _claim_message_id(adapter, message_id):
        return

    raw_content = str(data.get("content", "") or "").strip()
    text = raw_content
    image_paths: list[Any] = []
    media_types: list[Any] = []
    attachment_info = ""

    processor = getattr(adapter, "_process_attachments", None)
    if callable(processor):
        try:
            result = await processor(data.get("attachments"))
            if isinstance(result, Mapping):
                image_paths = list(_attachment_value(result, "image_paths", "image_urls") or [])
                media_types = list(
                    _attachment_value(result, "media_types", "image_media_types") or []
                )
                voices = list(result.get("voice_transcripts") or [])
                attachment_info = str(result.get("attachment_info", "") or "").strip()
                extra_parts = [str(item).strip() for item in voices if str(item).strip()]
                if attachment_info:
                    extra_parts.append(attachment_info)
                if extra_parts:
                    text = "\n\n".join(part for part in [text, *extra_parts] if part).strip()
        except Exception:
            _log(adapter, "warning", "QQ non-mention attachment processing failed", exc_info=True)

    if not text and not image_paths and not attachment_info:
        return

    timestamp_raw = str(data.get("timestamp", "") or "")
    parser = getattr(adapter, "_parse_qq_timestamp", None)
    try:
        timestamp = parser(timestamp_raw) if callable(parser) else datetime.now(tz=timezone.utc)
    except Exception:
        _log(adapter, "warning", "QQ non-mention timestamp parsing failed", exc_info=True)
        timestamp = datetime.now(tz=timezone.utc)

    record: dict[str, Any] = {
        "group_id": group_id,
        "member_id": member_id,
        "message_id": message_id,
        "text": text,
        "timestamp": timestamp,
        "image_paths": image_paths,
        "media_types": media_types,
        "attachment_info": attachment_info,
        "raw": _minimal_raw(data, group_id, member_id, message_id),
    }

    try:
        result = callback(record)
        if inspect.isawaitable(result):
            await result
    except asyncio.CancelledError:
        raise
    except Exception:
        # Observer consumers are intentionally outside Hermes' normal message
        # path.  Their failures must never surface as gateway dispatch errors.
        _log(adapter, "warning", "QQ non-mention observer callback failed", exc_info=True)


def _schedule(adapter: Any, payload: Mapping[str, Any], callback: Observer) -> None:
    """Schedule observation only when a running event loop is available."""

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        task = loop.create_task(_observe_message(adapter, payload, callback))
        # _observe_message isolates expected failures.  Retrieving an
        # unexpected task exception prevents "Task exception was never
        # retrieved" from obscuring the gateway logs.
        task.add_done_callback(_consume_task_exception)
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
            # Do not call the original method for this event.  v0.21.0 treats
            # it as unknown, but bypassing it keeps this guarantee intact if a
            # later adapter version starts routing the event to the agent.
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
