from __future__ import annotations

import base64
import json
import time
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .schemas import MAX_REGISTRY_TTL_SECONDS
from .schemas import REGISTRY_SCHEMA_VERSION
from examples.deployment.registry.enrollments import enforce_enrollment_exchange_rate_limit
from examples.deployment.registry.enrollments import enrollment_code_verifier_hash
from examples.deployment.registry.enrollments import redeem_enrollment_code
from examples.identity.deployment_context import canonical_api_base_url
from examples.identity.models import Deployment
from examples.identity.models import DEPLOYMENT_STATUS_ACTIVE
from examples.identity.models import TENANT_STATUS_ACTIVE
from examples.identity.runtime import get_settings


async def exchange_enrollment_code(
    raw_code: str,
    client_ip: str | None,
    db: AsyncSession,
    redis: Redis,
) -> UUID:
    """Atomically exchange one enrollment code for a deployment identifier."""
    # Hash before rate limiting so Redis never receives the raw enrollment
    # code.
    settings = get_settings()
    verifier_hash = enrollment_code_verifier_hash(
        raw_code,
        settings.deployment_enrollment_code_pepper,
    )
    retry_after: int | None = await enforce_enrollment_exchange_rate_limit(
        redis,
        client_ip=client_ip,
        verifier_hash=verifier_hash,
        maximum=settings.deployment_enrollment_rate_limit_max,
        window_seconds=(settings.deployment_enrollment_rate_limit_window_seconds),
    )
    if retry_after is not None:
        # The client can wait for the Redis fixed window rather than retrying.
        raise HTTPException(
            status_code=429,
            detail={'code': 'enrollment_rate_limited'},
            headers={'Retry-After': str(retry_after)},
        )

    # Redemption holds the database lock and consumes a valid code once.
    result = await redeem_enrollment_code(db, verifier_hash=verifier_hash)
    if result.status == 'invalid':
        raise HTTPException(
            status_code=403,
            detail={'code': 'invalid_enrollment_code'},
        )
    if result.status != 'redeemed' or result.deployment_id is None:
        raise HTTPException(
            status_code=410,
            detail={'code': 'enrollment_unavailable_or_expired'},
        )
    return result.deployment_id


async def get_deployment_registry_document(
    deployment_id: UUID,
    db: AsyncSession,
) -> dict[str, object]:
    """Build a fresh signed Registry document for one active deployment."""
    # Load one deployment; the tenant relationship supplies its active status.
    settings = get_settings()
    deployment = await db.scalar(
        select(Deployment).where(Deployment.id == deployment_id),
    )
    if deployment is None:
        raise HTTPException(
            status_code=404,
            detail={'code': 'deployment_not_found'},
        )
    if (
        deployment.status != DEPLOYMENT_STATUS_ACTIVE
        or deployment.tenant.status != TENANT_STATUS_ACTIVE
    ):
        raise HTTPException(
            status_code=410,
            detail={'code': 'deployment_revoked'},
        )
    # Signing happens only after deployment and tenant status are confirmed.
    document = build_registry_document(
        deployment,
        private_key_pem=settings.deployment_registry_ed25519_private_key,
        key_id=settings.deployment_registry_key_id,
        ttl_seconds=settings.deployment_registry_ttl_seconds,
    )
    return document


def build_registry_document(
    deployment: Deployment,
    private_key_pem: str,
    key_id: str,
    ttl_seconds: int,
    issued_at: int | None = None,
) -> dict[str, object]:
    """Build the exact nine-field signed public Registry document."""
    # Restrict document lifetime to the public Registry contract.
    if not 0 < ttl_seconds <= MAX_REGISTRY_TTL_SECONDS:
        raise ValueError(
            'deployment registry signing configuration is invalid',
        )
    now: int = int(time.time()) if issued_at is None else issued_at
    if now < 0:
        raise ValueError(
            'deployment registry signing configuration is invalid',
        )
    # Sign only the canonical base URL that clients will subsequently use.
    api_base_url: str = canonical_api_base_url(deployment.api_base_url)
    if api_base_url != deployment.api_base_url or deployment.config_revision < 1:
        raise ValueError(
            'deployment registry configuration is invalid',
        )
    # Assemble all public fields before separating the signable payload.
    document: dict[str, object] = {
        'schema_version': REGISTRY_SCHEMA_VERSION,
        'deployment_id': str(deployment.id),
        'tenant_id': str(deployment.tenant_id),
        'api_base_url': api_base_url,
        'config_revision': deployment.config_revision,
        'issued_at': now,
        'expires_at': now + ttl_seconds,
        'key_id': key_id,
    }
    if not private_key_pem.strip():
        raise ValueError(
            'deployment registry signing key is not configured',
        )
    # Deployment secrets may preserve newlines as literal escape sequences.
    private_key = load_pem_private_key(
        private_key_pem.strip().replace('\\n', '\n').encode('utf-8'),
        password=None,
    )
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError('deployment registry signing key must be Ed25519')  # noqa: TRY004 — invalid key configuration
    # Key selection and the signature itself are deliberately not self-signed.
    signed_document: dict[str, object] = document.copy()
    del signed_document['key_id']
    # Canonical JSON ensures every client signs and verifies identical bytes.
    signature = private_key.sign(
        json.dumps(
            signed_document,
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
        ).encode('utf-8'),
    )
    # Base64url carries binary signature bytes safely in the JSON document.
    document['signature'] = (
        base64.urlsafe_b64encode(signature)
        .decode(
            'ascii',
        )
        .rstrip('=')
    )
    return document
