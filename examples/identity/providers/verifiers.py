"""Application-owned ProviderVerifier dependencies and operations."""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import jwt
from fastapi import HTTPException
from pydantic import ValidationError

from examples.identity.schemas.auth import AppleTokenExchangeResponse
from examples.identity.schemas.auth import ProviderClaims
from examples.identity.settings.social import SocialSettings

GOOGLE_ISSUERS = ('accounts.google.com', 'https://accounts.google.com')
GOOGLE_JWKS_URL = 'https://www.googleapis.com/oauth2/v3/certs'
APPLE_ISSUER = 'https://appleid.apple.com'
APPLE_JWKS_URL = 'https://appleid.apple.com/auth/keys'
APPLE_TOKEN_URL = 'https://appleid.apple.com/auth/token'


class ProviderVerifier:
    def __init__(self, settings: SocialSettings, http_clients):
        self.settings, self.http_clients = (settings, http_clients)
        self._jwks: dict[str, jwt.PyJWKClient] = {}

    def _configured_google_client_ids(self) -> list[str]:
        """Return configured Google OAuth audience identifiers.

        Returns:
            Non-empty client IDs accepted in Google identity tokens.
        """
        settings = self.settings
        return [
            value.strip()
            for value in settings.google_client_ids.split(',')
            if value.strip()
        ]

    def _configured_apple_client_ids(self) -> list[str]:
        """Return configured Apple OAuth audience identifiers.

        Returns:
            Non-empty client IDs accepted in Apple identity tokens.
        """
        settings = self.settings
        return [
            value.strip()
            for value in settings.apple_client_ids.split(',')
            if value.strip()
        ]

    def normalise_email(self, email: str | None) -> str | None:
        """Return a trimmed, case-normalised optional email address.

        Args:
            email: Optional email address supplied by an identity provider.

        Returns:
            Normalised email address, or ``None`` when absent.
        """
        if email is None:
            return None
        normalized = email.strip().lower()
        return normalized or None

    def _provider_claims(self, payload: object) -> ProviderClaims:
        """Validate provider claims at the OpenID Connect boundary.

        Args:
            payload: Decoded provider-token claims.

        Returns:
            Strictly validated provider claims.
        """
        try:
            return ProviderClaims.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(
                status_code=401, detail='Invalid provider token',
            ) from exc

    def _jwks_client(self, jwks_url: str) -> jwt.PyJWKClient:
        """Return the application-owned JWKS client for an identity provider."""
        if jwks_url not in self._jwks:
            self._jwks[jwks_url] = jwt.PyJWKClient(jwks_url)
        return self._jwks[jwks_url]

    def _verify_jwt_with_jwks(
        self,
        token: str,
        jwks_url: str,
        audiences: Sequence[str],
        issuers: Sequence[str],
    ) -> ProviderClaims:
        """Verify provider-token claims against JWKS metadata.

        Args:
            token: Raw OpenID Connect identity token.
            jwks_url: Provider JSON Web Key Set URL.
            audiences: Accepted client audiences.
            issuers: Accepted token issuers.

        Returns:
            Verified provider claims.

        Raises:
            HTTPException: If token signature or registered claims are invalid.
        """
        if not audiences:
            raise HTTPException(status_code=500, detail='OAuth client not configured')
        try:
            signing_key = self._jwks_client(jwks_url).get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=['RS256'],
                audience=list(audiences),
                issuer=list(issuers),
            )
        except jwt.PyJWTError as exc:
            raise HTTPException(
                status_code=401, detail='Invalid provider token',
            ) from exc
        return self._provider_claims(payload)

    async def verify_google_id_token(
        self,
        id_token: str,
        *,
        expected_nonce: str | None = None,
        require_verified_email: bool = True,
    ) -> ProviderClaims:
        """Verify Google identity-token signature and registered claims.

        Args:
            id_token: Raw Google OpenID Connect identity token.
            expected_nonce: Optional one-use nonce bound to the native request.
            require_verified_email: Whether this caller requires a verified email.
                Account linking and the Keycloak exchange use the immutable
                provider subject rather than an email address, so they do not
                require an email claim.

        Returns:
            Verified Google provider claims.
        """
        payload = await asyncio.to_thread(
            self._verify_jwt_with_jwks,
            id_token,
            GOOGLE_JWKS_URL,
            self._configured_google_client_ids(),
            GOOGLE_ISSUERS,
        )
        claims = self._provider_claims(payload)
        if expected_nonce is not None and claims.nonce != expected_nonce:
            raise HTTPException(status_code=401, detail='Invalid provider token')
        if require_verified_email and (not claims.email_verified):
            raise HTTPException(status_code=401, detail='Google email is not verified')
        if require_verified_email and (not self.normalise_email(claims.email)):
            raise HTTPException(
                status_code=401, detail='Google account did not return an email address',
            )
        return claims

    def _load_apple_private_key(self) -> str:
        """Load the configured Apple signing private key.

        Returns:
            PEM-encoded Apple signing key.

        Raises:
            HTTPException: If the required Apple key configuration is unavailable.
        """
        settings = self.settings
        if settings.apple_private_key:
            return settings.apple_private_key.replace('\\n', '\n')
        if settings.apple_private_key_path:
            with open(settings.apple_private_key_path, encoding='utf-8') as file:
                return file.read()
        raise HTTPException(
            status_code=500, detail='Apple client secret is not configured',
        )

    def _build_apple_client_secret(self, client_id: str) -> str:
        """Build a signed Apple client-secret JWT for a client.

        Args:
            client_id: Apple Services ID or bundle identifier.

        Returns:
            Signed short-lived Apple client-secret JWT.
        """
        settings = self.settings
        if not settings.apple_team_id or not settings.apple_key_id:
            raise HTTPException(
                status_code=500, detail='Apple client secret is not configured',
            )
        now = datetime.now(timezone.utc)
        return jwt.encode(
            {
                'iss': settings.apple_team_id,
                'iat': int(now.timestamp()),
                'exp': int((now + timedelta(days=180)).timestamp()),
                'aud': APPLE_ISSUER,
                'sub': client_id,
            },
            self._load_apple_private_key(),
            algorithm='ES256',
            headers={'kid': settings.apple_key_id},
        )

    async def verify_apple_identity_token(
        self,
        identity_token: str | None,
        authorization_code: str,
        expected_nonce: str | None = None,
    ) -> ProviderClaims:
        """Verify Apple identity token and validate its authorisation code.

        Args:
            identity_token: Optional Apple OpenID Connect identity token.
            authorization_code: Apple authorisation code to validate.
            expected_nonce: Optional nonce expected in token claims.

        Returns:
            Verified Apple provider claims.
        """
        client_ids = self._configured_apple_client_ids()
        payload: ProviderClaims | None = None
        if identity_token:
            payload = self._provider_claims(
                await asyncio.to_thread(
                    self._verify_jwt_with_jwks,
                    identity_token,
                    APPLE_JWKS_URL,
                    client_ids,
                    (APPLE_ISSUER,),
                ),
            )
            client_id = payload.aud
            if client_id not in client_ids:
                raise HTTPException(status_code=401, detail='Invalid provider token')
            token_response = await self._exchange_apple_authorization_code(
                authorization_code, [client_id],
            )
        else:
            token_response = await self._exchange_apple_authorization_code(
                authorization_code, self._apple_exchange_client_id_candidates(),
            )
        token_response = AppleTokenExchangeResponse.model_validate(token_response)
        if token_response.id_token is not None:
            exchanged_payload = self._provider_claims(
                await asyncio.to_thread(
                    self._verify_jwt_with_jwks,
                    token_response.id_token,
                    APPLE_JWKS_URL,
                    client_ids,
                    (APPLE_ISSUER,),
                ),
            )
            if payload is None:
                payload = exchanged_payload
            elif exchanged_payload.sub != payload.sub:
                raise HTTPException(status_code=401, detail='Invalid provider token')
        if payload is None:
            raise HTTPException(status_code=401, detail='Invalid provider token')
        if expected_nonce and payload.nonce != expected_nonce:
            raise HTTPException(status_code=401, detail='Invalid provider token')
        return payload

    def _apple_exchange_client_id_candidates(self) -> list[str]:
        """Try web/service ID first, then native bundle ID for Apple code
        exchange."""
        settings = self.settings
        candidates = [
            settings.apple_service_id,
            settings.apple_bundle_id,
            *self._configured_apple_client_ids(),
        ]
        deduped: list[str] = []
        for candidate in candidates:
            if candidate and candidate not in deduped:
                deduped.append(candidate)
        return deduped

    async def _exchange_apple_authorization_code(
        self, authorization_code: str, client_ids: Sequence[str],
    ) -> AppleTokenExchangeResponse:
        """Validate an Apple authorisation code against an allowed client ID.

        Args:
            authorization_code: Apple authorisation code to exchange.
            client_ids: Candidate allowed Apple client IDs.

        Returns:
            Validated Apple token-exchange response.
        """
        last_error: HTTPException | None = None
        for client_id in client_ids:
            try:
                return AppleTokenExchangeResponse.model_validate(
                    await self._exchange_apple_authorization_code_once(
                        authorization_code, client_id,
                    ),
                )
            except HTTPException as exc:
                last_error = exc
            except ValidationError as exc:
                raise HTTPException(
                    status_code=401, detail='Invalid provider token',
                ) from exc
        if last_error is not None:
            raise last_error
        raise HTTPException(status_code=500, detail='Apple client not configured')

    async def _exchange_apple_authorization_code_once(
        self, authorization_code: str, client_id: str,
    ) -> AppleTokenExchangeResponse:
        """Exchange an Apple authorisation code for provider tokens once.

        Args:
            authorization_code: Apple authorisation code to exchange.
            client_id: Apple client ID bound to the code.

        Returns:
            Validated Apple token-exchange response.
        """
        settings = self.settings
        data = {
            'client_id': client_id,
            'client_secret': self._build_apple_client_secret(client_id),
            'code': authorization_code,
            'grant_type': 'authorization_code',
        }
        if client_id == settings.apple_service_id:
            data['redirect_uri'] = settings.apple_redirect_uri
        client = await self.http_clients.get('apple-token-exchange')
        response = await client.post(APPLE_TOKEN_URL, data=data)
        if response.status_code >= 400:
            raise HTTPException(status_code=401, detail='Invalid provider token')
        try:
            return AppleTokenExchangeResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise HTTPException(
                status_code=401, detail='Invalid provider token',
            ) from exc
