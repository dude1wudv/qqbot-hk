"""Bounded, process-local attention state for proactive group participation."""
from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .knowledge import lexical_score


class AttentionMode(str, Enum):
    PASSIVE = "PASSIVE"
    ACTIVE_TOPIC = "ACTIVE_TOPIC"
    DIRECT_CONVERSATION = "DIRECT_CONVERSATION"


@dataclass
class AttentionState:
    mode: AttentionMode = AttentionMode.PASSIVE
    member_ref: str = ""
    last_answered_question: str = ""
    expires_at: float = 0.0
    last_touched: float = field(default_factory=time.monotonic)
    reply_ids: deque[tuple[str, float]] = field(default_factory=deque)
    participation_attempts: deque[float] = field(default_factory=deque)
    unanswered: int = 0
    quiet_until: float = 0.0


class AttentionManager:
    def __init__(
        self,
        *,
        ttl_seconds: float = 120.0,
        relevance_threshold: float = 0.35,
        max_groups: int = 256,
        reply_ttl_seconds: float = 600.0,
        max_reply_ids: int = 100,
        max_interjections_per_minute: int = 2,
        unanswered_pause_seconds: float = 300,
    ) -> None:
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.relevance_threshold = max(0.0, min(1.0, float(relevance_threshold)))
        self.max_groups = max(1, int(max_groups))
        self.reply_ttl_seconds = max(1.0, float(reply_ttl_seconds))
        self.max_reply_ids = max(1, int(max_reply_ids))
        self.max_interjections_per_minute = max(1, int(max_interjections_per_minute))
        self.unanswered_pause_seconds = max(0, float(unanswered_pause_seconds))
        self._groups: OrderedDict[str, AttentionState] = OrderedDict()

    def _state(self, group_id: Any, *, create: bool = False) -> AttentionState | None:
        key = str(group_id)
        state = self._groups.get(key)
        if state is None and create:
            if len(self._groups) >= self.max_groups:
                passive = next((name for name, item in self._groups.items() if item.mode == AttentionMode.PASSIVE), None)
                if passive is None:
                    return None
                self._groups.pop(passive, None)
            state = AttentionState()
            self._groups[key] = state
        if state is not None:
            self._groups.move_to_end(key)
        return state
    def can_accept(self, group_id: Any) -> bool:
        key = str(group_id)
        return (
            key in self._groups
            or len(self._groups) < self.max_groups
            or any(item.mode == AttentionMode.PASSIVE for item in self._groups.values())
        )

    def get(self, group_id: Any, *, now: float | None = None) -> AttentionState:
        stamp = time.monotonic() if now is None else float(now)
        state = self._state(group_id)
        if state is None:
            return AttentionState(last_touched=stamp)
        self._prune_reply_ids(state, stamp)
        if state.mode != AttentionMode.PASSIVE and stamp >= state.expires_at:
            state.mode = AttentionMode.PASSIVE
            state.member_ref = ""
            state.last_answered_question = ""
            state.expires_at = 0.0
        state.last_touched = stamp
        return state

    def record_success(
        self,
        group_id: Any,
        member_ref: str,
        question: str,
        *,
        direct: bool,
        message_id: Any = None,
        now: float | None = None,
    ) -> None:
        stamp = time.monotonic() if now is None else float(now)
        state = self._state(group_id, create=True)
        if state is None:
            return
        state.mode = AttentionMode.DIRECT_CONVERSATION if direct else AttentionMode.ACTIVE_TOPIC
        state.member_ref = str(member_ref or "")
        state.last_answered_question = str(question or "").strip()[:1000]
        state.expires_at = stamp + self.ttl_seconds
        state.last_touched = stamp
        if direct:
            self.note_engagement(group_id)
        else:
            state.unanswered += 1
            if state.unanswered >= 2:
                state.quiet_until = stamp + self.unanswered_pause_seconds
        if message_id:
            self.remember_reply(group_id, message_id, now=stamp)

    def remember_reply(self, group_id: Any, message_id: Any, *, now: float | None = None) -> None:
        stamp = time.monotonic() if now is None else now
        state = self._state(group_id)
        if state is not None and message_id:
            state.reply_ids.append((str(message_id), stamp))
            self._prune_reply_ids(state, stamp)

    def continuation_score(self, group_id: Any, member_ref: str, text: str, *, now: float | None = None) -> int:
        state = self.get(group_id, now=now)
        if state.mode == AttentionMode.PASSIVE:
            return 0
        return 20 if lexical_score(text, state.last_answered_question) >= self.relevance_threshold else 0

    def note_engagement(self, group_id: Any) -> None:
        state = self._state(group_id)
        if state is not None:
            state.unanswered = 0
            state.quiet_until = 0.0

    def can_interject(self, group_id: Any, *, engaged: bool = False, now: float | None = None) -> bool:
        stamp = time.monotonic() if now is None else now
        state = self._state(group_id, create=True)
        if state is None:
            return False
        while state.participation_attempts and stamp - state.participation_attempts[0] >= 60:
            state.participation_attempts.popleft()
        if state.quiet_until and stamp >= state.quiet_until:
            state.unanswered = 0
            state.quiet_until = 0.0
        return len(state.participation_attempts) < self.max_interjections_per_minute and (
            engaged or stamp >= state.quiet_until
        )

    def reserve_interjection(self, group_id: Any, *, engaged: bool = False) -> bool:
        """Budget admissions, including in-flight/ignored turns, to prevent bursts."""
        if not self.can_interject(group_id, engaged=engaged):
            return False
        state = self._state(group_id)
        if engaged:
            self.note_engagement(group_id)
        state.participation_attempts.append(time.monotonic())
        return True

    def is_active_member(self, group_id: Any, member_ref: str, *, now: float | None = None) -> bool:
        state = self.get(group_id, now=now)
        return state.mode != AttentionMode.PASSIVE and state.member_ref == str(member_ref or "")

    def has_reply_id(self, group_id: Any, message_id: Any, *, now: float | None = None) -> bool:
        state = self.get(group_id, now=now)
        needle = str(message_id or "")
        return bool(needle and any(value == needle for value, _ in state.reply_ids))

    def clear(self, group_id: Any) -> None:
        self._groups.pop(str(group_id), None)

    def _prune_reply_ids(self, state: AttentionState, now: float) -> None:
        while state.reply_ids and now - state.reply_ids[0][1] > self.reply_ttl_seconds:
            state.reply_ids.popleft()
        while len(state.reply_ids) > self.max_reply_ids:
            state.reply_ids.popleft()


__all__ = ["AttentionManager", "AttentionMode", "AttentionState"]
