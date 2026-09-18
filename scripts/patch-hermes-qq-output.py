#!/usr/bin/env python3
"""Fail-closed QQ final-output patch for the pinned Hermes image."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys

PATCH_MARKER = 'QQBOT_HK_OUTPUT_PATCH = "v1"'
TARGETS = {
    "/opt/hermes/agent/turn_finalizer.py": "3e9658908b7421d3ee2da6516f2ac511541f7e134fcacaace8c807c996747fb1",
    "/opt/hermes/gateway/run_turn_runner.py": "450318a4123b71fb79ac42b10c0558364341ff26a36f98f0f8c39fe76866bf90",
    "/opt/hermes/gateway/run_turn.py": "d8456aa1246fab33dbfa86e60f46a6d1992731dbda6e7be7e223c3dda5c086d0",
}


class PatchError(RuntimeError):
    pass


PATCHES = {
    "turn_finalizer.py": [(
        """        response_text=final_response,\n        session_id=agent.session_id or \"\",\n""",
        f"""        response_text=final_response,\n        # {PATCH_MARKER}\n        user_message=original_user_message,\n        session_id=agent.session_id or \"\",\n""",
    )],
    "run_turn_runner.py": [(
        """        from gateway.run import _collect_auto_append_media_tags\n        if \"MEDIA:\" in final_response:\n""",
        f"""        from gateway.run import _collect_auto_append_media_tags\n        from gateway.response_filters import is_intentional_silence_response\n        {PATCH_MARKER}\n        if self._ctx.source.platform == Platform.QQBOT and is_intentional_silence_response(final_response):\n            return final_response\n        if \"MEDIA:\" in final_response:\n""",
    )],
    "run_turn.py": [(
        """        _intentional_silence = self._is_intentional_silence(agent_result, response)\n\n        # \"(empty)\" = the model produced no visible content after exhausting all retries.\n""",
        f"""        _intentional_silence = self._is_intentional_silence(agent_result, response)\n        from gateway.response_filters import is_intentional_silence_response\n        {PATCH_MARKER}\n        if source.platform == Platform.QQBOT and is_intentional_silence_response(response):\n            _intentional_silence = True\n\n        # \"(empty)\" = the model produced no visible content after exhausting all retries.\n""",
    ), (
        """        if _intentional_silence:\n            logger.info(\"Suppressing intentional silence marker for session %s\", session_entry.session_id)\n            response = \"\"\n\n        adapter = self._adapter_for_source(source)\n""",
        """        if _intentional_silence:\n            logger.info(\"Suppressing intentional silence marker for session %s\", session_entry.session_id)\n            if source.platform == Platform.QQBOT:\n                return None\n            response = \"\"\n\n        adapter = self._adapter_for_source(source)\n""",
    ), (
        """        # Same silence predicate as the normal path, else this branch leaks the literal marker.\n        if self._is_intentional_silence(_delivery_result, first_response):\n""",
        """        # Same silence predicate as the normal path, else this branch leaks the literal marker.\n        from gateway.response_filters import is_intentional_silence_response\n        _qq_silence = (\n            turn_ctx.source.platform == Platform.QQBOT\n            and is_intentional_silence_response(first_response)\n        )\n        if _qq_silence or self._is_intentional_silence(_delivery_result, first_response):\n""",
    )],
}

VERIFY_SENTINELS = {
    "turn_finalizer.py": (
        f"# {PATCH_MARKER}\n        user_message=original_user_message,",
    ),
    "run_turn_runner.py": (
        "self._ctx.source.platform == Platform.QQBOT and is_intentional_silence_response(final_response)",
    ),
    "run_turn.py": (
        "source.platform == Platform.QQBOT and is_intentional_silence_response(response)",
        "if source.platform == Platform.QQBOT:\n                return None",
        "if _qq_silence or self._is_intentional_silence",
    ),
}


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
