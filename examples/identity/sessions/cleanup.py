"""Compatibility read: per-token TTLs and the session index own token expiry."""
from __future__ import annotations
from examples.identity.cache import rate_limiter_service


async def prune_user_cache(redis_pool,username):
    return await rate_limiter_service.get_user_data(redis_pool,username)
