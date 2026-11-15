"""The shared resolver delegates current account checks to the host each time."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

from fastapi import HTTPException

from examples.identity.adapters.webui import verified_user


class WebUIIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        auth = ModuleType('open_webui.utils.auth')
        auth.get_verified_user_by_id = self.lookup = AsyncMock()
        self.enterContext(patch.dict(sys.modules, {'open_webui.utils.auth': auth}))

    async def test_each_lookup_uses_the_host_and_returns_its_projection(self):
        projection = {'id': 'owner', 'role': 'user', 'oauth': {'oidc': {'sub': 'verified-subject'}}}
        user = Mock()
        user.model_dump.return_value = projection
        self.lookup.side_effect = [user, None]

        self.assertIs(await verified_user('owner'), projection)
        user.model_dump.assert_called_once_with()
        with self.assertRaisesRegex(ValueError, '請先以已啟用帳號登入。'):
            await verified_user('owner')
        self.assertEqual(self.lookup.await_args_list, [unittest.mock.call('owner'), unittest.mock.call('owner')])

    async def test_host_authentication_errors_propagate_without_fallback(self):
        for status in (401, 403):
            failure = HTTPException(status, 'host account check rejected')
            self.lookup.side_effect = failure
            with self.subTest(status=status), self.assertRaises(HTTPException) as raised:
                await verified_user('owner')
            self.assertIs(raised.exception, failure)

    def test_importing_the_resolver_does_not_load_the_optional_host(self):
        root = Path(__file__).resolve().parents[1]
        code = """import sys
sys.path.insert(0, sys.argv[1])
from examples.identity.adapters.webui import verified_user
assert callable(verified_user)
assert not any(name == 'open_webui' or name.startswith('open_webui.') for name in sys.modules)
assert not any(name.startswith(('examples.openwebui', 'examples.identity.database')) for name in sys.modules)
"""
        subprocess.run(
            [sys.executable, '-I', '-c', code, str(root)],
            check=True, capture_output=True, text=True, timeout=30,
        )
