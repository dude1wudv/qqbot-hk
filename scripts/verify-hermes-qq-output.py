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

    def _adapter_for_source(self, _source):
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
    )
    silent = {"final_response": "[SILENT]", "failed": True}
    await harness._run_agent_deliver_first_response(turn_ctx, None, silent, silent, None)
    require(not harness.queued_calls, "QQ silence leaked through queued follow-up delivery")

    normal = {"final_response": "next reply", "failed": False}
    await harness._run_agent_deliver_first_response(turn_ctx, None, normal, normal, None)
    require(len(harness.queued_calls) == 1, "normal queued follow-up reply did not remain deliverable")
    require(harness.queued_calls[0][0][0] == "next reply", "queued reply text changed unexpectedly")


def main() -> None:
    verify_auto_media_boundary()
    asyncio.run(verify_normal_delivery_boundary())
    asyncio.run(verify_queued_followup_boundary())
    print("HERMES_QQ_OUTPUT=passed SILENT_MEDIA=blocked FAILED=blocked QUEUED=blocked NEXT_REPLY=deliverable")


if __name__ == "__main__":
    main()
