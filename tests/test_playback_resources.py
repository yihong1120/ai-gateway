"""Playback keeps app configuration and clients, and scopes only validated streams."""
from __future__ import annotations

import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from fastapi import Request
from pydantic import ValidationError

import examples.db_management.routers.playback as _playback_imports
from examples.db_management.schemas.playback import PlaybackRenewRequest
from examples.db_management.schemas.playback import PlaybackSessionRequest
from examples.db_management.schemas.playback import PlaybackWallRequest
from examples.db_management.services.playback.identity import PlaybackPrincipal
from examples.db_management.services.playback.rendering import PlaybackResponses
from examples.db_management.services.playback.service import create_playback_service
from examples.db_management.services.playback.service import PlaybackService
from examples.db_management.services.playback.upstream import PlaybackUpstream
from examples.identity.config import Settings
from examples.identity.runtime import IdentityResources
from examples.identity.settings.resources import ResourcesSettings
from examples.shared.runtime.http_clients import HttpClientPool


class PlaybackResourcesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(
            patch.dict(
                os.environ,
                {'PYTHON_DOTENV_DISABLED': '1', 'JWT_SECRET_KEY': 'test-secret'},
                clear=True,
            ),
        )
        self.principal = PlaybackPrincipal(
            'alice', 7, 'native:user:7', 'native', 'private-token',
        )
        self.stream = {
            'session_id': 'upstream',
            'key': 'authorized-camera',
            'label': 'site',
            'profile': 'clean',
            'rendition': 'detail',
            'language': None,
            'media_hls_url': '/hls/test?mt=stale&keep=1',
        }
        self.identity = SimpleNamespace(resolve=AsyncMock(return_value=self.principal))
        self.upstream = SimpleNamespace(post=AsyncMock(return_value=(self.stream, 201)))
        self.media = SimpleNamespace(
            create=AsyncMock(return_value=('capability', {'id': 'session'})),
            renew=AsyncMock(return_value={'id': 'session', 'scope': 'single'}),
            delete=AsyncMock(return_value=True),
        )
        self.service = PlaybackService(
            self.identity, self.upstream, self.media, PlaybackResponses(123),
        )

    async def test_two_apps_keep_url_timeout_and_client_after_environment_changes(self):
        with patch.dict(
            os.environ,
            {
                'PLAYBACK_STREAMING_API_URL': 'http://one.test',
                'PLAYBACK_UPSTREAM_TIMEOUT_SECONDS': '3',
            },
        ):
            one = IdentityResources(Settings(authjwt_secret_key='one'))
        with patch.dict(
            os.environ,
            {
                'PLAYBACK_STREAMING_API_URL': 'http://two.test',
                'PLAYBACK_UPSTREAM_TIMEOUT_SECONDS': '7',
            },
        ):
            two = IdentityResources(Settings(authjwt_secret_key='two'))
        one_playback = create_playback_service(one)
        two_playback = create_playback_service(two)
        self.addAsyncCleanup(one.http_clients.close)
        self.addAsyncCleanup(two.http_clients.close)
        self.assertEqual(one_playback.upstream.url, 'http://one.test')
        self.assertEqual(two_playback.upstream.url, 'http://two.test')
        first, second = await asyncio.gather(
            one.http_clients.get('streaming-playback'),
            two.http_clients.get('streaming-playback'),
        )
        self.assertEqual(first.timeout.read, 3)
        self.assertEqual(second.timeout.read, 7)
        self.assertIs(
            first, await one_playback.upstream.clients.get('streaming-playback'),
        )
        self.assertIsNot(first, second)

    async def test_upstream_reuses_named_transport_and_preserves_error_contract(self):
        requests = []

        def respond(request):
            requests.append(request)
            return (
                httpx.Response(200, json={'ok': True})
                if len(requests) == 1
                else httpx.Response(409, json={'detail': 'conflict'})
            )

        pool = HttpClientPool(
            {
                'streaming-playback': {
                    'transport': httpx.MockTransport(respond),
                    'timeout': 3,
                },
            },
        )
        self.addAsyncCleanup(pool.close)
        upstream = PlaybackUpstream(
            ResourcesSettings(playback_api_url='http://one.test'), pool,
        )
        self.assertEqual(
            await upstream.post(
                '/stream-playback', principal=self.principal, payload={},
            ),
            ({'ok': True}, 200),
        )
        with self.assertRaises(HTTPException) as caught:
            await upstream.post(
                '/stream-playback', principal=self.principal, payload={},
            )
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail, 'conflict')
        self.assertEqual(str(requests[0].url), 'http://one.test/stream-playback')
        self.assertEqual(requests[0].headers['authorization'], 'Bearer private-token')
        self.assertEqual(len(pool._clients), 1)

    async def test_single_scopes_only_validated_camera_and_replaces_stale_tokens(self):
        payload = PlaybackSessionRequest(
            site='site', camera='requested-camera', profile='clean',
        )
        body, status = await self.service.create_single(
            payload, self.principal, 'redis',
        )
        self.assertEqual(status, 201)
        self.assertEqual(
            self.media.create.call_args.kwargs['cameras'], ['authorized-camera'],
        )
        self.assertEqual(
            self.media.create.call_args.kwargs['principal'], self.principal,
        )
        self.assertEqual(body['expires_in'], 123)
        self.assertEqual(body['camera'], 'authorized-camera')
        self.assertEqual(
            parse_qs(urlsplit(body['hls_url']).query),
            {'mt': ['capability'], 'keep': ['1']},
        )
        self.assertNotIn('mt=stale', body['hls_url'])

    async def test_wall_uses_authorized_results_and_rejects_invalid_upstream_before_session(
        self,
    ):
        payload = PlaybackWallRequest(
            site='site', cameras=['requested'], profile='clean',
        )
        stream = {**self.stream, 'rendition': 'preview'}
        self.upstream.post.return_value = ({'items': [stream], 'max_streams': 16}, 200)
        body, _ = await self.service.create_wall(payload, self.principal, 'redis')
        self.assertEqual(body['count'], 1)
        self.assertEqual(body['quality'], 'preview')
        self.assertEqual(
            self.media.create.call_args.kwargs['cameras'], ['authorized-camera'],
        )
        self.media.create.reset_mock()
        self.upstream.post.return_value = ({'items': [{'key': 'unvalidated'}]}, 200)
        with self.assertRaises(HTTPException) as caught:
            await self.service.create_wall(payload, self.principal, 'redis')
        self.assertEqual(caught.exception.status_code, 502)
        self.media.create.assert_not_awaited()

    async def test_authentication_failure_prevents_upstream_and_session_mutation(self):
        self.identity.resolve.side_effect = HTTPException(status_code=401)
        with self.assertRaises(HTTPException):
            await self.service.playback_session_response(
                PlaybackSessionRequest(site='site', camera='cam'), None, 'redis', 'db',
            )
        self.upstream.post.assert_not_awaited()
        self.media.create.assert_not_awaited()

    async def test_renew_and_delete_keep_owner_and_noncacheable_response(self):
        response = await self.service.renew_playback_response(
            PlaybackRenewRequest(id='session'), None, 'redis', 'db',
        )
        self.media.renew.assert_awaited_once_with('redis', 'session', 'native:user:7')
        self.assertEqual(json.loads(response.body)['expires_in'], 123)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        response = await self.service.delete_playback_response(
            'session', None, 'redis', 'db',
        )
        self.media.delete.assert_awaited_once_with('redis', 'session', 'native:user:7')
        self.assertEqual(response.status_code, 204)
        self.media.delete.return_value = False
        with self.assertRaises(HTTPException) as caught:
            await self.service.delete_playback_response('session', None, 'redis', 'db')
        self.assertEqual(caught.exception.status_code, 404)

    async def test_http_boundary_uses_request_app_instead_of_ambient_resources(self):

        expected = object()
        playback = SimpleNamespace(
            playback_session_response=AsyncMock(return_value=expected),
        )
        app = SimpleNamespace(
            state=SimpleNamespace(playback=playback),
        )
        request = Request({'type': 'http', 'app': app})
        payload = PlaybackSessionRequest(site='site', camera='cam')
        self.assertIs(
            await _playback_imports.create_playback_session(payload, request, db='db', redis='redis'), expected,
        )
        playback.playback_session_response.assert_awaited_once_with(
            payload, request, 'redis', 'db',
        )

    def test_invalid_url_is_rejected_when_settings_are_built(self):
        for url in (
            'file:///tmp/x',
            'http://user:secret@host',
            'http://host?secret=x',
            'http://host:0',
            'http://host\n/path',
        ):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                ResourcesSettings(playback_api_url=url)
