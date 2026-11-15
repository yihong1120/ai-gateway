from __future__ import annotations

import httpx
from fastapi import HTTPException
from pydantic import TypeAdapter
from pydantic import ValidationError

from examples.db_management.schemas.playback import StreamingPlaybackErrorResponse

_STREAMING_RESPONSE = TypeAdapter(dict[str, object])


def _streaming_detail(response: httpx.Response) -> object:
    """Extract meaningful error detail from a streaming-service response.

    Args:
        response: Upstream HTTP response containing an error body.

    Returns:
        Parsed structured detail, raw JSON body, or response text.
    """
    try:
        body = response.json()
    except ValueError:
        return response.text or 'streaming_upstream_error'
    try:
        return StreamingPlaybackErrorResponse.model_validate(body).detail
    except ValidationError:
        return body


class PlaybackUpstream:
    def __init__(self, settings, clients):
        self.url, self.clients = settings.playback_api_url.rstrip('/'), clients

    async def post(self, path, *, principal, payload):
        client = await self.clients.get('streaming-playback')
        try:
            response = await client.post(
                self.url + path,
                json=payload,
                headers={'Authorization': f'Bearer {principal.access_token}'},
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise HTTPException(
                status_code=502, detail='streaming_upstream_unavailable',
            ) from exc
        if response.status_code >= 400:
            raise HTTPException(
                status_code=response.status_code, detail=_streaming_detail(response),
            )
        try:
            return _STREAMING_RESPONSE.validate_python(
                response.json(),
            ), response.status_code
        except (ValueError, ValidationError) as exc:
            raise HTTPException(
                status_code=502, detail='invalid_streaming_upstream_response',
            ) from exc
