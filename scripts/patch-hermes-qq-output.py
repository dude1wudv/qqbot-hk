#!/usr/bin/env python3
"""Fail-closed QQ final-output patch for the pinned Hermes image."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys

PATCH_MARKER = 'QQBOT_HK_OUTPUT_PATCH = "v1"'
TARGETS = {'/opt/hermes/agent/turn_finalizer.py': '4c1e83c6a23bcb7fc10f942306459b54b130975fdce3d76d8fd7f2432698632e',
 '/opt/hermes/gateway/run_turn_runner.py': '04f76f4857a8ba251b014d615b814f3920fef8212ad24e69b28fbbc4fdf2df39',
 '/opt/hermes/gateway/run_turn.py': 'd26c177a4c0edcdd8f901d4b1b7db789c1aa534d72177645afdab32af5509b7d',
 '/opt/hermes/agent/turn_context.py': '09a6159b0301da271f1a6bdcf15d924a05dee8d7d6b9e2f4ed46e406aac0aa4e',
 '/opt/hermes/gateway/run_turn_persistence.py': '31d1d1869b5c9d9d08b8f57bd3ccde4dd9c2844c4a2f74fae991d28842363880'}


class PatchError(RuntimeError):
    pass


PATCHES = {'turn_context.py': [('    original_user_message = persist_user_message if persist_user_message is not '
                      'None else user_message\n',
                      '    original_user_message = persist_user_message if persist_user_message is not '
                      'None else user_message\n'
                      '    # QQBOT_HK_OUTPUT_PATCH = "v1"\n'
                      '    agent._qqbot_output_user_context = (turn_id, original_user_message)\n')],
 'turn_finalizer.py': [('    # First hook to return a string wins; None/empty leaves the text unchanged.\n',
                        '    # QQBOT_HK_OUTPUT_PATCH = "v1"\n'
                        '    user_context = getattr(agent, "_qqbot_output_user_context", None)\n'
                        '    user_message = user_context[1] if user_context and user_context[0] == turn_id '
                        'else None\n'
                        '    # First hook to return a string wins; None/empty leaves the text '
                        'unchanged.\n'),
                       ('        response_text=final_response,\n'
                        '        session_id=agent.session_id or "",\n',
                        '        response_text=final_response,\n'
                        '        user_message=user_message,\n'
                        '        session_id=agent.session_id or "",\n')],
 'run_turn_runner.py': [('        from gateway.run import _collect_auto_append_media_tags\n'
                         '        if "MEDIA:" in final_response:\n',
                         '        from gateway.run import _collect_auto_append_media_tags\n'
                         '        from gateway.response_filters import is_intentional_silence_response\n'
                         '        QQBOT_HK_OUTPUT_PATCH = "v1"\n'
                         '        if self._ctx.source.platform == Platform.QQBOT and '
                         'is_intentional_silence_response(final_response):\n'
                         '            return final_response\n'
                         '        if "MEDIA:" in final_response:\n')],
 'run_turn.py': [('        # Same silence predicate as the normal path, else this branch leaks the literal '
                  'marker.\n'
                  '        if self._is_intentional_silence(_delivery_result, first_response):\n',
                  '        # Same silence predicate as the normal path, else this branch leaks the literal '
                  'marker.\n'
                  '        from gateway.response_filters import is_intentional_silence_response\n'
                  '        QQBOT_HK_OUTPUT_PATCH = "v1"\n'
                  '        _qq_silence = (\n'
                  '            turn_ctx.source.platform == Platform.QQBOT\n'
                  '            and is_intentional_silence_response(first_response)\n'
                  '        )\n'
                  '        if _qq_silence or self._is_intentional_silence(_delivery_result, '
                  'first_response):\n'),
                 ('            if silence_allowed(turn_ctx.persist_user_display_kind, '
                  'turn_ctx.reply_expected):\n',
                  '            if turn_ctx.source.platform == Platform.QQBOT or '
                  'silence_allowed(turn_ctx.persist_user_display_kind, turn_ctx.reply_expected):\n')],
 'run_turn_persistence.py': [('        _intentional_silence = self._is_intentional_silence(agent_result, '
                              'response)\n',
                              '        _intentional_silence = self._is_intentional_silence(agent_result, '
                              'response)\n'
                              '        from gateway.response_filters import '
                              'is_intentional_silence_response\n'
                              '        QQBOT_HK_OUTPUT_PATCH = "v1"\n'
                              '        if source.platform == Platform.QQBOT and '
                              'is_intentional_silence_response(response):\n'
                              '            _intentional_silence = True\n'),
                             ('        if _intentional_silence and not silence_allowed(_silence_kind, '
                              '_silence_reply_expected):\n',
                              '        if _intentional_silence and source.platform != Platform.QQBOT and '
                              'not silence_allowed(_silence_kind, _silence_reply_expected):\n'),
                             ('        if _intentional_silence:\n'
                              '            logger.info("Suppressing intentional silence marker for session '
                              '%s", session_entry.session_id)\n'
                              '            response = ""\n'
                              '\n'
                              '        adapter = self._delivery_adapter_for(source)\n',
                              '        if _intentional_silence:\n'
                              '            logger.info("Suppressing intentional silence marker for session '
                              '%s", session_entry.session_id)\n'
                              '            if source.platform == Platform.QQBOT:\n'
                              '                return None\n'
                              '            response = ""\n'
                              '\n'
                              '        adapter = self._delivery_adapter_for(source)\n')]}

VERIFY_SENTINELS = {'turn_context.py': ('agent._qqbot_output_user_context = (turn_id, original_user_message)',),
 'turn_finalizer.py': ('user_context = getattr(agent, "_qqbot_output_user_context", None)',
                       'user_message=user_message,'),
 'run_turn_runner.py': ('self._ctx.source.platform == Platform.QQBOT and '
                        'is_intentional_silence_response(final_response)',),
 'run_turn.py': ('if _qq_silence or self._is_intentional_silence',
                 'turn_ctx.source.platform == Platform.QQBOT or '
                 'silence_allowed(turn_ctx.persist_user_display_kind, turn_ctx.reply_expected)'),
 'run_turn_persistence.py': ('source.platform == Platform.QQBOT and '
                             'is_intentional_silence_response(response)',
                             'if source.platform == Platform.QQBOT:\n                return None')}


def verify_patched_source(path: Path, source: str) -> None:
    if source.count(PATCH_MARKER) != 1:
        raise PatchError(f"{path.name} patch marker mismatch")
    for sentinel in VERIFY_SENTINELS[path.name]:
        if source.count(sentinel) != 1:
            raise PatchError(f"{path.name} patched sentinel mismatch: {sentinel!r}")
    compile(source, str(path), "exec")


def _sha(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def patch_source(path: Path, source: str, expected_sha: str) -> str:
    if PATCH_MARKER in source:
        verify_patched_source(path, source)
        return source
    actual = _sha(source)
    if actual != expected_sha:
        raise PatchError(f"pinned {path.name} SHA mismatch: expected {expected_sha}, got {actual}")
    patched = source
    for old, new in PATCHES[path.name]:
        count = patched.count(old)
        if count != 1:
            raise PatchError(f"{path.name} sentinel mismatch: expected 1, got {count}")
        patched = patched.replace(old, new)
    verify_patched_source(path, patched)
    return patched


def patch_file(path: Path, expected_sha: str) -> bool:
    source = path.read_text(encoding="utf-8")
    patched = patch_source(path, source, expected_sha)
    if patched == source:
        return False
    stat = path.stat()
    temporary = path.with_name(path.name + ".qqbot-hk-output.tmp")
    temporary.write_text(patched, encoding="utf-8", newline="\n")
    os.chmod(temporary, stat.st_mode)
    os.replace(temporary, path)
    return True


def main() -> int:
    try:
        changed = []
        for name, expected in TARGETS.items():
            if patch_file(Path(name), expected):
                changed.append(Path(name).name)
    except (OSError, UnicodeError, SyntaxError, PatchError) as exc:
        print(f"ERROR: Hermes QQ output patch failed: {exc}", file=sys.stderr)
        return 1
    print("HERMES_QQ_OUTPUT_PATCH=" + ("applied:" + ",".join(changed) if changed else "already-applied"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
