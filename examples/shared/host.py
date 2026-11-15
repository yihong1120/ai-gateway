"""Optional notification/streaming integration boundary for an existing host."""
from __future__ import annotations

from functools import cached_property
from importlib import import_module

from fastapi import HTTPException


def _load(name):
    try:
        return import_module(name)
    except ModuleNotFoundError as error:
        if not (name == error.name or name.startswith((error.name or '') + '.')):
            raise
        raise HTTPException(503, 'host_integration_not_configured') from error


class HostIntegrations:
    @cached_property
    def notifications(self):
        return _load('examples.local_notification_server.site_recipient_cache')

    @cached_property
    def metadata(self):
        return _load('examples.streaming_web.metadata_keys')

    @cached_property
    def media_paths(self):
        return _load('examples.streaming_web.media_paths')

    @cached_property
    def overlay(self):
        return _load('examples.streaming_web.overlay_renderer')

    def require_sites(self):
        # Fail before any mutation can commit without invalidating host caches.
        for module, names in (
            (self.notifications, ('invalidate_site_notification_user_cache', 'refresh_site_notification_user_cache')),
            (self.metadata, ('increment_metadata_site_generation',)),
        ):
            if any(not callable(getattr(module, name, None)) for name in names):
                raise HTTPException(503, 'host_integration_not_configured')

    def require_streaming(self):
        if any(
            not callable(getattr(self.media_paths, name, None))
            for name in (
                'build_clean_demand_key',
                'build_media_path',
                'build_overlay_demand_key',
                'build_preview_media_path',
            )
        ) or not callable(getattr(self.overlay, 'normalise_label_language', None)):
            raise HTTPException(503, 'host_integration_not_configured')


integrations = HostIntegrations()
