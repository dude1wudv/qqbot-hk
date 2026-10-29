"""Safety bounded proactive member mentions for QQ group topics.

The normal observer deliberately keeps member OpenIDs out of durable state and
prompts.  This module keeps a short lived, process local routing table so a
maintenance tick can address a member who was recently seen in the same group.
It never writes OpenIDs to the store or audit log and claims a delivery before
the network call, which prevents uncertain sends from being retried as a burst.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Mapping


def _bounded(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


@dataclass(frozen=True)
class ProactiveMentionPolicy:
    enabled: bool = False
    cooldown_seconds: float = 3600.0
    max_per_day: int = 3
    max_groups: int = 256
    target_ttl_seconds: float = 86400.0

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> "ProactiveMentionPolicy":
        raw = config if isinstance(config, Mapping) else {}
        try:
            cooldown = max(60.0, float(raw.get("cooldown_seconds", 3600)))
        except (TypeError, ValueError):
            cooldown = 3600.0
        try:
            daily = max(0, min(24, int(raw.get("max_per_day", 3))))
        except (TypeError, ValueError):
            daily = 3
        try:
            groups = max(1, min(4096, int(raw.get("max_groups", 256))))
        except (TypeError, ValueError):
            groups = 256
        try:
            ttl = max(cooldown, min(7 * 86400.0, float(raw.get("target_ttl_seconds", 86400))))
        except (TypeError, ValueError):
            ttl = 86400.0
        return cls(
            enabled=bool(raw.get("enabled", False)),
            cooldown_seconds=cooldown,
            max_per_day=daily,
            max_groups=groups,
            target_ttl_seconds=ttl,
        )


@dataclass
class _Target:
    member_id: str
    display_name: str
    seen_at: float


class ProactiveMentionManager:
    """Select and claim one recent topic member at a time.

    ``member_id`` is only held in memory and is returned solely for composing
    the outgoing QQ mention.  Callers should pass the pseudonymous
    ``member_ref`` from :class:`AttentionManager` for all other bookkeeping.
    """

    def __init__(self, config: Mapping[str, Any] | None = None):
        self.policy = ProactiveMentionPolicy.from_config(config)
        self._targets: OrderedDict[str, dict[str, _Target]] = OrderedDict()
        self._claims: dict[tuple[str, str], tuple[float, str]] = {}
        self._daily: dict[str, deque[float]] = {}

    def register_target(
        self, group_id: Any, member_ref: Any, member_id: Any, display_name: Any = "", *, now: float | None = None
    ) -> bool:
        """Remember a transport target without persisting its OpenID."""
        if not self.policy.enabled:
            return False
        group = _bounded(group_id, 256)
        ref = _bounded(member_ref, 128)
        target = _bounded(member_id, 256)
        if not group or not ref or not target:
            return False
        stamp = time.time() if now is None else float(now)
        bucket = self._targets.get(group)
        if bucket is None:
            if len(self._targets) >= self.policy.max_groups:
                self._targets.popitem(last=False)
            bucket = {}
            self._targets[group] = bucket
        bucket[ref] = _Target(target, _bounded(display_name, 80), stamp)
        self._targets.move_to_end(group)
        self._prune(stamp)
        return True

    def _prune(self, now: float) -> None:
        cutoff = now - self.policy.target_ttl_seconds
        for group, bucket in list(self._targets.items()):
            for ref, item in list(bucket.items()):
                if item.seen_at < cutoff:
                    bucket.pop(ref, None)
            if not bucket:
                self._targets.pop(group, None)
        for group, rows in list(self._daily.items()):
            while rows and now - rows[0] >= 86400:
                rows.popleft()
            if not rows:
                self._daily.pop(group, None)

    def groups(self, *, now: float | None = None) -> tuple[str, ...]:
        """Return groups with a recently seen routable member."""
        stamp = time.time() if now is None else float(now)
        self._prune(stamp)
        return tuple(self._targets)

    @staticmethod
    def _topic_key(summary: str) -> str:
        return hashlib.sha256(_bounded(summary, 600).encode("utf-8")).hexdigest()[:16]

    def candidate(
        self, group_id: Any, topics: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...], *, now: float | None = None
    ) -> dict[str, str] | None:
        """Return a routable topic/member candidate, without consuming quota."""
        if not self.policy.enabled:
            return None
        stamp = time.time() if now is None else float(now)
        self._prune(stamp)
        group = _bounded(group_id, 256)
        bucket = self._targets.get(group, {})
        if not bucket:
            return None
        daily = self._daily.get(group, ())
        if self.policy.max_per_day and len(daily) >= self.policy.max_per_day:
            return None
        for topic in reversed(list(topics or ())):
            if not isinstance(topic, Mapping):
                continue
            summary = _bounded(topic.get("summary"), 600)
            if not summary:
                continue
            refs = topic.get("member_refs")
            if not isinstance(refs, (list, tuple)):
                continue
            for ref in reversed(refs):
                key = _bounded(ref, 128)
                target = bucket.get(key)
                if target is None:
                    continue
                claim = self._claims.get((group, key))
                if claim and stamp - claim[0] < self.policy.cooldown_seconds:
                    continue
                return {
                    "group_id": group,
                    "member_ref": key,
                    "member_id": target.member_id,
                    "display_name": target.display_name,
                    "summary": summary,
                    "topic_key": self._topic_key(summary),
                }
        return None

    def claim(self, candidate: Mapping[str, Any], *, now: float | None = None) -> bool:
        """Consume the cooldown before sending; never automatically release it."""
        if not self.policy.enabled:
            return False
        group = _bounded(candidate.get("group_id"), 256)
        ref = _bounded(candidate.get("member_ref"), 128)
        member_id = _bounded(candidate.get("member_id"), 256)
        topic_key = _bounded(candidate.get("topic_key"), 32)
        if not group or not ref or not member_id or not topic_key:
            return False
        stamp = time.time() if now is None else float(now)
        self._prune(stamp)
        daily = self._daily.setdefault(group, deque())
        if self.policy.max_per_day and len(daily) >= self.policy.max_per_day:
            return False
        previous = self._claims.get((group, ref))
        if previous and stamp - previous[0] < self.policy.cooldown_seconds:
            return False
        self._claims[(group, ref)] = (stamp, topic_key)
        daily.append(stamp)
        return True

    @staticmethod
    def render(candidate: Mapping[str, Any], message: str = "") -> str:
        """Build a short QQ plain-text message with one explicit member mention."""
        member_id = _bounded(candidate.get("member_id"), 256)
        summary = _bounded(message or candidate.get("summary"), 220)
        if not member_id or not summary:
            return ""
        if _bounded(candidate.get("group_id"), 256).startswith("dm:"):
            return f"你前面提到的「{summary[:160]}」后来怎么样了？"
        return f"<@{member_id}> 你前面提到的「{summary[:160]}」后来怎么样了？"


__all__ = ["ProactiveMentionPolicy", "ProactiveMentionManager"]
