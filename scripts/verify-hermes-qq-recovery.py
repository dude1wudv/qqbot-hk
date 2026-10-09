#!/usr/bin/env python3
"""Offline behavior check against the actual patched Hermes delivery ledger."""
from contextlib import contextmanager
import sqlite3
from unittest.mock import patch


def verify(ledger):
    assert ledger.QQBOT_HK_RECOVERY_PATCH == 'v1'
    conn = sqlite3.connect(':memory:')
    ledger._initialize_schema(conn)

    @contextmanager
    def transaction():
        with conn:
            yield conn

    def record(oid, platform='qqbot', kind='group', crash=False):
        args = dict(obligation_id=oid, session_key=f'agent:main:{platform}:{kind}:g',
                    platform=platform, chat_id='g', thread_id=None, content='测试正文')
        if crash:
            ledger.record_crash_left_reply(**args, since=0)
        else:
            ledger.record_obligation(**args)

    def legacy(oid, kind='group', owner=101, profile='default'):
        conn.execute('''INSERT INTO delivery_obligations
            (obligation_id,session_key,platform,chat_id,content,state,attempts,
             created_at,updated_at,owner_pid,owner_started_at,last_error,adapter_profile)
            VALUES (?,?, 'qqbot','g','历史正文','failed',0,900,900,?,7,'send_path_degraded',?)''',
            (oid, f'agent:main:qqbot:{kind}:g', owner, profile))
        conn.commit()

    try:
        with patch.object(ledger, '_transaction', transaction), patch.object(ledger, '_owner_stamp', return_value=(101, 7)), patch.object(ledger, '_owner_alive', return_value=False):
            record('group-new')
            record('group-crash', crash=True)
            assert conn.execute('SELECT COUNT(*) FROM delivery_obligations').fetchone()[0] == 0
            record('dm', kind='dm')
            record('telegram', platform='telegram')
            assert conn.execute('SELECT COUNT(*) FROM delivery_obligations').fetchone()[0] == 2
            conn.execute('DELETE FROM delivery_obligations')
            legacy('runtime')
            legacy('other-owner', owner=202)
            legacy('other-profile', profile='secondary')
            legacy('runtime-dm', kind='dm')
            claimed = ledger.sweep_failed_for_runtime('qqbot', now=1000)
            assert [row['obligation_id'] for row in claimed] == ['runtime-dm']
            assert conn.execute("SELECT state,content FROM delivery_obligations WHERE obligation_id='runtime'").fetchone() == ('abandoned', '')
            assert conn.execute("SELECT state FROM delivery_obligations WHERE obligation_id='other-owner'").fetchone()[0] == 'failed'
            assert conn.execute("SELECT state FROM delivery_obligations WHERE obligation_id='other-profile'").fetchone()[0] == 'failed'
            conn.execute('DELETE FROM delivery_obligations')
            legacy('boot')
            legacy('boot-dm', kind='dm')
            claimed = ledger.sweep_recoverable(now=1000)
            assert [row['obligation_id'] for row in claimed] == ['boot-dm']
            assert conn.execute("SELECT state,content FROM delivery_obligations WHERE obligation_id='boot'").fetchone() == ('abandoned', '')
            conn.execute('DELETE FROM delivery_obligations')
            legacy('live-owner')
            with patch.object(ledger, '_owner_alive', return_value=True):
                assert ledger.sweep_recoverable(now=1000) == []
            assert conn.execute('SELECT state FROM delivery_obligations').fetchone()[0] == 'failed'
    finally:
        conn.close()
    print('HERMES_QQ_RECOVERY_VERIFY=ok')


if __name__ == '__main__':
    from gateway import delivery_ledger
    verify(delivery_ledger)
