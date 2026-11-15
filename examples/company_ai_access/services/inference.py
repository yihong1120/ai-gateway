"""Bounded inference forwarding with sanitized errors and stream cleanup."""
from __future__ import annotations

import json

import httpx
from anyio import CancelScope
from fastapi.responses import StreamingResponse

from examples.company_ai_access.errors import error


class InferenceProxy:
    def __init__(self, settings, client):
        self.settings, self.client = settings, client

    async def forward(self, request, raw, device, body, path):
        body['metadata'] = {'user_id': device['subject']}
        body['litellm_metadata'] = {'user': device['subject']}
        headers = {
            'Authorization': 'Bearer ' + raw,
            'Content-Type': 'application/json',
            'x-litellm-customer-id': device['subject'],
        }
        for name in ('anthropic-version', 'anthropic-beta'):
            if path.startswith('/v1/messages') and name in request.headers:
                headers[name] = request.headers[name]
        try:
            upstream = await self.client.send(
                self.client.build_request(
                    'POST',
                    self.settings.litellm_url + path,
                    headers=headers,
                    json=body,
                    timeout=httpx.Timeout(600, connect=10),
                ),
                stream=True,
            )
        except httpx.HTTPError:
            raise error(503, 'ai_access_inference_unavailable')
        if not upstream.is_success:
            status = upstream.status_code if upstream.status_code in {400, 401, 403, 404, 413, 422, 429} else 502
            code = 'ai_access_upstream_rejected'
            try:
                # Consume only bounded error responses. Never expose provider
                # messages, account IDs, keys or upstream URLs to the client.
                content = bytearray()
                async for block in upstream.aiter_bytes():
                    content.extend(block)
                    if len(content) > 65536:
                        break
                payload = json.loads(content)
                if (
                    isinstance(payload, dict)
                    and isinstance(payload.get('error'), dict)
                    and payload['error'].get('type') == 'budget_exceeded'
                ):
                    code = 'ai_access_budget_exceeded'
            except (ValueError, httpx.HTTPError):
                pass
            finally:
                with CancelScope(shield=True):
                    await upstream.aclose()
            raise error(status, code)

        return stream_response(upstream)


def stream_response(upstream, headers=None):
    async def chunks():
        try:
            async for block in upstream.aiter_bytes():
                yield block
        finally:
            with CancelScope(shield=True):
                await upstream.aclose()

    return StreamingResponse(
        chunks(),
        status_code=upstream.status_code,
        headers={
            'Content-Type': upstream.headers.get('content-type', 'application/json'),
            'Cache-Control': 'no-store',
            'X-Accel-Buffering': 'no',
            **(headers or {}),
        },
    )
