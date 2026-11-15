"""HTTP compatibility entries; PostgreSQL recovery service owns account actions."""

from __future__ import annotations
import hashlib
from redis.asyncio import Redis
from examples.identity.runtime import get_identity, get_settings
from examples.identity.redis_counters import increment_window
from fastapi import HTTPException


def _hash_identifier(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _resend_rate_key(email):
    return f'email_verification_rate:email:{_hash_identifier(email)}'


def _resend_daily_rate_key(email):
    return f'email_verification_daily_rate:email:{_hash_identifier(email)}'


async def _enforce_resend_rate_limit(
    email: str,
    redis_pool: Redis,
) -> None:
    """Enforce short-window and daily verification-resend limits.

    Args:
        email: Email address requesting a resend.
        redis_pool: Redis connection holding rate-limit counters.

    Raises:
        HTTPException: If either resend limit is exceeded.
    """
    settings = get_settings()
    key = _resend_rate_key(email)
    current, ttl = await increment_window(
        redis_pool, key, settings.email_verification_resend_rate_limit_seconds
    )
    if current > 1:
        retry_after = (
            ttl if ttl > 0 else settings.email_verification_resend_rate_limit_seconds
        )
        raise HTTPException(
            status_code=429,
            detail={
                'code': 'verification_resend_rate_limited',
                'retry_after_seconds': retry_after,
            },
            headers={'Retry-After': str(retry_after)},
        )

    daily_key = _resend_daily_rate_key(email)
    daily_count, ttl = await increment_window(
        redis_pool, daily_key, settings.email_verification_daily_limit_window_seconds
    )
    if daily_count > settings.email_verification_daily_limit:
        retry_after = (
            ttl if ttl > 0 else settings.email_verification_daily_limit_window_seconds
        )
        raise HTTPException(
            status_code=429,
            detail={
                'code': 'verification_daily_limit_exceeded',
                'retry_after_seconds': retry_after,
            },
            headers={'Retry-After': str(retry_after)},
        )


async def send_signup_verification_email(user, db):
    return await get_identity().recovery.send_verification(user, db)


async def resend_verification_email(email, db, redis_pool):
    await _enforce_resend_rate_limit(email.strip().lower(), redis_pool)
    return await get_identity().recovery.resend_verification(email, db)


async def verify_email_token(raw_token, db):
    return await get_identity().recovery.verify(raw_token, db)
