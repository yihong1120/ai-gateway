"""External tools use fresh per-user host OIDC credentials, never a fallback."""
from __future__ import annotations

import asyncio
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import jwt
from pydantic import ValidationError

from company_agent.services.external_tools.config import MCPServerConfig
from company_agent.services.external_tools.connection import external_tools_connection
from company_agent.services.external_tools.identity import AUTHORIZATION_REQUIRED
from company_agent.services.external_tools.identity import WebUIUserTokens


def session(identifier='login', owner='alice', provider='oidc', created_at=10, **kwargs):
    return SimpleNamespace(
        id=identifier, user_id=owner, provider=provider, created_at=created_at,
        token={'access_token': 'stale-session-token'}, **kwargs,
    )


class ExternalMCPIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user = {
            'id': 'alice', 'role': 'user', 'oauth': {'oidc': {'sub': 'alice-sub'}},
        }
        self.verified = AsyncMock(return_value=self.user)
        self.sessions = AsyncMock(return_value=[session()])
        module = types.ModuleType('open_webui.models.oauth_sessions')
        module.OAuthSessions = SimpleNamespace(get_sessions_by_user_id=self.sessions)
        self.modules = patch.dict(sys.modules, {'open_webui.models.oauth_sessions': module})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        verified = patch(
            'company_agent.services.external_tools.identity.webui.verified_user', self.verified,
        )
        verified.start()
        self.addCleanup(verified.stop)
        self.token = jwt.encode(
            {'sub': 'alice-sub'}, 'test-only-signing-key-at-least-32-bytes', algorithm='HS256',
        )
        self.manager = SimpleNamespace(
            get_oauth_token=AsyncMock(return_value={'access_token': self.token}),
        )
        self.resolve = WebUIUserTokens(self.manager)

    async def test_refresh_is_owned_by_host_manager_and_raw_session_token_is_unused(self):
        self.assertEqual(await self.resolve('alice', 'alice-sub'), self.token)
        self.verified.assert_awaited_once_with('alice')
        self.sessions.assert_awaited_once_with('alice')
        self.manager.get_oauth_token.assert_awaited_once_with('alice', 'login')

    async def test_newest_oidc_session_selected_without_using_updated_at_or_expiry(self):
        self.sessions.return_value = [
            session('latest-non-oidc', provider='mcp:external', created_at=999),
            session('new', created_at=20, updated_at=20, expires_at=1),
            session('old', created_at=10, updated_at=999, expires_at=999999),
        ]
        await self.resolve('alice', 'alice-sub')
        self.manager.get_oauth_token.assert_awaited_once_with('alice', 'new')

    async def test_creation_time_ties_have_stable_session_id_order(self):
        self.sessions.return_value = [session('b'), session('a')]
        await self.resolve('alice', 'alice-sub')
        self.manager.get_oauth_token.assert_awaited_once_with('alice', 'b')

    async def test_changed_subject_or_disabled_user_stops_before_session_access(self):
        for changes in (
            {'oauth': {'oidc': {'sub': 'changed'}}},
            {'oauth': {}}, {'role': 'pending'}, {'id': 'bob'},
        ):
            with self.subTest(changes=changes):
                self.verified.return_value = {**self.user, **changes}
                with self.assertRaisesRegex(ValueError, AUTHORIZATION_REQUIRED):
                    await self.resolve('alice', 'alice-sub')
        self.sessions.assert_not_awaited()
        self.manager.get_oauth_token.assert_not_awaited()

    async def test_identity_is_rechecked_on_each_tool_call(self):
        await self.resolve('alice', 'alice-sub')
        self.verified.return_value = {**self.user, 'role': 'pending'}
        with self.assertRaisesRegex(ValueError, AUTHORIZATION_REQUIRED):
            await self.resolve('alice', 'alice-sub')
        self.assertEqual(self.verified.await_count, 2)
        self.manager.get_oauth_token.assert_awaited_once()

    async def test_no_own_oidc_session_does_not_use_other_provider_or_user(self):
        for sessions in (
            [], [session(provider='mcp:external')], [session(owner='bob')],
        ):
            with self.subTest(sessions=sessions):
                self.sessions.return_value = sessions
                with self.assertRaisesRegex(ValueError, AUTHORIZATION_REQUIRED):
                    await self.resolve('alice', 'alice-sub')
        self.manager.get_oauth_token.assert_not_awaited()

    async def test_failed_newest_session_does_not_fall_back_to_older_login(self):
        self.sessions.return_value = [session('older'), session('newest', created_at=20)]
        self.manager.get_oauth_token.return_value = None
        with self.assertRaisesRegex(ValueError, AUTHORIZATION_REQUIRED):
            await self.resolve('alice', 'alice-sub')
        self.manager.get_oauth_token.assert_awaited_once_with('alice', 'newest')

    async def test_invalid_access_tokens_fail_without_exposing_credentials(self):
        for token in (
            None, {}, {'access_token': ''}, {'access_token': 123},
            {'access_token': 'secret\nInjected: header'}, {'access_token': ' secret'},
            {'access_token': 'opaque-token'}, {'access_token': 'bad.jwt.parts'},
        ):
            with self.subTest(token=token):
                self.manager.get_oauth_token.return_value = token
                with self.assertRaises(ValueError) as failure:
                    await self.resolve('alice', 'alice-sub')
                self.assertEqual(str(failure.exception), AUTHORIZATION_REQUIRED)

    async def test_old_subject_session_after_account_rebind_is_rejected_without_fallback(self):
        old_token = jwt.encode(
            {'sub': 'previous-sub'}, 'test-only-signing-key-at-least-32-bytes', algorithm='HS256',
        )
        self.sessions.return_value = [session('older'), session('newest', created_at=20)]
        self.manager.get_oauth_token.return_value = {'access_token': old_token}
        with self.assertRaisesRegex(ValueError, AUTHORIZATION_REQUIRED):
            await self.resolve('alice', 'alice-sub')
        self.manager.get_oauth_token.assert_awaited_once_with('alice', 'newest')

    async def test_missing_token_subject_is_rejected(self):
        self.manager.get_oauth_token.return_value = {
            'access_token': jwt.encode(
                {'iss': 'synthetic-issuer'}, 'test-only-signing-key-at-least-32-bytes', algorithm='HS256',
            ),
        }
        with self.assertRaisesRegex(ValueError, AUTHORIZATION_REQUIRED):
            await self.resolve('alice', 'alice-sub')

    async def test_provider_database_and_identity_failures_are_sanitized(self):
        for dependency in (self.verified, self.sessions, self.manager.get_oauth_token):
            with self.subTest(dependency=dependency):
                dependency.side_effect = RuntimeError('SECRET_TOKEN upstream response')
                with self.assertRaises(ValueError) as failure:
                    await self.resolve('alice', 'alice-sub')
                self.assertEqual(str(failure.exception), AUTHORIZATION_REQUIRED)
                self.assertTrue(failure.exception.__suppress_context__)
                dependency.side_effect = None

    async def test_cancellation_propagates(self):
        self.manager.get_oauth_token.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.resolve('alice', 'alice-sub')

    async def test_invalid_bound_identity_stops_before_host_lookups(self):
        for owner, subject in (('', 'sub'), ('alice', ''), (' alice', 'sub'), ('alice', None)):
            with self.subTest(owner=owner, subject=subject):
                with self.assertRaisesRegex(ValueError, AUTHORIZATION_REQUIRED):
                    await self.resolve(owner, subject)
        self.verified.assert_not_awaited()
        self.sessions.assert_not_awaited()

    async def test_malformed_session_fails_without_asking_host_for_token(self):
        self.sessions.return_value = [session(created_at='not-a-timestamp')]
        with self.assertRaisesRegex(ValueError, AUTHORIZATION_REQUIRED):
            await self.resolve('alice', 'alice-sub')
        self.manager.get_oauth_token.assert_not_awaited()


