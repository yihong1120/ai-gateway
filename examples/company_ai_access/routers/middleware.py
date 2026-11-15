"""Bound JSON input before FastAPI parses credential-bearing request bodies."""
from __future__ import annotations

from fastapi import HTTPException


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('method') != 'POST':
            return await self.app(scope, receive, send)
        path = scope.get('path', '')
        maximum = 8 * 1024 * 1024 if path.startswith('/company-ai/v1/') else 16000
        total = 0

        async def limited_receive():
            nonlocal total
            message = await receive()
            if message['type'] == 'http.request':
                total += len(message.get('body', b''))
                if total > maximum:
                    raise HTTPException(413, detail={'code': 'ai_access_request_too_large'})
            return message
        await self.app(scope, limited_receive, send)
