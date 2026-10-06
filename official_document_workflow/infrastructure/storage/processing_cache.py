"""Owner-isolated encrypted checkpoints; never a calendar-write journal."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy

import psycopg


UPLOAD_CACHE_REVISION = 'single-pass-v1'
active_requests = ContextVar('official_document_requests', default=None)
upload_outcome = ContextVar('official_document_upload_outcome', default=None)


class ProcessingCache:
    def __init__(self, store, owner):
        self.store, self.owner = store, owner

    @staticmethod
    def key(*parts):
        return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def get(self, key):
        with self.store.db() as db:
            row = db.execute(
                'SELECT payload FROM processing_chunks WHERE owner=%s AND key=%s',
                (self.owner, key),
            ).fetchone()
        return self.store.decode(self.owner, row['payload']) if row else None

    def put(self, key, value):
        payload = self.store.encode(self.owner, value)
        with self.store.db() as db:
            db.execute(
                'INSERT INTO processing_chunks VALUES (%s,%s,%s,%s) ON CONFLICT(owner,key) DO UPDATE SET payload=excluded.payload,updated=excluded.updated',
                (self.owner, key, payload, time.time()),
            )

    @asynccontextmanager
    async def lock(self, key):
        # A session lock serializes identical batches across workers. Closing
        # the connection releases it even when the request is cancelled.
        async with await psycopg.AsyncConnection.connect(self.store.dsn, autocommit=True, connect_timeout=5) as db:
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
        return await asyncio.to_thread(self.backend.get, key)

    def put(self, key, value):
        self.pending[key] = deepcopy(value)

    def commit(self):
        for key, value in self.pending.items():
            self.backend.put(key, value)


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
    from .automation_store import store

    backend = ProcessingCache(await asyncio.to_thread(store), owner)
    identity = await asyncio.to_thread(upload_identity, prepared)
    requests = CachedRequests(backend, {'uploads': identity, 'valves': pipe.valves.model_dump(mode='json')}, emitter)
    async with backend.lock(requests.key('batch')):
        token = active_requests.set(requests)
        try:
            yield
            if requests.valid:
                await asyncio.to_thread(requests.commit)
        finally:
            active_requests.reset(token)
