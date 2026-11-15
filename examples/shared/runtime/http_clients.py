"""Named HTTP profiles owned by one application lifespan."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

import httpx


class HttpClientPool:
    def __init__(self, profiles=None):
        self._strict = profiles is not None
        self._profiles = deepcopy(profiles or {})
        self._clients = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def get(self, name, **options):
        async with self._lock:
            if self._closed:
                raise RuntimeError('HTTP client pool is closed.')
            configured = self._profiles.get(name)
            if configured is not None and options and configured != options:
                raise ValueError('Conflicting options for named HTTP client: ' + name)
            if configured is None:
                if self._strict:
                    raise KeyError('Unknown HTTP client profile: ' + name)
                configured = self._profiles[name] = deepcopy(options)
            if name not in self._clients:
                self._clients[name] = httpx.AsyncClient(**configured)
            return self._clients[name]

    async def close(self):
        async with self._lock:
            self._closed = True
            clients, self._clients = list(self._clients.values()), {}
        results = await asyncio.gather(*(client.aclose() for client in clients), return_exceptions=True)
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise BaseExceptionGroup('HTTP client cleanup failed', failures)


_current_pool = ContextVar('application_http_clients', default=None)


@contextmanager
def application_http_clients(pool):
    """Bind only the current request/task; another application's pool is isolated."""
    token = _current_pool.set(pool)
    try:
        yield
    finally:
        _current_pool.reset(token)


async def get_application_http_client(name):
    pool = _current_pool.get()
    return await pool.get(name) if pool is not None else None
