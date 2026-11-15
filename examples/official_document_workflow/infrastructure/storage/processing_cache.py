"""Owner-isolated encrypted checkpoints; never a calendar-write journal."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from contextlib import asynccontextmanager
from contextlib import AsyncExitStack
from contextvars import ContextVar
from copy import deepcopy

import psycopg

from examples.shared.runtime.database import cache_owner_lock_key
from examples.shared.runtime.modules import ModuleReference

_workflow_automation_store = ModuleReference(
    'examples.official_document_workflow.infrastructure.storage.automation_store',
)


logger = logging.getLogger(__name__)


def _unavailable(phase, error):
    logger.warning('Document cache %s unavailable (%s)', phase, type(error).__name__)


UPLOAD_CACHE_REVISION = 'ai-tentative-schedule-v1'
active_requests = ContextVar('official_document_requests', default=None)
upload_outcome = ContextVar('official_document_upload_outcome', default=None)


class ProcessingCache:
    def __init__(self, store, owner):
        self.store, self.owner = store, owner

    @staticmethod
    def key(*parts):
        return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def get(self, key):
        try:
            with self.store.db() as db:
                row = db.execute(
                    'SELECT payload FROM processing_chunks WHERE owner=%s AND key=%s',
                    (self.owner, key),
                ).fetchone()
            return self.store.decode(self.owner, row['payload']) if row else None
        except Exception as error:
            _unavailable('read', error)
            return None

    def put(self, key, value):
        return self.put_many({key: value})

    def put_many(self, values):
        if not values:
            return True
        try:
            now = time.time()
            # Validate/encrypt everything before starting the single transaction.
            rows = [
                (self.owner, key, self.store.encode(self.owner, value), now)
                for key, value in values.items()
            ]
            with self.store.db() as db, db.cursor() as cursor:
                cursor.executemany(
                    'INSERT INTO processing_chunks VALUES (%s,%s,%s,%s) ON CONFLICT(owner,key) '
                    'DO UPDATE SET payload=excluded.payload,updated=excluded.updated', rows,
                )
            return True
        except Exception as error:
            _unavailable('write', error)
            return False

    @asynccontextmanager
    async def lock(self, key):
        # A session lock serializes identical batches across workers. Closing
        # the connection releases it even when the request is cancelled.
        async with await psycopg.AsyncConnection.connect(self.store.dsn, autocommit=True, connect_timeout=5) as db:
            await db.execute(
                'SELECT pg_advisory_lock_shared(hashtextextended(%s,0))',
                (cache_owner_lock_key(self.store.schema, self.owner),),
            )
            await db.execute(
                'SELECT pg_advisory_lock(hashtextextended(%s,0))',
                (f'{self.store.schema}:{self.owner}:{key}',),
            )
            yield


class CachedRequests:
    """Stage successful model responses until source validation has completed."""

    def __init__(self, backend, settings, emitter):
        self.backend, self.settings, self.emitter = backend, settings, emitter
        self.pending = {}
        self.valid = True

    def key(self, *parts):
        return self.backend.key(UPLOAD_CACHE_REVISION, self.settings, *parts)

    async def get(self, key):
        if key in self.pending:
            return deepcopy(self.pending[key])
        try:
            return await asyncio.to_thread(self.backend.get, key)
        except Exception as error:
            _unavailable('read', error)
            return None

    def put(self, key, value):
        self.pending[key] = deepcopy(value)

    def commit(self):
        return self.backend.put_many(self.pending)


def upload_identity(prepared):
    identities = []
    for item in prepared:
        # Office conversion can change PDF IDs/timestamps between runs.
        # Identify the original upload; model keys also include extracted text.
        with (item.original_path or item.pdf_path).open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        identities.append((item.source_file.filename, digest))
    return identities


@asynccontextmanager
async def cached_upload(pipe, owner, prepared, emitter):
    if not pipe.valves.document_analysis_cache_enabled:
        yield
        return
    store = _workflow_automation_store.attribute('store')

    stack = AsyncExitStack()
    try:
        backend = ProcessingCache(await asyncio.to_thread(store), owner)
        identity = await asyncio.to_thread(upload_identity, prepared)
        requests = CachedRequests(
            backend, {'uploads': identity, 'valves': pipe.valves.model_dump(mode='json')}, emitter,
        )
        await stack.enter_async_context(backend.lock(requests.key('batch')))
    except Exception as error:
        try:
            await stack.aclose()
        except Exception as close_error:
            _unavailable('close', close_error)
        _unavailable('setup', error)
        yield
        return
    token = active_requests.set(requests)
    try:
        yield
        if requests.valid:
            task = asyncio.create_task(asyncio.to_thread(requests.commit))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # A thread cannot be cancelled midway through a transaction.
                await asyncio.gather(task, return_exceptions=True)
                raise
            except Exception as error:
                _unavailable('commit', error)
    finally:
        active_requests.reset(token)
        try:
            await stack.aclose()
        except Exception as error:
            _unavailable('close', error)
