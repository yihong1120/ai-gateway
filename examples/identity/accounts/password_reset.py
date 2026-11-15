"""Password-action HTTP entries; token consumption is part of the account transaction."""

from __future__ import annotations
import hashlib
from redis.asyncio import Redis
from fastapi import HTTPException
from examples.identity.runtime import get_identity, get_settings
from examples.identity.redis_counters import increment_window
from examples.identity.accounts.recovery import token_hash


def _hash_identifier(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _email_rate_key(email):
    return f'password_reset_rate:email:{_hash_identifier(email)}'


def _ip_rate_key(client_ip):
    return f'password_reset_rate:ip:{_hash_identifier(client_ip)}'


async def _increment_rate_limit(redis_pool, key, window_seconds):
    count, _ = await increment_window(redis_pool, key, window_seconds)
    return count


async def _enforce_forgot_password_rate_limits(
    email: str,
    client_ip: str | None,
    redis_pool: Redis,
) -> None:
    """Limit reset requests by email address and client IP.

    Args:
        email: Email address requesting a reset.
        client_ip: Optional requesting client address.
        redis_pool: Redis connection holding rate-limit state.

    Raises:
        HTTPException: If either rate limit is exceeded.
    """
    settings = get_settings()
    email_count = await _increment_rate_limit(
        redis_pool,
        _email_rate_key(email),
        settings.password_reset_email_rate_limit_seconds,
    )
    if email_count > 1:
        raise HTTPException(status_code=429, detail='Too many requests.')

    if not client_ip:
        return

    ip_count = await _increment_rate_limit(
        redis_pool,
        _ip_rate_key(client_ip),
        settings.password_reset_ip_rate_limit_window_seconds,
    )
    if ip_count > settings.password_reset_ip_rate_limit_max:
        raise HTTPException(status_code=429, detail='Too many requests.')


async def request_password_reset(email, db, redis_pool, client_ip=None):
    await _enforce_forgot_password_rate_limits(
        email.strip().lower(), client_ip, redis_pool
    )
    return await get_identity().recovery.request_reset(email, db)


async def reset_password(raw_token, new_password, db, redis_pool):
    recovery = get_identity().recovery
    await recovery.reset(raw_token, new_password, db)
    try:
        await recovery.drain_revocations(
            db, redis_pool, limit=1, action=token_hash(raw_token.strip())
        )
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            'Password updated; session revocation deferred'
        )
    return {'message': 'Password reset successfully.'}
