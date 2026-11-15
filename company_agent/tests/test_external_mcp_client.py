"""Generic discovery, schema/auth isolation and read-only MCP execution."""
from __future__ import annotations

import asyncio
import copy
import json
import re
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from unittest.mock import patch

import httpx2

from company_agent.services.external_tools.client import BoundedTransport
from company_agent.services.external_tools.client import ExternalTools
from company_agent.services.external_tools.client import ExternalToolUnavailable
from company_agent.services.external_tools.client import MAX_BYTES
from company_agent.services.external_tools.config import MCPServerConfig


def definition(name='list_items', schema=None):
    return {
        'name': name, 'description': 'List authorized items',
        'inputSchema': schema or {'type': 'object', 'properties': {}, 'additionalProperties': False},
        'annotations': {'readOnlyHint': True, 'destructiveHint': False},
    }


class FakeSession:
    def __init__(self, tools, result=None):
        self.tools = tools
        self.discover = AsyncMock()
        self.list_tools = AsyncMock(return_value={'tools': copy.deepcopy(tools)})
        self.call_tool = AsyncMock(return_value=result or {'structuredContent': {'items': [1]}, 'isError': False})


class Factory:
    def __init__(self):
        self.tools = [definition()]
        self.result = None
        self.sessions = []
        self.openings = []
        self.closed = 0
        self.failure = None

    @asynccontextmanager
    async def __call__(self, url, token):
        self.openings.append((url, token))
        if self.failure:
            raise self.failure
        session = FakeSession(self.tools, self.result)
        self.sessions.append(session)
        try:
            yield session
        finally:
            self.closed += 1


class ExternalMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = MCPServerConfig(name='records', url='https://records.example/mcp', allowed_tools=['list_items'])
        self.tokens = AsyncMock(side_effect=['personal-token-1', 'personal-token-2', 'personal-token-3'])
        self.factory = Factory()
        self.client = ExternalTools([self.config], self.tokens, session_factory=self.factory)

    async def test_discovery_and_invocation_refresh_identity_and_recheck_schema(self):
        spec, = await self.client.discover('alice', 'subject-a')
        result = await self.client.invoke('alice', 'subject-a', spec, {})
        self.assertEqual(result['result'], {'items': [1]})
        self.assertEqual(result['server'], 'records')
        self.assertEqual(result['tool'], 'list_items')
        self.assertNotIn('personal-token', str(result))
        self.assertEqual(
            self.factory.openings, [
                (self.config.url, 'personal-token-1'), (self.config.url, 'personal-token-2'),
            ],
        )
        self.assertEqual(
            [call.args for call in self.tokens.await_args_list],
            [('alice', 'subject-a'), ('alice', 'subject-a')],
        )
        self.assertEqual(self.factory.closed, 2)
        self.factory.sessions[1].list_tools.assert_awaited_once()
        self.factory.sessions[1].call_tool.assert_awaited_once_with('list_items', {}, read_timeout_seconds=20)

    async def test_personal_identity_is_not_cached_across_owners(self):
        await self.client.discover('alice', 'subject-a')
        await self.client.discover('bob', 'subject-b')
        self.assertEqual(self.tokens.await_args_list[1].args, ('bob', 'subject-b'))
        self.assertEqual(self.factory.openings[1][1], 'personal-token-2')

    async def test_allowlist_is_explicit_and_every_required_tool_must_exist(self):
        self.factory.tools.append(definition('another_read'))
        specs = await self.client.discover('alice', 'subject-a')
        self.assertEqual([spec.name for spec in specs], ['list_items'])
        self.factory.tools = [definition('another_read')]
        with self.assertRaisesRegex(ExternalToolUnavailable, '未全部提供'):
            await self.client.discover('alice', 'subject-a')

    async def test_unapproved_server_and_tool_cannot_invoke(self):
        spec, = await self.client.discover('alice', 'subject-a')
        other = ExternalTools([], self.tokens, session_factory=self.factory)
        with self.assertRaisesRegex(ExternalToolUnavailable, '未經設定允許'):
            await other.invoke('alice', 'subject-a', spec, {})
        self.assertEqual(self.tokens.await_count, 1)

    async def test_missing_readonly_or_destructive_hint_blocks_discovery(self):
        for hints in ({}, {'readOnlyHint': True}, {'readOnlyHint': True, 'destructiveHint': True}):
            self.tokens.reset_mock(side_effect=True)
            self.tokens.return_value = 'personal-token'
            self.factory.tools[0]['annotations'] = hints
            with self.subTest(annotations=hints), self.assertRaisesRegex(ExternalToolUnavailable, '唯讀'):
                await self.client.discover('alice', 'subject-a')

    async def test_schema_change_or_withdrawn_readonly_stops_before_execution(self):
        spec, = await self.client.discover('alice', 'subject-a')
        self.factory.tools[0]['description'] = 'Changed contract'
        with self.assertRaisesRegex(ExternalToolUnavailable, '契約已變更'):
            await self.client.invoke('alice', 'subject-a', spec, {})
        self.factory.sessions[-1].call_tool.assert_not_awaited()

    async def test_rejects_reserved_runtime_remote_refs_and_nonobject_schemas(self):
        self.tokens.reset_mock(side_effect=True)
        self.tokens.return_value = 'personal-token'
        for schema in (
            {'type': 'array'},
            {'type': 'object', 'properties': {'runtime': {'type': 'string'}}},
            {'type': 'object', 'properties': {'specification': {'type': 'string'}}},
            {'type': 'object', 'properties': {'x': {'$ref': 'https://outside.example/schema'}}},
        ):
            self.factory.tools = [definition(schema=schema)]
            with self.subTest(schema=schema), self.assertRaises(ExternalToolUnavailable):
                await self.client.discover('alice', 'subject-a')

    async def test_reserved_payload_is_rejected_even_when_schema_accepts_additional_properties(self):
        self.factory.tools = [definition(schema={'type': 'object', 'additionalProperties': True})]
        spec, = await self.client.discover('alice', 'subject-a')
        for reserved in ('runtime', 'specification'):
            with self.subTest(reserved=reserved), self.assertRaisesRegex(ExternalToolUnavailable, '參數'):
                await self.client.invoke('alice', 'subject-a', spec, {reserved: {'server': 'model-controlled'}})
        self.assertEqual(self.tokens.await_count, 1)
        self.assertEqual(len(self.factory.openings), 1)
        self.factory.sessions[0].call_tool.assert_not_awaited()

    async def test_local_schema_refs_validate_arguments_without_external_requests(self):
        schema = {
            'type': 'object', 'properties': {'count': {'$ref': '#/$defs/Count'}},
            '$defs': {'Count': {'type': 'integer', 'minimum': 1, 'maximum': 5}},
            'required': ['count'], 'additionalProperties': False,
        }
        self.factory.tools = [definition(schema=schema)]
        spec, = await self.client.discover('alice', 'subject-a')
        for args in ({'count': 20}, {'count': 1, 'token': 'model-supplied'}, {'runtime': 'bad'}):
            with self.subTest(args=args), self.assertRaisesRegex(ExternalToolUnavailable, '參數'):
                await self.client.invoke('alice', 'subject-a', spec, args)
        self.assertEqual(self.tokens.await_count, 1)
        await self.client.invoke('alice', 'subject-a', spec, {'count': 2})

    async def test_renamed_long_tools_have_unique_valid_runtime_names(self):
        names = ['find.records', 'find_records', 'x' * 128]
        self.config = self.config.model_copy(update={'allowed_tools': names})
        self.factory.tools = [definition(name) for name in names]
        client = ExternalTools([self.config], self.tokens, session_factory=self.factory)
        specs = await client.discover('alice', 'subject-a')
        aliases = [spec.runtime_name for spec in specs]
        self.assertEqual(len(set(aliases)), len(aliases))
        self.assertTrue(all(len(name) <= 64 and re.fullmatch(r'[A-Za-z0-9_-]+', name) for name in aliases))

    async def test_failed_session_is_safe_and_not_retried(self):
        self.factory.failure = RuntimeError('secret-upstream-response personal-token')
        with self.assertRaises(ExternalToolUnavailable) as caught:
            await self.client.discover('alice', 'subject-a')
        self.assertNotIn('secret', str(caught.exception))
        self.assertEqual(len(self.factory.openings), 1)

    async def test_expired_identity_never_opens_session_or_falls_back(self):
        self.tokens.side_effect = ValueError('refresh-secret')
        with self.assertRaises(ExternalToolUnavailable) as caught:
            await self.client.discover('alice', 'subject-a')
        self.assertNotIn('refresh-secret', str(caught.exception))
        self.assertFalse(self.factory.openings)

    async def test_cancellation_propagates(self):
        self.tokens.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.client.discover('alice', 'subject-a')

    async def test_text_results_are_returned_without_interpreting_domain_content(self):
        self.factory.result = {'content': [{'type': 'text', 'text': 'opaque domain text'}], 'isError': False}
        spec, = await self.client.discover('alice', 'subject-a')
        result = await self.client.invoke('alice', 'subject-a', spec, {})
        self.assertEqual(result['result'], 'opaque domain text')

    async def test_remote_failure_is_not_success_or_untrusted_error_text(self):
        self.factory.result = {'content': [{'type': 'text', 'text': 'upstream-secret'}], 'isError': True}
        spec, = await self.client.discover('alice', 'subject-a')
        with self.assertRaises(ExternalToolUnavailable) as caught:
            await self.client.invoke('alice', 'subject-a', spec, {})
        self.assertNotIn('upstream-secret', str(caught.exception))
        self.factory.sessions[-1].call_tool.assert_awaited_once()

    async def test_result_caps_and_credential_echo_fail_closed(self):
        self.tokens.reset_mock(side_effect=True)
        self.tokens.return_value = 'personal-token'
        for result in (
            {'structuredContent': {'text': 'x' * MAX_BYTES}},
            {'structuredContent': {'echo': 'personal-token'}},
            {'content': [{'type': 'image', 'data': 'opaque'}]},
        ):
            self.factory.result = result
            spec, = await self.client.discover('alice', 'subject-a')
            with self.subTest(result=list(result)), self.assertRaises(ExternalToolUnavailable):
                await self.client.invoke('alice', 'subject-a', spec, {})

    async def test_repeated_cursor_is_rejected_without_unbounded_discovery(self):
        session = FakeSession([definition()])
        session.list_tools.side_effect = [
            {'tools': [definition()], 'nextCursor': 'next'},
            {'tools': [], 'nextCursor': 'next'},
        ]
        with self.assertRaisesRegex(ExternalToolUnavailable, '分頁'):
            await self.client._catalog(session, self.config, 'personal-token')
        self.assertEqual(session.list_tools.await_count, 2)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_sdk_discovers_and_calls_over_authenticated_http_without_legacy_fallback(self):
        handler = MCPHandler()
        config = MCPServerConfig(name='records', url='https://records.example/mcp', allowed_tools=['list_items'])
        tokens = AsyncMock(side_effect=['personal-token-1', 'personal-token-2'])
        with patch('company_agent.services.external_tools.client.BoundedTransport', return_value=httpx2.MockTransport(handler)):
            client = ExternalTools([config], tokens)
            spec, = await client.discover('alice', 'subject-a')
            result = await client.invoke('alice', 'subject-a', spec, {})
        self.assertEqual(result['result'], {'opaque': 7})
        self.assertEqual(
            handler.methods, [
                'server/discover', 'tools/list',
                'server/discover', 'tools/list', 'tools/call',
            ],
        )
        self.assertEqual(handler.authorizations, ['Bearer personal-token-1'] * 2 + ['Bearer personal-token-2'] * 3)

    async def test_redirect_is_refused_before_sdk_or_httpx_can_follow_it(self):
        requests = []
        transport = BoundedTransport(httpx2.MockTransport(RedirectHandler(requests)))
        async with httpx2.AsyncClient(transport=transport, follow_redirects=True) as http:
            with self.assertRaisesRegex(ExternalToolUnavailable, '轉址'):
                await http.get('https://records.example/mcp')
        self.assertEqual(len(requests), 1)

    async def test_streamed_body_is_bounded_before_sdk_reads_json(self):
        transport = BoundedTransport(httpx2.MockTransport(OversizedHandler()))
        async with httpx2.AsyncClient(transport=transport) as http:
            with self.assertRaisesRegex(ExternalToolUnavailable, '上限'):
                await http.get('https://records.example/mcp')


class RedirectHandler:
    def __init__(self, requests):
        self.requests = requests

    def __call__(self, request):
        self.requests.append(request)
        return httpx2.Response(307, headers={'Location': 'https://records.example/other'})


class OversizedHandler:
    def __call__(self, request):
        return httpx2.Response(200, content=b'x' * (MAX_BYTES + 1))


class MCPHandler:
    def __init__(self):
        self.methods = []
        self.authorizations = []

    def __call__(self, request):
        self.authorizations.append(request.headers['Authorization'])
        value = json.loads(request.content)
        method = value['method']
        self.methods.append(method)
        if method == 'server/discover':
            result = {'supportedVersions': ['2026-07-28'], 'capabilities': {'tools': {}}}
        elif method == 'tools/list':
            result = {'tools': [definition()], 'ttlMs': 0, 'cacheScope': 'private', 'resultType': 'complete'}
        elif method == 'tools/call':
            result = {'content': [], 'structuredContent': {'opaque': 7}, 'isError': False, 'resultType': 'complete'}
        else:
            raise AssertionError('Unexpected MCP request')
        return httpx2.Response(200, json={'jsonrpc': '2.0', 'id': value['id'], 'result': result})
