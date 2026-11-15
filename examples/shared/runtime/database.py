"""Reference-counted transaction pools owned by application scopes.

An unscoped one-off transaction borrows a temporary pool. A scope retains only
DSNs it actually uses, and closing one app never closes another app's pool or
an active transaction. Session advisory leases still use dedicated connections.
"""

from __future__ import annotations

import atexit
import asyncio
from contextlib import contextmanager, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock

from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool


@dataclass
class _Entry:
    pool: ConnectionPool
    owners: int = 0
    borrowers: int = 0


class PoolRegistry:
    def __init__(self):
        self._entries = {}
        self._lock = Lock()

    def _entry(self, dsn):
        if dsn not in self._entries:
            self._entries[dsn] = _Entry(
                ConnectionPool(
                    make_conninfo(dsn, connect_timeout=5),
                    min_size=0,
                    max_size=8,
                    timeout=5,
                    max_waiting=64,
                    open=True,
                    kwargs={
                        'options': '-c lock_timeout=5000 -c statement_timeout=30000'
                    },
                )
            )
        return self._entries[dsn]

    def retain(self, dsn):
        with self._lock:
            entry = self._entry(dsn)
            entry.owners += 1
            return entry

    def release(self, dsn, entry, *, borrowed=False):
        closing = None
        with self._lock:
            if self._entries.get(dsn) is not entry:
                return
            if borrowed:
                entry.borrowers -= 1
            else:
                entry.owners -= 1
            if entry.owners == entry.borrowers == 0:
                closing = self._entries.pop(dsn).pool
        if closing is not None:
            closing.close()

    @contextmanager
    def borrow(self, dsn):
        with self._lock:
            entry = self._entry(dsn)
            entry.borrowers += 1
        try:
            yield entry.pool
        finally:
            self.release(dsn, entry, borrowed=True)

    def close(self):
        """Process-exit fallback; ordinary shutdown releases application owners."""
        with self._lock:
            entries, self._entries = list(self._entries.values()), {}
        failures = []
        for entry in entries:
            try:
                entry.pool.close()
            except Exception as error:
                failures.append(error)
        if failures:
            raise ExceptionGroup('Database pool cleanup failed', failures)


_registry = PoolRegistry()
_current_scope = ContextVar('database_pool_scope', default=None)


class PoolScope:
    def __init__(self, registry=None):
        self.registry = registry if registry is not None else _registry
        self._leases = {}
        self._lock = Lock()
        self._closed = False

    def retain(self, dsn):
        with self._lock:
            if self._closed:
                raise RuntimeError('Database pool scope is closed.')
            if dsn not in self._leases:
                self._leases[dsn] = self.registry.retain(dsn)

    @contextmanager
    def bind(self):
        token = _current_scope.set(self)
        try:
            yield self
        finally:
            _current_scope.reset(token)

    def close(self):
        with self._lock:
            self._closed = True
            leases, self._leases = self._leases, {}
        failures = []
        for dsn, entry in leases.items():
            try:
                self.registry.release(dsn, entry)
            except Exception as error:
                failures.append(error)
        if failures:
            raise ExceptionGroup('Database scope cleanup failed', failures)


@asynccontextmanager
async def application_database_pools():
    scope = PoolScope()
    try:
        with scope.bind():
            yield scope
    finally:
        await asyncio.to_thread(scope.close)


class DatabasePoolMiddleware:
    def __init__(self, app, owner):
        self.app, self.owner = app, owner

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            await self.app(scope, receive, send)
            return
        with self.owner.state.database_pools.bind():
            await self.app(scope, receive, send)


def cache_owner_lock_key(schema, owner):
    return f'{schema}:cache-owner:{owner}'


def shared_content_lock_namespace(schema):
    return f'{schema}:shared-content:'


@contextmanager
def connection(dsn, schema, *, row_factory=None):
    scope = _current_scope.get()
    registry = scope.registry if scope is not None else _registry
    if scope is not None:
        scope.retain(dsn)
    with registry.borrow(dsn) as pool, pool.connection() as db:
        previous = db.row_factory
        try:
            db.row_factory = row_factory or previous
            db.execute(
                sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(schema))
            )
            yield db
        finally:
            db.row_factory = previous


def close_pools():
    _registry.close()


atexit.register(close_pools)
