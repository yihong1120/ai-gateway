"""Async registry transactions and bounded, lifespan-owned PostgreSQL resources."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from examples.company_ai_access.storage.registry import digest


class Repository:
    def __init__(self, registry):
        self.registry = registry
        self._transaction = ContextVar('access_transaction', default=None)
        self.pool = AsyncConnectionPool(
            make_conninfo(registry.dsn, connect_timeout=5),
            min_size=0,
            max_size=8,
            max_waiting=64,
            timeout=5,
            open=False,
            kwargs={
                'row_factory': dict_row,
                'options': '-c lock_timeout=5000 -c statement_timeout=30000',
            },
        )

    async def __aenter__(self):
        await self.pool.open()
        return self

    async def __aexit__(self, *exc):
        await self.pool.close()

    @asynccontextmanager
    async def db(self):
        active = self._transaction.get()
        if active and active[0] is asyncio.current_task():
            yield active[1]
            return
        async with self.pool.connection() as db:
            await db.execute(sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(self.registry.schema)))
            yield db

    @asynccontextmanager
    async def transaction(self):
        """Commit related state and audit writes together; never hold across HTTP."""
        async with self.db() as db:
            token = self._transaction.set((asyncio.current_task(), db))
            try:
                yield
            finally:
                self._transaction.reset(token)

    @asynccontextmanager
    async def lease(self):
        async with await psycopg.AsyncConnection.connect(
            make_conninfo(self.registry.dsn, connect_timeout=5),
            autocommit=True,
        ) as db:
            key = self.registry.schema + ':runtime'
            row = await (await db.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0))', (key,))).fetchone()
            if not row[0]:
                raise RuntimeError('AI access registry is already in use; stop the service before migration.')
            try:
                yield
            finally:
                if not db.closed:
                    await db.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))', (key,))

    async def get(self, kind, identifier):
        async with self.db() as db:
            row = await (
                await db.execute(
                    'SELECT data FROM records WHERE kind=%s AND id=%s',
                    (kind, identifier),
                )
            ).fetchone()
        return row['data'] if row else None

    async def put(self, kind, row):
        await self.put_many([(kind, row)])

    async def put_many(self, rows):
        async with self.db() as db:
            for kind, row in rows:
                await db.execute(
                    'INSERT INTO records VALUES (%s,%s,%s,%s) ON CONFLICT(kind,id) DO UPDATE SET subject=excluded.subject,data=excluded.data',
                    (kind, row['id'], row.get('subject', ''), Jsonb(row)),
                )

    async def prune(self, before):
        async with self.db() as db:
            await db.execute(
                "DELETE FROM records WHERE kind='enrollment' AND (data->>'expires_at')::bigint<%s",
                (before,),
            )
            await db.execute('DELETE FROM audit WHERE at<%s', (before - 90 * 86400,))

    async def list(self, kind, subject=None):
        async with self.db() as db:
            rows = await (
                await db.execute(
                    'SELECT data FROM records WHERE kind=%s' + (' AND subject=%s' if subject is not None else ''),
                    (kind, subject) if subject is not None else (kind,),
                )
            ).fetchall()
        return [row['data'] for row in rows]

    async def by_key(self, key):
        async with self.db() as db:
            row = await (
                await db.execute(
                    "SELECT data FROM records WHERE kind='device' AND data->>'key_hash'=%s",
                    (digest(key),),
                )
            ).fetchone()
        return row['data'] if row else None

    async def audit(self, at, event, subject='', device=''):
        async with self.db() as db:
            await db.execute(
                'INSERT INTO audit(at,event,subject,device) VALUES (%s,%s,%s,%s)',
                (at, event, subject, device),
            )

    async def limit(self, identifier, now, *, maximum, window):
        async with self.db() as db:
            row = await (
                await db.execute(
                    """INSERT INTO limits VALUES (%s,1,%s)
                ON CONFLICT(id) DO UPDATE SET
                    count=CASE WHEN limits.until<=%s THEN 1 ELSE limits.count+1 END,
                    until=CASE WHEN limits.until<=%s THEN excluded.until ELSE limits.until END
                WHERE limits.until<=%s OR limits.count<%s RETURNING id""",
                    (identifier, now + window, now, now, now, maximum),
                )
            ).fetchone()
            await db.execute('DELETE FROM limits WHERE until<=%s', (now,))
        return row is not None

    def seal(self, secret):
        return self.registry.cipher.encrypt(secret.encode()).decode()

    def open(self, sealed):
        return self.registry.cipher.decrypt(sealed.encode()).decode()
