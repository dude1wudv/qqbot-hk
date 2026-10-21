"""Bounded QQ image inputs, shared by the plugin and pinned gateway patch."""
from __future__ import annotations

import asyncio
import mimetypes
from collections.abc import Mapping
from pathlib import Path

MAX_IMAGES = 5
MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_TYPES = frozenset({"image", "image_url", "input_image"})


def _elements(payload):
    value = payload.get("msg_elements") if isinstance(payload, Mapping) else None
    return value if isinstance(value, list) else []


def payload_attachments(payload):
    """Use the same current/quoted attachment view for every QQ ingress path."""
    if not isinstance(payload, Mapping):
        return []
    attachments = []

    def collect_quoted(node):
        for element in _elements(node):
            if isinstance(element, Mapping):
                collect_quoted(element)
                value = element.get("attachments")
                if isinstance(value, list):
                    attachments.extend(value)

    if str(payload.get("message_type")) == "103":
        collect_quoted(payload)
    direct = payload.get("attachments")
    if isinstance(direct, list):
        attachments.extend(direct)
    return [item for item in attachments if isinstance(item, Mapping)]


def image_attachments(payload):
    return [a for a in payload_attachments(payload)
            if str(a.get("content_type", "")).lower().startswith("image/")]


def limit_attachments(attachments, limit=MAX_IMAGES):
    """Keep latest image attachments; leave non-image handling to the adapter."""
    if not isinstance(attachments, list):
        return []
    remaining = max(0, limit)
    result = []
    for item in reversed(attachments):
        if isinstance(item, Mapping) and str(item.get("content_type", "")).lower().startswith("image/"):
            if not remaining:
                continue
            remaining -= 1
        result.append(item)
    return list(reversed(result))


def limit_payload_images(payload):
    """Share one download budget across direct and quoted attachments."""
    if not isinstance(payload, Mapping):
        return {}
    remaining = MAX_IMAGES

    def bounded(node):
        nonlocal remaining
        result = dict(node)
        result["attachments"] = limit_attachments(node.get("attachments"), remaining)
        remaining -= len(image_attachments({"attachments": result["attachments"]}))
        if "msg_elements" in node:
            # Visit newer quoted elements first, after this message's images.
            result["msg_elements"] = list(reversed([
                bounded(element) if isinstance(element, Mapping) else element
                for element in reversed(_elements(node))
            ]))
        return result

    return bounded(payload)


def reply_reference_id(payload):
    if not isinstance(payload, Mapping):
        return ""
    direct = str(payload.get("reply_to_message_id") or "").strip()
    if direct:
        return direct
    for element in _elements(payload):
        if not isinstance(element, Mapping):
            continue
        for key in ("message_id", "msg_id", "id"):
            value = str(element.get(key) or "").strip()
            if value:
                return value
        nested = reply_reference_id(element)
        if nested:
            return nested
    return ""


def attachment_metadata(attachments):
    """Signed attachment URLs stay in transient dispatch data, never metadata."""
    return [{key: item[key] for key in ("content_type", "filename", "size")
             if key in item and item[key] is not None}
            for item in attachments if isinstance(item, Mapping)]


def quoted_metadata(payload):
    result = []
    for element in _elements(payload):
        if not isinstance(element, Mapping):
            continue
        item = {key: element[key] for key in ("message_id", "msg_id", "id", "message_type", "content")
                if key in element}
        attachments = element.get("attachments")
        if isinstance(attachments, list):
            item["attachments"] = attachment_metadata(attachments)
        if "msg_elements" in element:
            item["msg_elements"] = quoted_metadata(element)
        result.append(item)
    return result


def trim_images(messages, limit=MAX_IMAGES):
    """Retire oldest image PARTS in place, including tool and API sidecar content.

    Sidecars replace content on replay. Budget both possible views conservatively,
    without turning ephemeral API context into durable user-authored text.
    """
    remaining = max(0, limit)

    def visit(value):
        nonlocal remaining
        if isinstance(value, list):
            return list(reversed([visit(part) for part in reversed(value)]))
        if not isinstance(value, dict):
            return value
        if value.get("type") in IMAGE_TYPES:
            if remaining:
                remaining -= 1
                return value
            return {"type": "text", "text": "[较早图片已从上下文清理]"}
        result = dict(value)
        if "content" in result:
            result["content"] = visit(result["content"])
        return result

    for message in reversed(messages or []):
        if not isinstance(message, dict):
            continue
        before = remaining
        message["content"] = visit(message.get("content"))
        content_remaining = remaining
        if "api_content" in message:
            remaining = before
            message["api_content"] = visit(message["api_content"])
            remaining = min(remaining, content_remaining)
    return messages


async def image_inputs(paths, roots):
    """Read only bounded regular cache files, never URLs or arbitrary local paths."""
    roots = tuple(Path(str(root)).resolve() for root in roots)

    def load(raw):
        try:
            path = Path(raw).resolve(strict=True)
            if not path.is_file() or not any(path.is_relative_to(root) for root in roots):
                return None
            mime = mimetypes.guess_type(path.name)[0] or ""
            if not mime.startswith("image/") or path.stat().st_size > MAX_IMAGE_BYTES:
                return None
            with path.open("rb") as stream:
                data = stream.read(MAX_IMAGE_BYTES + 1)
            if not data or len(data) > MAX_IMAGE_BYTES:
                return None
            return {"type": "image", "data": data, "mime_type": mime, "file_name": path.name}
        except (OSError, RuntimeError, ValueError):
            return None

    result = []
    for path in list(dict.fromkeys(paths))[-MAX_IMAGES:]:
        block = await asyncio.to_thread(load, path)
        if block:
            result.append(block)
    return result
