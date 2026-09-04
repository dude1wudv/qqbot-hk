"""Pure policy and moderation decisions for Smart Group QQ."""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


MAX_RULE_TEXT = 1500
MATCH_TYPES = frozenset({"exact", "contains", "regex"})


@dataclass(frozen=True)
class Decision:
    action: str = "allow"
    rule_id: str | None = None
    notice: str | None = None
    confidence: float | None = None
    source: str = "none"

    @property
    def blocked(self) -> bool:
        return self.action == "block"

    @property
    def replied(self) -> bool:
        return self.action == "reply"


@dataclass(frozen=True)
class CompiledRule:
    id: str
    match: str
    pattern: str
    reply: str = ""
    notice: str = ""
    enabled: bool = True
    expression: Any = None


def normalize_text(text: Any) -> str:
    """NFKC-normalize text and collapse all Unicode whitespace."""
    if text is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(text))
    return " ".join(normalized.split())


def _settings_value(settings: Mapping[str, Any] | None, *keys: str, default: Any = None) -> Any:
    current: Any = settings or {}
    for key in keys:
        if not isinstance(current, Mapping):
            return default
        current = current.get(key, default)
    return current


def compile_rule_list(
    raw_rules: Iterable[Mapping[str, Any]] | None,
    *,
    result_field: str,
    logger: logging.Logger | None = None,
) -> tuple[tuple[CompiledRule, ...], tuple[str, ...]]:
    """Compile and validate rules, returning valid rules and redacted errors.

    Invalid configuration is intentionally ignored (fail-open) while retaining
    an error string that callers can log without including rule text.
    """
    compiled: list[CompiledRule] = []
    errors: list[str] = []
    seen: set[str] = set()
    for position, raw in enumerate(raw_rules or ()):
        if not isinstance(raw, Mapping):
            errors.append(f"rule[{position}]: not an object")
            continue
        rule_id = str(raw.get("id", "")).strip()
        match = str(raw.get("match", "")).strip().lower()
        pattern = str(raw.get("pattern", ""))
        value = str(raw.get(result_field, raw.get("reply", raw.get("notice", ""))))
        if not rule_id:
            errors.append(f"rule[{position}]: empty id")
            continue
        if rule_id in seen:
            errors.append(f"rule {rule_id!r}: duplicate id")
            continue
        seen.add(rule_id)
        if match not in MATCH_TYPES:
            errors.append(f"rule {rule_id!r}: unknown match")
            continue
        if not pattern:
            errors.append(f"rule {rule_id!r}: empty pattern")
            continue
        if len(value) > MAX_RULE_TEXT:
            errors.append(f"rule {rule_id!r}: response too long")
            continue
        expression = None
        if match == "regex":
            try:
                expression = re.compile(pattern, re.IGNORECASE | re.UNICODE)
            except re.error:
                errors.append(f"rule {rule_id!r}: invalid regex")
                continue
        compiled.append(
            CompiledRule(
                id=rule_id,
                match=match,
                pattern=pattern,
                reply=value if result_field == "reply" else str(raw.get("reply", "")),
                notice=value if result_field == "notice" else str(raw.get("notice", "")),
                enabled=bool(raw.get("enabled", True)),
                expression=expression,
            )
        )
    if logger:
        for error in errors:
            logger.error("smart_group_qq invalid rule: %s", error)
    return tuple(compiled), tuple(errors)


def compile_policy(settings: Mapping[str, Any] | None, logger: logging.Logger | None = None) -> "PolicyEngine":
    return PolicyEngine(settings or {}, logger=logger)


def _matches(rule: CompiledRule, normalized: str) -> bool:
    if not rule.enabled:
        return False
    needle = normalize_text(rule.pattern)
    subject = normalized.casefold()
    if rule.match == "exact":
        return subject == needle.casefold()
    if rule.match == "contains":
        return needle.casefold() in subject
    return bool(rule.expression.search(normalized))


def match_static_moderation(
    text: Any, rules: Sequence[CompiledRule | Mapping[str, Any]] | None
) -> Decision:
    normalized = normalize_text(text)
    for item in rules or ():
        rule = item if isinstance(item, CompiledRule) else _coerce_rule(item, "notice")
        if rule is not None and _matches(rule, normalized):
            return Decision(
                action="block", rule_id=rule.id, notice=rule.notice or "此消息未能通过群聊安全审核。", source="static"
            )
    return Decision(source="none")


