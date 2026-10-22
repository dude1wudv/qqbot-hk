"""Bounded, process-local attention state for proactive group participation."""
from __future__ import annotations

import math
import time
from collections import OrderedDict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .knowledge import lexical_score


class AttentionMode(str, Enum):
    PASSIVE = "PASSIVE"
    ACTIVE_TOPIC = "ACTIVE_TOPIC"
    DIRECT_CONVERSATION = "DIRECT_CONVERSATION"


MODE_HINTS = frozenset({"auto", "casual", "task", "support"})
MAX_FOCUS_MESSAGES = 6


def _message_ids(values: Any) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, Mapping)) or not isinstance(values, Iterable):
        return ()
    return tuple(dict.fromkeys(
        value for value in values if isinstance(value, str) and 0 < len(value) <= 256
    ))


@dataclass(frozen=True)
class ParticipationDecision:
    reply: bool = False
    confidence: float = 0.0
    engaged: bool = False
    topic_id: str | None = None
    focus_message_ids: tuple[str, ...] = ()
    mode_hint: str = "auto"


def parse_participation_decision(
    value: Any,
    *,
    candidate_message_ids: Iterable[str],
    candidate_topic_ids: Iterable[str] = (),
    min_confidence: float = 0.70,
) -> ParticipationDecision:
    """Keep the participation gate strict; discard unusable optional focus metadata.

    IDs are references into the supplied prompt, never new transport destinations.
    Older three-field classifier results remain valid.
    """
    if not isinstance(value, Mapping):
        return ParticipationDecision()
    confidence = value.get("confidence")
    if (type(value.get("reply")) is not bool or type(value.get("engaged")) is not bool
            or type(confidence) not in (int, float) or not 0 <= confidence <= 1
            or not math.isfinite(confidence)):
        return ParticipationDecision()
    if (type(min_confidence) not in (int, float) or not 0 <= min_confidence <= 1
            or not math.isfinite(min_confidence)):
        return ParticipationDecision()
    threshold = float(min_confidence)
    admitted = value["reply"] and confidence >= threshold
    if not admitted:
        return ParticipationDecision(confidence=float(confidence))
    topics = set(_message_ids(candidate_topic_ids))
    messages = set(_message_ids(candidate_message_ids))
    topic_id = value.get("topic_id")
    if not isinstance(topic_id, str) or topic_id not in topics:
        topic_id = None
    focus = tuple(item for item in _message_ids(value.get("focus_message_ids", ())) if item in messages)
    mode = value.get("mode_hint", "auto")
    if not isinstance(mode, str) or mode not in MODE_HINTS:
        mode = "auto"
    return ParticipationDecision(
        reply=True, confidence=float(confidence), engaged=value["engaged"],
        topic_id=topic_id, focus_message_ids=focus[:MAX_FOCUS_MESSAGES], mode_hint=mode,
    )


