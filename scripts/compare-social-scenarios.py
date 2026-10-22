#!/usr/bin/env python3
"""Compare deterministic social-state behavior using synthetic data only.

Run from any directory with the project Python environment. No Hermes runtime,
model API, QQ connection, credentials, or user database is loaded. Baseline
attention.py and memory.py come from a local Git commit; both versions use the
current public Store and lexical helpers. This is a component comparison, not
an end-to-end chatbot or model-intelligence benchmark.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch


DEFAULT_BASELINE = "8fda3b3"
MODULE_PATH = "plugins/smart_group_qq"
COMPARED_MODULES = ("attention", "memory")
SHARED_MODULES = ("expressions", "knowledge", "llm_policy", "store")
SEED_TURNS = (
    {"member": "synthetic-a", "text": "Windows deployment failed", "reply_id": "reply-a", "at": 100},
    {"member": "synthetic-b", "text": "Friday dinner menu", "reply_id": "reply-b", "at": 105},
)
FACT = "明早一同出门跑步"
ALIAS = "晨练"
FIXTURE = {
    "seed_turns": SEED_TURNS,
    "continuations": ["Windows deployment logs", "Friday dinner menu"],
    "quoted_text": "请详细说一下", "irrelevant_text": "今晚看电影吗",
    "memory_fact": FACT, "memory_aliases": [ALIAS, "exercise"],
}


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def git_read(repo: Path, *args: str) -> bytes:
    # Local Git reads only. No fetch, remote access, or shell interpolation.
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=False, timeout=30,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Local git {' '.join(args)} failed: {detail}")
    return result.stdout


def package(name: str) -> None:
    module = ModuleType(name)
    module.__package__ = name
    # Dependencies are explicitly loaded below, never the plugin initializer.
    module.__path__ = []
    sys.modules[name] = module


def source_module(package_name: str, name: str, source: bytes, origin: str) -> ModuleType:
    module_name = f"{package_name}.{name}"
    module = ModuleType(module_name)
    module.__package__ = package_name
    module.__file__ = origin
    sys.modules[module_name] = module
    exec(compile(source, origin, "exec"), module.__dict__)
    setattr(sys.modules[package_name], name, module)
    return module


def load_versions(current_root: Path, baseline_repo: Path, baseline: str) -> tuple[Any, Any, dict]:
    sha = git_read(baseline_repo, "rev-parse", "--verify", f"{baseline}^{{commit}}").decode().strip()
    root = current_root / MODULE_PATH
    shared_sources = {name: (root / f"{name}.py").read_bytes() for name in SHARED_MODULES}
    current_sources = {name: (root / f"{name}.py").read_bytes() for name in COMPARED_MODULES}
    old_sources = {
        name: git_read(baseline_repo, "show", f"{sha}:{MODULE_PATH}/{name}.py")
        for name in COMPARED_MODULES
    }
    package("_qq_social_shared")
    shared = {
        name: source_module("_qq_social_shared", name, source, str(root / f"{name}.py"))
        for name, source in shared_sources.items()
    }
    versions = []
    for label, sources in (("baseline", old_sources), ("current", current_sources)):
        package_name = f"_qq_social_{label}"
        package(package_name)
        for name, module in shared.items():
            sys.modules[f"{package_name}.{name}"] = module
            setattr(sys.modules[package_name], name, module)
        loaded = {
            name: source_module(package_name, name, source, f"{label}:{MODULE_PATH}/{name}.py")
            for name, source in sources.items()
        }
        versions.append(SimpleNamespace(**loaded, Store=shared["store"].Store))
    provenance = {
        "baseline_commit": sha,
        "compared_modules": [f"{MODULE_PATH}/{name}.py" for name in COMPARED_MODULES],
        "shared_current_dependencies": [f"{MODULE_PATH}/{name}.py" for name in SHARED_MODULES],
        "source_sha256": {
            "baseline": {name: digest(source) for name, source in old_sources.items()},
            "current": {name: digest(source) for name, source in current_sources.items()},
            "shared": {name: digest(source) for name, source in shared_sources.items()},
        },
    }
    return *versions, provenance


def seed_attention(version: Any) -> Any:
    manager = version.attention.AttentionManager()
    for turn in SEED_TURNS:
        manager.record_success(
            "synthetic-group", turn["member"], turn["text"], direct=True,
            message_id=turn["reply_id"], now=turn["at"],
        )
    return manager


def selection(
    manager: Any, query: str, expected_summary: str, *, at: int,
    member: str = "synthetic-c", message_id: str = "incoming-c", reference: str | None = None,
) -> dict:
    if not hasattr(manager, "direct_decision") or not hasattr(manager, "topic_context"):
        return {
            "topic_id": None, "selected_topic_summary": None, "topic_hit": None,
            "selected_topic_members": None, "focus_message_ids": None, "current_event_focused": None,
            "metadata_note": "This version has no public topic-selection/focus metadata; no failure is inferred.",
        }
    decision = manager.direct_decision(
        "synthetic-group", member, query, message_id=message_id,
        reference_id=reference, candidate_message_ids=(message_id, "reply-a"), now=at,
    )
    topic = next((item for item in manager.topic_context("synthetic-group", now=at)
                  if item["id"] == decision.topic_id), None)
    return {
        "topic_id": decision.topic_id,
        "selected_topic_summary": topic["summary"] if topic else None,
        "topic_hit": bool(topic and topic["summary"] == expected_summary),
        "selected_topic_members": topic["member_refs"] if topic else [],
        "focus_message_ids": list(decision.focus_message_ids),
        "current_event_focused": message_id in decision.focus_message_ids,
        "metadata_note": "Addressed-turn metadata only; the real QQ send anchor is not exercised.",
    }


def attention_scenarios(version: Any) -> dict:
    manager = seed_attention(version)
    query = FIXTURE["continuations"][0]
    first = {
        "continuation_score": manager.continuation_score("synthetic-group", "synthetic-c", query, now=106),
        **selection(manager, query, SEED_TURNS[0]["text"], at=106),
    }
    manager.record_success("synthetic-group", "synthetic-c", query, direct=True,
                           message_id="reply-c", now=107)
    query = FIXTURE["continuations"][1]
    second = {
        "continuation_score": manager.continuation_score("synthetic-group", "synthetic-b", query, now=108),
        **selection(manager, query, SEED_TURNS[1]["text"], at=108,
                    member="synthetic-b", message_id="incoming-b"),
    }
    active = {member: manager.is_active_member("synthetic-group", member, now=108)
              for member in ("synthetic-a", "synthetic-b", "synthetic-c")}
    quote = seed_attention(version)
    reference = {
        "reference_recognized": quote.has_reply_id("synthetic-group", "reply-a", now=106),
        "reference_cross_group_rejected": not quote.has_reply_id("other-group", "reply-a", now=106),
        **selection(quote, FIXTURE["quoted_text"], SEED_TURNS[0]["text"], at=106, reference="reply-a"),
    }
    unrelated_score = manager.continuation_score(
        "synthetic-group", "synthetic-d", FIXTURE["irrelevant_text"], now=109,
    )
    budget = version.attention.AttentionManager(max_interjections_per_minute=2)
    with patch.object(version.attention.time, "monotonic", return_value=300):
        admissions = [budget.reserve_interjection("synthetic-budget") for _ in range(3)]
    quiet = version.attention.AttentionManager()
    quiet.record_success("synthetic-quiet", "synthetic-a", SEED_TURNS[0]["text"], direct=False, now=100)
    quiet.record_success("synthetic-quiet", "synthetic-b", SEED_TURNS[1]["text"], direct=False, now=105)
    return {
        "interleaved_topics": {
            "return_to_deployment": first, "return_to_dinner": second,
            "active_member_flags_after_third_turn": active,
            "cross_group_continuation_score": manager.continuation_score("other-group", "synthetic-c", query, now=108),
        },
        "quoted_reference": reference,
        "unrelated_interjection_gate": {
            "unrelated_continuation_score": unrelated_score,
            "budget_admissions_at_same_second": admissions,
            "third_attempt_blocked": admissions == [True, True, False],
            "budget_reopens_after_60_seconds": budget.can_interject("synthetic-budget", now=360),
            "unanswered_retreat_blocks_ambient": not quiet.can_interject("synthetic-quiet", now=106),
            "explicit_engagement_can_bypass_retreat": quiet.can_interject("synthetic-quiet", engaged=True, now=106),
            "semantic_participation_judgment": "not_measured",
            "note": "No classifier was called. A zero continuation bonus and a rate-limit gate are not proof that every irrelevant message is suppressed.",
        },
    }


def memory_payload(*, indexed: bool) -> dict:
    result = {
        "summary": "已整理合成群的安排。", "topics": [], "facts": [FACT],
        "decisions": [], "todos": [], "open_questions": [], "participants": [],
    }
    if indexed:
        result["concept_index"] = [{"section": "facts", "index": 0, "concepts": [ALIAS, "exercise"]}]
    return result


def memory_scenarios(version: Any, database: Path, current_payload: dict) -> dict:
    with closing(version.Store(str(database), member_secret="synthetic-comparison-only")) as store:
        memory = version.memory.GroupMemory(store)
        normalized = version.memory.normalize_summary_payload(memory_payload(indexed=True))
        store.set_memory("synthetic-indexed", normalized["summary"], structured=normalized, updated_at=100)
        alias_result = memory.recall("synthetic-indexed", ALIAS, overview_chars=0)
        literal_result = memory.recall("synthetic-indexed", "跑步", overview_chars=0)
        legacy = memory_payload(indexed=False)
        store.set_memory("synthetic-legacy", legacy["summary"], structured=legacy, last_history_id=17, updated_at=100)
        legacy_result = memory.recall("synthetic-legacy", "跑步", overview_chars=0)
        # Both readers see exactly the current optional JSON for rollback compatibility.
        store.set_memory("synthetic-rollback", current_payload["summary"], structured=current_payload,
                         last_history_id=23, updated_at=100)
        before = store.memory_payload("synthetic-rollback")
        rollback_result = memory.recall("synthetic-rollback", "跑步", overview_chars=0)
        memory.presentation("synthetic-rollback")
        after = store.memory_payload("synthetic-rollback")
        return {
            "synonym_memory_recall": {
                "alias_query": ALIAS, "alias_fact_hit": FACT in alias_result,
                "alias_result": alias_result, "literal_control_hit": FACT in literal_result,
                "foreign_group_empty": memory.recall("other-group", ALIAS, overview_chars=0) == "",
                "index_terms_hidden": all(term not in alias_result for term in (ALIAS, "exercise", "concept_index")),
                "concept_entries_after_normalization": len(normalized["concept_index"]) if "concept_index" in normalized else None,
                "index_note": "Tags are supplied synthetic fixtures, not evidence of successful model-generated labels. Missing index metadata is null.",
            },
            "legacy_json_compatibility": {
                "legacy_literal_fact_hit": FACT in legacy_result,
                "legacy_has_no_concept_index": "concept_index" not in legacy,
                "legacy_cursor_preserved": store.memory_payload("synthetic-legacy")["last_history_id"] == 17,
                "current_optional_json_fact_hit": FACT in rollback_result,
                "current_optional_json_read_is_non_mutating": before == after,
                "current_optional_json_cursor_preserved": after["last_history_id"] == 23,
                "sqlite_schema_version": store.db.execute("PRAGMA user_version").fetchone()[0],
                "scope": "Real memory readers with the shared current Store; not a full old-image/database rollback rehearsal.",
            },
        }


def acceptance_checks(results: dict) -> list[dict]:
    checks = []

    def check(name: str, actual: Any, expected: Any) -> None:
        checks.append({"name": name, "passed": actual == expected, "actual": actual, "expected": expected})

    for label, result in results.items():
        gate = result["unrelated_interjection_gate"]
        check(f"{label}.unrelated_continuation_bonus", gate["unrelated_continuation_score"], 0)
        for key in ("third_attempt_blocked", "budget_reopens_after_60_seconds", "unanswered_retreat_blocks_ambient",
                    "explicit_engagement_can_bypass_retreat"):
            check(f"{label}.{key}", gate[key], True)
        quote = result["quoted_reference"]
        for key in ("reference_recognized", "reference_cross_group_rejected"):
            check(f"{label}.{key}", quote[key], True)
        check(f"{label}.cross_group_continuation", result["interleaved_topics"]["cross_group_continuation_score"], 0)
        recall = result["synonym_memory_recall"]
        for key in ("literal_control_hit", "foreign_group_empty", "index_terms_hidden"):
            check(f"{label}.{key}", recall[key], True)
        for key, actual in result["legacy_json_compatibility"].items():
            if type(actual) is bool:
                check(f"{label}.{key}", actual, True)
        check(f"{label}.unchanged_schema", result["legacy_json_compatibility"]["sqlite_schema_version"], 7)
    current = results["current"]
    for name in ("return_to_deployment", "return_to_dinner"):
        check(f"current.{name}.continuation_score", current["interleaved_topics"][name]["continuation_score"], 20)
        check(f"current.{name}.topic_hit", current["interleaved_topics"][name]["topic_hit"], True)
    check("current.active_members_retained", list(current["interleaved_topics"]["active_member_flags_after_third_turn"].values()), [True] * 3)
    check("current.quote_topic_hit", current["quoted_reference"]["topic_hit"], True)
    check("current.quote_current_event_focused", current["quoted_reference"]["current_event_focused"], True)
    check("current.alias_fact_hit", current["synonym_memory_recall"]["alias_fact_hit"], True)
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-root", type=Path, default=Path(__file__).resolve().parents[1],
                        help="Repository or deployed source root containing plugins/smart_group_qq.")
    parser.add_argument("--baseline-repo", type=Path, help="Local Git repository containing the baseline (defaults to current-root).")
    parser.add_argument("--baseline", default=DEFAULT_BASELINE, help="Local baseline revision (default: 8fda3b3).")
    parser.add_argument("--compact", action="store_true", help="Print compact JSON.")
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        baseline, current, provenance = load_versions(
            args.current_root.resolve(), (args.baseline_repo or args.current_root).resolve(), args.baseline,
        )
        current_payload = current.memory.normalize_summary_payload(memory_payload(indexed=True))
        with TemporaryDirectory(prefix="qq-social-synthetic-") as temporary:
            results = {
                label: {**attention_scenarios(version),
                        **memory_scenarios(version, Path(temporary) / f"{label}.sqlite3", current_payload)}
                for label, version in (("baseline", baseline), ("current", current))
            }
        checks = acceptance_checks(results)
        failed = [item for item in checks if not item["passed"]]
        report = {
            "report_version": 1,
            "status": "passed" if not failed else "failed",
            "purpose": "Deterministic component behavior comparison, not a model intelligence score.",
            "provenance": provenance,
            "fixtures": FIXTURE,
            "fixture_sha256": digest(json.dumps(FIXTURE, ensure_ascii=False, sort_keys=True).encode()),
            "method": {
                "data": "Fixed synthetic turns, members, groups and temporary SQLite databases only.",
                "attention_clock": "Explicit synthetic monotonic seconds; reserve_interjection clock fixed at 300.",
                "plugin_initialization": "Not executed; exact source modules loaded into isolated module namespaces.",
                "model_api_calls": 0, "qq_messages_sent": 0,
                "limits": "Topic/focus measurements use public addressed-turn metadata. No participation model, transport, production data or real conversation outcomes are assessed.",
            },
            "results": results,
            "not_measured": {
                "reply_length": "not_measured", "model_latency": "not_measured", "token_usage": "not_measured",
                "reason": "These require real model responses; deterministic component checks cannot establish model intelligence or live delivery.",
            },
            "acceptance": {"checks": len(checks), "passed": len(checks) - len(failed), "failed": failed},
        }
        print(json.dumps(report, ensure_ascii=False, indent=None if args.compact else 2))
        return 1 if failed else 0
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
