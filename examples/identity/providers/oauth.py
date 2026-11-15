"""Stable route API for the application-owned identity service."""
from __future__ import annotations

from typing import Literal

from fastapi import Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from examples.identity.accounts.legal import SignupConsentPayload
from examples.identity.deployment_context import DeploymentBinding
from examples.identity.models import User
from examples.identity.runtime import get_identity
from examples.identity.schemas.auth import IdentityListResponse
from examples.identity.schemas.auth import IdentityRead
from examples.identity.schemas.auth import ProviderClaims
from examples.identity.schemas.auth import TokenPairData

Provider = Literal['google', 'apple']


async def verify_google_id_token(
    id_token: str,
    *,
    expected_nonce: str | None = None,
    require_verified_email: bool = True,
) -> ProviderClaims:
    return await get_identity().providers.verify_google_id_token(
        id_token,
        expected_nonce=expected_nonce,
        require_verified_email=require_verified_email,
    )


async def verify_apple_identity_token(
    identity_token: str | None,
    authorization_code: str,
    expected_nonce: str | None = None,
) -> ProviderClaims:
    return await get_identity().providers.verify_apple_identity_token(
        identity_token, authorization_code, expected_nonce,
    )


async def authenticate_provider_user(
    provider: Provider,
    claims: ProviderClaims,
    db: AsyncSession,
    redis_pool: Redis,
    consent_payload: SignupConsentPayload | None = None,
    hash_refresh_token: bool = False,
    deployment: DeploymentBinding | None = None,
    request: Request | None = None,
) -> TokenPairData:
    return await get_identity().provider_accounts.authenticate_provider_user(
        provider,
        claims,
        db,
        redis_pool,
        consent_payload,
        hash_refresh_token,
        deployment,
        request,
    )


async def login_with_google(
    id_token: str,
    db: AsyncSession,
    redis_pool: Redis,
    email: str | None = None,
    display_name: str | None = None,
    device_lang: str | None = None,
    consent_payload: SignupConsentPayload | None = None,
    hash_refresh_token: bool = False,
    deployment: DeploymentBinding | None = None,
    request: Request | None = None,
) -> TokenPairData:
    return await get_identity().provider_accounts.login_with_google(
        id_token,
        db,
        redis_pool,
        email,
        display_name,
        device_lang,
        consent_payload,
        hash_refresh_token,
        deployment,
        request,
    )


async def login_with_apple(
    identity_token: str | None,
    authorization_code: str,
    db: AsyncSession,
    redis_pool: Redis,
    email: str | None = None,
    given_name: str | None = None,
    family_name: str | None = None,
    nonce: str | None = None,
    device_lang: str | None = None,
    consent_payload: SignupConsentPayload | None = None,
    hash_refresh_token: bool = False,
    deployment: DeploymentBinding | None = None,
    request: Request | None = None,
) -> TokenPairData:
    return await get_identity().provider_accounts.login_with_apple(
        identity_token,
        authorization_code,
        db,
        redis_pool,
        email,
        given_name,
        family_name,
        nonce,
        device_lang,
        consent_payload,
        hash_refresh_token,
        deployment,
        request,
    )


async def list_user_identities(user: User, db: AsyncSession) -> IdentityListResponse:
    return await get_identity().provider_accounts.list_user_identities(user, db)


async def link_provider_identity(
    user: User, provider: Provider, claims: ProviderClaims, db: AsyncSession,
) -> IdentityRead:
    return await get_identity().provider_accounts.link_provider_identity(
        user, provider, claims, db,
    )


async def link_google_identity(
    user: User, id_token: str, db: AsyncSession,
) -> IdentityRead:
    return await get_identity().provider_accounts.link_google_identity(
        user, id_token, db,
    )


async def link_apple_identity(
    user: User,
    identity_token: str | None,
    authorization_code: str,
    db: AsyncSession,
    nonce: str | None = None,
) -> IdentityRead:
    return await get_identity().provider_accounts.link_apple_identity(
        user, identity_token, authorization_code, db, nonce,
    )


async def unlink_identity(
    user: User, identity_id: int, db: AsyncSession,
) -> dict[str, str]:
    return await get_identity().provider_accounts.unlink_identity(user, identity_id, db)
