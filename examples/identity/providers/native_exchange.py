"""Application-owned NativeSocialService dependencies and operations."""

from __future__ import annotations
from examples.identity.settings.native import NativeSettings
from examples.identity.settings.oidc import OidcSettings
from examples.identity.schemas.auth import ProviderClaims
import base64
import hmac
import ipaddress
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlencode
from fastapi import HTTPException
from fastapi import Request
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from examples.identity.sessions.jwt import JwtAuthorizationCredentials
from examples.identity.models import User
from examples.identity.models import USER_STATUS_ACTIVE
from examples.identity.models import UserIdentity
from examples.identity.models import UserProfile
from examples.identity.schemas.auth import NativeSocialCredential
from examples.identity.schemas.auth import NativeSocialEmailLinkConfirmRequest
from examples.identity.schemas.auth import NativeSocialExchangeBeginRequest
from examples.identity.schemas.auth import NativeSocialExchangeBeginResponse
from examples.identity.schemas.auth import NativeSocialExchangeCompleteRequest
from examples.identity.schemas.auth import NativeSocialExchangeCompleteResponse
from examples.identity.schemas.auth import NativeSocialLinkBeginResponse
from examples.identity.schemas.auth import NativeSocialLinkCompleteRequest
from examples.identity.schemas.auth import NativeSocialLinkResponse

NativeSocialProvider = Literal['google', 'apple']


@dataclass(frozen=True, slots=True)
class EmailLinkCandidate:
    """One local account eligible for a verified-email link confirmation."""

    local_user_id: int
    keycloak_subject: str


_KEYCLOAK_SUBJECT_RE = re.compile('^[A-Za-z0-9._:-]{1,255}$')
_REDEEM_HMAC_MAX_AGE_SECONDS = 30


