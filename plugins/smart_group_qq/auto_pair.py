"""Time-bounded QQ direct-message enrollment into Hermes pairing."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping

logger = logging.getLogger(__name__)


def utc_deadline(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _platform_name(source: Any) -> str:
    platform = getattr(source, "platform", "")
    return str(getattr(platform, "value", platform)).lower()


def auto_pair_qq_dm(
    config: Mapping[str, Any],
    gateway: Any,
    source: Any,
    *,
    now: datetime | None = None,
) -> bool:
    """Persist one QQ DM pairing grant during an explicit enrollment window."""
    if not bool(config.get("enabled", False)):
        return False
    deadline = utc_deadline(config.get("until_utc"))
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return False
    if deadline is None or current.astimezone(timezone.utc) >= deadline:
        return False
    if _platform_name(source) != "qqbot" or getattr(source, "chat_type", "") != "dm":
        return False
    user_id = str(getattr(source, "user_id", "") or "").strip()
    if not user_id or gateway is None:
        return False

    try:
        resolver = getattr(gateway, "_pairing_store_for", None)
        pairing_store = resolver(source) if callable(resolver) else getattr(gateway, "pairing_store", None)
        lock = getattr(pairing_store, "_lock", None)
        approve = getattr(pairing_store, "_approve_user", None)
        is_approved = getattr(pairing_store, "is_approved", None)
        if lock is None or not callable(approve) or not callable(is_approved):
            return False
        with lock:
            if is_approved("qqbot", user_id):
                return False
            approve("qqbot", user_id, str(getattr(source, "user_name", "") or ""))
        logger.info("auto-approved one QQ DM sender during the bounded enrollment window")
        return True
    except Exception:
        logger.warning("bounded QQ auto-pair failed closed", exc_info=True)
        return False


def build_auto_pair_handler(ctx: Any):
    config = ctx.get_config("auto_pair", {})
    config = config if isinstance(config, Mapping) else {}

    def handle(event: Any = None, gateway: Any = None, **_: Any):
        source = getattr(event, "source", None)
        if source is not None:
            auto_pair_qq_dm(config, gateway, source)
        return {"action": "allow"}

    return handle


__all__ = ["auto_pair_qq_dm", "build_auto_pair_handler", "utc_deadline"]
