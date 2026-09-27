#!/usr/bin/env python3
"""Offline smoke checks for QQ async context rotation against a temporary SessionDB."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

MARKER = '# QQBOT_HK_CONTEXT_PATCH = "v1"'


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check_patched_source(root: Path) -> None:
    seams = {
        "gateway/run_turn.py": ("context_boundary", "after_turn"),
        "gateway/run_turn_runner.py": ("agent.compression_enabled = False",),
        "agent/conversation_loop.py": ("trim_images(s.messages)", "trim_images(s.api_messages)"),
        "gateway/platforms/qqbot/adapter.py": ("limit_attachments(attachments)", "image_urls[-5:]"),
        "hermes_state_compression.py": ("expected_parent_watermark", "QQ compression parent changed"),
    }
    for relative, tokens in seams.items():
        source = (root / relative).read_text(encoding="utf-8")
        require(MARKER in source, f"missing QQ context patch marker: {relative}")
        for token in tokens:
            require(token in source, f"missing patched seam {token!r}: {relative}")
    print("source seams: QQ async compression, five-image bound, watermark CAS present")


def check_session_db(root: Path) -> None:
    sys.path.insert(0, str(root))
    from hermes_state import SessionDB

    with tempfile.TemporaryDirectory(prefix="qq-context-verify-") as tmp:
        db_path = Path(tmp) / "synthetic-state.db"
        db = SessionDB(db_path=db_path)
        try:
            parent_id = "synthetic-parent"
            child_id = "synthetic-child"
            db.create_session(
                parent_id, source="qqbot", model="synthetic/model",
                model_config={"reasoning_effort": "low", "route": "qq"},
                session_key="synthetic:group-a", user_id="synthetic-user",
                chat_id="synthetic-group", chat_type="group",
            )
            for index in range(12):
                db.append_message(parent_id, "user", f"synthetic-{index}")
            watermark = db.get_active_message_watermark(parent_id)
            holder = "qq-context-verifier"
            require(db.try_acquire_compression_lock(parent_id, holder, ttl_seconds=60),
                    "failed to acquire synthetic compression lease")
            handoff = ([{"role": "assistant", "content": "synthetic summary"}] +
                       [{"role": "user", "content": f"latest-{index}"} for index in range(10)])

            # An intervening durable append changes the CAS watermark. Failed CAS must
            # leave the parent open and must not expose a partial child session.
            db.append_message(parent_id, "user", "concurrent synthetic append")
            try:
                db.publish_compression_child(
                    parent_session_id=parent_id, child_session_id="rejected-child",
                    source="qqbot", messages=handoff, model="synthetic/model",
                    model_config={"reasoning_effort": "low", "route": "qq"},
                    compression_lock_holder=holder,
                    expected_parent_watermark=watermark,
                )
            except Exception as exc:
                require("watermark" in str(exc).lower() or "changed" in str(exc).lower(),
                        "CAS failed for an unexpected reason")
            else:
                raise AssertionError("watermark CAS accepted a changed parent")
            parent = db.get_session(parent_id)
            require(parent is not None and parent.get("ended_at") is None,
                    "watermark CAS failure closed the parent")
            require(db.get_session("rejected-child") is None,
                    "watermark CAS failure left a child session")

            # Retry at the current watermark, committing a real transaction with an
            # exact summary + ten-row transcript. The database creates route metadata
            # by inheriting the parent's gateway identity.
            current = db.get_active_message_watermark(parent_id)
            db.publish_compression_child(
                parent_session_id=parent_id, child_session_id=child_id,
                source="qqbot", messages=handoff, model=parent["model"],
                model_config=json.loads(parent["model_config"])
                if isinstance(parent.get("model_config"), str) else parent.get("model_config"),
                compression_lock_holder=holder,
                expected_parent_watermark=current,
            )
            parent = db.get_session(parent_id)
            child = db.get_session(child_id)
            require(parent.get("ended_at") is not None, "successful publish did not close parent")
            require(child is not None, "successful publish did not create child")
            require(child.get("model") == "synthetic/model", "child model was not inherited")
            require(child.get("session_key") == "synthetic:group-a", "child route key was not inherited")
            require(child.get("chat_id") == "synthetic-group", "child chat route was not inherited")
            require(child.get("model_config") == parent.get("model_config"),
                    "child model config did not match parent")
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT role, content FROM messages WHERE session_id = ? AND active = 1 ORDER BY id",
                    (child_id,),
                ).fetchall()
            expected_rows = [(row["role"], row["content"]) for row in handoff]
            require(rows == expected_rows,
                    f"child transcript order/content mismatch: got {len(rows)} rows")
            print("SessionDB: CAS refusal kept parent open; publish committed route/model and 11 ordered rows")
            check_runtime_publication(root, db)
        finally:
            try:
                db.release_compression_lock("synthetic-parent", "qq-context-verifier")
            except Exception:
                pass
            db.close()
def check_runtime_publication(root: Path, db) -> None:
    """Drive the actual QQ Compactor -> publish -> SessionStore CAS integration."""
    import asyncio
    import threading
    import types
    import importlib.util

    runtime_dir_candidates = [root / "qqbot-hk" / "runtime", root / "runtime", root]
    runtime_dir = next((path for path in runtime_dir_candidates
                        if (path / "qqbot_context.py").is_file()), None)
    require(runtime_dir is not None, "qqbot_context.py is missing from Hermes image")
    media_candidates = [
        root / "qqbot-hk" / "plugins" / "smart_group_qq" / "media.py",
        root / "plugins" / "smart_group_qq" / "media.py",
    ]
    media_path = next((path for path in media_candidates if path.is_file()), None)
    require(media_path is not None, "smart_group_qq/media.py is missing from Hermes image")
    sys.path.insert(0, str(runtime_dir))
    media_spec = importlib.util.spec_from_file_location("qqbot_hk_media", media_path)
    media = importlib.util.module_from_spec(media_spec)
    sys.modules["qqbot_hk_media"] = media
    media_spec.loader.exec_module(media)
    context_spec = importlib.util.spec_from_file_location(
        "qqbot_context", runtime_dir / "qqbot_context.py"
    )
    context = importlib.util.module_from_spec(context_spec)
    sys.modules["qqbot_context"] = context
    context_spec.loader.exec_module(context)
    from gateway.session import SessionStore

    parent_id = "runtime-parent"
    route_key = "synthetic:runtime-group"
    model_config = {"reasoning_effort": "low", "route": "runtime"}
    db.create_session(
        parent_id, source="qqbot", model="synthetic/runtime-model",
        model_config=model_config, session_key=route_key,
        user_id="synthetic-user", chat_id="synthetic-runtime-group",
        chat_type="group",
    )
    transcript = [
        {"role": "user" if index % 2 == 0 else "assistant",
         "content": f"durable-runtime-row-{index}-" + ("x" * 180)}
        for index in range(14)
    ]
    for row in transcript:
        db.append_message(parent_id, row["role"], row["content"])

    # Use a real SessionStore instance and its real load_transcript /
    # advance_compression_session methods, with this isolated SessionDB pinned.
    store = SessionStore.__new__(SessionStore)
    store._db = db
    store._entries = {
        route_key: types.SimpleNamespace(session_key=route_key, session_id=parent_id)
    }
    store._loaded = True
    store._routing_db_loaded = True
    store._routing_fallback_baseline = None
    store._lock = threading.RLock()
    store._save = lambda: None
    store._db_for_session_id = lambda session_id: db
    # Use canonical replay dictionaries (timestamps/sidecars included) so the
    # runtime publication fingerprint compares like-for-like with durable rows.
    transcript = store.load_transcript(parent_id)
    require(len(transcript) == 14, "synthetic parent did not replay all 14 rows")
    entry = types.SimpleNamespace(
        session_key=route_key, session_id=parent_id, last_prompt_tokens=0
    )
    events = []
    runner = types.SimpleNamespace(
        session_store=store,
        _is_session_run_current=lambda quick_key, generation: True,
        _rebind_turn_lease=lambda quick_key, generation, sid: events.append(("rebind", sid)),
        _evict_cached_agent=lambda key: events.append(("evict", key)),
    )
    compactor = context.Compactor(
        summarizer=lambda *_: asyncio.sleep(0, result="synthetic summary")
    )
    async def run_boundary():
        compactor.start(route_key, parent_id, transcript)
        await compactor.jobs[route_key].task
        runner._qq_context_compactor = compactor
        return await context.context_boundary(
            runner, types.SimpleNamespace(platform="qqbot"), entry,
            transcript, "synthetic-quick-key", 7,
        )

    result = asyncio.run(run_boundary())
    child_id = entry.session_id
    require(child_id != parent_id, "runtime.publish did not rotate the session id")
    require(store._entries[route_key].session_id == child_id,
            "SessionStore route CAS did not advance to child")
    require(result[0]["content"] == context.SUMMARY_PREFIX + "synthetic summary",
            "context boundary did not return the summary handoff")
    require([event[0] for event in events] == ["rebind", "evict"],
            f"turn lease/cache integration callbacks missing or misordered: {events}")
    require(events[0][1] == child_id, "turn lease was not rebound to child")
    require(events[1][1] == route_key, "cached agent was not evicted by route key")
    require(db.get_session(parent_id).get("ended_at") is not None,
            "runtime publish did not close parent")
    child = db.get_session(child_id)
    require(child is not None and child.get("model") == "synthetic/runtime-model",
            "runtime child did not inherit model")
    require(child.get("model_config") == json.dumps(model_config),
            "runtime child did not inherit model config")
    require(child.get("session_key") == route_key
            and child.get("chat_id") == "synthetic-runtime-group",
            "runtime child did not inherit group route")
    persisted = store.load_transcript(child_id)
    expected = [result[0], *transcript[-10:]]
    require([(row.get("role"), row.get("content")) for row in persisted]
            == [(row["role"], row["content"]) for row in expected],
            "runtime child transcript is not exact ordered summary + latest 10")
    require(len(persisted) == 11,
            f"runtime child has {len(persisted)} rows; expected exactly 11")
    print("runtime.publish: SessionStore route, 11-row handoff, lease rebind and cache eviction verified")




def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-root", default="/opt/hermes")
    args = parser.parse_args()
    root = Path(args.hermes_root).resolve()
    check_patched_source(root)
    check_session_db(root)
    print("HERMES_QQ_CONTEXT_VERIFY=ok")


if __name__ == "__main__":
    main()
