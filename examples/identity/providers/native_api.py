"""Stable route API for the application-owned identity service."""
from __future__ import annotations

from typing import Literal

from fastapi import Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from examples.identity.models import User
from examples.identity.runtime import get_identity
from examples.identity.schemas.auth import NativeSocialEmailLinkConfirmRequest
from examples.identity.schemas.auth import NativeSocialExchangeBeginRequest
from examples.identity.schemas.auth import NativeSocialExchangeBeginResponse
from examples.identity.schemas.auth import NativeSocialExchangeCompleteRequest
from examples.identity.schemas.auth import NativeSocialExchangeCompleteResponse
from examples.identity.schemas.auth import NativeSocialLinkBeginResponse
from examples.identity.schemas.auth import NativeSocialLinkCompleteRequest
from examples.identity.schemas.auth import NativeSocialLinkResponse
from examples.identity.sessions.jwt import JwtAuthorizationCredentials

NativeSocialProvider = Literal['google', 'apple']


async def begin_native_social_exchange(
    payload: NativeSocialExchangeBeginRequest, request: Request, redis: Redis,
) -> NativeSocialExchangeBeginResponse:
    return await get_identity().native_social.begin_native_social_exchange(
        payload, request, redis,
    )


async def complete_native_social_exchange(
    payload: NativeSocialExchangeCompleteRequest, redis: Redis, db: AsyncSession,
) -> NativeSocialExchangeCompleteResponse:
    return await get_identity().native_social.complete_native_social_exchange(
        payload, redis, db,
    )


async def begin_native_social_link(
    provider: NativeSocialProvider,
    credentials: JwtAuthorizationCredentials,
    redis: Redis,
) -> NativeSocialLinkBeginResponse:
    return await get_identity().native_social.begin_native_social_link(
        provider, credentials, redis,
    )


async def complete_native_social_link(
    payload: NativeSocialLinkCompleteRequest,
    credentials: JwtAuthorizationCredentials,
    redis: Redis,
    db: AsyncSession,
    user: User,
) -> NativeSocialLinkResponse:
    return await get_identity().native_social.complete_native_social_link(
        payload, credentials, redis, db, user,
    )


async def confirm_native_social_email_link(
    payload: NativeSocialEmailLinkConfirmRequest,
    credentials: JwtAuthorizationCredentials,
    redis: Redis,
    db: AsyncSession,
    user: User,
) -> NativeSocialLinkResponse:
    return await get_identity().native_social.confirm_native_social_email_link(
        payload, credentials, redis, db, user,
    )


async def redeem_keycloak_native_social_exchange(
    request: Request, redis: Redis,
) -> dict[str, str]:
    return await get_identity().native_social.redeem_keycloak_native_social_exchange(
        request, redis,
    )
