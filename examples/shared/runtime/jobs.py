"""Application-bound job service shared by requests and inherited background tasks."""
from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from typing import Protocol


class JobService(Protocol):
    queue: Any
    identity: Callable[[str], Awaitable[dict]]

    async def _io(self, invoke, *args, **kwargs): ...

    def render(self, result) -> str: ...


_current = ContextVar('application_job_service', default=None)


@contextmanager
def application_jobs(current):
    token = _current.set(current)
    try:
        yield current
    finally:
        _current.reset(token)


def service() -> JobService:
    current = _current.get()
    if current is None:
        raise RuntimeError('No job service is bound to this application task.')
    return current


class JobServiceMiddleware:
    def __init__(self, app, owner):
        self.app, self.owner = app, owner

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            await self.app(scope, receive, send)
            return
        with application_jobs(self.owner.state.job_service):
            await self.app(scope, receive, send)
