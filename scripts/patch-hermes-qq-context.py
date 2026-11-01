#!/usr/bin/env python3
"""Fail-closed integration seams for the pinned Hermes QQ context policy."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path

MARKER = '# QQBOT_HK_CONTEXT_PATCH = "v2"'

PATCHES = {'gateway/run_turn.py': ('e997f1c0fc0471085ef5651eaa64bf97e3757ce926099c157a21390ca4c7a75f',
                         [('            return await self._deliver_turn_response_then_limit_offer(\n',
                           '            from qqbot_context import after_turn\n'
                           '            if not agent_failed_early and not is_context_overflow_failure:\n'
                           '                await after_turn(self, source, session_entry, _quick_key, '
                           'run_generation)\n'
                           '            return await self._deliver_turn_response_then_limit_offer(\n'),
                          ('            event._heartbeat_execution_started = True\n',
                           '            event._heartbeat_execution_started = True\n'
                           '            from qqbot_context import notify_turn\n'
                           "            await notify_turn(self, 'started', event, _quick_key, "
                           'run_generation)\n'),
                          ('            # Restore session context variables to their pre-handler state\n',
                           '            from qqbot_context import notify_turn\n'
                           "            await notify_turn(self, 'finished', event, _quick_key, "
                           'run_generation)\n'
                           '            # Restore session context variables to their pre-handler '
                           'state\n')]),
 'gateway/run_turn_runner.py': ('887c34cbacce687093735a08bcfe28b71f47b39f7c16c9dcde8656eafe97d3eb',
                                [('        self._wire_turn_agent_callbacks(agent, turn_route, '
                                  'reasoning_config, stream_delta_cb, interim_cb, want_interim)\n',
                                  "        if platform_key == 'qqbot':\n"
                                  '            # QQ owns async compaction; disable native preflight, idle '
                                  'and micro compaction.\n'
                                  '            agent.compression_enabled = False\n'
                                  "            _qq_compressor = getattr(agent, 'context_compressor', "
                                  'None)\n'
                                  '            if _qq_compressor is not None:\n'
                                  '                _qq_compressor._micro_compact_enabled = False\n'
                                  '        self._wire_turn_agent_callbacks(agent, turn_route, '
                                  'reasoning_config, stream_delta_cb, interim_cb, want_interim)\n'),
                                 ('            with sudo_prompt_scope(self, agent), '
                                  'secret_prompt_scope(self, agent):\n',
                                  "            if platform_key == 'qqbot':\n"
                                  '                agent._qq_deadline_exceeded = False\n'
                                  '            with sudo_prompt_scope(self, agent), '
                                  'secret_prompt_scope(self, agent):\n'),
                                 ('        persisted_turn = _persisted_turn(agent, agent_history, result, '
                                  'compressions)\n',
                                  '        from qqbot_request_deadline import finish_turn_result\n'
                                  '        result = finish_turn_result(agent, result)\n'
                                  '        persisted_turn = _persisted_turn(agent, agent_history, result, '
                                  'compressions)\n')]),
 'gateway/slash_commands_session.py': ('265df12e6a442a7d744d429c3acf73358070b34a0059a896c3c98f95a81b2a9d',
                                       [('        self._invalidate_session_run_generation(session_key, '
                                         'reason="session_reset")\n',
                                         '        self._invalidate_session_run_generation(session_key, '
                                         'reason="session_reset")\n'
                                         '        from qqbot_context import cancel_context_scope\n'
                                         '        await cancel_context_scope(self, source, session_key)\n'),
                                        ('        from agent.conversation_compression import '
                                         'finalize_context_engine_compression_notification\n',
                                         '        from qqbot_context import is_qq, manual_compress\n'
                                         '        if is_qq(source):\n'
                                         '            return await manual_compress(self, source, '
                                         'session_entry, history, request)\n'
                                         '        from agent.conversation_compression import '
                                         'finalize_context_engine_compression_notification\n')]),
 'agent/chat_completion_helpers.py': ('d5ac7612af5819891293c4ec182f71a1a9e59f6aa32e98f15266a85fca64e4a8',
                                      [('    kwargs = _build_api_kwargs_for_mode(agent, api_messages, '
                                        'tools_for_api)\n',
                                        '    kwargs = _build_api_kwargs_for_mode(agent, api_messages, '
                                        'tools_for_api)\n'
                                        '    from qqbot_request_deadline import request_policy\n'
                                        '    kwargs = request_policy(agent, kwargs)\n'),
                                       ('        self.on_first_delta = on_first_delta\n'
                                        '        self.worker = None',
                                        '        self.on_first_delta = on_first_delta\n'
                                        '        from qqbot_request_deadline import start_request\n'
                                        '        start_request(self)\n'
                                        '        self.worker = None'),
                                       ('        self.stale_timeout = stale_timeout\n'
                                        '        self.call_start = call_start\n',
                                        '        self.stale_timeout = stale_timeout\n'
                                        '        self.call_start = call_start\n'
                                        '        from qqbot_request_deadline import start_request\n'
                                        '        start_request(self)\n'),
                                       ('        if not self.abort("stale_call_kill"):\n',
                                        '        from qqbot_request_deadline import request_expired, '
                                        'mark_expired\n'
                                        '        if request_expired(self, mark=False):\n'
                                        '            with self.lock:\n'
                                        '                if self.done or self.cancelled:\n'
                                        '                    return\n'
                                        '                mark_expired(self)\n'
                                        "            self.abort('qq_request_deadline')\n"
                                        '            return\n'
                                        '        if not self.abort("stale_call_kill"):\n'),
                                       ('    stale_timeout = _resolve_direct_stale_timeout(agent, '
                                        'api_kwargs)\n',
                                        '    from qqbot_request_deadline import bound_inline_timeout\n'
                                        '    stale_timeout = bound_inline_timeout(agent, '
                                        '_resolve_direct_stale_timeout(agent, api_kwargs))\n')]),
 'agent/chat_completion_stream_monitor.py': ('5b03c94adbc01eefa2d5ea7cccc1548ffd46c6f57e129095d7234bc41fee763a',
                                             [('            _hb_now = time.time()\n',
                                               '            _hb_now = time.time()\n'
                                               '            from qqbot_request_deadline import '
                                               'request_expired\n'
                                               '            if request_expired(self):\n'
                                               '                self._abort_for_interrupt(_hb_now - '
                                               "self.last_chunk_time['t'])\n"
                                               '                return\n')]),
 'agent/chat_completion_nonstream.py': ('34d4afad0a0c7228a0518eed4a2521ba1a340f509c83784c74abceb56f1becb7',
                                        [('        self.agent = agent\n',
                                          '        self.agent = agent\n'
                                          '        from qqbot_request_deadline import start_request\n'
                                          '        start_request(self)\n'),
                                         ('            elapsed = now - self.call_start\n',
                                          '            elapsed = now - self.call_start\n'
                                          '            from qqbot_request_deadline import request_expired\n'
                                          '            if request_expired(self):\n'
                                          '                self._interrupt(elapsed)\n')]),
 'agent/conversation_loop.py': ('36dd6f02b169a544a95598dee74cb775914420dc700cc88cc946effd103b72bc',
                                [('            _run_phase(build_api_request, agent, s)\n',
                                  "            if getattr(agent, 'platform', None) == 'qqbot':\n"
                                  '                from qqbot_hk_media import trim_images\n'
                                  '                trim_images(s.messages)\n'
                                  '                trim_images(s.api_messages)\n'
                                  '            _run_phase(build_api_request, agent, s)\n')]),
 'hermes_state_compression.py': ('b41cb7af654c38db96b91d8bb39ea0736ab25fe638c81d100d28e663a0a34798',
                                 [('\n'
                                   '        watermark: Optional[int] = None, watermark_ceiling: '
                                   'Optional[int] = None) -> None:\n',
                                   '\n'
                                   '        watermark: Optional[int] = None, watermark_ceiling: '
                                   'Optional[int] = None,\n'
                                   '        expected_parent_watermark: Optional[int] = None) -> None:\n'),
                                  ('\n'
                                   '            watermark: Optional[int] = None, watermark_ceiling: '
                                   'Optional[int] = None) -> None:\n',
                                   '\n'
                                   '            watermark: Optional[int] = None, watermark_ceiling: '
                                   'Optional[int] = None,\n'
                                   '            expected_parent_watermark: Optional[int] = None) -> '
                                   'None:\n'),
                                  ('            lease_ttl_seconds=lease_ttl_seconds, watermark=watermark, '
                                   'watermark_ceiling=watermark_ceiling), messages)\n',
                                   '            lease_ttl_seconds=lease_ttl_seconds, watermark=watermark, '
                                   'watermark_ceiling=watermark_ceiling,\n'
                                   '            expected_parent_watermark=expected_parent_watermark), '
                                   'messages)\n'),
                                  ('        if not messages:\n'
                                   '            raise RuntimeError("Compression child handoff must not be '
                                   'empty")\n',
                                   '        if expected_parent_watermark is not None:\n'
                                   '            actual = conn.execute(\n'
                                   "                'SELECT COALESCE(MAX(id), 0) FROM messages WHERE "
                                   "session_id = ? AND active = 1',\n"
                                   '                (parent_session_id,),\n'
                                   '            ).fetchone()[0]\n'
                                   '            if actual != expected_parent_watermark:\n'
                                   "                raise CompressionSessionBusyError('QQ compression "
                                   "parent changed')\n"
                                   '        if not messages:\n'
                                   '            raise RuntimeError("Compression child handoff must not be '
                                   'empty")\n')]),
 'gateway/run_turn_prepare.py': ('d78059989e24fc3c15f282c41bccd3e07913dde3f0ad457b0d7117e6b084e9d6',
                                 [('            history = await self._hmwa_run_session_hygiene(\n'
                                   '                event, source, session_entry, session_key, history, '
                                   '_quick_key, run_generation,\n'
                                   '            )',
                                   '            from qqbot_context import context_boundary, is_qq\n'
                                   '            if is_qq(source):\n'
                                   '                history = await context_boundary(self, source, '
                                   'session_entry, history, _quick_key, run_generation)\n'
                                   '            else:\n'
                                   '                history = await self._hmwa_run_session_hygiene(\n'
                                   '                    event, source, session_entry, session_key, '
                                   'history, _quick_key, run_generation,\n'
                                   '                )')]),
 'plugins/platforms/qqbot/adapter.py': ('7ec18a27c521f38c713dcc004a91ad7b842c344f389aa9eba60895cc3712f425',
                                        [('        att = await self._process_attachments(attachments)\n',
                                          '        from qqbot_hk_media import limit_payload_images\n'
                                          "        d = limit_payload_images({**d, 'attachments': "
                                          'attachments})\n'
                                          '        att = await '
                                          "self._process_attachments(d['attachments'])\n"),
                                         ('        for att in attachments if isinstance(attachments, list) '
                                          'else ():\n',
                                          '        from qqbot_hk_media import limit_attachments\n'
                                          '        for att in limit_attachments(attachments):\n'),
                                         ('        if not text.strip() and not image_urls:\n',
                                          '        image_urls, image_media_types = image_urls[-5:], '
                                          'image_media_types[-5:]\n'
                                          '        if not text.strip() and not image_urls:\n'),
                                         ('            raise ValueError(f"Blocked unsafe URL: {url[:80]}")',
                                          "            raise ValueError('Blocked unsafe attachment URL')"),
                                         ('            resp = await self._http_client.get(url, '
                                          'timeout=30.0, headers=self._qq_media_headers())\n'
                                          '            resp.raise_for_status()\n'
                                          '            data = resp.content',
                                          '            from qqbot_hk_media import MAX_IMAGE_BYTES\n'
                                          '            async with self._http_client.stream(\n'
                                          "                'GET', url, timeout=15.0, "
                                          'headers=self._qq_media_headers(), follow_redirects=False,\n'
                                          '            ) as resp:\n'
                                          '                resp.raise_for_status()\n'
                                          '                data = bytearray()\n'
                                          '                async for chunk in resp.aiter_bytes():\n'
                                          '                    data.extend(chunk)\n'
                                          '                    if len(data) > MAX_IMAGE_BYTES:\n'
                                          "                        raise ValueError('Attachment exceeds "
                                          "size limit')\n"
                                          '                data = bytes(data)'),
                                         ('            logger.debug("[%s] Download failed for %s: %s", '
                                          'self._log_tag, url[:80], exc)',
                                          "            logger.debug('[%s] Attachment download failed "
                                          "(%s)', self._log_tag, type(exc).__name__)")])}


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
