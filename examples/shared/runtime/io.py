"""Offload blocking work and drain an in-flight transaction before cancellation."""
from __future__ import annotations

import asyncio


async def run_sync(invoke, *args, **kwargs):
    pending = asyncio.create_task(asyncio.to_thread(invoke, *args, **kwargs))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        await asyncio.gather(pending, return_exceptions=True)
        raise
