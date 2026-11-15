from __future__ import annotations

from typing import Any

from redis.asyncio import Redis

from .identity import PlaybackPrincipal
from examples.db_management.schemas.playback import StreamingPlaybackItem
from examples.identity.sessions.store import create_media_session
from examples.identity.sessions.store import delete_media_session
from examples.identity.sessions.store import renew_media_session
from examples.shared.host import integrations


def _streaming_session_descriptors(
    stream_items: list[StreamingPlaybackItem],
) -> dict[str, dict[str, object]]:
    """Build trusted upstream session data for later HLS recovery.

    Args:
        stream_items: Validated stream items supplied by the upstream service.

    Returns:
        Mapping keyed by upstream session identifier.
    """
    return {
        item.session_id: {
            'label': item.label,
            'stream_name': item.key,
            'profile': item.profile,
            'rendition': item.rendition,
            'language': item.language,
        }
        for item in stream_items
    }


def _playback_demand_keys(
    *,
    site: str,
    cameras: list[str],
    profile: str,
    quality: str,
    language: str | None,
) -> list[str]:
    """Build producer-demand leases for a scoped playback capability.

    Args:
        site: Site containing the requested cameras.
        cameras: Camera keys authorised by the upstream response.
        profile: Normalised clean or overlay render profile.
        quality: Detail or preview rendition quality.
        language: Optional label language for overlay rendering.

    Returns:
        Exact demand keys required while the media session remains active.

    Raises:
        ValueError: If an unsupported profile reaches this internal helper.
    """
    paths = [
        integrations.media_paths.build_media_path(site, camera) for camera in cameras
    ]
    if quality == 'preview':
        paths = [
            integrations.media_paths.build_preview_media_path(path) for path in paths
        ]
    if profile == 'clean':
        return [integrations.media_paths.build_clean_demand_key(path) for path in paths]
    if profile == 'overlay':
        label_language = integrations.overlay.normalise_label_language(language)
        return [
            integrations.media_paths.build_overlay_demand_key(path, label_language)
            for path in paths
        ]
    raise ValueError(f'unsupported playback profile: {profile}')


class MediaSessions:
    async def renew(self, redis, key, owner):
        return await renew_media_session(redis, key, owner=owner)

    async def delete(self, redis, key, owner):
        return await delete_media_session(redis, key, owner=owner)

    async def create(
        self,
        redis: Redis,
        *,
        principal: PlaybackPrincipal,
        site: str,
        cameras: list[str],
        profile: str,
        quality: str,
        language: str | None,
        playback_sessions: dict[str, dict[str, object]],
    ) -> tuple[str, dict[str, Any]]:
        """Create a media session scoped to validated playback streams.

        Args:
            redis: Redis connection used to store the media session.
            principal: Authenticated caller that owns the media session.
            site: Site containing the authorised cameras.
            cameras: Camera keys authorised by the upstream response.
            profile: Normalised clean or overlay render profile.
            quality: Detail or preview rendition quality.
            language: Optional label language for overlay rendering.
            playback_sessions: Trusted upstream sessions bound to this capability.

        Returns:
            Scoped media token and the persisted media-session record.
        """
        kwargs: dict[str, Any] = {
            'user_id': principal.user_id,
            'username': principal.username,
            'site': site,
            'profile': profile,
            'parent': principal.parent,
            'platform': principal.platform,
            'language': language,
            'quality': quality,
            'purpose': 'playback',
            'demand_keys': _playback_demand_keys(
                site=site,
                cameras=cameras,
                profile=profile,
                quality=quality,
                language=language,
            ),
            'playback_sessions': playback_sessions,
        }
        if len(cameras) == 1:
            kwargs['camera'] = cameras[0]
        else:
            kwargs['cameras'] = cameras
        return await create_media_session(redis, **kwargs)