class ExternalMCPConfigurationTests(unittest.TestCase):
    def test_fixed_endpoint_and_explicit_allowlist(self):
        settings = MCPServerConfig(
            name='source', url='https://source.example/api/mcp/',
            allowed_tools=['search.records'],
        )
        self.assertEqual(settings.url, 'https://source.example/api/mcp')
        for value in ('http://127.0.0.1:8092/mcp', 'http://localhost/mcp', 'http://[::1]/mcp'):
            self.assertEqual(MCPServerConfig(name='source', url=value, allowed_tools=['read']).url, value)

    def test_rejects_unsafe_endpoint_or_implicit_tool_access(self):
        for value in (
            'http://remote.example/mcp', 'https://user:secret@source.example/mcp',
            'https://source.example/mcp?key=secret', 'https://source.example/mcp#fragment',
            'https://source.example/../mcp', 'https://source.example/%2e%2e/mcp',
            'https://source.example//mcp', 'https://source.example\\mcp',
            'https://source.example/mcp\n',
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                MCPServerConfig(name='source', url=value, allowed_tools=['read'])
        for value in ([], ['read', 'read'], ['*'], ['read'] * 101):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                MCPServerConfig(name='source', url='https://source.example/mcp', allowed_tools=value)
        for name in ('Source', 'a' * 17, 'source/path', 'source-name'):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                MCPServerConfig(name=name, url='https://source.example/mcp', allowed_tools=['read'])


class ExternalMCPConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_connection_needs_no_host_or_sdk(self):
        async with external_tools_connection(SimpleNamespace(), []) as connection:
            self.assertIsNone(connection)

    async def test_enabled_connection_requires_personal_oidc_host(self):
        app = SimpleNamespace(state=SimpleNamespace())
        with self.assertRaisesRegex(ValueError, 'OIDC'):
            async with external_tools_connection(app, [object()]):
                self.fail('must not yield a connector without OIDC')
