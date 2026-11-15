from __future__ import annotations

from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from .identity import PlaybackIdentity
from .identity import PlaybackPrincipal
from .media import _streaming_session_descriptors
from .media import MediaSessions
from .rendering import _normalise_profile
from .rendering import _playback_endpoints
from .rendering import _wall_upstream_payload
from .rendering import PlaybackResponses
from .upstream import PlaybackUpstream
from examples.db_management.schemas.playback import PlaybackRenewRequest
from examples.db_management.schemas.playback import PlaybackSessionRequest
from examples.db_management.schemas.playback import PlaybackWallRequest
from examples.db_management.schemas.playback import StreamingPlaybackBatchResponse
from examples.db_management.schemas.playback import StreamingPlaybackItem


class PlaybackService:
    def __init__(self, identity, upstream, media, responses):
        self.identity, self.upstream = identity, upstream
        self.media, self.responses = media, responses

    async def create_single(
        self,
        payload: PlaybackSessionRequest,
        principal: PlaybackPrincipal,
        redis: Redis,
    ) -> tuple[dict[str, object], int]:
        """Create one detail-quality playback media capability.

        Args:
            payload: Validated single-camera playback request.
            principal: Authenticated caller that will own the session.
            redis: Redis connection used to create media-session state.

        Returns:
            Signed single-playback body and upstream HTTP status code.

        Raises:
            HTTPException: If upstream validation or media-session creation fails.
        """
        profile = _normalise_profile(payload.profile)
        stream_item, status_code = await self.upstream.post(
            '/stream-playback',
            principal=principal,
            payload={
                'label': payload.site,
                'key': payload.camera,
                'session_id': payload.session_id,
                'profile': profile,
                'rendition': 'detail',
                'language': payload.language,
                'transport': payload.transport,
            },
        )
        try:
            stream = StreamingPlaybackItem.model_validate(stream_item)
        except ValidationError as exc:
            raise HTTPException(
                status_code=502,
                detail='invalid_streaming_upstream_response',
            ) from exc
        token, session = await self.media.create(
            redis,
            principal=principal,
            site=payload.site,
            cameras=[stream.key],
            profile=profile,
            quality='detail',
            language=stream.language or payload.language,
            playback_sessions=_streaming_session_descriptors([stream]),
        )
        return (
            self.responses.single(
                media_session=session,
                stream_item=stream.model_dump(),
                media_token=token,
                site=payload.site,
                camera=stream.key,
            ),
            status_code,
        )

    async def create_wall(
        self,
        payload: PlaybackWallRequest,
        principal: PlaybackPrincipal,
        redis: Redis,
    ) -> tuple[dict[str, object], int]:
        """Create a preview-quality media capability for a camera wall.

        Args:
            payload: Validated multi-camera playback request.
            principal: Authenticated caller that will own the session.
            redis: Redis connection used to create media-session state.

        Returns:
            Signed wall-playback body and upstream HTTP status code.

        Raises:
            HTTPException: If no cameras are authorised, upstream validation fails,
                or media-session creation fails.
        """
        profile = _normalise_profile(payload.profile)
        upstream, status_code = await self.upstream.post(
            '/stream-playback/batch',
            principal=principal,
            payload=_wall_upstream_payload(payload, profile),
        )
        try:
            batch = StreamingPlaybackBatchResponse.model_validate(upstream)
        except ValidationError as exc:
            raise HTTPException(
                status_code=502,
                detail='invalid_streaming_upstream_response',
            ) from exc
        streams = batch.items
        cameras = [stream.key for stream in streams]
        if not cameras:
            raise HTTPException(status_code=404, detail='cameras_not_found')
        token, session = await self.media.create(
            redis,
            principal=principal,
            site=payload.site,
            cameras=cameras,
            profile=profile,
            quality='preview',
            language=next(
                (stream.language for stream in streams if stream.language is not None),
                payload.language,
            ),
            playback_sessions=_streaming_session_descriptors(streams),
        )
        return (
            self.responses.wall(
                media_session=session,
                stream_items=[stream.model_dump() for stream in streams],
                media_token=token,
                site=payload.site,
                profile=profile,
                max_streams=batch.max_streams,
            ),
            status_code,
        )

    async def playback_session_response(
        self,
        payload: PlaybackSessionRequest,
        request: Request,
        redis: Redis,
        db: AsyncSession,
    ) -> JSONResponse:
        """Create a signed single-camera playback response.

        Args:
            payload: Validated single-camera playback request.
            request: HTTP request used to authenticate the caller.
            redis: Redis connection used for authentication and media state.

        Returns:
            Non-cacheable signed single-camera playback response.
        """
        principal = await self.identity.resolve(request, redis, db)
        body, status_code = await self.create_single(
            payload,
            principal,
            redis,
        )
        return JSONResponse(
            body,
            status_code=status_code,
            headers={'Cache-Control': 'no-store'},
        )

    async def playback_wall_response(
        self,
        payload: PlaybackWallRequest,
        request: Request,
        redis: Redis,
        db: AsyncSession,
    ) -> JSONResponse:
        """Create signed playback responses for a camera wall.

        Args:
            payload: Validated multi-camera playback request.
            request: HTTP request used to authenticate the caller.
            redis: Redis connection used for authentication and media state.

        Returns:
            Non-cacheable signed multi-camera playback response.
        """
        principal = await self.identity.resolve(request, redis, db)
        body, status_code = await self.create_wall(
            payload,
            principal,
            redis,
        )
        return JSONResponse(
            body,
            status_code=status_code,
            headers={'Cache-Control': 'no-store'},
        )

    async def renew_playback_response(
        self,
        payload: PlaybackRenewRequest,
        request: Request,
        redis: Redis,
        db: AsyncSession,
    ) -> JSONResponse:
        """Renew an existing signed playback session.

        Args:
            payload: Public identifier of the media session to renew.
            request: HTTP request used to authenticate the caller.
            redis: Redis connection holding authentication and media state.

        Returns:
            Non-cacheable response confirming renewal without changing HLS URLs.

        Raises:
            HTTPException: If the session expired or is not owned by the caller.
        """
        principal = await self.identity.resolve(request, redis, db)
        current = await self.media.renew(
            redis,
            payload.id,
            principal.parent,
        )
        if current is None:
            raise HTTPException(status_code=401, detail='expired_media_session')
        return JSONResponse(
            {
                'id': str(current['id']),
                'mode': (
                    'multi_stream' if current.get('scope') == 'batch' else 'single'
                ),
                'renew_endpoint': _playback_endpoints()['renew_endpoint'],
                'expires_in': self.responses.ttl_seconds,
                'renewed': True,
                'hls_urls_changed': False,
            },
            headers={'Cache-Control': 'no-store'},
        )

    async def delete_playback_response(
        self,
        session_id: str,
        request: Request,
        redis: Redis,
        db: AsyncSession,
    ) -> Response:
        """Delete a signed playback session owned by the caller.

        Args:
            session_id: Public media-session identifier to delete.
            request: HTTP request used to authenticate the caller.
            redis: Redis connection holding authentication and media state.

        Returns:
            Empty non-cacheable successful deletion response.

        Raises:
            HTTPException: If the session is unavailable or not owned by the
                caller.
        """
        principal = await self.identity.resolve(request, redis, db)
        if not await self.media.delete(
            redis,
            session_id,
            principal.parent,
        ):
            raise HTTPException(status_code=404, detail='session_not_found')
        return Response(status_code=204, headers={'Cache-Control': 'no-store'})


def create_playback_service(resources):
    """Bind the host's identity and transport dependencies once at app creation."""

    return PlaybackService(
        PlaybackIdentity(resources.jwt_access, resources.bff),
        PlaybackUpstream(resources.settings.resources, resources.http_clients),
        MediaSessions(), PlaybackResponses(resources.media_ttl_seconds),
    )