def match_keyword_reply(
    text: Any, rules: Sequence[CompiledRule | Mapping[str, Any]] | None
) -> Decision:
    normalized = normalize_text(text)
    for item in rules or ():
        rule = item if isinstance(item, CompiledRule) else _coerce_rule(item, "reply")
        if rule is not None and _matches(rule, normalized):
            return Decision(action="reply", rule_id=rule.id, notice=rule.reply, source="keyword")
    return Decision(source="none")


def _coerce_rule(raw: Mapping[str, Any], field: str) -> CompiledRule | None:
    rules, _ = compile_rule_list([raw], result_field=field)
    return rules[0] if rules else None


async def semantic_moderation(
    ctx: Any,
    text: Any,
    *,
    enabled: bool = False,
    timeout_seconds: float = 5,
    min_confidence: float = 0.92,
    notice: str = "此消息未能通过群聊安全审核。",
) -> Decision:
    """Run optional structured semantic moderation, fail-open on every error."""
    if not enabled:
        return Decision(source="semantic_disabled")
    llm = getattr(ctx, "llm", None) if ctx is not None else None
    complete = getattr(llm, "complete_structured", None)
    if not callable(complete):
        return Decision(source="semantic_unavailable")
    schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["allow", "block"]},
            "confidence": {"type": "number"},
            "category": {"type": "string"},
        },
        "required": ["action", "confidence"],
        "additionalProperties": True,
    }
    prompt = (
        "Classify this group message for safety. Return only the requested JSON. "
        "Do not repeat the message in your response.\nMessage:\n" + str(text)
    )
    try:
        try:
            result = complete(prompt=prompt, schema=schema, timeout=timeout_seconds)
        except TypeError:
            try:
                result = complete(prompt, schema=schema, timeout=timeout_seconds)
            except TypeError:
                result = complete(prompt, timeout=timeout_seconds)
        if inspect.isawaitable(result):
            result = await asyncio.wait_for(result, timeout=float(timeout_seconds))
        if isinstance(result, str):
            result = json.loads(result)
        if not isinstance(result, Mapping):
            return Decision(source="semantic_invalid")
        action = str(result.get("action", "allow")).lower()
        confidence = float(result.get("confidence"))
        if not 0 <= confidence <= 1:
            return Decision(source="semantic_invalid")
        if action == "block" and confidence >= float(min_confidence):
            return Decision(action="block", notice=notice, confidence=confidence, source="semantic")
        # A low-confidence block is not a block.  Never let an allow response
        # from a semantic model override static moderation (caller orders it).
        return Decision(action="allow", confidence=confidence, source="semantic")
    except Exception:
        return Decision(source="semantic_error")


class PolicyEngine:
    def __init__(self, settings: Mapping[str, Any] | None = None, *, logger: logging.Logger | None = None):
        self.settings = settings or {}
        self.logger = logger
        moderation = self.settings.get("moderation", {})
        self.static_rules, self.static_errors = compile_rule_list(
            moderation.get("static_rules", ()), result_field="notice", logger=logger
        )
        semantic = moderation.get("semantic", {})
        self.semantic_enabled = bool(semantic.get("enabled", False))
        self.semantic_timeout = float(semantic.get("timeout_seconds", 5))
        self.semantic_min_confidence = float(semantic.get("min_confidence", 0.92))
        self.semantic_notice = str(semantic.get("notice", "此消息未能通过群聊安全审核。"))[:MAX_RULE_TEXT]
        self.keyword_rules, self.keyword_errors = compile_rule_list(
            self.settings.get("keyword_replies", ()), result_field="reply", logger=logger
        )

    def static(self, text: Any) -> Decision:
        return match_static_moderation(text, self.static_rules)

    def keyword(self, text: Any) -> Decision:
        return match_keyword_reply(text, self.keyword_rules)

    async def check(self, ctx: Any, text: Any) -> Decision:
        static = self.static(text)
        if static.blocked:
            return static
        return await semantic_moderation(
            ctx,
            text,
            enabled=self.semantic_enabled,
            timeout_seconds=self.semantic_timeout,
            min_confidence=self.semantic_min_confidence,
            notice=self.semantic_notice,
        )

    async def moderate(self, ctx: Any, text: Any) -> Decision:
        return await self.check(ctx, text)


__all__ = [
    "Decision", "CompiledRule", "PolicyEngine", "compile_policy", "compile_rule_list",
    "match_static_moderation", "match_keyword_reply", "normalize_text", "semantic_moderation",
]
