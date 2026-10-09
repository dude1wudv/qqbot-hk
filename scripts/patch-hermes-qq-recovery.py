#!/usr/bin/env python3
"""Disable unanchored durable replay for QQ groups in the pinned Hermes image."""
from __future__ import annotations
import hashlib
from pathlib import Path
import os
import sys

TARGET = Path('/opt/hermes/gateway/delivery_ledger.py')
EXPECTED_SHA = '2454f213f94484ffaf4d61b24b17c0b3eec85e30c8bb5bf38d2eb159c7be7875'
MARKER = 'QQBOT_HK_RECOVERY_PATCH = "v1"'
HELPER = '''QQBOT_HK_RECOVERY_PATCH = "v1"


def _qq_group_without_anchor(platform: str, session_key: str) -> bool:
    # Ledger rows have no inbound message ID, epoch or successful chunk cursor.
    # Replaying them can duplicate a reply or attach it to a later conversation.
    return platform == "qqbot" and ":qqbot:group:" in ":" + session_key + ":"


def _abandon_qq_group(conn, oid, now):
    conn.execute("UPDATE delivery_obligations SET state='abandoned', content='', "
                 "updated_at=?, last_error='qq_group_missing_reply_anchor' WHERE obligation_id=?",
                 (now, oid))


'''
PATCHES = [
    ('def record_obligation(', HELPER + 'def record_obligation('),
    ('    now, (pid, started) = time.time(), _owner_stamp()\n    with _DB_LOCK, _transaction() as conn:\n',
     '    if _qq_group_without_anchor(platform, session_key):\n        return\n    now, (pid, started) = time.time(), _owner_stamp()\n    with _DB_LOCK, _transaction() as conn:\n'),
    ('    now = time.time()\n    with _DB_LOCK, _transaction() as conn:\n',
     '    if _qq_group_without_anchor(platform, session_key):\n        return\n    now = time.time()\n    with _DB_LOCK, _transaction() as conn:\n'),
    ('                continue  # a live gateway still owns this row\n',
     '                continue  # a live gateway still owns this row\n'
     '            if _qq_group_without_anchor(platform, session_key):\n'
     '                _abandon_qq_group(conn, oid, now)\n                continue\n'),
    ('            due = retry_not_before(updated_at, last_error, attempts)\n',
     '            if _qq_group_without_anchor(row_platform, session_key):\n'
     '                _abandon_qq_group(conn, oid, now)\n                continue\n'
     '            due = retry_not_before(updated_at, last_error, attempts)\n'),
]


def patch_source(source: str) -> str:
    if MARKER in source:
        for _, replacement in PATCHES:
            if source.count(replacement) != 1:
                raise ValueError('incomplete QQ recovery patch')
        compile(source, str(TARGET), 'exec')
        return source
    if hashlib.sha256(source.encode()).hexdigest() != EXPECTED_SHA:
        raise ValueError('pinned delivery_ledger.py SHA mismatch')
    for original, replacement in PATCHES:
        if source.count(original) != 1:
            raise ValueError('QQ recovery patch sentinel mismatch')
        source = source.replace(original, replacement)
    compile(source, str(TARGET), 'exec')
    return source


def main() -> int:
    try:
        source = TARGET.read_text()
        patched = patch_source(source)
        if patched != source:
            temporary = TARGET.with_suffix('.qq-recovery.tmp')
            temporary.write_text(patched)
            os.chmod(temporary, TARGET.stat().st_mode)
            os.replace(temporary, TARGET)
    except (OSError, ValueError, SyntaxError) as exc:
        print(f'ERROR: QQ recovery patch failed: {exc}', file=sys.stderr)
        return 1
    print('HERMES_QQ_RECOVERY_PATCH=ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
