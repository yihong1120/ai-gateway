"""Private response headers and the host authentication dependency."""
from __future__ import annotations

from fastapi.routing import APIRoute
from open_webui.utils.auth import get_verified_user as active


__all__ = ['PrivateRoute', 'active']


class PrivateRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request):
            response = await handler(request)
            response.headers.update({
                'Cache-Control': 'no-store',
                'X-Content-Type-Options': 'nosniff',
                'Referrer-Policy': 'no-referrer',
            })
            return response

        return handle
