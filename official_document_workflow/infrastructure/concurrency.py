"""Bounded shared resources and FIFO admission across eligible owners.

These limits belong to the single application process. EIP session locks and
durable write receipts still protect account operations independently.
"""
from __future__ import annotations

import asyncio
import os
from collections import deque
from contextlib import asynccontextmanager
from copy import copy
from dataclasses import dataclass
from functools import wraps
from inspect import signature


def configured_limit(name, default, maximum=32):
    value = int(os.getenv(name, default))
    if not 1 <= value <= maximum:
        raise ValueError(f'{name} must be between 1 and {maximum}')
    return value


@dataclass
class Admission:
    owner: str
    ready: asyncio.Future
    granted: bool = False


class OwnerSlots:
    """One active call per owner; busy owners cannot block other owners."""

    def __init__(self, limit, *, max_waiting=500, max_per_owner=20):
        if not 1 <= limit <= 32:
            raise ValueError('Concurrency must be between 1 and 32')
        if max_waiting < 1 or max_per_owner < 1:
            raise ValueError('Waiting capacity must be positive')
        self.max_waiting, self.max_per_owner = max_waiting, max_per_owner
        self.limit = limit
        self.active = set()
        self.waiting = deque()

    def dispatch(self):
        for entry in tuple(self.waiting):
            if len(self.active) >= self.limit:
                break
            if entry.ready.cancelled():
                self.waiting.remove(entry)
            elif entry.owner not in self.active:
                self.waiting.remove(entry)
                self.active.add(entry.owner)
                entry.granted = True
                entry.ready.set_result(None)

    @asynccontextmanager
    async def slot(self, owner):
        if len(self.waiting) >= self.max_waiting:
            raise ValueError('處理佇列已滿，請稍後再試。')
        if sum(entry.owner == owner for entry in self.waiting) >= self.max_per_owner:
            raise ValueError('本人的等待工作已達上限，請等待完成。')
        entry = Admission(owner, asyncio.get_running_loop().create_future())
        self.waiting.append(entry)
        self.dispatch()
        try:
            await entry.ready
            yield
        finally:
            if entry.granted:
                self.active.remove(owner)
            else:
                if entry in self.waiting:
                    self.waiting.remove(entry)
            self.dispatch()


DOCUMENTS = OwnerSlots(configured_limit('DOCUMENT_MAX_CONCURRENT_JOBS', 4))
MODEL_LIMIT = configured_limit('MODEL_MAX_CONCURRENT_REQUESTS', 8, 64)
OCR_LIMIT = configured_limit('OCR_MAX_CONCURRENT_REQUESTS', 2, 8)
MODELS = asyncio.Semaphore(MODEL_LIMIT)
OCR = asyncio.Semaphore(OCR_LIMIT)


def document_request(invoke):
    """Admit complete upload pipelines, including image and Office conversion."""
    parameters = signature(invoke)

    @wraps(invoke)
    async def limited(*args, **kwargs):
        arguments = parameters.bind_partial(*args, **kwargs)
        bound = arguments.arguments
        user = bound.get('__user__') or {}
        owner = user.get('id')
        if bound.get('__task__') or not isinstance(owner, str) or not owner:
            return await invoke(*args, **kwargs)
        # Open WebUI caches a Pipe instance and reloads its valves per request.
        # Freeze this invocation's settings before another caller replaces them.
        pipe = copy(bound['self'])
        if hasattr(pipe, 'valves'):
            pipe.valves = pipe.valves.model_copy(deep=True)
        bound['self'] = pipe
        if bound.get('__metadata__') is not None:
            # Durable submission must not wait behind an OCR/document slot.
            return await invoke(*arguments.args, **arguments.kwargs)
        await pipe._emit_processing_status(bound.get('__event_emitter__'), '等待公文處理名額；不同帳號可並行處理。')
        async with DOCUMENTS.slot(owner):
            return await invoke(*arguments.args, **arguments.kwargs)
    return limited


def model_request(invoke):
    """Wait for capacity before the provider's network timeout begins."""
    @wraps(invoke)
    async def limited(*args, **kwargs):
        async with MODELS:
            return await invoke(*args, **kwargs)
    return limited


async def run_ocr(invoke, *args, **kwargs):
    # Wait asynchronously; waiting OCR must not consume all executor threads.
    async with OCR:
        pending = asyncio.create_task(asyncio.to_thread(invoke, *args, **kwargs))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            # Native/synchronous inference cannot be cancelled. Keep its slot
            # until the outstanding request ends, without launching a replay.
            await asyncio.gather(pending, return_exceptions=True)
            raise
