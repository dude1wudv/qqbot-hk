#!/usr/bin/env python3
"""Offline smoke for the patched Hermes QQ output boundary."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent / "qqbot-hk" / "plugins"))

from gateway.config import Platform
from gateway.run_turn import GatewayTurnMixin
from gateway.run_turn_runner import TurnRunner

# Import the project plugin from the image-provided copy.  This intentionally
# exercises the same package that the gateway image will load, not a duplicate.
import smart_group_qq  # noqa: F401


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


class _DeliveryHarness(GatewayTurnMixin):
    def __init__(self) -> None:
        self.media_calls: list[tuple] = []
        self.queued_calls: list[tuple] = []
        self.voice_calls: list[tuple] = []

    def _delivery_adapter_for(self, _source):
        return SimpleNamespace(_streaming_tts_turn_completed=lambda *_a, **_k: False)

    def _should_send_voice_reply(self, *_args, **_kwargs):
        return False

    async def _send_voice_reply(self, *args, **kwargs):
        self.voice_calls.append((args, kwargs))

    async def _deliver_media_from_response(self, *args, **kwargs):
        self.media_calls.append((args, kwargs))

    async def _deliver_queued_first_response(self, *args, **kwargs):
        self.queued_calls.append((args, kwargs))


def _qq_source():
    return SimpleNamespace(platform=Platform.QQBOT, chat_id="group-smoke")


def verify_auto_media_boundary() -> None:
    runner = object.__new__(TurnRunner)
    runner._ctx = SimpleNamespace(source=_qq_source())
    result = {"messages": [{"role": "tool", "content": "MEDIA:/tmp/example.mp3"}]}
    output = runner._append_auto_media_tags("[SILENT]", result, [], [])
    require(output == "[SILENT]", "QQ silence marker was changed by auto-media append")


async def verify_normal_delivery_boundary() -> None:
    harness = _DeliveryHarness()
    source = _qq_source()
    event = SimpleNamespace(source=source)
    session = SimpleNamespace(session_id="smoke-session")
    # A failed result with the exact marker must not leak voice/media/footer.
    output = await harness._hmwa_deliver_turn_response(
        event, source, session, "group-session", 1,
        {"failed": True, "already_sent": True}, [], "[SILENT]", "footer", True,
    )
    require(output is None, "QQ silence did not short-circuit normal delivery")
    require(not harness.media_calls and not harness.voice_calls, "QQ silence leaked normal media/voice")


async def verify_queued_followup_boundary() -> None:
    harness = _DeliveryHarness()
    source = _qq_source()
    turn_ctx = SimpleNamespace(
        session_key="group-session",
        source=source,
        stream_consumer_holder=[None],
        run_generation=1,
        event_message_id="event-1",
        inbound_message_id="inbound-1",
        _status_thread_metadata={},
        mute_notification_reply=False,
        persist_user_display_kind="user",
    )
    silent = {"final_response": "[SILENT]", "failed": True}
    await harness._run_agent_deliver_first_response(turn_ctx, None, silent, silent, None)
    require(not harness.queued_calls, "QQ silence leaked through queued follow-up delivery")

    normal = {"final_response": "next reply", "failed": False}
    await harness._run_agent_deliver_first_response(turn_ctx, None, normal, normal, None)
    require(len(harness.queued_calls) == 1, "normal queued follow-up reply did not remain deliverable")
    require(harness.queued_calls[0][0][0] == "next reply", "queued reply text changed unexpectedly")


async def verify_first_bubble_quote() -> None:
    from gateway.config import PlatformConfig
    from gateway.platforms.qqbot.adapter import QQAdapter

    adapter = QQAdapter(PlatformConfig(enabled=True, extra={"markdown_support": False}))
    adapter._chat_type_map["group-smoke"] = "group"
    bodies = []

    async def connected():
        return True

    async def request(method, path, body, **kwargs):
        bodies.append(body.copy())
        return {"id": str(len(bodies))}

    adapter._ensure_connected = connected
    adapter._api_request = request
    smart_group_qq._configure_adapter(adapter)
    sent = await smart_group_qq._send_all(adapter, "group-smoke", "passive-anchor", "x" * 1600)
    require(sent and len(bodies) == 2, "QQ quote smoke did not send two packets")
    require(all(body.get("msg_id") == "passive-anchor" for body in bodies), "QQ passive anchor lost")
    require("message_reference" in bodies[0] and "message_reference" not in bodies[1],
            "QQ continuation repeated the visible quote")


async def verify_native_receipt_delivery() -> None:
    from gateway.config import PlatformConfig
    from gateway.platforms.qqbot.adapter import QQAdapter
    from smart_group_qq.store import Store
    from smart_group_qq.response import ReplyRegistry
    from smart_group_qq.delivery import LocalReplies
    from smart_group_qq.formatter import format_for_qq

    store = Store(":memory:", member_secret="offline-synthetic-member-secret")
    adapter = QQAdapter(PlatformConfig(enabled=True, extra={"markdown_support": False}))
    adapter._chat_type_map["group-smoke"] = "group"
    registry = ReplyRegistry(store.memory_epoch, lambda *_a, **_k: None, lambda *_a: None)
    replies = LocalReplies(store, registry)
    smart_group_qq._configure_adapter(adapter, registry)
    bodies, failures = [], []

    async def connected():
        return True

    async def request(method, path, body, **kwargs):
        bodies.append(body.copy())
        if failures:
            value = failures.pop(0)
            if value:
                raise value
        return {"id": str(len(bodies))}

    async def drain():
        await asyncio.gather(*tuple(replies.tasks.values()))

    adapter._ensure_connected = connected
    adapter._api_request = request
    raw = '```python\n    print("**literal**")\n' + '    # retain  spaces\n' * 90 + '```'
    prepared = format_for_qq(raw)
    replies.schedule("group-smoke", "code", adapter, "group-smoke", text=raw)
    await drain()
    require("".join(body["content"] for body in bodies) == prepared,
            "native formatting changed prepared code or packet whitespace")
    require(all(body["content"].strip() and len(body["content"]) <= 1500 for body in bodies),
            "native packets empty or exceed transport limit")

    bodies.clear()
    store.save_command_receipt("group-smoke", "restart", prepared, prepared=True)
    require(replies.replay("group-smoke", "restart", adapter, "group-smoke"), "pending receipt not resumed")
    await drain()
    require("".join(body["content"] for body in bodies) == prepared, "cached final body formatted twice")

    bodies.clear()
    failures.extend([None, RuntimeError("QQ Bot API error [429] synthetic")])
    replies.schedule("group-smoke", "refused", adapter, "group-smoke", text="x" * 1700)
    await drain()
    receipt = store.get_command_receipt("group-smoke", "refused")
    require(receipt["delivery_status"] == "failed" and receipt["sent_chunks"] == [0],
            "known failure did not preserve successful prefix")
    require(len(bodies) == 2, "native layer retried before receipt decided")
    require(replies.replay("group-smoke", "refused", adapter, "group-smoke"), "known failure cannot resume")
    await drain()
    require(len(bodies) == 3 and len(bodies[-1]["content"]) == 200, "successful prefix was resent")

    bodies.clear()
    failures.append(TimeoutError("synthetic ambiguous delivery"))
    replies.schedule("group-smoke", "unknown", adapter, "group-smoke", text="hello")
    await drain()
    require(store.get_command_receipt("group-smoke", "unknown")["delivery_status"] == "unknown",
            "ambiguous send incorrectly marked safe to retry")
    require(len(bodies) == 1 and not replies.replay("group-smoke", "unknown", adapter, "group-smoke"),
            "unknown outcome resent automatically")
    store.close()


def main() -> None:
    verify_auto_media_boundary()
    asyncio.run(verify_normal_delivery_boundary())
    asyncio.run(verify_queued_followup_boundary())
    asyncio.run(verify_first_bubble_quote())
    asyncio.run(verify_native_receipt_delivery())
    print("HERMES_QQ_OUTPUT=passed SILENT_MEDIA=blocked FAILED=blocked QUEUED=blocked NEXT_REPLY=deliverable")


if __name__ == "__main__":
    main()
