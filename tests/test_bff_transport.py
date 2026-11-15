"""Failed requests release disposable clients while pooled clients stay app-owned."""

from contextlib import asynccontextmanager
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import anyio
import httpx
from fastapi import HTTPException, Request

from examples.bff import proxy_http, proxy_streams
from examples.identity.config import Settings
from examples.identity.runtime import IdentityResources, identity_scope
from examples.shared.runtime.http_clients import HttpClientPool


class ClosingTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self.handler = handler
        self.closed = False

    async def handle_async_request(self, request):
        return await self.handler(request)

    async def aclose(self):
        await anyio.sleep(0)
        self.closed = True


class EventStream(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False

    async def __aiter__(self):
        yield b'data: first\n\n'
        yield b'data: second\n\n'

    async def aclose(self):
        await anyio.sleep(0)
        self.closed = True


class BffTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'PYTHON_DOTENV_DISABLED': '1'}, clear=True))
        self.resources = IdentityResources(Settings(authjwt_secret_key='synthetic-secret'))
        self.enterContext(identity_scope(self.resources))
        self.addAsyncCleanup(self.resources.http_clients.close)

    @asynccontextmanager
    async def upstream(self, handler, *, pooled):
        transport = ClosingTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        pool = HttpClientPool() if pooled else None
        request = Request({
            'type': 'http', 'method': 'GET', 'path': '/bff/chat/events',
            'scheme': 'https', 'server': ('site.test', 443), 'query_string': b'',
            'client': ('192.0.2.1', 123),
            'headers': [(b'cookie', b'browser=secret'), (b'authorization', b'Bearer browser')],
            'app': SimpleNamespace(state=SimpleNamespace(http_clients=pool)),
        }, receive=self.empty_body)
        with patch.object(httpx, 'AsyncClient', return_value=client):
            try:
                yield request, client, transport
            finally:
                if pool:
                    await pool.close()
                elif not client.is_closed:
                    await client.aclose()

    @staticmethod
    async def empty_body():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def test_protocol_errors_keep_existing_502_and_close_only_owned_clients(self):
        async def reject(request):
            raise httpx.RemoteProtocolError('synthetic upstream protocol failure')

        for operation in (proxy_http._send_proxy_request, proxy_streams._open_proxy_stream):
            for pooled in (False, True):
                with self.subTest(operation=operation.__name__, pooled=pooled):
                    async with self.upstream(reject, pooled=pooled) as (request, client, transport):
                        with self.assertRaises(HTTPException) as error:
                            await operation(request, 'https://upstream.test/events', 'server-token')
                        self.assertEqual(error.exception.status_code, 502)
                        self.assertEqual(error.exception.detail, 'bff_upstream_unavailable')
                        self.assertEqual(transport.closed, not pooled)
                        self.assertEqual(client.is_closed, not pooled)

    async def test_header_rejection_keeps_503_and_releases_only_owned_clients(self):
        async def unused(request):
            self.fail('Rejected headers must not reach the upstream.')

        for operation in (proxy_http._send_proxy_request, proxy_streams._open_proxy_stream):
            for pooled in (False, True):
                with self.subTest(operation=operation.__name__, pooled=pooled):
                    async with self.upstream(unused, pooled=pooled) as (request, client, transport):
                        with self.assertRaises(HTTPException) as error:
                            await operation(request, self.resources.bff.upstreams['ai_access'] + '/devices', 'server-token')
                        self.assertEqual(error.exception.status_code, 503)
                        self.assertEqual(error.exception.detail, 'company_ai_network_proxy_not_configured')
                        self.assertEqual(transport.closed, not pooled)
                        self.assertEqual(client.is_closed, not pooled)

    async def test_cancelled_open_finishes_owned_cleanup_and_keeps_pooled_clients(self):
        for operation in (proxy_http._send_proxy_request, proxy_streams._open_proxy_stream):
            for pooled in (False, True):
                with self.subTest(operation=operation.__name__, pooled=pooled):
                    async def cancel(request):
                        scope.cancel()
                        await anyio.sleep(0)
                        self.fail('Cancellation must interrupt the upstream request.')

                    async with self.upstream(cancel, pooled=pooled) as (request, client, transport):
                        with anyio.CancelScope() as scope:
                            await operation(request, 'https://upstream.test/events', 'server-token')
                            self.fail('Cancellation must propagate out of the proxy operation.')
                        self.assertTrue(scope.cancelled_caught)
                        self.assertEqual(transport.closed, not pooled)
                        self.assertEqual(client.is_closed, not pooled)

    async def test_successful_stream_preserves_bytes_and_transfers_ownership_until_complete(self):
        for pooled in (False, True):
            with self.subTest(pooled=pooled):
                body = EventStream()
                requests = []

                async def stream(request):
                    requests.append(request)
                    return httpx.Response(200, stream=body, headers={'content-type': 'text/event-stream'})

                async with self.upstream(stream, pooled=pooled) as (request, client, transport):
                    response = await proxy_streams._proxy_streaming_request(
                        request, None, 'synthetic-session', 'https://upstream.test/events', 'server-token',
                    )
                    self.assertFalse(transport.closed)
                    self.assertFalse(client.is_closed)
                    content = b''.join([chunk async for chunk in response.body_iterator])
                    self.assertEqual(content, b'data: first\n\ndata: second\n\n')
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers['content-type'], 'text/event-stream')
                    self.assertEqual(requests[0].headers['authorization'], 'Bearer server-token')
                    self.assertNotIn('cookie', requests[0].headers)
                    self.assertTrue(body.closed)
                    self.assertEqual(transport.closed, not pooled)
                    self.assertEqual(client.is_closed, not pooled)
