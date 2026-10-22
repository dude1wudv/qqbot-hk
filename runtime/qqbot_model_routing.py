"""QQ main-turn routing and a single pre-output native model fallback.

The gateway's configured model and native /model and /reasoning resolution own
the primary route. Auxiliary calls do not pass through this module.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
import logging
import threading
from typing import Any


FAST_MODEL = "deepseek/deepseek-v4.1-flash-fast"
FLASH_MODEL = "deepseek/deepseek-v4.1-flash"
PROVIDER = "sub2api_deepseek"
BASE_URL = "http://sub2api:8080/v1"
RETRYABLE_REASONS = frozenset({"rate_limit", "upstream_rate_limit", "overloaded", "server_error", "timeout"})
_STATE = "_qq_model_route_state"
_CURRENT = object()
logger = logging.getLogger(__name__)


class ModelRoutingBusy(RuntimeError):
    """A model control cannot evict the agent executing the current task."""


@dataclass
class TurnRoutingState:
    manual: bool
    enabled: bool
    reasoning: Any
    attempted: bool = False
    progressed: bool = False
    progress_reason: str = ""
    fallback_active: bool = False
    lock: Any = field(default_factory=threading.RLock, repr=False)


def _platform(value: Any) -> str:
    platform = getattr(value, "platform", None)
    return str(getattr(platform, "value", platform) or "")


def _session_key(runner, source, session_key=None):
    if _platform(source) != "qqbot":
        raise ValueError("QQ model controls require a QQ source")
    key = session_key or runner._session_key_for_source(source)
    if not key:
        raise ValueError("QQ session key unavailable")
    return key


def _manual_override(runner, session_key):
    reader = getattr(runner, "_session_model_override", None)
    override = reader(session_key) if callable(reader) else None
    if not isinstance(override, Mapping) or not override:
        store = getattr(runner, "session_store", None)
        reader = getattr(store, "get_model_override", None)
        override = reader(session_key) if callable(reader) else None
    return override if isinstance(override, Mapping) and override else None


def session_route_status(runner, source, session_key=None):
    """Read only safe model/effort fields; never resolve or return credentials."""
    key = _session_key(runner, source, session_key)
    override = _manual_override(runner, key)
    model = str((override or {}).get("model") or FAST_MODEL)
    reason = runner._resolve_session_reasoning_config(source=source, session_key=key, model=model)
    state = runner._peek_session_state(key)
    explicit = state is not None and state.conversation.reasoning_override is not None
    effort = "off" if isinstance(reason, Mapping) and reason.get("enabled") is False else (
        str(reason.get("effort") or "high") if isinstance(reason, Mapping) else "high"
    )
    return {"mode": "manual" if override else "auto", "model": model, "reasoning": effort,
            "reasoning_manual": explicit, "background_model": FLASH_MODEL, "background_reasoning": "low"}


async def clear_manual_model(runner, source, session_key=None):
    """Clear only native model selection at an idle boundary, preserving the transcript/effort."""
    key = _session_key(runner, source, session_key)
    state = runner._peek_session_state(key)
    turn = getattr(state, "turn", None)
    if turn is not None and (getattr(turn, "agent", None) is not None or getattr(turn, "started_ts", 0)):
        raise ModelRoutingBusy("QQ model switch waits for the current turn")
    restore = getattr(runner, "_restore_session_model_override", None)
    # This tiny native durable write and cache eviction must share the idle
    # boundary. Awaiting AsyncSessionStore here would let a new turn start
    # between the busy check and eviction.
    store = getattr(runner, "session_store", None)
    setter = getattr(store, "set_model_override", None)
    if not callable(setter) or not callable(restore):
        raise RuntimeError("Native session model control unavailable")
    setter(key, None)
    # The native restore helper evicts the cached agent without a session reset.
    if state is not None:
        state.conversation.one_turn_restore = None
        state.conversation.last_resolved_model = ""
    restore(key, {"had_override": False, "override": None})


def capture_route_state(agent):
    state = getattr(agent, _STATE, None)
    return state if isinstance(state, TurnRoutingState) else None


def configure_turn(agent, runner, *, source, session_key):
    """Run after native callbacks and reasoning are rebound, once per main QQ turn."""
    if _platform(source) != "qqbot" or _platform(agent) != "qqbot":
        return
    manual = bool(_manual_override(runner, session_key))
    provider = str(getattr(agent, "provider", "") or "").removeprefix("custom:")
    known_route = (provider == PROVIDER
                   and str(getattr(agent, "base_url", "") or "").rstrip("/") == BASE_URL
                   and getattr(agent, "api_mode", None) == "chat_completions")
    state = TurnRoutingState(manual=manual, enabled=known_route and not manual,
                             reasoning=deepcopy(getattr(agent, "reasoning_config", None)))
    setattr(agent, _STATE, state)
    # The native cached-agent refresh runs before this hook. No global fallback
    # or hand-selected model may widen this turn's same-provider policy.
    chain = [{"provider": PROVIDER, "model": FLASH_MODEL, "api_mode": "chat_completions"}] if state.enabled else []
    agent._fallback_chain = chain
    agent._fallback_model = chain[0] if chain else None
    agent._fallback_index = 0
    # Native primary restoration runs at the next conversation prologue; its
    # snapshot must not replace this turn's explicit /reasoning selection.
    primary = getattr(agent, "_primary_runtime", None)
    if isinstance(primary, dict):
        primary["reasoning_config"] = deepcopy(state.reasoning)
    logger.info("QQ_MODEL_ROUTE mode=%s selected=%s", "manual" if manual else "auto",
                getattr(agent, "model", "") if getattr(agent, "model", "") in {FAST_MODEL, FLASH_MODEL} else "other")


def note_progress(agent, reason, *, state=_CURRENT):
    """Latch once; request-local tokens prevent a stale worker affecting a newer turn."""
    current = capture_route_state(agent)
    state = current if state is _CURRENT else state
    if state is None or current is not state:
        return
    with state.lock:
        if not state.progressed:
            model = getattr(agent, "model", "")
            effort = state.reasoning or {}
            effort = ("off" if isinstance(effort, Mapping) and effort.get("enabled") is False
                      else effort.get("effort", "high") if isinstance(effort, Mapping) else "high")
            logger.info("QQ_MODEL_ROUTE actual=%s effort=%s progress=%s",
                        model if model in {FAST_MODEL, FLASH_MODEL} else "other", effort, reason)
        state.progressed = True
        state.progress_reason = state.progress_reason or reason
        agent._fallback_index = len(getattr(agent, "_fallback_chain", ()) or ())


def _field(value, key):
    return value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)


def note_stream_chunk(agent, chunk, *, state=_CURRENT):
    """Observe model output even when QQ hides streaming and thinking UI."""
    for choice in _field(chunk, "choices") or ():
        delta = _field(choice, "delta")
        extra = _field(delta, "model_extra") or {}
        fields = ("content", "reasoning", "reasoning_content", "reasoning_details", "tool_calls", "refusal")
        if any(_field(delta, key) or _field(extra, key) for key in fields):
            note_progress(agent, "stream", state=state)
            return


def observe_response(agent, response, *, state=_CURRENT):
    if response is not None:
        note_progress(agent, "response", state=state)
    return response


def preserve_reasoning(agent):
    state = capture_route_state(agent)
    if state is None:
        return False
    agent.reasoning_config = deepcopy(state.reasoning)
    return True


def guarded_fallback(agent, reason, reset_at, native):
    """Delegate one eligible pre-output switch to Hermes; never rerun a whole turn."""
    if _platform(agent) != "qqbot":
        return native(agent, reason, reset_at=reset_at)
    state = capture_route_state(agent)
    if state is None:
        return False
    code = getattr(reason, "value", reason)
    with state.lock:
        if (not state.enabled or state.manual or state.attempted or state.progressed
                or getattr(agent, "model", None) != FAST_MODEL
                or not isinstance(code, str) or code not in RETRYABLE_REASONS
                or getattr(agent, "_interrupt_requested", False) or getattr(agent, "_qq_deadline_exceeded", False)):
            return False
        state.attempted = True
        # Reassert the one permitted candidate before handing off to native code.
        agent._fallback_chain = [{"provider": PROVIDER, "model": FLASH_MODEL, "api_mode": "chat_completions"}]
        agent._fallback_index = 0
        try:
            switched = bool(native(agent, reason, reset_at=reset_at))
        finally:
            agent.reasoning_config = deepcopy(state.reasoning)
        state.fallback_active = switched
        logger.info("QQ_MODEL_ROUTE fallback=%s reason=%s", "activated" if switched else "unavailable", code)
        return switched


__all__ = ["FAST_MODEL", "FLASH_MODEL", "PROVIDER", "BASE_URL", "RETRYABLE_REASONS", "ModelRoutingBusy",
           "TurnRoutingState", "session_route_status", "clear_manual_model", "configure_turn", "capture_route_state",
           "note_progress", "note_stream_chunk", "observe_response", "preserve_reasoning", "guarded_fallback"]