@dataclass
class TopicState:
    id: str
    mode: AttentionMode = AttentionMode.ACTIVE_TOPIC
    member_ref: str = ""
    member_refs: deque[str] = field(default_factory=lambda: deque(maxlen=8))
    last_answered_question: str = ""
    expires_at: float = 0.0
    last_touched: float = 0.0
    focus_message_ids: tuple[str, ...] = ()
    mode_hint: str = "auto"


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
    topics: OrderedDict[str, TopicState] = field(default_factory=OrderedDict)
    current_topic_id: str | None = None
    reply_topics: dict[str, str] = field(default_factory=dict)


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
        max_topics_per_group: int = 3,
    ) -> None:
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.relevance_threshold = max(0.0, min(1.0, float(relevance_threshold)))
        self.max_groups = max(1, int(max_groups))
        self.reply_ttl_seconds = max(1.0, float(reply_ttl_seconds))
        self.max_reply_ids = max(1, int(max_reply_ids))
        self.max_interjections_per_minute = max(0, int(max_interjections_per_minute))
        self.unanswered_pause_seconds = max(0, float(unanswered_pause_seconds))
        self.max_topics_per_group = max(1, min(3, int(max_topics_per_group)))
        self._groups: OrderedDict[str, AttentionState] = OrderedDict()
        self._next_topic_id = 0

    def _state(self, group_id: Any, *, create: bool = False, now: float | None = None) -> AttentionState | None:
        key = str(group_id)
        state = self._groups.get(key)
        if state is None and create:
            if len(self._groups) >= self.max_groups:
                stamp = time.monotonic() if now is None else now
                for item in self._groups.values():
                    self._prune_topics(item, stamp)
                passive = next((name for name, item in self._groups.items() if item.mode == AttentionMode.PASSIVE), None)
                if passive is None:
                    return None
                self._groups.pop(passive, None)
            state = AttentionState()
            self._groups[key] = state
        if state is not None:
            self._groups.move_to_end(key)
        return state

    def can_accept(self, group_id: Any, *, now: float | None = None) -> bool:
        key = str(group_id)
        if key not in self._groups and len(self._groups) >= self.max_groups:
            stamp = time.monotonic() if now is None else now
            for state in self._groups.values():
                self._prune_topics(state, stamp)
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
        self._prune_topics(state, stamp)
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
        topic_id: str | None = None,
        reference_id: str | None = None,
        focus_message_ids: Iterable[str] = (),
        mode_hint: str = "auto",
    ) -> str | None:
        stamp = time.monotonic() if now is None else float(now)
        state = self._state(group_id, create=True, now=stamp)
        if state is None:
            return
        self._prune_reply_ids(state, stamp)
        self._prune_topics(state, stamp)
        topic = self._find_topic(state, question, topic_id=topic_id, reference_id=reference_id)
        if topic is None:
            if len(state.topics) >= self.max_topics_per_group:
                self._drop_topic(state, next(iter(state.topics)))
            self._next_topic_id += 1
            topic = TopicState(id=f"topic-{self._next_topic_id}")
            state.topics[topic.id] = topic
        topic.mode = AttentionMode.DIRECT_CONVERSATION if direct else AttentionMode.ACTIVE_TOPIC
        topic.member_ref = str(member_ref or "")
        if topic.member_ref:
            if topic.member_ref in topic.member_refs:
                topic.member_refs.remove(topic.member_ref)
            topic.member_refs.append(topic.member_ref)
        topic.last_answered_question = str(question or "").strip()[:1000]
        topic.expires_at = stamp + self.ttl_seconds
        topic.last_touched = stamp
        topic.focus_message_ids = _message_ids(focus_message_ids)[:MAX_FOCUS_MESSAGES]
        topic.mode_hint = mode_hint if isinstance(mode_hint, str) and mode_hint in MODE_HINTS else "auto"
        state.topics.move_to_end(topic.id)
        state.current_topic_id = topic.id
        self._refresh_focus(state)
        state.last_touched = stamp
        if direct:
            self.note_engagement(group_id)
        else:
            state.unanswered += 1
            if self.unanswered_pause_seconds > 0 and state.unanswered >= 2:
                state.quiet_until = stamp + self.unanswered_pause_seconds
        if message_id:
            self.remember_reply(group_id, message_id, topic_id=topic.id, now=stamp)
        return topic.id

    def remember_reply(
        self, group_id: Any, message_id: Any, *, topic_id: str | None = None, now: float | None = None,
    ) -> None:
        stamp = time.monotonic() if now is None else now
        state = self._state(group_id)
        if state is not None and message_id:
            self._prune_topics(state, stamp)
            reply_id = str(message_id)
            state.reply_ids = deque((value, sent) for value, sent in state.reply_ids if value != reply_id)
            state.reply_ids.append((reply_id, stamp))
            topic_id = topic_id or state.current_topic_id
            state.reply_topics.pop(reply_id, None)
            if topic_id in state.topics:
                state.reply_topics[reply_id] = topic_id
            self._prune_reply_ids(state, stamp)

    def continuation_score(self, group_id: Any, member_ref: str, text: str, *, now: float | None = None) -> int:
        state = self.get(group_id, now=now)
        if state.mode == AttentionMode.PASSIVE:
            return 0
        return 20 if self._find_topic(state, text) is not None else 0

    def topic_context(self, group_id: Any, *, now: float | None = None) -> list[dict[str, Any]]:
        """Prompt-only snapshots, newest first; reading them never renews a topic."""
        state = self.get(group_id, now=now)
        return [
            {"id": topic.id, "member_refs": list(topic.member_refs),
             "summary": topic.last_answered_question[:300], "mode_hint": topic.mode_hint,
             "focus_message_ids": list(topic.focus_message_ids)}
            for topic in reversed(state.topics.values())
        ]

    def direct_decision(
        self, group_id: Any, member_ref: str, text: str, *, message_id: str,
        reference_id: str | None = None, candidate_message_ids: Iterable[str] = (),
        now: float | None = None,
    ) -> ParticipationDecision:
        """Build an addressed turn without a model call or speculative state write.

        A quote supplies topic continuity, not the previous turn's response mode.
        The current event is always an eligible focus; other IDs must be supplied.
        """
        state = self.get(group_id, now=now)
        topic = self._find_topic(state, text, reference_id=reference_id)
        candidates = set(_message_ids(candidate_message_ids))
        current = _message_ids((message_id,))
        candidates.update(current)
        focus = tuple(value for value in _message_ids((*current, reference_id)) if value in candidates)
        return ParticipationDecision(
            reply=True, confidence=1.0, engaged=True, topic_id=topic.id if topic else None,
            focus_message_ids=focus, mode_hint="auto",
        )

    def note_engagement(self, group_id: Any) -> None:
        state = self._state(group_id)
        if state is not None:
            state.unanswered = 0
            state.quiet_until = 0.0

    def can_interject(self, group_id: Any, *, engaged: bool = False, now: float | None = None) -> bool:
        stamp = time.monotonic() if now is None else now
        state = self._state(group_id, create=True, now=stamp)
        if state is None:
            return False
        while state.participation_attempts and stamp - state.participation_attempts[0] >= 60:
            state.participation_attempts.popleft()
        if state.quiet_until and stamp >= state.quiet_until:
            state.unanswered = 0
            state.quiet_until = 0.0
        return (self.max_interjections_per_minute == 0 or len(state.participation_attempts) < self.max_interjections_per_minute) and (
            engaged or stamp >= state.quiet_until
        )

    def reserve_interjection(self, group_id: Any, *, engaged: bool = False) -> bool:
        """Budget admissions, including in-flight/ignored turns, to prevent bursts."""
        if not self.can_interject(group_id, engaged=engaged):
            return False
        state = self._state(group_id)
        if engaged:
            self.note_engagement(group_id)
        if self.max_interjections_per_minute:
            state.participation_attempts.append(time.monotonic())
        return True

    def is_active_member(self, group_id: Any, member_ref: str, *, now: float | None = None) -> bool:
        state = self.get(group_id, now=now)
        return bool(member_ref and any(str(member_ref) in topic.member_refs for topic in state.topics.values()))

    def has_reply_id(self, group_id: Any, message_id: Any, *, now: float | None = None) -> bool:
        state = self.get(group_id, now=now)
        needle = str(message_id or "")
        return bool(needle and any(value == needle for value, _ in state.reply_ids))

    def clear(self, group_id: Any) -> None:
        self._groups.pop(str(group_id), None)

    def _prune_reply_ids(self, state: AttentionState, now: float) -> None:
        while state.reply_ids and now - state.reply_ids[0][1] > self.reply_ttl_seconds:
            state.reply_topics.pop(state.reply_ids.popleft()[0], None)
        while len(state.reply_ids) > self.max_reply_ids:
            state.reply_topics.pop(state.reply_ids.popleft()[0], None)

    @staticmethod
    def _drop_topic(state: AttentionState, topic_id: str) -> None:
        state.topics.pop(topic_id, None)
        state.reply_topics = {key: value for key, value in state.reply_topics.items() if value != topic_id}

    @staticmethod
    def _refresh_focus(state: AttentionState) -> None:
        if state.current_topic_id not in state.topics:
            state.current_topic_id = next(reversed(state.topics), None)
        topic = state.topics.get(state.current_topic_id)
        state.mode = topic.mode if topic else AttentionMode.PASSIVE
        state.member_ref = topic.member_ref if topic else ""
        state.last_answered_question = topic.last_answered_question if topic else ""
        state.expires_at = topic.expires_at if topic else 0.0

    def _prune_topics(self, state: AttentionState, now: float) -> None:
        for topic_id, topic in list(state.topics.items()):
            if now >= topic.expires_at:
                self._drop_topic(state, topic_id)
        self._refresh_focus(state)

    def _find_topic(
        self, state: AttentionState, text: str, *, topic_id: str | None = None,
        reference_id: str | None = None,
    ) -> TopicState | None:
        if isinstance(topic_id, str) and topic_id in state.topics:
            return state.topics[topic_id]
        referenced_topic = state.reply_topics.get(str(reference_id or ""))
        if referenced_topic in state.topics:
            return state.topics[referenced_topic]
        if not text or not state.topics:
            return None
        ranked = [(lexical_score(str(text)[:6000], topic.last_answered_question), topic.last_touched, topic)
                  for topic in state.topics.values()]
        score, _, topic = max(ranked, key=lambda item: (item[0], item[1]))
        return topic if score >= self.relevance_threshold else None


__all__ = [
    "AttentionManager", "AttentionMode", "AttentionState", "TopicState",
    "ParticipationDecision", "parse_participation_decision", "MODE_HINTS",
]
