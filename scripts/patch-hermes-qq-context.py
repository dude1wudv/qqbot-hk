#!/usr/bin/env python3
"""Fail-closed integration seams for the pinned Hermes QQ context policy."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path

MARKER = '# QQBOT_HK_CONTEXT_PATCH = "v1"'

PATCHES = {
    "gateway/run_turn.py": (
        "5ad6fa0f615613f7380c6454117f0de83f9156a6ddbbbf7132607a8825a8abe6",
        [(
            "            history = await self._hmwa_run_session_hygiene(\n                event, source, session_entry, session_key, history, _quick_key, run_generation,\n            )",
            "            from qqbot_context import context_boundary, is_qq\n            if is_qq(source):\n                history = await context_boundary(self, source, session_entry, history, _quick_key, run_generation)\n            else:\n                history = await self._hmwa_run_session_hygiene(\n                    event, source, session_entry, session_key, history, _quick_key, run_generation,\n                )",
        ), (
            "            return await self._hmwa_deliver_turn_response(\n",
            "            from qqbot_context import after_turn\n            if not agent_failed_early and not is_context_overflow_failure:\n                await after_turn(self, source, session_entry, _quick_key, run_generation)\n            return await self._hmwa_deliver_turn_response(\n",
        )],
    ),
    "gateway/run_turn_runner.py": (
        "92dbd34879014af4308cc8142a12a57cc61642a2650739beff28e9228502b53c",
        [(
            "        self._wire_turn_agent_callbacks(agent, turn_route, reasoning_config, stream_delta_cb, interim_cb, want_interim)\n",
            "        if platform_key == 'qqbot':\n            # QQ owns async compaction; no native synchronous preflight/idle compression.\n            agent.compression_enabled = False\n        self._wire_turn_agent_callbacks(agent, turn_route, reasoning_config, stream_delta_cb, interim_cb, want_interim)\n",
        )],
    ),
    "agent/conversation_loop.py": (
        "2086d4d084a7cba728a5862f2f27d172860908af967e2b83a43d22fe757548c2",
        [(
            "            _run_phase(build_api_request, agent, s)\n",
            "            if getattr(agent, 'platform', None) == 'qqbot':\n                from qqbot_hk_media import trim_images\n                trim_images(s.messages)\n                trim_images(s.api_messages)\n            _run_phase(build_api_request, agent, s)\n",
        )],
    ),
    "hermes_state_compression.py": (
        "539f164c81cca0ae095b6b0c32f498ee720fc969cdd2eb8d03b948818edd2169",
        [(
            "        watermark: Optional[int] = None, watermark_ceiling: Optional[int] = None) -> None:\n",
            "        watermark: Optional[int] = None, watermark_ceiling: Optional[int] = None,\n        expected_parent_watermark: Optional[int] = None) -> None:\n",
        ), (
            "            if not messages:\n                raise RuntimeError(\"Compression child handoff must not be empty\")\n",
            "            if expected_parent_watermark is not None:\n                actual = conn.execute(\n                    'SELECT COALESCE(MAX(id), 0) FROM messages WHERE session_id = ? AND active = 1',\n                    (parent_session_id,),\n                ).fetchone()[0]\n                if actual != expected_parent_watermark:\n                    raise CompressionSessionBusyError('QQ compression parent changed')\n            if not messages:\n                raise RuntimeError(\"Compression child handoff must not be empty\")\n",
        )],
    ),
    "gateway/platforms/qqbot/adapter.py": (
        "6f5238779829e7275261848541c27a98cc28b9b4361a84ef60f20a53488ba29d",
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
