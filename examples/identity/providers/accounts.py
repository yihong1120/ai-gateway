"""Application-owned ProviderAccounts dependencies and operations."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from datetime import timezone
from typing import Literal

from fastapi import HTTPException
from fastapi import Request
from redis.asyncio import Redis
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from examples.identity.accounts.legal import record_user_consent
from examples.identity.accounts.legal import SignupConsentPayload
from examples.identity.accounts.legal import validate_signup_consents
from examples.identity.deployment_context import DeploymentBinding
from examples.identity.deployment_context import resolve_request_deployment
from examples.identity.models import User
from examples.identity.models import USER_STATUS_ACTIVE
from examples.identity.models import USER_STATUS_EMAIL_UNVERIFIED
from examples.identity.models import USER_STATUS_PENDING_ADMIN_APPROVAL
from examples.identity.models import USER_STATUS_REJECTED
from examples.identity.models import USER_STATUS_SUSPENDED
from examples.identity.models import UserIdentity
from examples.identity.models import UserProfile
from examples.identity.schemas.auth import IdentityListResponse
from examples.identity.schemas.auth import IdentityRead
from examples.identity.schemas.auth import ProviderClaims
from examples.identity.schemas.auth import TokenPairData
from examples.identity.sessions.authentication import issue_token_pair_for_user

Provider = Literal['google', 'apple']
OAUTH_DISABLED_PASSWORD_HASH = 'oauth_disabled:provider-only'


class ProviderAccounts:
    def __init__(self, verifier):
        self.verifier = verifier

    def _status_error(self, status: str) -> HTTPException:
        """Return the HTTP error corresponding to an account status.

        Args:
            status: Account lifecycle status.

        Returns:
            Appropriate authentication HTTP exception.
        """
        if status == USER_STATUS_EMAIL_UNVERIFIED:
            return HTTPException(
                status_code=403, detail={'code': 'email_unverified', 'status': status},
            )
        if status == USER_STATUS_PENDING_ADMIN_APPROVAL:
            return HTTPException(
                status_code=403,
                detail={'code': 'pending_admin_approval', 'status': status},
            )
        if status == USER_STATUS_SUSPENDED:
            return HTTPException(
                status_code=403, detail={'code': 'account_suspended', 'status': status},
            )
        if status == USER_STATUS_REJECTED:
            return HTTPException(
                status_code=403, detail={'code': 'account_rejected', 'status': status},
            )
        return HTTPException(
            status_code=403, detail={'code': 'user_not_active', 'status': status},
        )

    def _ensure_active_user(self, user: User) -> None:
        """Reject a user account that is not active.

        Args:
            user: Account whose lifecycle status is checked.

        Raises:
            HTTPException: If the account is not active.
        """
        if user.status != USER_STATUS_ACTIVE:
            raise self._status_error(user.status)

    def _username_from_claims(self, provider: Provider, claims: ProviderClaims) -> str:
        """Derive a stable local username from provider claims.

        Args:
            provider: Identity-provider name.
            claims: Verified identity-provider claims.

        Returns:
            Bounded local username candidate.
        """
        email = self.verifier.normalise_email(claims.email)
        source = email.split('@', 1)[0] if email else f'{provider}_{claims.sub}'
        username = re.sub('[^A-Za-z0-9_.-]+', '_', source).strip('._-')
        return username[:64] or f'{provider}_user'

    def _identity_username(self, provider: Provider, claims: ProviderClaims) -> str:
        """Return a deterministic, collision-resistant OAuth username.

        Args:
            provider: Identity-provider name.
            claims: Verified identity-provider claims.

        Returns:
            A stable username at most 80 characters long.
        """
        base = self._username_from_claims(provider, claims)
        digest = hashlib.blake2s(
            f'{provider}:{claims.sub}'.encode(), digest_size=6,
        ).hexdigest()
        return f'{base[:67]}_{digest}'

    def _profile_names(
        self, provider: Provider, claims: ProviderClaims,
    ) -> tuple[str, str]:
        """Derive bounded family and given names from provider claims.

        Args:
            provider: Identity-provider name.
            claims: Verified identity-provider claims.

        Returns:
            Bounded family-name and given-name pair.
        """
        given_name = (claims.given_name or '').strip()
        family_name = (claims.family_name or '').strip()
        if given_name or family_name:
            return ((family_name or provider.title())[:50], (given_name or 'User')[:50])
        name = (claims.name or '').strip()
        if name:
            parts = name.split()
            if len(parts) > 1:
                return (parts[0][:50], ' '.join(parts[1:])[:50])
            return (provider.title(), parts[0][:50])
        email = self.verifier.normalise_email(claims.email)
        if email:
            return (provider.title(), email.split('@', 1)[0][:50])
        return (provider.title(), 'User')

    def _new_identity(
        self, user: User, provider: Provider, claims: ProviderClaims,
    ) -> UserIdentity:
        """Build an unpersisted provider identity for a user.

        Args:
            user: Local user receiving the identity.
            provider: Identity-provider name.
            claims: Verified identity-provider claims.

        Returns:
            New unpersisted identity model.
        """
        email = self.verifier.normalise_email(claims.email)
        return UserIdentity(
            user=user,
            provider=provider,
            provider_user_id=claims.sub,
            email=email,
            email_verified=claims.email_verified,
            display_name=str(
                claims.name
                or ' '.join(
                    part
                    for part in [
                            (claims.given_name or '').strip(),
                            (claims.family_name or '').strip(),
                    ]
                    if part
                )
                or '',
            )
            or None,
            raw_profile=claims.model_dump(),
            raw_email_is_private=claims.is_private_email
            or bool(email and email.endswith('@privaterelay.appleid.com')),
        )

    def _display_name_from_claims(self, claims: ProviderClaims) -> str | None:
        """Return the optional display name supplied by an identity provider.

        Args:
            claims: Verified identity-provider claims.

        Returns:
            Bounded display name, or ``None`` when absent.
        """
        display_name = str(
            claims.name
            or ' '.join(
                part
                for part in [
                    (claims.given_name or '').strip(),
                    (claims.family_name or '').strip(),
                ]
                if part
            )
            or '',
        )
        return display_name or None

    async def _find_identity_user(
        self, db: AsyncSession, provider: Provider, provider_user_id: str,
    ) -> User | None:
        """Find the local user associated with a provider subject.

        Args:
            db: Database session used to load identity and user data.
            provider: Identity-provider name.
            provider_user_id: Stable external provider subject.

        Returns:
            Associated user, or ``None`` when no identity exists.
        """
        identity = await db.scalar(
            select(UserIdentity).where(
                UserIdentity.provider == provider,
                UserIdentity.provider_user_id == provider_user_id,
            ),
        )
        if identity is None:
            return None
        return await db.get(User, identity.user_id)

    async def _find_user_by_email(self, db: AsyncSession, email: str) -> User | None:
        """Find the local user with a matching profile email address.

        Args:
            db: Database session used to search profiles.
            email: Normalised profile email address.

        Returns:
            Matching local user, or ``None`` when absent.
        """
        return await db.scalar(
            select(User)
            .join(UserProfile, UserProfile.user_id == User.id)
            .where(UserProfile.email == email.strip().lower()),
        )

    async def _create_pending_user_with_identity(
        self,
        db: AsyncSession,
        provider: Provider,
        claims: ProviderClaims,
        deployment: DeploymentBinding | None = None,
    ) -> User:
        """Create a pending account and identity from provider claims.

        Args:
            db: Database session used to persist account and identity.
            provider: Identity-provider name.
            claims: Verified identity-provider claims.

        Returns:
            Newly created pending user.
        """
        email = self.verifier.normalise_email(claims.email)
        if not email:
            raise HTTPException(
                status_code=400,
                detail='Provider account did not return an email address',
            )
        family_name, given_name = self._profile_names(provider, claims)
        user = User(
            username=self._identity_username(provider, claims),
            password_hash=OAUTH_DISABLED_PASSWORD_HASH,
            role='user',
            status=USER_STATUS_PENDING_ADMIN_APPROVAL,
            email_verified_at=datetime.now(timezone.utc),
            group_id=None,
            **{'tenant_id': deployment.tenant_id} if deployment else {},
        )
        try:
            db.add(user)
            await db.flush()
            db.add(
                UserProfile(
                    user_id=user.id,
                    family_name=family_name,
                    given_name=given_name,
                    middle_name=None,
                    email=email,
                    mobile_number=None,
                ),
            )
            db.add(self._new_identity(user, provider, claims))
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            existing = await self._find_identity_user(db, provider, claims.sub)
            if existing is not None:
                return existing
            raise HTTPException(
                status_code=409, detail='OAuth account creation conflict',
            ) from exc
        await db.refresh(user, attribute_names=['profile', 'group'])
        return user

    async def authenticate_provider_user(
        self,
        provider: Provider,
        claims: ProviderClaims,
        db: AsyncSession,
        redis_pool: Redis,
        consent_payload: SignupConsentPayload | None = None,
        hash_refresh_token: bool = False,
        deployment: DeploymentBinding | None = None,
        request: Request | None = None,
    ) -> TokenPairData:
        """Resolve a verified provider identity to a local user and issue
        tokens."""
        provider_user_id = claims.sub
        if deployment is None and isinstance(request, Request):
            deployment = await resolve_request_deployment(request, db)
        if deployment is None and isinstance(request, Request):
            raise HTTPException(status_code=409, detail='deployment_required')
        user = await self._find_identity_user(db, provider, provider_user_id)
        if user is not None:
            self._ensure_active_user(user)
            if deployment is not None:
                return await issue_token_pair_for_user(
                    user,
                    db,
                    redis_pool,
                    hash_refresh_token=hash_refresh_token,
                    deployment=deployment,
                )
            return await issue_token_pair_for_user(
                user, db, redis_pool, hash_refresh_token=hash_refresh_token,
            )
        email = self.verifier.normalise_email(claims.email)
        if email:
            existing_user = await self._find_user_by_email(db, email)
            if existing_user is not None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        'code': 'account_link_required',
                        'message': 'Please login with your existing account and link this provider.',
                    },
                )
        if consent_payload is None:
            raise HTTPException(
                status_code=400,
                detail={
                    'code': 'legal_consent_required',
                    'message': 'Legal consent is required for new accounts.',
                },
            )
        await validate_signup_consents(consent_payload, db)
        user = await self._create_pending_user_with_identity(
            db, provider, claims, deployment,
        )
        await record_user_consent(user.id, consent_payload, db, request=None)
        raise self._status_error(user.status)

    async def login_with_google(
        self,
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
        """Authenticate a Google identity token and issue local tokens.

        Args:
            id_token: Google OpenID Connect identity token.
            db: Database session used to resolve the account.
            redis_pool: Redis connection used to issue token state.
            email: Optional client-supplied email assertion.
            display_name: Optional client-supplied display name.
            device_lang: Optional client device language.
            consent_payload: Required consents for a new account.
            hash_refresh_token: Whether browser refresh tokens are cache-hashed.

        Returns:
            Locally issued token-pair data.
        """
        claims = await self.verifier.verify_google_id_token(id_token)
        if display_name and (not claims.name):
            claims.name = display_name
        if device_lang:
            claims.device_lang = device_lang
        return await self.authenticate_provider_user(
            'google',
            claims,
            db,
            redis_pool,
            consent_payload=consent_payload,
            hash_refresh_token=hash_refresh_token,
            deployment=deployment,
            request=request,
        )

    async def login_with_apple(
        self,
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
        """Authenticate an Apple identity and issue local tokens.

        Args:
            identity_token: Optional Apple identity token.
            authorization_code: Apple authorisation code.
            db: Database session used to resolve the account.
            redis_pool: Redis connection used to issue token state.
            email: Optional client-supplied email assertion.
            given_name: Optional client-supplied given name.
            family_name: Optional client-supplied family name.
            nonce: Optional expected identity-token nonce.
            device_lang: Optional client device language.
            consent_payload: Required consents for a new account.
            hash_refresh_token: Whether browser refresh tokens are cache-hashed.

        Returns:
            Locally issued token-pair data.
        """
        claims = await self.verifier.verify_apple_identity_token(
            identity_token, authorization_code, expected_nonce=nonce,
        )
        if email and (not claims.email):
            claims.email = email
        if given_name and (not claims.given_name):
            claims.given_name = given_name
        if family_name and (not claims.family_name):
            claims.family_name = family_name
        if device_lang:
            claims.device_lang = device_lang
        return await self.authenticate_provider_user(
            'apple',
            claims,
            db,
            redis_pool,
            consent_payload=consent_payload,
            hash_refresh_token=hash_refresh_token,
            deployment=deployment,
            request=request,
        )

    def _user_has_password(self, user: User) -> bool:
        """Return whether the user has a configured password credential.

        Args:
            user: User whose password credential is inspected.

        Returns:
            ``True`` when the user has a usable password hash.
        """
        return not str(user.password_hash).startswith('oauth_disabled:')

    def _identity_read(self, identity: UserIdentity) -> IdentityRead:
        """Convert a persisted provider identity into its public schema.

        Args:
            identity: Persisted provider identity.

        Returns:
            Safe public identity response.
        """
        linked_at = identity.linked_at
        return IdentityRead(
            id=identity.id,
            provider=identity.provider,
            email=identity.email,
            display_name=identity.display_name,
            linked_at=linked_at.isoformat().replace('+00:00', 'Z'),
        )

    async def list_user_identities(
        self, user: User, db: AsyncSession,
    ) -> IdentityListResponse:
        """Return the current user's linked provider identities.

        Args:
            user: Current authenticated user.
            db: Database session used to refresh identity data.

        Returns:
            Public identities and password-credential availability.
        """
        identities = (
            (
                await db.execute(
                    select(UserIdentity)
                    .where(UserIdentity.user_id == user.id)
                    .order_by(UserIdentity.linked_at.asc()),
                )
            )
            .scalars()
            .all()
        )
        return IdentityListResponse(
            identities=[self._identity_read(identity) for identity in identities],
            has_password=self._user_has_password(user),
        )

    async def _find_identity(
        self, db: AsyncSession, provider: Provider, provider_user_id: str,
    ) -> UserIdentity | None:
        """Find a provider identity matching an external subject.

        Args:
            db: Database session used to search identities.
            provider: Identity-provider name.
            provider_user_id: Stable external provider subject.

        Returns:
            Matching identity, or ``None`` when absent.
        """
        return await db.scalar(
            select(UserIdentity).where(
                UserIdentity.provider == provider,
                UserIdentity.provider_user_id == provider_user_id,
            ),
        )

    async def _find_current_user_provider_identity(
        self, db: AsyncSession, user: User, provider: Provider,
    ) -> UserIdentity | None:
        """Find the user's existing identity for one provider.

        Args:
            db: Database session used to search identities.
            user: Local user that owns the identity.
            provider: Identity-provider name.

        Returns:
            Matching identity, or ``None`` when absent.
        """
        return await db.scalar(
            select(UserIdentity).where(
                UserIdentity.user_id == user.id, UserIdentity.provider == provider,
            ),
        )

    def _update_identity_from_claims(
        self, identity: UserIdentity, claims: ProviderClaims,
    ) -> None:
        """Synchronise mutable provider claims onto a persisted identity.

        Args:
            identity: Persisted provider identity to update.
            claims: Newly verified provider claims.
        """
        email = self.verifier.normalise_email(claims.email)
        if email:
            identity.email = email
        identity.email_verified = claims.email_verified
        display_name = self._display_name_from_claims(claims)
        if display_name:
            identity.display_name = display_name
        identity.raw_profile = claims.model_dump()
        identity.raw_email_is_private = claims.is_private_email or bool(
            identity.email and identity.email.endswith('@privaterelay.appleid.com'),
        )

    async def link_provider_identity(
        self, user: User, provider: Provider, claims: ProviderClaims, db: AsyncSession,
    ) -> IdentityRead:
        """Link a verified provider identity to the current user.

        Args:
            user: Authenticated local user receiving the identity.
            provider: Identity-provider name.
            claims: Verified identity-provider claims.
            db: Database session used to persist the link.

        Returns:
            Public linked identity response.
        """
        provider_user_id = claims.sub
        existing = await self._find_identity(db, provider, provider_user_id)
        if existing is not None and existing.user_id != user.id:
            raise HTTPException(
                status_code=409,
                detail={
                    'code': 'identity_already_linked_to_another_user',
                    'message': 'This provider account is already linked to another user.',
                },
            )
        if existing is not None:
            self._update_identity_from_claims(existing, claims)
            await db.commit()
            await db.refresh(existing)
            return self._identity_read(existing)
        current_provider = await self._find_current_user_provider_identity(
            db, user, provider,
        )
        if current_provider is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    'code': 'provider_already_linked',
                    'message': f'This user already has a different account linked for {provider}.',
                },
            )
        identity = self._new_identity(user, provider, claims)
        db.add(identity)
        await db.commit()
        await db.refresh(identity)
        return self._identity_read(identity)

    async def link_google_identity(
        self, user: User, id_token: str, db: AsyncSession,
    ) -> IdentityRead:
        """Verify and link a Google identity token.

        Args:
            user: Authenticated local user receiving the identity.
            id_token: Google OpenID Connect identity token.
            db: Database session used to persist the link.

        Returns:
            Public linked Google identity.
        """
        claims = await self.verifier.verify_google_id_token(id_token)
        return await self.link_provider_identity(user, 'google', claims, db)

    async def link_apple_identity(
        self,
        user: User,
        identity_token: str | None,
        authorization_code: str,
        db: AsyncSession,
        nonce: str | None = None,
    ) -> IdentityRead:
        """Verify and link an Apple identity token.

        Args:
            user: Authenticated local user receiving the identity.
            identity_token: Optional Apple identity token.
            authorization_code: Apple authorisation code.
            db: Database session used to persist the link.
            nonce: Optional expected identity-token nonce.

        Returns:
            Public linked Apple identity.
        """
        claims = await self.verifier.verify_apple_identity_token(
            identity_token, authorization_code, expected_nonce=nonce,
        )
        return await self.link_provider_identity(user, 'apple', claims, db)

    async def unlink_identity(
        self, user: User, identity_id: int, db: AsyncSession,
    ) -> dict[str, str]:
        """Unlink a provider identity while retaining a login method.

        Args:
            user: Authenticated local user that owns the identity.
            identity_id: Identifier of the identity to remove.
            db: Database session used to remove the identity.

        Returns:
            Confirmation message after unlinking.

        Raises:
            HTTPException: If the identity is absent or is the last login method.
        """
        identity = await db.get(UserIdentity, identity_id)
        if identity is None or identity.user_id != user.id:
            raise HTTPException(status_code=404, detail='Identity not found')
        identity_count = await db.scalar(
            select(func.count(UserIdentity.id)).where(UserIdentity.user_id == user.id),
        )
        if not self._user_has_password(user) and int(identity_count or 0) <= 1:
            raise HTTPException(
                status_code=400,
                detail={
                    'code': 'last_login_method',
                    'message': 'Cannot unlink the last login method.',
                },
            )
        await db.delete(identity)
        await db.commit()
        return {'message': 'Identity unlinked successfully.'}
