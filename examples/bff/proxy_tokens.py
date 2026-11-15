"""BFF tokens operations."""

from __future__ import annotations

import asyncio

import time

from collections.abc import Mapping

from typing import Any

from fastapi import HTTPException

from redis.asyncio import Redis

from redis.exceptions import RedisError

from examples.identity.deployment_context import DeploymentBinding

from examples.identity.sessions.store import acquire_refresh_lock

from examples.identity.sessions.store import auth_tokens

from examples.identity.sessions.store import delete_auth_session

from examples.identity.sessions.store import get_auth_session

from examples.identity.sessions.store import OIDC_ONLINE_SSO_SESSION_MODE

from examples.identity.sessions.store import release_refresh_lock

from examples.identity.sessions.store import save_auth_tokens

from examples.bff.oidc_services import refresh_oidc_tokens

from examples.identity.schemas.auth import RefreshRequest

from examples.identity.sessions.authentication import refresh_tokens

from examples.identity.settings.bff import get_bff_options
from examples.bff.proxy_config import logger


def _is_terminal_refresh_error(exc: HTTPException) -> bool:
    """Return whether a refresh error permanently invalidates a BFF session.

    Args:
        exc: Refresh-token HTTP exception.

    Returns:
        ``True`` for a permanent authentication failure.
    """
    if exc.status_code == 409:
        return True
    if exc.status_code != 401:
        return False
    detail = str(exc.detail).lower()
    if detail.startswith('oidc_'):
        return True
    return any(marker in detail for marker in ('expired', 'revoked', 'reused', 'invalid', 'recognised'))


async def get_proxy_access_token(
    redis: Redis,
    session_id: str,
    *,
    force_refresh: bool = False,
    deployment: DeploymentBinding | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return a valid BFF access token, refreshing it when necessary.

    Args:
        redis: Redis connection holding BFF session state.
        session_id: Opaque browser session identifier.
        force_refresh: Whether to rotate despite a still-valid access token.

    Returns:
        Access token and current server-side session record.

    Raises:
        HTTPException: If the session is absent, refresh is rejected, or a peer
            refresh does not complete in time.
    """
    session = await get_auth_session(redis, session_id)
    if session is None:
        raise HTTPException(status_code=401, detail='app_session_expired')
    if session.get('auth_provider') == 'oidc' and session.get('oidc_session_mode') != OIDC_ONLINE_SSO_SESSION_MODE:
        # Sessions minted before the BFF moved away from offline tokens cannot
        # participate in browser SSO. Clear only that opaque BFF session so
        # the next application navigation performs one normal Keycloak login.
        await delete_auth_session(redis, session_id)
        raise HTTPException(status_code=401, detail='app_session_expired')

    access_token, refresh_token = auth_tokens(session)
    remaining = int(session.get('access_expires_at') or 0) - int(time.time())
    if not force_refresh and remaining > get_bff_options().refresh_before_seconds:
        return access_token, session

    lock_owner = await acquire_refresh_lock(redis, session_id)
    if lock_owner is None:
        return await _wait_for_proxy_session_refresh(
            redis,
            session_id,
            access_token,
            session,
            remaining,
        )

    try:
        return await _refresh_proxy_session(
            redis,
            session_id,
            access_token,
            refresh_token,
            force_refresh=force_refresh,
            deployment=deployment,
        )
    finally:
        await release_refresh_lock(redis, session_id, lock_owner)


async def _wait_for_proxy_session_refresh(
    redis: Redis,
    session_id: str,
    access_token: str,
    session: dict[str, Any],
    remaining: int,
) -> tuple[str, dict[str, Any]]:
    """Reuse a peer's token refresh before treating the session as busy.

    Args:
        redis: Redis connection holding BFF session state.
        session_id: Opaque browser session identifier.
        access_token: Token observed before waiting for the lock owner.
        session: Last known server-side session record.
        remaining: Seconds remaining on the last known access token.

    Returns:
        Refreshed token and session, or the still-valid original pair.

    Raises:
        HTTPException: If the session vanishes or no valid token becomes
            available before the wait period ends.
    """
    for _ in range(40):
        await asyncio.sleep(0.05)
        latest = await get_auth_session(redis, session_id)
        if latest is None:
            raise HTTPException(status_code=401, detail='app_session_expired')
        latest_access, _ = auth_tokens(latest)
        if latest_access != access_token:
            return latest_access, latest
    if remaining > 0:
        return access_token, session
    raise HTTPException(status_code=503, detail='session_refresh_busy')


async def _refresh_proxy_session(
    redis: Redis,
    session_id: str,
    access_token: str,
    refresh_token: str,
    *,
    force_refresh: bool,
    deployment: DeploymentBinding | None,
) -> tuple[str, dict[str, Any]]:
    """Refresh a lock-owning BFF session and persist its rotated tokens.

    Args:
        redis: Redis connection holding BFF session and refresh state.
        session_id: Opaque browser session identifier.
        access_token: Token observed before acquiring the refresh lock.
        refresh_token: Current refresh token for the session.
        force_refresh: Whether to rotate even if another worker changed tokens.

    Returns:
        Rotated access token and updated server-side session record.

    Raises:
        HTTPException: If session or refresh-token validation fails.
    """
    latest = await get_auth_session(redis, session_id)
    if latest is None:
        raise HTTPException(status_code=401, detail='app_session_expired')
    latest_access, latest_refresh = auth_tokens(latest)
    if latest_access != access_token and not force_refresh:
        return latest_access, latest
    result: Mapping[str, object]
    try:
        if latest.get('auth_provider', 'legacy') == 'oidc':
            result = await refresh_oidc_tokens(
                latest_refresh or refresh_token,
            )
        else:
            result = await refresh_tokens(
                RefreshRequest(refresh_token=latest_refresh or refresh_token),
                redis,
                hash_refresh_token=True,
                deployment=deployment,
            )
    except HTTPException as exc:
        if _is_terminal_refresh_error(exc):
            await delete_auth_session(redis, session_id)
            raise HTTPException(
                status_code=401,
                detail='app_session_expired',
            ) from exc
        raise
    new_access = str(result['access_token'])
    new_refresh = str(result['refresh_token'])
    returned_features = result.get('feature_names')
    feature_names = (
        [str(feature) for feature in returned_features]
        if isinstance(returned_features, list) and latest.get('auth_provider', 'legacy') == 'legacy'
        else None
    )
    await save_auth_tokens(
        redis,
        session_id,
        latest,
        new_access,
        new_refresh,
        feature_names,
    )
    return new_access, latest


async def _get_proxy_access_token_or_503(
    redis: Redis,
    session_id: str,
    request_path: str,
    *,
    force_refresh: bool = False,
    deployment: DeploymentBinding | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return a BFF access token or translate Redis failure to HTTP 503.

    Args:
        redis: Redis connection holding BFF session state.
        session_id: Opaque browser session identifier.
        request_path: Original BFF request path for observability.
        force_refresh: Whether to rotate despite a valid access token.

    Returns:
        Valid access token and current server-side session record.

    Raises:
        HTTPException: If Redis is unavailable or token refresh fails.
    """
    try:
        return await get_proxy_access_token(
            redis,
            session_id,
            force_refresh=force_refresh,
            deployment=deployment,
        )
    except RedisError as exc:
        logger.warning(
            'BFF Redis connection failed path=%s error_type=%s',
            request_path,
            type(exc).__name__,
            exc_info=True,
        )
        raise HTTPException(
            status_code=503,
            detail='bff_redis_unavailable',
        ) from exc
