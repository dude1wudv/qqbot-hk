"""Keep QQ control messages out of the ambient/busy conversation queue."""

from __future__ import annotations

import functools
from collections.abc import Mapping

from .commands import clean_text, normalize_command_text

_STATE = "_smart_group_qq_command_ingress"


def install_command_ingress(adapter_class):
    """Patch only the QQ class; Hermes still owns authorization and native dispatch."""
    if _STATE in adapter_class.__dict__:
        return
    original = getattr(adapter_class, "handle_message", None)
    if not callable(original):
        return
    strip_mention = getattr(adapter_class, "_strip_at_mention", None)
    state = {
        "handle_own": adapter_class.__dict__.get("handle_message"),
        "strip_own": adapter_class.__dict__.get("_strip_at_mention"),
    }

    @functools.wraps(original)
    async def handle_message(self, event):
        raw = getattr(event, "raw_message", None)
        synthetic = isinstance(raw, Mapping) and raw.get("_smart_group_qq_nonmention")
        if getattr(event, "allow_gateway_control", True) and not getattr(event, "internal", False) and not synthetic:
            text = normalize_command_text(event.text)
            if text.startswith("/"):
                event.text = text
                command = text[1:].partition(" ")[0]
                source = getattr(event, "source", None)
                # Reset-like native commands retain Hermes' cancellation/guard lifecycle.
                if getattr(source, "chat_type", None) == "group" and command not in {"reset", "clear", "new", "stop"}:
                    key = self._event_session_key(event)
                    if key in self._active_sessions:
                        # Same gateway entry point, including pre_dispatch + auth + busy policy.
                        # Local commands reply now; /model and /reasoning get Hermes' explicit
                        # busy response rather than being silently merged into an LLM turn.
                        await self._dispatch_inline_reply(event)
                        return
        return await original(self, event)

    adapter_class.handle_message = handle_message
    if callable(strip_mention):
        # The upstream plain @\S+ pattern consumes @bot/command as one mention.
        adapter_class._strip_at_mention = staticmethod(clean_text)
    setattr(adapter_class, _STATE, state)


def uninstall_command_ingress(adapter_class):
    state = adapter_class.__dict__.get(_STATE)
    if state is None:
        return
    for name, key in (("handle_message", "handle_own"), ("_strip_at_mention", "strip_own")):
        if state[key] is None:
            if name in adapter_class.__dict__:
                delattr(adapter_class, name)
        else:
            setattr(adapter_class, name, state[key])
    delattr(adapter_class, _STATE)
