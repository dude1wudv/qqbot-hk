#!/usr/bin/env python3
"""Fail-closed integration seams for the pinned Hermes QQ context policy."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path

MARKER = '# QQBOT_HK_CONTEXT_PATCH = "v2"'

PATCHES = {
    "gateway/run_turn.py": (
        "2a28ec43fcf6260f728ed752c0024a5914f2cf1230ac21766ce4e6c4c9cc38a9",
        [(
            "            history = await self._hmwa_run_session_hygiene(\n                event, source, session_entry, session_key, history, _quick_key, run_generation,\n            )",
            "            from qqbot_context import context_boundary, is_qq\n            if is_qq(source):\n                history = await context_boundary(self, source, session_entry, history, _quick_key, run_generation)\n            else:\n                history = await self._hmwa_run_session_hygiene(\n                    event, source, session_entry, session_key, history, _quick_key, run_generation,\n                )",
        ), (
            "            return await self._hmwa_deliver_turn_response(\n",
            "            from qqbot_context import after_turn\n            if not agent_failed_early and not is_context_overflow_failure:\n                await after_turn(self, source, session_entry, _quick_key, run_generation)\n            return await self._hmwa_deliver_turn_response(\n",
        ), (
            "            event._heartbeat_execution_started = True\n",
            "            event._heartbeat_execution_started = True\n            from qqbot_context import notify_turn\n            await notify_turn(self, 'started', event, _quick_key, run_generation)\n",
        ), (
            "            # Restore session context variables to their pre-handler state\n",
            "            from qqbot_context import notify_turn\n            await notify_turn(self, 'finished', event, _quick_key, run_generation)\n            # Restore session context variables to their pre-handler state\n",
        )],
    ),
    "gateway/run_turn_runner.py": (
        "e6ee78adeb4d4999780459472debdba763e45f4924bb09842baa0b5c0597ff14",
        [(
            "        self._wire_turn_agent_callbacks(agent, turn_route, reasoning_config, stream_delta_cb, interim_cb, want_interim)\n",
            "        if platform_key == 'qqbot':\n            # QQ owns async compaction; disable native preflight, idle and micro compaction.\n            agent.compression_enabled = False\n            _qq_compressor = getattr(agent, 'context_compressor', None)\n            if _qq_compressor is not None:\n                _qq_compressor._micro_compact_enabled = False\n        self._wire_turn_agent_callbacks(agent, turn_route, reasoning_config, stream_delta_cb, interim_cb, want_interim)\n",
        ), (
            "        result = self._run_conversation_with_approval(agent, agent_history, observed_group_context, persist_msg, persist_ts)\n",
            "        if platform_key == 'qqbot':\n            agent._qq_deadline_exceeded = False\n        result = self._run_conversation_with_approval(agent, agent_history, observed_group_context, persist_msg, persist_ts)\n        from qqbot_request_deadline import finish_turn_result\n        result = finish_turn_result(agent, result)\n",
        )],
    ),
    "gateway/slash_commands_session.py": (
        "b7cdb1be5aa153b84d9cff3aa7c35c7ad2cce3c931dbebfca7738fa2ef6b4ee4",
        [(
            "        self._invalidate_session_run_generation(session_key, reason=\"session_reset\")\n",
            "        self._invalidate_session_run_generation(session_key, reason=\"session_reset\")\n        from qqbot_context import cancel_context_scope\n        await cancel_context_scope(self, source, session_key)\n",
        ), (
            "        from agent.conversation_compression import finalize_context_engine_compression_notification\n",
            "        from qqbot_context import is_qq, manual_compress\n        if is_qq(source):\n            return await manual_compress(self, source, session_entry, history, request)\n        from agent.conversation_compression import finalize_context_engine_compression_notification\n",
        )],
    ),
    "agent/chat_completion_helpers.py": (
        "5231a0e674806db6ad7be60e2d40a19a4157f7d2baa238aaa9af66f61e0eea4c",
        [(
            "    kwargs = _build_api_kwargs_for_mode(agent, api_messages, tools_for_api)\n",
            "    kwargs = _build_api_kwargs_for_mode(agent, api_messages, tools_for_api)\n    from qqbot_request_deadline import request_policy\n    kwargs = request_policy(agent, kwargs)\n",
        ), (
            "        self.on_first_delta = on_first_delta\n        self.worker = None",
            "        self.on_first_delta = on_first_delta\n        from qqbot_request_deadline import start_request\n        start_request(self)\n        self.worker = None",
        ), (
            "        self.stale_timeout = stale_timeout\n        self.call_start = call_start\n",
            "        self.stale_timeout = stale_timeout\n        self.call_start = call_start\n        from qqbot_request_deadline import start_request\n        start_request(self)\n",
        ), (
            "        if not self.abort(\"stale_call_kill\"):\n",
            "        from qqbot_request_deadline import request_expired, mark_expired\n        if request_expired(self, mark=False):\n            if self.abort('qq_request_deadline'):\n                mark_expired(self)\n            return\n        if not self.abort(\"stale_call_kill\"):\n",
        ), (
            "    stale_timeout = _resolve_direct_stale_timeout(agent, api_kwargs)\n",
            "    from qqbot_request_deadline import bound_inline_timeout\n    stale_timeout = bound_inline_timeout(agent, _resolve_direct_stale_timeout(agent, api_kwargs))\n",
        ), (
            "            lambda request: summary_client.chat.completions.create(**bypass_chat_sdk_request_transform(request, summary_client)),\n",
            "            lambda request: (interruptible_api_call(agent, request) if getattr(agent, 'platform', None) == 'qqbot'\n                else summary_client.chat.completions.create(**bypass_chat_sdk_request_transform(request, summary_client))),\n",
        )],
    ),
    "agent/chat_completion_stream_monitor.py": (
        "8c232eb9e90335ce599b5c8d403c6e369e3fb9e01e78d4a178c517b62da420c5",
        [(
            "            _hb_now = time.time()\n",
            "            _hb_now = time.time()\n            from qqbot_request_deadline import request_expired\n            if request_expired(self):\n                self._abort_for_interrupt(_hb_now - self.last_chunk_time['t'])\n                return\n",
        )],
    ),
    "agent/chat_completion_nonstream.py": (
        "e56a634360d4f4400740966916427210ac86310a6c57ae6ba778b997e2fd6bc5",
        [(
            "        self.agent = agent\n",
            "        self.agent = agent\n        from qqbot_request_deadline import start_request\n        start_request(self)\n",
        ), (
            "            elapsed = now - self.call_start\n",
            "            elapsed = now - self.call_start\n            from qqbot_request_deadline import request_expired\n            if request_expired(self):\n                self._interrupt(elapsed)\n",
        )],
    ),
    "agent/conversation_loop.py": (
        "c93ee86e1da583abc7cc57417380cd71241ae028c04a4cb8456580b52ffbf3d8",
        [(
            "            _run_phase(build_api_request, agent, s)\n",
            "            if getattr(agent, 'platform', None) == 'qqbot':\n                from qqbot_hk_media import trim_images\n                trim_images(s.messages)\n                trim_images(s.api_messages)\n            _run_phase(build_api_request, agent, s)\n",
        )],
    ),
    "hermes_state_compression.py": (
        "c920bb587aa3e96778e71bad2082831b37f76f73ddc76272e4cc79c1fd7571e8",
        [(
            "        watermark: Optional[int] = None, watermark_ceiling: Optional[int] = None) -> None:\n",
            "        watermark: Optional[int] = None, watermark_ceiling: Optional[int] = None,\n        expected_parent_watermark: Optional[int] = None) -> None:\n",
        ), (
            "            if not messages:\n                raise RuntimeError(\"Compression child handoff must not be empty\")\n",
            "            if expected_parent_watermark is not None:\n                actual = conn.execute(\n                    'SELECT COALESCE(MAX(id), 0) FROM messages WHERE session_id = ? AND active = 1',\n                    (parent_session_id,),\n                ).fetchone()[0]\n                if actual != expected_parent_watermark:\n                    raise CompressionSessionBusyError('QQ compression parent changed')\n            if not messages:\n                raise RuntimeError(\"Compression child handoff must not be empty\")\n",
        )],
    ),
    "gateway/platforms/qqbot/adapter.py": (
        "f8f7b73d714efc4f0295fc8c1c92e9df205375c65bb63a804be4a0f2e0089e3a",
        [(
            "        att = await self._process_attachments(attachments)\n",
            "        from qqbot_hk_media import limit_payload_images\n        d = limit_payload_images({**d, 'attachments': attachments})\n        att = await self._process_attachments(d['attachments'])\n",
        ), (
            "        for att in attachments if isinstance(attachments, list) else ():\n",
            "        from qqbot_hk_media import limit_attachments\n        for att in limit_attachments(attachments):\n",
        ), (
            "        if not text.strip() and not image_urls:\n",
            "        image_urls, image_media_types = image_urls[-5:], image_media_types[-5:]\n        if not text.strip() and not image_urls:\n",
        ), (
            "            raise ValueError(f\"Blocked unsafe URL: {url[:80]}\")",
            "            raise ValueError('Blocked unsafe attachment URL')",
        ), (
            "            resp = await self._http_client.get(url, timeout=30.0, headers=self._qq_media_headers())\n            resp.raise_for_status()\n            data = resp.content",
            "            from qqbot_hk_media import MAX_IMAGE_BYTES\n            async with self._http_client.stream(\n                'GET', url, timeout=15.0, headers=self._qq_media_headers(), follow_redirects=False,\n            ) as resp:\n                resp.raise_for_status()\n                data = bytearray()\n                async for chunk in resp.aiter_bytes():\n                    data.extend(chunk)\n                    if len(data) > MAX_IMAGE_BYTES:\n                        raise ValueError('Attachment exceeds size limit')\n                data = bytes(data)",
        ), (
            "            logger.debug(\"[%s] Download failed for %s: %s\", self._log_tag, url[:80], exc)",
            "            logger.debug('[%s] Attachment download failed (%s)', self._log_tag, type(exc).__name__)",
        )],
    ),
}


def patch_source(source, expected, replacements):
    if MARKER in source:
        for _, replacement in replacements:
            if source.count(replacement) != 1:
                raise ValueError("patched sentinel mismatch")
        compile(source, "patched-hermes.py", "exec")
        return source
    if hashlib.sha256(source.encode()).hexdigest() != expected:
        raise ValueError("pinned source SHA mismatch")
    for anchor, replacement in replacements:
        if source.count(anchor) != 1:
            raise ValueError("patch anchor mismatch")
        source = source.replace(anchor, replacement, 1)
    source += "\n" + MARKER + "\n"
    compile(source, "patched-hermes.py", "exec")
    return source


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default="/opt/hermes")
    args = parser.parse_args()
    staged = []
    # Validate every source before modifying any file.
    for relative, (digest, replacements) in PATCHES.items():
        path = Path(args.root) / relative
        source = path.read_text(encoding="utf-8")
        staged.append((path, patch_source(source, digest, replacements)))
    for path, patched in staged:
        temporary = path.with_name(path.name + ".qq-context.tmp")
        temporary.write_text(patched, encoding="utf-8", newline="\n")
        os.chmod(temporary, path.stat().st_mode)
        os.replace(temporary, path)
    print("HERMES_QQ_CONTEXT_PATCH=applied")


if __name__ == "__main__":
    main()