class NativeSocialService:
    def __init__(
        self,
        native: NativeSettings,
        oidc: OidcSettings,
        verifier,
        transactions,
        keycloak,
    ):
        self.native, self.oidc, self.verifier = native, oidc, verifier
        self.transactions, self.keycloak = (transactions, keycloak)

    def _not_enabled(self) -> HTTPException:
        """Return the consistent disabled-feature response."""
        return HTTPException(
            status_code=404, detail='native_social_exchange_not_enabled'
        )

    def _require_enabled(self) -> None:
        """Fail closed unless the server has every native-exchange secret."""
        settings = self.native
        if not settings.exchange_enabled:
            raise self._not_enabled()

    async def _verify_credential(
        self,
        provider: NativeSocialProvider,
        credential: NativeSocialCredential,
        nonce: str,
    ) -> ProviderClaims:
        """Verify a provider assertion and require the transaction nonce.

        Email is intentionally not used here.  Only the issuer-verified provider
        ``sub`` can be linked or used to authenticate a Keycloak account.
        """
        if provider == 'google':
            if not credential.id_token or credential.authorization_code:
                raise HTTPException(
                    status_code=401, detail='invalid_provider_credential'
                )
            return await self.verifier.verify_google_id_token(
                credential.id_token, expected_nonce=nonce, require_verified_email=False
            )
        if not credential.authorization_code:
            raise HTTPException(status_code=401, detail='invalid_provider_credential')
        return await self.verifier.verify_apple_identity_token(
            credential.id_token, credential.authorization_code, expected_nonce=nonce
        )

    def _provider_subject(self, claims: ProviderClaims) -> str:
        """Return a bounded, non-secret stable provider subject."""
        subject = claims.sub
        if not subject or len(subject.encode('utf-8')) > 512:
            raise HTTPException(status_code=401, detail='invalid_provider_credential')
        return subject

    def _normalised_verified_email(self, claims: ProviderClaims) -> str | None:
        """Return only a provider-verified, canonical email address.

        Email is deliberately used solely to offer a one-time account-link
        confirmation. It is never accepted as a login identifier or copied into
        the resulting Keycloak federated identity.
        """
        if not claims.email_verified or not isinstance(claims.email, str):
            return None
        email = claims.email.strip().lower()
        return email or None

    async def _find_email_link_candidate(
        self,
        db: AsyncSession,
        *,
        provider: NativeSocialProvider,
        provider_subject: str,
        claims: ProviderClaims,
    ) -> EmailLinkCandidate | None:
        """Find one active, already-Keycloak-linked local account by email.

        A provider assertion must already prove possession of a verified email.
        Even then this function does not authenticate the account; its result can
        only be used after a separate recent Keycloak reauthentication that proves
        control of the target Keycloak account.
        """
        email = self._normalised_verified_email(claims)
        if email is None:
            return None
        existing_provider_identity = await db.scalar(
            select(UserIdentity.id).where(
                UserIdentity.provider == provider,
                UserIdentity.provider_user_id == provider_subject,
            )
        )
        if existing_provider_identity is not None:
            return None
        rows = await db.execute(
            select(User.id, UserIdentity.provider_user_id)
            .join(UserProfile, UserProfile.user_id == User.id)
            .join(UserIdentity, UserIdentity.user_id == User.id)
            .where(
                User.status == USER_STATUS_ACTIVE,
                User.email_verified_at.is_not(None),
                UserProfile.email == email,
                UserIdentity.provider == self.oidc.identity_provider,
            )
        )
        candidates = [
            EmailLinkCandidate(local_user_id=user_id, keycloak_subject=subject)
            for user_id, subject in rows.all()
            if isinstance(user_id, int)
            and isinstance(subject, str)
            and _KEYCLOAK_SUBJECT_RE.fullmatch(subject)
        ]
        return candidates[0] if len(candidates) == 1 else None

    async def _store_email_link_confirmation(
        self,
        redis: Redis,
        *,
        provider: NativeSocialProvider,
        provider_subject: str,
        candidate: EmailLinkCandidate,
    ) -> str:
        """Store the non-secret proof required after Keycloak reauthentication."""
        settings = self.native
        transaction_id = self.transactions.identifier()
        await self.transactions.store(
            redis,
            transaction_id,
            {
                'kind': 'email-link-confirmation',
                'provider': provider,
                'provider_subject_b64': base64.urlsafe_b64encode(
                    provider_subject.encode('utf-8')
                )
                .rstrip(b'=')
                .decode('ascii'),
                'local_user_id': candidate.local_user_id,
                'keycloak_subject': candidate.keycloak_subject,
            },
            settings.link_ttl_seconds,
        )
        return transaction_id

    def _authorisation_url(
        self, transaction_id: str, record: Mapping[str, object]
    ) -> str:
        """Build an exact standard Keycloak authorisation-code request."""
        params = {
            'response_type': 'code',
            'client_id': self.transactions.required_string(record, 'client_id'),
            'redirect_uri': self.transactions.required_string(record, 'redirect_uri'),
            'code_challenge': self.transactions.required_string(
                record, 'code_challenge'
            ),
            'code_challenge_method': 'S256',
            'state': self.transactions.required_string(record, 'state'),
            'native_social_exchange': transaction_id,
        }
        return (
            f'{self.oidc.issuer_url}/protocol/openid-connect/auth?{urlencode(params)}'
        )

    async def begin_native_social_exchange(
        self, payload: NativeSocialExchangeBeginRequest, request: Request, redis: Redis
    ) -> NativeSocialExchangeBeginResponse:
        """Create a nonce- and PKCE-bound native social sign-in transaction."""
        settings = self.native
        self._require_enabled()
        self.transactions.validate_client_binding(
            payload.client_id, payload.redirect_uri
        )
        await self.transactions.enforce_begin_rate_limit(request, redis)
        transaction_id = self.transactions.identifier()
        nonce = self.transactions.identifier()
        await self.transactions.store(
            redis,
            transaction_id,
            {
                'kind': 'login-begin',
                'provider': payload.provider,
                'nonce': nonce,
                'client_id': payload.client_id,
                'redirect_uri': payload.redirect_uri,
                'code_challenge': payload.code_challenge,
                'state': payload.state,
            },
            settings.exchange_ttl_seconds,
        )
        return NativeSocialExchangeBeginResponse(
            transaction_id=transaction_id,
            nonce=nonce,
            expires_in=settings.exchange_ttl_seconds,
        )

    async def complete_native_social_exchange(
        self,
        payload: NativeSocialExchangeCompleteRequest,
        redis: Redis,
        db: AsyncSession,
    ) -> NativeSocialExchangeCompleteResponse:
        """Validate native credentials and hand a one-use proof to Keycloak."""
        settings = self.native
        self._require_enabled()
        record = await self.transactions.consume(redis, payload.transaction_id)
        if record.get('kind') != 'login-begin':
            raise HTTPException(
                status_code=401, detail='native_social_exchange_expired'
            )
        provider = self.transactions.provider(record)
        nonce = self.transactions.required_string(record, 'nonce')
        claims = await self._verify_credential(provider, payload, nonce)
        subject = self._provider_subject(claims)
        candidate = await self._find_email_link_candidate(
            db, provider=provider, provider_subject=subject, claims=claims
        )
        if candidate is not None:
            confirmation_id = await self._store_email_link_confirmation(
                redis, provider=provider, provider_subject=subject, candidate=candidate
            )
            raise HTTPException(
                status_code=409,
                detail={
                    'code': 'account_link_required',
                    'link_transaction_id': confirmation_id,
                    'expires_in': settings.link_ttl_seconds,
                },
            )
        await self.transactions.store(
            redis,
            payload.transaction_id,
            {
                'kind': 'login-redeem',
                'provider': provider,
                'provider_subject_b64': base64.urlsafe_b64encode(
                    subject.encode('utf-8')
                )
                .rstrip(b'=')
                .decode('ascii'),
                'client_id': self.transactions.required_string(record, 'client_id'),
                'redirect_uri': self.transactions.required_string(
                    record, 'redirect_uri'
                ),
                'code_challenge': self.transactions.required_string(
                    record, 'code_challenge'
                ),
            },
            settings.exchange_ttl_seconds,
        )
        return NativeSocialExchangeCompleteResponse(
            authorization_url=self._authorisation_url(payload.transaction_id, record),
            expires_in=settings.exchange_ttl_seconds,
        )

    def _keycloak_identity_from_credentials(
        self, credentials: JwtAuthorizationCredentials
    ) -> tuple[str, str | None]:
        """Require a recently reauthenticated Keycloak access token for linking."""
        settings = self.native
        payload = credentials.payload
        if payload.get('iss') != self.oidc.issuer_url:
            raise HTTPException(
                status_code=401, detail='keycloak_reauthentication_required'
            )
        subject = payload.get('sub')
        auth_time = payload.get('auth_time')
        now = time.time()
        if (
            not isinstance(subject, str)
            or not _KEYCLOAK_SUBJECT_RE.fullmatch(subject)
            or isinstance(auth_time, bool)
            or (not isinstance(auth_time, (int, float)))
            or (auth_time > now + 60)
            or (now - auth_time > settings.link_max_auth_age_seconds)
        ):
            raise HTTPException(
                status_code=401, detail='keycloak_reauthentication_required'
            )
        sid = payload.get('sid') or payload.get('session_state')
        if sid is not None and (not isinstance(sid, str)):
            raise HTTPException(
                status_code=401, detail='keycloak_reauthentication_required'
            )
        return (subject, sid)

    async def begin_native_social_link(
        self,
        provider: NativeSocialProvider,
        credentials: JwtAuthorizationCredentials,
        redis: Redis,
    ) -> NativeSocialLinkBeginResponse:
        """Start a nonce-bound link transaction after fresh Keycloak auth."""
        settings = self.native
        self._require_enabled()
        keycloak_subject, session_id = self._keycloak_identity_from_credentials(
            credentials
        )
        transaction_id = self.transactions.identifier()
        nonce = self.transactions.identifier()
        await self.transactions.store(
            redis,
            transaction_id,
            {
                'kind': 'link-begin',
                'provider': provider,
                'nonce': nonce,
                'keycloak_subject': keycloak_subject,
                'keycloak_session_id': session_id,
            },
            settings.link_ttl_seconds,
        )
        return NativeSocialLinkBeginResponse(
            transaction_id=transaction_id,
            nonce=nonce,
            expires_in=settings.link_ttl_seconds,
        )

    def _provider_subject_from_record(self, record: Mapping[str, object]) -> str:
        """Decode and validate the provider subject retained in a proof record."""
        encoded_subject = self.transactions.required_string(
            record, 'provider_subject_b64'
        )
        try:
            padded = encoded_subject + '=' * (-len(encoded_subject) % 4)
            subject = base64.urlsafe_b64decode(padded.encode('ascii')).decode('utf-8')
        except (UnicodeDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=401, detail='native_social_exchange_invalid'
            ) from exc
        if not subject or len(subject.encode('utf-8')) > 512:
            raise HTTPException(
                status_code=401, detail='native_social_exchange_invalid'
            )
        return subject

    async def _ensure_local_identity_available(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        provider: NativeSocialProvider,
        provider_subject: str,
    ) -> None:
        """Reject a provider subject that belongs to another local user."""
        identity = await db.scalar(
            select(UserIdentity).where(
                UserIdentity.provider == provider,
                UserIdentity.provider_user_id == provider_subject,
            )
        )
        if identity is not None and identity.user_id != user_id:
            raise HTTPException(
                status_code=409, detail='provider_identity_already_linked'
            )
        identity_for_user = await db.scalar(
            select(UserIdentity).where(
                UserIdentity.user_id == user_id, UserIdentity.provider == provider
            )
        )
        if (
            identity_for_user is not None
            and identity_for_user.provider_user_id != provider_subject
        ):
            raise HTTPException(status_code=409, detail='provider_already_linked')

    async def _sync_local_provider_identity(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        provider: NativeSocialProvider,
        provider_subject: str,
    ) -> None:
        """Mirror a completed Keycloak link in Visionnaire's local identity map."""
        await self._ensure_local_identity_available(
            db, user_id=user_id, provider=provider, provider_subject=provider_subject
        )
        existing = await db.scalar(
            select(UserIdentity).where(
                UserIdentity.user_id == user_id, UserIdentity.provider == provider
            )
        )
        if existing is not None:
            if existing.provider_user_id != provider_subject:
                raise HTTPException(status_code=409, detail='provider_already_linked')
            return
        db.add(
            UserIdentity(
                user_id=user_id,
                provider=provider,
                provider_user_id=provider_subject,
                email=None,
                email_verified=False,
                display_name=None,
                raw_profile=None,
                raw_email_is_private=False,
            )
        )
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            await self._ensure_local_identity_available(
                db,
                user_id=user_id,
                provider=provider,
                provider_subject=provider_subject,
            )
            existing = await db.scalar(
                select(UserIdentity).where(
                    UserIdentity.user_id == user_id, UserIdentity.provider == provider
                )
            )
            if existing is not None and existing.provider_user_id == provider_subject:
                return
            raise HTTPException(
                status_code=409, detail='provider_identity_already_linked'
            ) from exc

    async def complete_native_social_link(
        self,
        payload: NativeSocialLinkCompleteRequest,
        credentials: JwtAuthorizationCredentials,
        redis: Redis,
        db: AsyncSession,
        user: User,
    ) -> NativeSocialLinkResponse:
        """Validate provider proof and attach it to the current Keycloak user."""
        self._require_enabled()
        current_subject, current_session_id = self._keycloak_identity_from_credentials(
            credentials
        )
        record = await self.transactions.consume(redis, payload.transaction_id)
        if record.get('kind') != 'link-begin':
            raise HTTPException(status_code=401, detail='native_social_link_expired')
        if not hmac.compare_digest(
            self.transactions.required_string(record, 'keycloak_subject'),
            current_subject,
        ):
            raise HTTPException(status_code=401, detail='native_social_link_expired')
        stored_session_id = record.get('keycloak_session_id')
        if isinstance(stored_session_id, str):
            if not isinstance(current_session_id, str) or not hmac.compare_digest(
                stored_session_id, current_session_id
            ):
                raise HTTPException(
                    status_code=401, detail='native_social_link_expired'
                )
        provider = self.transactions.provider(record)
        claims = await self._verify_credential(
            provider, payload, self.transactions.required_string(record, 'nonce')
        )
        provider_subject = self._provider_subject(claims)
        await self._ensure_local_identity_available(
            db, user_id=user.id, provider=provider, provider_subject=provider_subject
        )
        status = await self.keycloak.link_federated_identity(
            keycloak_subject=current_subject,
            provider=provider,
            provider_subject=provider_subject,
        )
        await self._sync_local_provider_identity(
            db, user_id=user.id, provider=provider, provider_subject=provider_subject
        )
        return NativeSocialLinkResponse(provider=provider, status=status)

    async def confirm_native_social_email_link(
        self,
        payload: NativeSocialEmailLinkConfirmRequest,
        credentials: JwtAuthorizationCredentials,
        redis: Redis,
        db: AsyncSession,
        user: User,
    ) -> NativeSocialLinkResponse:
        """Finish a verified-email link only after fresh Keycloak proof.

        The original Google/Apple assertion has already been consumed. This route
        simply binds its immutable subject to the *preselected* local Keycloak
        account after the user proves possession of that account's own credential.
        """
        self._require_enabled()
        current_subject, _ = self._keycloak_identity_from_credentials(credentials)
        record = await self.transactions.consume(redis, payload.transaction_id)
        if record.get('kind') != 'email-link-confirmation':
            raise HTTPException(status_code=401, detail='native_social_link_expired')
        local_user_id = record.get('local_user_id')
        if not isinstance(local_user_id, int) or local_user_id != user.id:
            raise HTTPException(status_code=401, detail='native_social_link_expired')
        if not hmac.compare_digest(
            self.transactions.required_string(record, 'keycloak_subject'),
            current_subject,
        ):
            raise HTTPException(status_code=401, detail='native_social_link_expired')
        provider = self.transactions.provider(record)
        provider_subject = self._provider_subject_from_record(record)
        await self._ensure_local_identity_available(
            db, user_id=user.id, provider=provider, provider_subject=provider_subject
        )
        status = await self.keycloak.link_federated_identity(
            keycloak_subject=current_subject,
            provider=provider,
            provider_subject=provider_subject,
        )
        await self._sync_local_provider_identity(
            db, user_id=user.id, provider=provider, provider_subject=provider_subject
        )
        return NativeSocialLinkResponse(provider=provider, status=status)

    def _is_loopback_request(self, request: Request) -> bool:
        """Return whether a Keycloak redemption request came from loopback."""
        if request.client is None:
            return False
        try:
            return ipaddress.ip_address(request.client.host).is_loopback
        except ValueError:
            return request.client.host.lower() == 'localhost'

    def _hmac_signature(self, timestamp: str, body: bytes) -> str:
        """Build the canonical HMAC for a private Keycloak redemption request."""
        settings = self.native
        digest = hmac.digest(
            settings.exchange_shared_secret.encode('utf-8'),
            timestamp.encode('ascii') + b'.' + body,
            'sha256',
        )
        return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')

    def _parse_redeem_body(self, body: bytes) -> dict[str, str]:
        """Parse the custom Keycloak request as a strict, string-only object."""
        try:
            data = json.loads(body)
        except (UnicodeDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=401, detail='native_social_exchange_invalid'
            ) from exc
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=401, detail='native_social_exchange_invalid'
            )
        fields = ('transaction_id', 'client_id', 'redirect_uri', 'code_challenge')
        if set(data) != set(fields) or any(
            (
                not isinstance(data.get(field), str) or not data[field]
                for field in fields
            )
        ):
            raise HTTPException(
                status_code=401, detail='native_social_exchange_invalid'
            )
        return {field: data[field] for field in fields}

    async def redeem_keycloak_native_social_exchange(
        self, request: Request, redis: Redis
    ) -> dict[str, str]:
        """Redeem one proof for the Keycloak authenticator over loopback HMAC."""
        self._require_enabled()
        if not self._is_loopback_request(request):
            raise HTTPException(
                status_code=403, detail='native_social_exchange_forbidden'
            )
        timestamp = request.headers.get('X-Visionnaire-Timestamp', '')
        signature = request.headers.get('X-Visionnaire-Signature', '')
        if not timestamp.isdecimal():
            raise HTTPException(
                status_code=401, detail='native_social_exchange_invalid'
            )
        if abs(time.time() - int(timestamp)) > _REDEEM_HMAC_MAX_AGE_SECONDS:
            raise HTTPException(
                status_code=401, detail='native_social_exchange_invalid'
            )
        body = await request.body()
        if not hmac.compare_digest(self._hmac_signature(timestamp, body), signature):
            raise HTTPException(
                status_code=401, detail='native_social_exchange_invalid'
            )
        payload = self._parse_redeem_body(body)
        record = await self.transactions.consume(redis, payload['transaction_id'])
        if record.get('kind') != 'login-redeem':
            raise HTTPException(
                status_code=401, detail='native_social_exchange_invalid'
            )
        for name in ('client_id', 'redirect_uri', 'code_challenge'):
            if not hmac.compare_digest(
                self.transactions.required_string(record, name), payload[name]
            ):
                raise HTTPException(
                    status_code=401, detail='native_social_exchange_invalid'
                )
        encoded_subject = self.transactions.required_string(
            record, 'provider_subject_b64'
        )
        try:
            padded = encoded_subject + '=' * (-len(encoded_subject) % 4)
            subject = base64.urlsafe_b64decode(padded.encode('ascii')).decode('utf-8')
        except (UnicodeDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=401, detail='native_social_exchange_invalid'
            ) from exc
        if not subject or len(subject.encode('utf-8')) > 512:
            raise HTTPException(
                status_code=401, detail='native_social_exchange_invalid'
            )
        return {
            'provider': self.transactions.provider(record),
            'provider_subject_b64': encoded_subject,
        }
