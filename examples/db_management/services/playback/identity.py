from __future__ import annotations
from dataclasses import dataclass
from fastapi import HTTPException, Request
from jwt.exceptions import InvalidTokenError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession
from examples.identity.deployment_context import DeploymentBinding, resolve_request_deployment
from examples.identity.sessions.jwt import JwtAuthorizationCredentials
from examples.identity.sessions.store import auth_session_key, get_auth_session
from examples.identity.sessions.revocation import is_access_token_revoked
from examples.bff.proxy import get_proxy_access_token
from examples.bff.security import check_csrf


@dataclass(frozen=True)
class PlaybackPrincipal:
    """Represent an authenticated identity authorised for playback.

    Attributes:
        username: Authenticated account username.
        user_id: Database identifier of the authenticated user.
        parent: Session or native-user namespace owning media sessions.
        platform: Client platform used to select security behaviour.
        access_token: Validated access token forwarded to the streaming
            service.
    """

    username: str
    user_id: int
    parent: str
    platform: str
    access_token: str


def _bearer_token(request: Request) -> str | None:
    """Extract a bearer token from an API request.

    Args:
        request: Request whose authorisation header is inspected.

    Returns:
        Stripped bearer token, or ``None`` for a non-bearer header.
    """
    scheme, _, value = request.headers.get('authorization', '').partition(' ')
    return value.strip() if scheme.lower() == 'bearer' and value else None


class PlaybackIdentity:
    def __init__(self, jwt, bff):
        self.jwt, self.bff = jwt, bff

    async def _decode_access_token(
        self,
        token: str,
        redis: Redis,
        db: AsyncSession,
        deployment: DeploymentBinding,
    ) -> JwtAuthorizationCredentials:
        """Validate a non-revoked access token for playback.

        Args:
            token: Raw bearer access token.
            redis: Redis connection used for revocation checks.

        Returns:
            Decoded credentials with validated subject and payload.

        Raises:
            HTTPException: If the token is invalid, revoked, or revocation state is
                unavailable.
        """
        try:
            credentials = await self.jwt.decode_access_token_for_deployment(
                token,
                db,
                deployment,
            )
        except InvalidTokenError as exc:
            raise HTTPException(
                status_code=401,
                detail='Could not validate credentials',
                headers={'WWW-Authenticate': 'Bearer'},
            ) from exc
        try:
            if await is_access_token_revoked(
                redis,
                {'jti': credentials.subject['jti']},
            ):
                raise HTTPException(
                    status_code=401,
                    detail='Could not validate credentials',
                    headers={'WWW-Authenticate': 'Bearer'},
                )
        except RedisError as exc:
            raise HTTPException(
                status_code=503,
                detail='Authentication revocation service unavailable',
            ) from exc
        return credentials

    async def resolve(
        self,
        request: Request,
        redis: Redis,
        db: AsyncSession,
    ) -> PlaybackPrincipal:
        """Resolve native bearer or BFF-cookie credentials to playback scope.

        Args:
            request: Request containing a bearer token or BFF session cookie.
            redis: Redis connection holding session and revocation state.

        Returns:
            Authenticated playback principal bound to a media-session owner.

        Raises:
            HTTPException: If authentication, session lookup, or CSRF validation
                fails.
        """
        deployment = await resolve_request_deployment(request, db)
        bearer = _bearer_token(request)
        if bearer:
            credentials = await self._decode_access_token(
                bearer,
                redis,
                db,
                deployment,
            )
            user_id = credentials.subject['user_id']
            return PlaybackPrincipal(
                username=credentials.subject['username'],
                user_id=user_id,
                parent=f'native:user:{user_id}',
                platform='native',
                access_token=bearer,
            )
        session_id = request.cookies.get(self.bff.cookie_name)
        app_session = await get_auth_session(redis, session_id)
        if not session_id or app_session is None:
            raise HTTPException(status_code=401, detail='app_session_expired')
        check_csrf(request, app_session, request.headers.get('x-csrf-token'))
        access_token, _ = await get_proxy_access_token(
            redis,
            session_id,
            deployment=deployment,
        )
        credentials = await self._decode_access_token(
            access_token,
            redis,
            db,
            deployment,
        )
        return PlaybackPrincipal(
            username=credentials.subject['username'],
            user_id=credentials.subject['user_id'],
            parent=auth_session_key(session_id),
            platform='web',
            access_token=access_token,
        )
