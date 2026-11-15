"""Exercise the installed WebUI callback with synthetic IdP data, without writes.

Run with webui_env's Python; it needs the installed WebUI, not pytest. Provider
verification and credential issuance are mocked boundaries, not an SSO login.
"""
from __future__ import annotations

import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

from starlette.requests import Request
from starlette.responses import Response

from examples.shared.runtime.modules import ModuleReference

_examples_deployment_dev_app = ModuleReference('examples.deployment.dev.app')
_examples_deployment_dev_run = ModuleReference('examples.deployment.dev.run')
_webui_utils = ModuleReference('open_webui.utils')
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
load_environment = _examples_deployment_dev_run.attribute('load_environment')


class OAuthCallbackTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        load_environment('webui')  # Refuse production/shared state before imports.
        oauth, auth = _webui_utils.attributes('oauth', 'auth')
        dev_role = _examples_deployment_dev_app.attribute('dev_role')
        cls.oauth = oauth
        cls.auth = auth
        cls.dev_role = staticmethod(dev_role)

    async def callback(self, userinfo, id_claims=None, expected_role='admin'):
        oauth = self.oauth
        config = SimpleNamespace(
            ENABLE_OAUTH=True, OAUTH_EMAIL_CLAIM='email', OAUTH_USERNAME_CLAIM='name',
            OAUTH_SUB_CLAIM='sub', OAUTH_ALLOWED_DOMAINS=['*'],
            OAUTH_UPDATE_NAME_ON_LOGIN=False, OAUTH_UPDATE_EMAIL_ON_LOGIN=False,
            OAUTH_UPDATE_PICTURE_ON_LOGIN=False, ENABLE_OAUTH_GROUP_MANAGEMENT=False,
            JWT_EXPIRES_IN='1h',
        )
        user = SimpleNamespace(
            id='callback-test-user', role=expected_role,
            oauth={'oidc': {'sub': 'callback-test-sub'}},
        )
        token = {'access_token': 'synthetic-test-token', 'expires_in': 300}
        if id_claims is not None:
            token['userinfo'] = id_claims
        client = SimpleNamespace(
            authorize_access_token=AsyncMock(return_value=token),
            userinfo=AsyncMock(return_value=userinfo),
        )
        request = Request({
            'type': 'http', 'method': 'GET', 'scheme': 'https',
            'path': '/oauth/oidc/callback', 'query_string': b'',
            'headers': [], 'server': ('ai-dev.changdar-server.mooo.com', 443),
        })
        # Use the actual installed callback and update_user_role_from_oauth.
        # Every provider/DB write/session boundary is mocked for this fixture.
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    oauth, 'OAUTH_PROVIDERS',
                    {'oidc': {'register': lambda registry: client}},
                ),
            )
            stack.enter_context(patch.object(oauth, 'get_oauth_runtime_config', AsyncMock(return_value=config)))
            stack.enter_context(patch.object(oauth.Users, 'get_user_by_oauth_sub', AsyncMock(return_value=user)))
            role_write = stack.enter_context(patch.object(oauth.Users, 'update_user_role_by_id', AsyncMock()))
            stack.enter_context(
                patch.object(
                    oauth.Config, 'get', AsyncMock(
                        return_value='https://ai-dev.changdar-server.mooo.com',
                    ),
                ),
            )
            stack.enter_context(
                patch.object(
                    oauth.OAuthSessions,
                    'get_sessions_by_user_id', AsyncMock(return_value=[]),
                ),
            )
            session_write = stack.enter_context(
                patch.object(
                    oauth.OAuthSessions, 'create_session',
                    AsyncMock(return_value=SimpleNamespace(id='test-session')),
                ),
            )
            signin = stack.enter_context(
                patch.object(
                    self.auth, 'create_signin_response',
                    AsyncMock(return_value={'token': 'synthetic-webui-token'}),
                ),
            )
            manager = oauth.OAuthManager(SimpleNamespace())
            manager._clients['oidc'] = client
            manager.get_user_role = self.dev_role
            result = await manager.handle_callback(request, 'oidc', Response())
            role_write.assert_not_awaited()
            return result, signin.await_count, session_write.await_count

    @staticmethod
    def identity(**extra):
        return {
            'sub': 'callback-test-sub', 'email': 'callback@example.test',
            'name': 'Callback fixture', **extra,
        }

    async def assert_allowed(self, userinfo, id_claims=None, role='admin'):
        result, signins, sessions = await self.callback(userinfo, id_claims, role)
        self.assertEqual(result.headers['location'], 'https://ai-dev.changdar-server.mooo.com/auth')
        self.assertEqual((signins, sessions), (1, 1))
        self.assertTrue(any(cookie.startswith('token=') for cookie in result.headers.getlist('set-cookie')))

    async def assert_denied(self, userinfo, id_claims=None):
        result, signins, sessions = await self.callback(userinfo, id_claims)
        self.assertIn('?error=Keycloak+DEV+membership+is+required.', result.headers['location'])
        self.assertEqual((signins, sessions), (0, 0))
        self.assertNotIn('set-cookie', result.headers)

    async def test_existing_dev_admin_callback(self):
        await self.assert_allowed(self.identity(dev_roles=['dev-admin']))

    async def test_existing_dev_user_callback(self):
        await self.assert_allowed(self.identity(dev_roles=['dev-user']), role='user')

    async def test_verified_id_token_roles_backfilled_by_native_callback(self):
        await self.assert_allowed(self.identity(), {'sub': 'callback-test-sub', 'dev_roles': ['dev-admin']})

    async def test_missing_roles_denied_before_session(self):
        await self.assert_denied(self.identity())

    async def test_shared_realm_admin_denied_before_session(self):
        await self.assert_denied(self.identity(realm_access={'roles': ['admin']}))

    async def test_empty_userinfo_roles_not_overridden_by_id_token(self):
        await self.assert_denied(self.identity(dev_roles=[]), {'dev_roles': ['dev-admin']})


if __name__ == '__main__':
    unittest.main(verbosity=2)
