from __future__ import annotations
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from fastapi import HTTPException
from examples.db_management.schemas.playback import PlaybackProfile, PlaybackWallRequest

PLAYBACK_PUBLIC_BASE_PATH = '/hazard/api/db_management/api/playback'


def _normalise_profile(profile: PlaybackProfile | str | None) -> str:
    """Normalise a playback profile to a supported render mode.

    Args:
        profile: Requested profile value from client or stored state.

    Returns:
        Normalised ``clean`` or ``overlay`` profile.

    Raises:
        HTTPException: If the supplied profile is unsupported.
    """
    value = (profile or 'clean').strip().lower()
    if value not in {'clean', 'overlay'}:
        raise HTTPException(status_code=422, detail='unsupported_profile')
    return value


def _with_media_token(url: object, media_token: str) -> str:
    """Attach the scoped media token to a playback URL.

    Args:
        url: Upstream playback URL to sign.
        media_token: Scoped token authorising access to the media session.

    Returns:
        URL with stale media-token parameters removed and the new token added.
    """
    parts = urlsplit(str(url))
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in {'mt', 'media_token'}
    ]
    query.append(('mt', media_token))
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(
                query,
            ),
            parts.fragment,
        ),
    )


def _signed_stream_item(
    item: dict[str, object],
    media_token: str,
) -> dict[str, object]:
    """Add the scoped media token to HLS and playback URLs.

    Args:
        item: Upstream stream item containing media URLs.
        media_token: Scoped token authorising access to the media session.

    Returns:
        Copy of the stream item with signed HLS and playback URLs.
    """
    signed = dict(item)
    media_hls_url = _with_media_token(
        item.get('media_hls_url', ''),
        media_token,
    )
    signed['media_hls_url'] = media_hls_url
    if item.get('playback_url'):
        signed['playback_url'] = _with_media_token(
            item['playback_url'],
            media_token,
        )
        signed['hls_url'] = signed['playback_url']
    else:
        signed['hls_url'] = media_hls_url
    return signed


def _playback_endpoints() -> dict[str, str]:
    """Return the public playback endpoint URLs.

    Returns:
        Mapping of single-session, wall, and renewal endpoint URLs.
    """
    return {
        'single_endpoint': f'{PLAYBACK_PUBLIC_BASE_PATH}/sessions',
        'wall_endpoint': f'{PLAYBACK_PUBLIC_BASE_PATH}/walls',
        'renew_endpoint': f'{PLAYBACK_PUBLIC_BASE_PATH}/sessions/renew',
    }


def _wall_upstream_payload(
    payload: PlaybackWallRequest,
    profile: str,
) -> dict[str, object]:
    """Build the streaming-service request for a preview playback wall.

    Args:
        payload: Validated multi-camera wall request.
        profile: Normalised clean or overlay render profile.

    Returns:
        Upstream request body for either default or explicitly scoped cameras.
    """
    body: dict[str, object] = {
        'label': payload.site,
        'profile': profile,
        'rendition': 'preview',
        'language': payload.language,
        'transport': payload.transport,
    }
    if payload.cameras:
        del body['label']
        body['streams'] = [
            {
                'label': payload.site,
                'key': camera,
                'profile': profile,
                'rendition': 'preview',
                'language': payload.language,
                'transport': payload.transport,
            }
            for camera in payload.cameras
        ]
    return body


class PlaybackResponses:
    def __init__(self, ttl_seconds):
        self.ttl_seconds = ttl_seconds

    def single(
        self,
        *,
        media_session: dict[str, object],
        stream_item: dict[str, object],
        media_token: str,
        site: str,
        camera: str,
    ) -> dict[str, object]:
        """Build the public response body for one playback stream.

        Args:
            media_session: Persisted scoped media session.
            stream_item: Validated upstream stream item.
            media_token: Scoped token used to sign media URLs.
            site: Requested site label.
            camera: Resolved camera key.

        Returns:
            Single-camera playback response body.
        """
        signed = _signed_stream_item(stream_item, media_token)
        return {
            **_playback_endpoints(),
            **signed,
            'id': str(media_session['id']),
            'mode': 'single',
            'site': site,
            'camera': camera,
            'title': camera,
            'quality': 'detail',
            'token_transport': 'query',
            'expires_in': self.ttl_seconds,
        }

    def wall(
        self,
        *,
        media_session: dict[str, object],
        stream_items: list[dict[str, object]],
        media_token: str,
        site: str,
        profile: str,
        max_streams: int | None,
    ) -> dict[str, object]:
        """Build the public response body for a multi-camera playback wall.

        Args:
            media_session: Persisted scoped media session.
            stream_items: Validated upstream stream items.
            media_token: Scoped token used to sign media URLs.
            site: Requested site label.
            profile: Normalised render profile.
            max_streams: Upstream maximum concurrent streams.

        Returns:
            Responsive multi-camera playback response body.
        """
        items: list[dict[str, object]] = []
        for item in stream_items:
            signed = _signed_stream_item(item, media_token)
            camera = str(signed.get('key') or '')
            items.append(
                {
                    'camera': camera,
                    'title': camera,
                    'detail_camera': camera,
                    'stream_id': signed.get('stream_id'),
                    'session_id': signed.get('session_id'),
                    'status': signed.get('status'),
                    'state': signed.get('state'),
                    'profile': signed.get('profile'),
                    'language': signed.get('language'),
                    'overlay_ready': signed.get('overlay_ready'),
                    'preview_hls_url': signed['hls_url'],
                    'hls_url': signed['hls_url'],
                    'playback_url': signed.get('playback_url'),
                },
            )
        return {
            **_playback_endpoints(),
            'id': str(media_session['id']),
            'mode': 'multi_stream',
            'layout': 'responsive',
            'site': site,
            'quality': 'preview',
            'profile': profile,
            'token_transport': 'query',
            'expires_in': self.ttl_seconds,
            'count': len(items),
            'max_streams': max_streams,
            'items': items,
        }
