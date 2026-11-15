"""WebUI owns its live check; offline adapters exercise identity and streaming."""
from __future__ import annotations

from contextlib import redirect_stdout
from datetime import timedelta
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from examples.openwebui import smoke


class WebUISmokeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.root / 'config.toml'
        self.config.write_text(
            '[webui]\nDATABASE_URL="webui-test-db"\nWEBUI_SECRET_KEY="webui-test-secret"\n'
            '[litellm]\nLITELLM_DATABASE_URL="postgresql://unused/smoke"\n'
            'LITELLM_MASTER_KEY="must-not-enter-webui"\n',
        )
        self.enterContext(patch.dict(os.environ, {'AI_GATEWAY_CONFIG': str(self.config)}, clear=True))

    async def test_help_never_loads_configuration_or_imports_the_host(self):
        with patch.object(smoke, 'read_service_snapshot') as read, redirect_stdout(StringIO()) as output:
            with self.assertRaises(SystemExit) as raised:
                await smoke.main(['--help'])
        self.assertEqual(raised.exception.code, 0)
        read.assert_not_called()
        self.assertIn('--base-url', output.getvalue())
        self.assertIn('--llm-config', output.getvalue())

    async def test_stream_and_optional_spend_keep_verified_operator_and_private_settings(self):
        for billing in ('disabled', 'matching', 'different-owner'):
            with self.subTest(billing=billing):
                user = SimpleNamespace(id='operator-test', oauth={'oidc': {'sub': 'verified-test-subject'}})
                modules = {}
                for name in (
                    'open_webui', 'open_webui.models', 'open_webui.models.users',
                    'open_webui.utils', 'open_webui.utils.auth', 'open_webui.utils.mfa',
                ):
                    module = ModuleType(name)
                    module.__path__ = []
                    modules[name] = module
                modules['open_webui.models.users'].Users = SimpleNamespace(get_first_user=AsyncMock(return_value=user))
                token = modules['open_webui.utils.auth'].create_token = Mock(return_value='short-lived-test-token')
                auth = modules['open_webui.utils.mfa'].get_auth = AsyncMock(
                    return_value=SimpleNamespace(session_stamp='current-test-stamp'),
                )
                calls = []

                def respond(request):
                    calls.append(request.url.path)
                    if request.url.path == '/health':
                        return httpx.Response(200, json={'status': True})
                    self.assertEqual(request.headers['Authorization'], 'Bearer short-lived-test-token')
                    if request.url.path == '/api/models':
                        return httpx.Response(200, json={'data': [{'id': 'gemini-3.5-flash-lite'}]})
                    self.assertEqual(request.url.path, '/api/chat/completions')
                    self.assertTrue(json.loads(request.content)['stream'])
                    chunk = {'id': 'completion-test', 'choices': [{'delta': {'content': 'OK'}}]}
                    return httpx.Response(
                        200, headers={'x-litellm-call-id': 'call-test'},
                        text='data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n',
                    )

                real_client = httpx.AsyncClient
                client = real_client(base_url='http://webui.test', transport=httpx.MockTransport(respond))
                output_file = self.root / (billing + '.json')
                argv = ['--base-url', 'http://webui.test', '--output', str(output_file)]
                if billing != 'disabled':
                    argv += ['--llm-config', str(self.config)]
                printed = StringIO()
                with patch.dict(sys.modules, modules), \
                        patch.object(smoke.httpx, 'AsyncClient', return_value=client), \
                        patch('psycopg.connect') as connect, redirect_stdout(printed):
                    query = connect.return_value.__enter__.return_value.execute
                    query.return_value.fetchall.return_value = [
                        ('other-subject' if billing == 'different-owner' else 'verified-test-subject', 0.01),
                    ]
                    if billing == 'different-owner':
                        with self.assertRaisesRegex(AssertionError, 'verified end user'):
                            await smoke.main(argv)
                        self.assertFalse(output_file.exists())
                    else:
                        await smoke.main(argv)
                        result = json.loads(output_file.read_text())
                        self.assertTrue(result['verified_operator_stream'])
                        self.assertEqual(result.get('verified_end_user_spend', False), billing == 'matching')
                    if billing == 'disabled':
                        connect.assert_not_called()
                    else:
                        connect.assert_called_once_with('postgresql://unused/smoke')
                        self.assertEqual(query.call_args.args[1], (['completion-test', 'call-test'], 'success'))
                self.assertEqual(calls, ['/health', '/api/models', '/api/chat/completions'])
                auth.assert_awaited_once_with(user.id)
                token.assert_called_once_with(
                    {'id': user.id, 'typ': 'automation', 'session_stamp': 'current-test-stamp'},
                    timedelta(minutes=3),
                )
                self.assertEqual(os.environ['PYTHON_DOTENV_DISABLED'], '1')
                self.assertEqual(os.environ['ENABLE_DB_MIGRATIONS'], 'false')
                self.assertNotIn('LITELLM_MASTER_KEY', os.environ)
                for value in ('short-lived-test-token', 'webui-test-secret', 'verified-test-subject', 'current-test-stamp'):
                    self.assertNotIn(value, printed.getvalue())
