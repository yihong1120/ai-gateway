from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from fastapi import Response
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from examples.db_management.schemas.playback import PlaybackRenewRequest
from examples.db_management.schemas.playback import PlaybackSessionRequest
from examples.db_management.schemas.playback import PlaybackWallRequest
from examples.identity.database import get_db
from examples.identity.redis_pool import get_redis_pool
from examples.shared.host import integrations

router = APIRouter(prefix='/api/playback', tags=['playback'], dependencies=[Depends(integrations.require_streaming)])


@router.post('/sessions')
async def create_playback_session(
    payload: PlaybackSessionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis_pool),
) -> JSONResponse:
    """Create one signed single-camera playback session."""
    return await request.app.state.playback.playback_session_response(payload, request, redis, db)


@router.post('/walls')
async def create_playback_wall(
    payload: PlaybackWallRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis_pool),
) -> JSONResponse:
    """Create signed playback sessions for a camera wall."""
    return await request.app.state.playback.playback_wall_response(payload, request, redis, db)


@router.post('/sessions/renew')
async def renew_playback_session(
    payload: PlaybackRenewRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis_pool),
) -> JSONResponse:
    """Renew the signed URLs for a playback session."""
    return await request.app.state.playback.renew_playback_response(payload, request, redis, db)


@router.delete('/sessions/{session_id}', status_code=204)
async def delete_playback_session(
    session_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis_pool),
) -> Response:
    """Delete a public playback session."""
    return await request.app.state.playback.delete_playback_response(session_id, request, redis, db)
