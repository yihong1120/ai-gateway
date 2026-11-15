"""Encrypted pure content results coordinated by PostgreSQL session locks."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import time

from anyio import CancelScope
import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from examples.official_document_workflow.domain.documents import CloudRateLimited, ProcessingError
from examples.shared.runtime.concurrency import ModelNotStarted
from examples.shared.runtime.database import shared_content_lock_namespace
from examples.shared.runtime.io import run_sync

logger = logging.getLogger(__name__)
_MISS = object()
_FAILED = object()
_UNKNOWN = object()
_UNAVAILABLE = '共用文件快取暫時無法協調；本次未啟動重複判讀，請稍後重試。'
_COOLDOWN = '相同文件的判讀稍早未完成；本次未自動重送，請稍後重試。'
_UNCERTAIN = '相同文件的付費判讀已啟動但結果尚待核對；未自動重送。'


class SharedContentCache:
    def __init__(self, store, scope, revision, ttl_seconds=604800):
        if not scope or not revision or isinstance(ttl_seconds, bool) or not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError('Shared content cache requires scope, revision and a positive finite TTL.')
        self._store, self.scope, self.revision, self.ttl_seconds = store, scope, revision, ttl_seconds

    @property
    def store(self):
        store = self._store
        if callable(store):
            store = store()
            self._store = store
        return store

    def key(self, stage, *semantic_parts):
        content = json.dumps(
            [self.scope, self.revision, stage, *semantic_parts],
            sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False,
        )
        return hashlib.sha256(content.encode()).hexdigest()

    def _lock_name(self, key):
        return shared_content_lock_namespace(self.store.schema) + self.scope + ':' + key

    def _encode(self, key, value, *, failed=False, started=False):
        if started:
            kind, value = 'unknown', 'producer_started'
        elif failed:
            kind, value = 'failure', 'producer_failed'
        elif isinstance(value, bytes):
            kind, value = 'bytes', base64.b64encode(value).decode('ascii')
        elif isinstance(value, (dict, list)):
            kind = 'json'
        else:
            raise ValueError('Shared content results must be JSON objects, arrays or bytes.')
        envelope = {'purpose': 'shared-content-v1', 'scope': self.scope, 'key': key,
                    'revision': self.revision, 'kind': kind, 'value': value}
        return self.store.cipher.encrypt(json.dumps(envelope, ensure_ascii=False, allow_nan=False).encode())

    def _decode(self, key, row, validate):
        if row is None:
            return _MISS
        # Even damaged evidence cannot prove a previously started paid call did
        # not execute. Never turn an unresolved marker into an automatic miss.
        if row['state'] == 'unknown':
            return _UNKNOWN
        if row['revision'] != self.revision:
            return _MISS
        try:
            envelope = json.loads(self.store.cipher.decrypt(bytes(row['payload'])))
            if any(envelope.get(name) != expected for name, expected in (
                ('purpose', 'shared-content-v1'), ('scope', self.scope), ('key', key), ('revision', self.revision),
            )):
                return _MISS
            if row['state'] == 'failed':
                if envelope.get('kind') == 'failure' and envelope.get('value') == 'producer_failed':
                    return _FAILED
                return _MISS
            if envelope['kind'] == 'bytes':
                value = base64.b64decode(envelope['value'], validate=True)
            elif envelope['kind'] == 'json' and isinstance(envelope['value'], (dict, list)):
                value = envelope['value']
            else:
                return _MISS
            if validate is not None:
                validate(value)
            return value
        except Exception:
            return _MISS

    @staticmethod
    def _validate(value, validator):
        if not isinstance(value, (dict, list, bytes)):
            raise ValueError('Shared content results must be JSON objects, arrays or bytes.')
        if validator is not None:
            validator(value)

    @staticmethod
    def _query():
        return "SELECT state,revision,payload FROM shared_content_cache WHERE scope=%s AND key=%s AND (state='unknown' OR expires_at>CURRENT_TIMESTAMP)"

    @staticmethod
    def _upsert():
        return """INSERT INTO shared_content_cache(scope,key,revision,state,payload,created_at,expires_at)
            VALUES(%s,%s,%s,%s,%s,CURRENT_TIMESTAMP,
                CASE WHEN %s='unknown' THEN 'infinity'::timestamptz ELSE CURRENT_TIMESTAMP + %s * INTERVAL '1 second' END)
            ON CONFLICT(scope,key) DO UPDATE SET revision=excluded.revision,state=excluded.state,
                payload=excluded.payload,created_at=excluded.created_at,expires_at=excluded.expires_at"""

    def _values(self, key, payload, state):
        return self.scope, key, self.revision, state, payload, state, 30 if state == 'failed' else self.ttl_seconds

    def _connect(self):
        db = None
        try:
            db = psycopg.connect(self.store.dsn, autocommit=True, connect_timeout=5,
                                 options='-c statement_timeout=5000', row_factory=dict_row)
            db.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.store.schema)))
            return db
        except Exception:
            if db is not None:
                db.close()
            raise ProcessingError(_UNAVAILABLE) from None

    def _read(self, db, key, validate):
        try:
            row = db.execute(self._query(), (self.scope, key)).fetchone()
        except Exception:
            raise ProcessingError(_UNAVAILABLE) from None
        value = self._decode(key, row, validate)
        if value is _FAILED:
            raise ProcessingError(_COOLDOWN)
        return value

    def _publish(self, db, key, payload, *, state='ready'):
        db.execute(self._upsert(), self._values(key, payload, state))

    @staticmethod
    def _warning(error):
        logger.warning('Shared content cache publish unavailable (%s)', type(error).__name__)

    def get_or_compute(self, key, factory, validate=None, *, replay_safe=True):
        with self._connect() as db:
            value = self._read(db, key, validate)
            if value is not _MISS and value is not _UNKNOWN:
                return value
            try:
                while not db.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0))', (self._lock_name(key),)).fetchone()['pg_try_advisory_lock']:
                    time.sleep(0.1)
            except Exception:
                raise ProcessingError(_UNAVAILABLE) from None
            value = self._read(db, key, validate)
            if value is _UNKNOWN:
                raise ProcessingError(_UNCERTAIN)
            if value is not _MISS:
                return value
            if not replay_safe:
                try:
                    self._publish(db, key, self._encode(key, None, started=True), state='unknown')
                except Exception:
                    raise ProcessingError(_UNAVAILABLE) from None
            try:
                value = factory()
                self._validate(value, validate)
            except BaseException as error:
                if replay_safe or isinstance(error, (CloudRateLimited, ModelNotStarted)):
                    try:
                        self._publish(db, key, self._encode(key, None, failed=True), state='failed')
                    except Exception as error:
                        self._warning(error)
                raise
            try:
                payload = self._encode(key, value)
                self._publish(db, key, payload)
            except Exception as error:
                self._warning(error)
            return value

    async def _aconnect(self):
        db = None
        try:
            store = await run_sync(lambda: self.store)
            db = await psycopg.AsyncConnection.connect(store.dsn, autocommit=True, connect_timeout=5,
                                                      options='-c statement_timeout=5000', row_factory=dict_row)
            await db.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(store.schema)))
            return db
        except BaseException as error:
            if db is not None:
                with CancelScope(shield=True):
                    await db.close()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise ProcessingError(_UNAVAILABLE) from None

    async def _aread(self, db, key, validate):
        try:
            cursor = await db.execute(self._query(), (self.scope, key))
            row = await cursor.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ProcessingError(_UNAVAILABLE) from None
        value = await run_sync(self._decode, key, row, validate)
        if value is _FAILED:
            raise ProcessingError(_COOLDOWN)
        return value

    async def _apublish(self, db, key, payload, *, state='ready'):
        await db.execute(self._upsert(), self._values(key, payload, state))

    async def _produce(self, db, key, factory, validate, replay_safe):
        if not replay_safe:
            try:
                await self._apublish(db, key, self._encode(key, None, started=True), state='unknown')
            except Exception:
                raise ProcessingError(_UNAVAILABLE) from None
        try:
            value = await factory()
            await run_sync(self._validate, value, validate)
        except BaseException as error:
            if replay_safe or isinstance(error, (CloudRateLimited, ModelNotStarted)):
                try:
                    await self._apublish(db, key, self._encode(key, None, failed=True), state='failed')
                except Exception as error:
                    self._warning(error)
            raise
        try:
            payload = await run_sync(self._encode, key, value)
            await self._apublish(db, key, payload)
        except Exception as error:
            self._warning(error)
        return value

    async def aget_or_compute(self, key, async_factory, validate=None, *, replay_safe=True):
        db = await self._aconnect()
        try:
            value = await self._aread(db, key, validate)
            if value is not _MISS and value is not _UNKNOWN:
                return value
            try:
                while True:
                    cursor = await db.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0))', (self._lock_name(key),))
                    if (await cursor.fetchone())['pg_try_advisory_lock']:
                        break
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise ProcessingError(_UNAVAILABLE) from None
            value = await self._aread(db, key, validate)
            if value is _UNKNOWN:
                raise ProcessingError(_UNCERTAIN)
            if value is not _MISS:
                return value
            producer = asyncio.create_task(self._produce(db, key, async_factory, validate, replay_safe))
            try:
                return await asyncio.shield(producer)
            except asyncio.CancelledError:
                with CancelScope(shield=True):
                    while not producer.done():
                        try:
                            await asyncio.shield(producer)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    if not producer.cancelled():
                        producer.exception()
                raise
        finally:
            with CancelScope(shield=True):
                await db.close()
