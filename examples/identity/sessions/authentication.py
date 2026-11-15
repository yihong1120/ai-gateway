from __future__ import annotations

from examples.identity.runtime import get_identity, get_settings


import httpx
from fastapi import HTTPException
from fastapi import Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from examples.shared.runtime.http_clients import get_application_http_client

from examples.identity.deployment_context import DeploymentBinding
from examples.identity.deployment_context import resolve_request_deployment
from examples.identity.providers.mode import require_local_login
from examples.identity.models import User
from examples.identity.models import USER_STATUS_ACTIVE
from examples.identity.models import USER_STATUS_EMAIL_UNVERIFIED
from examples.identity.models import USER_STATUS_PENDING_ADMIN_APPROVAL
from examples.identity.models import USER_STATUS_REJECTED
from examples.identity.models import USER_STATUS_SUSPENDED
from examples.identity.models import UserProfile
from examples.identity.schemas.auth import TokenPairData
from examples.identity.schemas.auth import UserLogin
from examples.identity.sessions.login_guard import check_login_guard
from examples.identity.sessions.login_guard import clear_login_guard
from examples.identity.sessions.login_guard import clear_login_guard_for_identifier
from examples.identity.sessions.login_guard import record_failed_login

# Configuration settings for JWT authentication
HCAPTCHA_VERIFY_URL = 'https://api.hcaptcha.com/siteverify'
ALGORITHM = 'HS256'
SUPER_ADMIN = 'ChangDar'
from examples.identity.sessions.tokens import ACCESS_TTL, REFRESH_TTL  # noqa: F401 - OAuth response contract


async def clear_login_guard_for_identifiers(
    redis_pool: Redis,
    identifiers: list[str],
) -> None:
    """Clear login guards for a user's known aliases."""
    seen: set[str] = set()
    for identifier in identifiers:
        normalized = identifier.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        await clear_login_guard_for_identifier(redis_pool, normalized)




async def _authenticate(
    db: AsyncSession,
    identifier: str,
    password: str,
) -> User:
    """Authenticate user credentials and verify active status.

    Args:
        db (AsyncSession): Database session for queries.
        identifier (str): User's username or profile e-mail address.
        password (str): User's password.

    Returns:
        User: Authenticated user object.

    Raises:
        HTTPException: When credentials are incorrect or user is inactive.
    """
    login_identifier = identifier.strip()
    user = await db.scalar(
        select(User).where(User.username == login_identifier),
    )

    if user is None:
        user = await db.scalar(
            select(User)
            .join(UserProfile, UserProfile.user_id == User.id)
            .where(UserProfile.email == login_identifier.lower()),
        )

    if not user or not await user.check_password(password):
        raise HTTPException(
            status_code=401,
            detail='Wrong username/e-mail or password',
        )

    if user.status == USER_STATUS_EMAIL_UNVERIFIED:
        raise HTTPException(
            status_code=403,
            detail={
                'code': 'email_unverified',
                'status': user.status,
                'message': 'Please verify your email before logging in.',
            },
        )

    if user.status == USER_STATUS_PENDING_ADMIN_APPROVAL:
        raise HTTPException(
            status_code=403,
            detail={
                'code': 'pending_admin_approval',
                'status': user.status,
                'message': 'Account is waiting for administrator approval.',
            },
        )

    if user.status == USER_STATUS_REJECTED:
        raise HTTPException(
            status_code=403,
            detail={
                'code': 'account_rejected',
                'status': user.status,
                'message': 'Account application was rejected.',
            },
        )

    if user.status == USER_STATUS_SUSPENDED:
        raise HTTPException(
            status_code=403,
            detail={
                'code': 'account_suspended',
                'status': user.status,
                'message': 'Account is suspended.',
            },
        )

    if user.status != USER_STATUS_ACTIVE:
        raise HTTPException(
            status_code=403,
            detail={'code': 'user_not_active', 'status': user.status},
        )

    return user


