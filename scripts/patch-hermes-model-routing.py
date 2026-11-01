#!/usr/bin/env python3
"""Pinned, single-application QQ model routing hooks, after the existing QQ patches."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path


MARKER = '# QQBOT_HK_MODEL_ROUTING_PATCH = "v1"'
FALLBACK_SIGNATURE = 'def try_activate_fallback(agent, reason: FailoverReason | None = None, reset_at=None) -> bool:\n'

PATCHES = {'gateway/run_turn_runner.py': ('51318d28c7ad56c8a3931a8616823aed1651d979c6a92b20d2133a572a063341',
                                [('        self._wire_turn_agent_callbacks(agent, turn_route, '
                                  'reasoning_config, stream_delta_cb, interim_cb, want_interim)\n',
                                  '        self._wire_turn_agent_callbacks(agent, turn_route, '
                                  'reasoning_config, stream_delta_cb, interim_cb, want_interim)\n'
                                  '        from qqbot_model_routing import configure_turn\n'
                                  '        configure_turn(agent, runner, source=ctx.source, '
                                  'session_key=ctx.session_key)\n')]),
 'agent/chat_completion_helpers.py': ('e520c85402364da7d385ae4d3fd5a7b9a37c58429108cd3a58987226381c0299',
                                      [('def try_activate_fallback(agent, reason: FailoverReason | None = '
                                        'None, reset_at=None) -> bool:\n',
                                        'def try_activate_fallback(agent, reason: FailoverReason | None = '
                                        'None, reset_at=None) -> bool:\n'
                                        '    from qqbot_model_routing import guarded_fallback\n'
                                        '    return guarded_fallback(agent, reason, reset_at, '
                                        '_qq_native_try_activate_fallback)\n'
                                        '\n'
                                        '\n'
                                        'def _qq_native_try_activate_fallback(agent, reason: '
                                        'FailoverReason | None = None, reset_at=None) -> bool:\n'),
                                       ('    try:\n'
                                        '        # Re-resolve reasoning_config for the new fallback model '
                                        '(Closes #21256). Wrapped in try/except\n',
                                        '    from qqbot_model_routing import preserve_reasoning\n'
                                        '    if preserve_reasoning(agent):\n'
                                        '        return\n'
                                        '    try:\n'
                                        '        # Re-resolve reasoning_config for the new fallback model '
                                        '(Closes #21256). Wrapped in try/except\n'),
                                       ('    if agent.api_mode == "codex_responses":\n'
                                        '        return agent._run_codex_stream(api_kwargs, '
                                        'client=make_client("codex_stream_request"),\n',
                                        '    from qqbot_model_routing import capture_route_state, '
                                        'observe_response\n'
                                        '    qq_route_state = capture_route_state(agent)\n'
                                        '    if agent.api_mode == "codex_responses":\n'
                                        '        return agent._run_codex_stream(api_kwargs, '
                                        'client=make_client("codex_stream_request"),\n'),
                                       ('    return _chat_completion_capturing_headers(agent, '
                                        'request_client, api_kwargs)\n',
                                        '    response = _chat_completion_capturing_headers(agent, '
                                        'request_client, api_kwargs)\n'
                                        '    return observe_response(agent, response, '
                                        'state=qq_route_state)\n'),
                                       ('        self.worker = None  # request thread; None in inline '
                                        'mode\n',
                                        '        from qqbot_model_routing import capture_route_state\n'
                                        '        self._qq_model_route_state = capture_route_state(agent)\n'
                                        '        self.worker = None  # request thread; None in inline '
                                        'mode\n'),
                                       ('    def _accept_chat_chunk(self, stream_attempt_id: int, chunk: '
                                        'Any) -> bool:\n',
                                        '    def _accept_chat_chunk(self, stream_attempt_id: int, chunk: '
                                        'Any) -> bool:\n'
                                        '        from qqbot_model_routing import note_stream_chunk\n'
                                        '        note_stream_chunk(self.agent, chunk, '
                                        'state=self._qq_model_route_state)\n'),
                                       ('                    self.result["response"] = '
                                        '_with_stream_emitters(\n'
                                        '                        self.agent, lambda: '
                                        'self._call_wire(stream_attempt_id))\n'
                                        '                    return  # success\n',
                                        '                    self.result["response"] = '
                                        '_with_stream_emitters(\n'
                                        '                        self.agent, lambda: '
                                        'self._call_wire(stream_attempt_id))\n'
                                        '                    from qqbot_model_routing import '
                                        'observe_response\n'
                                        '                    observe_response(self.agent, '
                                        'self.result["response"], state=self._qq_model_route_state)\n'
                                        '                    return  # success\n')]),
 'agent/conversation_loop.py': ('76a4689a9c4ee7da543473ce051bafc034d43e2110bbcf4838418682c562b086',
                                [('            _v = _run_phase(\n'
                                  '                run_tool_round if s.assistant_message.tool_calls else '
                                  'finish_text_response, agent, s\n',
                                  '            if s.assistant_message.tool_calls:\n'
                                  '                from qqbot_model_routing import note_progress\n'
                                  "                note_progress(agent, 'tools')\n"
                                  '            _v = _run_phase(\n'
                                  '                run_tool_round if s.assistant_message.tool_calls else '
                                  'finish_text_response, agent, s\n')])}


def patch_source(source, expected_sha256, replacements):
    if MARKER in source:
        if source.count(MARKER) != 1 or any(source.count(new) != 1 for _, new in replacements):
            raise ValueError("QQ model routing patched sentinel mismatch")
        compile(source, "patched-hermes-model-routing.py", "exec")
        return source
    if hashlib.sha256(source.encode("utf-8")).hexdigest() != expected_sha256:
        raise ValueError("QQ model routing pinned source SHA mismatch")
    for old, new in replacements:
        if source.count(old) != 1:
            raise ValueError("QQ model routing source anchor mismatch")
        source = source.replace(old, new, 1)
    source += "\n" + MARKER + "\n"
    compile(source, "patched-hermes-model-routing.py", "exec")
    return source


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default="/opt/hermes")
    args = parser.parse_args()
    staged = []
    for relative, (digest, replacements) in PATCHES.items():
        path = Path(args.root) / relative
        staged.append((path, patch_source(path.read_text(encoding="utf-8"), digest, replacements)))
    # Validate all sources before replacing any; a changed upstream cannot be half-patched.
    for path, source in staged:
        temporary = path.with_name(path.name + ".qq-model-routing.tmp")
        temporary.write_text(source, encoding="utf-8", newline="\n")
        os.chmod(temporary, path.stat().st_mode)
        os.replace(temporary, path)
    print("HERMES_QQ_MODEL_ROUTING_PATCH=applied")


if __name__ == "__main__":
    main()
