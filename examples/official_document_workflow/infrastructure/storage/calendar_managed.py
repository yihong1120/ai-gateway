"""Encrypted creation provenance and non-expiring manual-supersession backups."""
from __future__ import annotations

import hashlib
import json
import time

from examples.shared.runtime.modules import ModuleReference

_workflow_runtime = ModuleReference('examples.official_document_workflow.runtime')


def normalized_detail(detail):
    value = json.loads(json.dumps(detail, ensure_ascii=False))
    if not value or len(value[0]) < 43:
        raise ValueError('行程快照不完整，未清理。')
    # The editor's per-request form data is not event content or provenance.
    value[0][1] = ''
    return value


def digest(detail):
    return hashlib.sha256(json.dumps(normalized_detail(detail), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class ManagedCalendar:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def scope(identity, event_id):
        return f'calendar-managed:{identity}:{event_id}'

    def begin(self, identity, source_key):
        with self.store.db() as db:
            row = db.execute(
                "INSERT INTO calendar_creation_intents(identity,source_key,state) VALUES(%s,%s,'unknown') "
                'ON CONFLICT DO NOTHING RETURNING source_key', (identity, source_key),
            ).fetchone()
            if row is None:
                raise ValueError('已有行程建立意圖，結果未重新確認，不重送。')

    def register(self, identity, event_id, source_key, line, detail, aliases=()):
        payload = self.store.encode(
            self.scope(identity, event_id), {
                'line': line, 'detail': normalized_detail(detail), 'aliases': list(aliases),
            },
        )
        with self.store.db() as db:
            intent = db.execute(
                "UPDATE calendar_creation_intents SET state='created' WHERE identity=%s AND source_key=%s AND state='unknown' RETURNING source_key", (
                    identity, source_key,
                ),
            ).fetchone()
            if intent is None:
                raise ValueError('缺少送出前的建立來源紀錄，未宣稱為系統建立。')
            row = db.execute(
                'INSERT INTO calendar_managed_events(identity,event_id,source_key,state,payload) '
                "VALUES(%s,%s,%s,'ready',%s) ON CONFLICT DO NOTHING RETURNING event_id",
                (identity, event_id, source_key, payload),
            ).fetchone()
            if row is None:
                raise ValueError('行程已有建立來源紀錄，未覆寫。')

    def advance(self, identity, before, after):
        event_id = str(before[0][0])
        with self.store.db() as db:
            row = db.execute(
                'SELECT * FROM calendar_managed_events WHERE identity=%s AND event_id=%s FOR UPDATE',
                (identity, event_id),
            ).fetchone()
            if row is None or row['state'] != 'ready':
                return
            value = self.store.decode(self.scope(identity, event_id), row['payload'])
            if digest(value['detail']) != digest(before):
                db.execute(
                    "UPDATE calendar_managed_events SET state='review' WHERE identity=%s AND event_id=%s", (
                        identity, event_id,
                    ),
                )
                return
            value['detail'] = normalized_detail(after)
            db.execute(
                'UPDATE calendar_managed_events SET payload=%s,updated_at=CURRENT_TIMESTAMP WHERE identity=%s AND event_id=%s',
                (self.store.encode(self.scope(identity, event_id), value), identity, event_id),
            )

    def due(self, identity, keys=None, limit=20):
        with self.store.db() as db:
            rows = db.execute(
                "SELECT m.* FROM calendar_managed_events m WHERE identity=%s AND state='ready' "
                'AND ((%s AND next_check<=%s) OR (NOT %s AND (source_key=ANY(%s) OR EXISTS('
                'SELECT 1 FROM event_receipts r WHERE r.event_id=m.event_id AND r.key=ANY(%s))))) '
                'ORDER BY next_check,event_id LIMIT %s',
                (identity, keys is None, time.time(), keys is None, keys or [], keys or [], limit),
            ).fetchall()
        for row in rows:
            row['value'] = self.store.decode(self.scope(identity, row['event_id']), row['payload'])
        return rows

    def managed(self, event_id):
        with self.store.db() as db:
            return db.execute('SELECT 1 FROM calendar_managed_events WHERE event_id=%s LIMIT 1', (event_id,)).fetchone() is not None

    def unresolved_creation(self, identity):
        with self.store.db() as db:
            return db.execute("SELECT 1 FROM calendar_creation_intents WHERE identity=%s AND state='unknown' LIMIT 1", (identity,)).fetchone() is not None

    def attention(self, identity):
        with self.store.db() as db:
            return db.execute("SELECT count(*) AS count FROM calendar_managed_events WHERE identity=%s AND state IN ('review','deleting')", (identity,)).fetchone()['count']

    def state(self, identity, event_id, state):
        if state not in ('ready', 'review', 'deleting', 'deleted'):
            raise ValueError('Invalid calendar cleanup state')
        with self.store.db() as db:
            db.execute(
                'UPDATE calendar_managed_events SET state=%s,next_check=%s,updated_at=CURRENT_TIMESTAMP '
                'WHERE identity=%s AND event_id=%s', (state, time.time()+3600, identity, event_id),
            )

    def backup(self, identity, event_id, value):
        scope = self.scope(identity, event_id)
        encrypted = self.store.encode(scope, value)
        # Verify encryption/serialization before storing the only deletion backup.
        if self.store.decode(scope, encrypted) != value:
            raise ValueError('行程備份驗證失敗，未清理。')
        with self.store.db() as db:
            row = db.execute(
                "UPDATE calendar_managed_events SET backup=%s WHERE identity=%s AND event_id=%s AND state='ready' RETURNING backup",
                (encrypted, identity, event_id),
            ).fetchone()
            if row is None or self.store.decode(scope, row['backup']) != value:
                raise ValueError('行程備份無法持久保存，未清理。')


def managed_calendar():
    current_resources = _workflow_runtime.attribute('current_resources')
    return ManagedCalendar(current_resources().store)
