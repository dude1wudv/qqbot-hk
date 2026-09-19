#!/usr/bin/env python3
"""Behavior smoke for QQ automatic session rotation after compression breaker trips."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from types import SimpleNamespace

from gateway.config import Platform
from gateway.run_inbound import GatewayInboundMixin


@dataclass
class FakeEvent:
    text: str
    message_id: str = "message"

    def get_command(self):
        return self.text.lstrip("/").split(maxsplit=1)[0] if self.text.startswith("/") else None


class FakeStore:
    async def get_or_create_session(self, source):
        return SimpleNamespace(session_id="session")


class FakeDB:
    def __init__(self, ineffective: int, fallback: int):
        self.ineffective = ineffective
        self.fallback = fallback

    async def get_compression_ineffective_count(self, session_id):
        return self.ineffective

    async def get_compression_fallback_streak(self, session_id):
        return self.fallback


class FakeAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class Harness(GatewayInboundMixin):
    def __init__(self, ineffective: int, fallback: int, enabled: bool = True):
        self.config = SimpleNamespace(platforms={
            Platform.QQBOT: SimpleNamespace(extra={
                "auto_new_on_compression_ineffective": enabled,
            }),
        })
        self.async_session_store = FakeStore()
        self._session_db = FakeDB(ineffective, fallback)
        self.adapter = FakeAdapter()
        self.resets = []

    async def _handle_reset_command(self, event):
        self.resets.append(replace(event))

    def _adapter_for_source(self, source):
        return self.adapter


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


async def verify() -> None:
    source = SimpleNamespace(platform=Platform.QQBOT, chat_id="chat")

    tripped = Harness(2, 0)
    recovered = await tripped._hm_auto_new_on_ineffective_compression(FakeEvent("hello"), source)
    require(recovered is True, "tripped breaker did not rotate the session")
    require([event.text for event in tripped.resets] == ["/new"], "reset was not a /new equivalent")
    require(len(tripped.adapter.sent) == 1, "automatic recovery notice was not sent")

    fallback = Harness(0, 2)
    require(
        await fallback._hm_auto_new_on_ineffective_compression(FakeEvent("hello"), source) is True,
        "fallback streak did not rotate the session",
    )

    healthy = Harness(1, 1)
    require(
        await healthy._hm_auto_new_on_ineffective_compression(FakeEvent("hello"), source) is False,
        "healthy session rotated prematurely",
    )
    require(healthy.resets == [], "healthy session invoked reset")

    manual = Harness(2, 2)
    require(
        await manual._hm_auto_new_on_ineffective_compression(FakeEvent("/compress"), source) is False,
        "manual /compress was intercepted",
    )
    require(manual.resets == [], "manual command invoked automatic reset")

    disabled = Harness(2, 2, enabled=False)
    require(
        await disabled._hm_auto_new_on_ineffective_compression(FakeEvent("hello"), source) is False,
        "disabled recovery still rotated a session",
    )


def main() -> None:
    asyncio.run(verify())
    print("HERMES_COMPRESSION_RECOVERY=passed AUTO_NEW=tripped_only MANUAL_COMMANDS=preserved")


if __name__ == "__main__":
    main()
