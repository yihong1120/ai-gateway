"""Shared same-origin browser body and private response contract."""
from __future__ import annotations

import json
import os

from fastapi import HTTPException
from fastapi.routing import APIRoute


class PrivateResponseHandler:
    def __init__(self, handler):
        self.handler = handler

    async def __call__(self, request):
        response = await self.handler(request)
        response.headers.update({
            'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer',
        })
        return response


class PrivateRoute(APIRoute):
    def get_route_handler(self):
        return PrivateResponseHandler(super().get_route_handler())


async def data(request):
    expected = os.getenv('EIP_WEBUI_ORIGIN', str(request.base_url).rstrip('/'))
    if request.headers.get('origin') != expected or request.headers.get('x-company-action') != '1':
        raise HTTPException(403, '請從本站工作頁操作。')
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > 16000:
            raise HTTPException(413, '請求過大。')
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError()
    return value