async def _verify_hcaptcha(
    hcaptcha_token: str | None,
    hcaptcha_bypass_key: str | None = None,
) -> None:
    """Verify an hCaptcha token before credential authentication.

    Args:
        hcaptcha_token: Client hCaptcha response token.
        hcaptcha_bypass_key: Trusted server-side bypass credential.

    Raises:
        HTTPException: If hCaptcha validation is required and fails.
    """
    settings = get_settings()
    if not settings.hcaptcha_enabled:
        return

    if (
        get_settings().hcaptcha_bypass_key
        and hcaptcha_bypass_key
        and hcaptcha_bypass_key == get_settings().hcaptcha_bypass_key
    ):
        return

    token = (hcaptcha_token or '').strip()
    if not token:
        raise HTTPException(status_code=400, detail='hCaptcha token required')

    if not get_settings().hcaptcha_secret_key or not get_settings().hcaptcha_site_key:
        raise HTTPException(status_code=500, detail='hCaptcha not configured')

    try:
        client = await get_application_http_client('hcaptcha')
        if client is not None:
            response = await client.post(
                HCAPTCHA_VERIFY_URL,
                data={
                    'secret': get_settings().hcaptcha_secret_key,
                    'response': token,
                    'sitekey': get_settings().hcaptcha_site_key,
                },
            )
            response.raise_for_status()
            result = response.json()
        else:
            async with httpx.AsyncClient(timeout=10.0) as ephemeral_client:
                response = await ephemeral_client.post(
                    HCAPTCHA_VERIFY_URL,
                    data={
                        'secret': get_settings().hcaptcha_secret_key,
                        'response': token,
                        'sitekey': get_settings().hcaptcha_site_key,
                    },
                )
                response.raise_for_status()
                result = response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(
            status_code=403,
            detail='hCaptcha verification failed',
        )

    if result.get('success') is not True:
        raise HTTPException(
            status_code=403,
            detail='hCaptcha verification failed',
        )


async def verify_refresh_token(refresh_token, redis_pool, deployment=None):
    return await get_identity().local_tokens.verify(redis_pool,refresh_token,deployment)


async def login_user(
    payload: UserLogin,
    db: AsyncSession,
    redis_pool: Redis,
    hcaptcha_bypass_key: str | None = None,
    client_ip: str | None = None,
    hash_refresh_token: bool = False,
    deployment: DeploymentBinding | None = None,
    request: Request | None = None,
) -> TokenPairData:
    """Authenticate user, issue JWT tokens, and store session in Redis cache.

    Args:
        payload (UserLogin): Login credentials (username/e-mail and password).
        db (AsyncSession): Database session.
        redis_pool (Redis): Redis connection pool for caching sessions.

    Returns:
        TokenPairData: Generated tokens and user-related details.
    """
    settings = get_settings()
    require_local_login()
    if deployment is None and request is not None:
        deployment = await resolve_request_deployment(request, db)

    await _verify_hcaptcha(payload.hcaptcha_token, hcaptcha_bypass_key)

    await check_login_guard(
        redis_pool,
        payload.identifier,
        client_ip,
        policy=settings,
    )

    try:
        user = await _authenticate(
            db,
            payload.identifier,
            payload.password,
        )
    except HTTPException as exc:
        if exc.status_code == 401:
            await record_failed_login(
                redis_pool,
                payload.identifier,
                client_ip,
                policy=settings,
            )
        raise

    await clear_login_guard(redis_pool, payload.identifier, client_ip)

    return await issue_token_pair_for_user(
        user,
        db,
        redis_pool,
        hash_refresh_token=hash_refresh_token,
        deployment=deployment,
    )


async def issue_token_pair_for_user(user, db, redis_pool, hash_refresh_token=False, deployment=None):
    # All refresh records are hashes now, independent of the caller's transport.
    tokens = get_identity().local_tokens
    epochs = await tokens.sessions.epochs(redis_pool,user.username)
    features = await feature_names_for_group(db,user.group_id)
    return await tokens.issue(redis_pool,user,features,deployment,epochs=epochs)


async def logout_user(refresh_token, authorization, redis_pool):
    await get_identity().local_tokens.logout(redis_pool,refresh_token,authorization)


async def refresh_tokens(payload, redis_pool, hash_refresh_token=False, deployment=None, request=None, db=None):
    require_local_login()
    token = payload.refresh_token or ''
    if not token:
        raise HTTPException(status_code=401,detail='Missing refresh token')
    if deployment is None and request is not None and db is not None:
        deployment = await resolve_request_deployment(request,db)
    return await get_identity().local_tokens.rotate(redis_pool,token,deployment)

from examples.identity.authorization.features import feature_names_for_group
