"""Native Claude tool round-trip with loopback model/IdP/MCP replacements."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

from company_agent.tests.test_nas import NASFixture
from company_mcp.adapters.company import Resources
from examples.official_document_workflow.runtime import current_resources
from tests.company_mcp import test_resources as fixture
from tests.company_mcp.runtime import close_client
from tests.company_mcp.runtime import start


class ModelHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if '/count_tokens' in self.path:
            value = json.dumps({'input_tokens': 100}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(value)
            return
        self.server.calls.append(body)
        results = [
            part for message in body['messages'] for part in message.get('content', [])
            if isinstance(part, dict) and part.get('type') == 'tool_result'
        ]
        if results:
            assert 'all_owned_bindings' in str(results[-1]['content'])
            block = {'type': 'text', 'text': 'CLAUDE_COMPANY_MCP_OK'}
            reason = 'end_turn'
        else:
            tool = next(tool for tool in body['tools'] if tool['name'].endswith('connections_list'))
            block = {'type': 'tool_use', 'id': 'tool_company_fixture', 'name': tool['name'], 'input': {}}
            reason = 'tool_use'
        message = {
            'id': 'msg_fixture', 'type': 'message', 'role': 'assistant', 'model': body['model'],
            'content': [block], 'stop_reason': reason, 'stop_sequence': None,
            'usage': {'input_tokens': 100, 'output_tokens': 10},
        }
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream' if body.get('stream') else 'application/json')
        self.end_headers()
        if not body.get('stream'):
            self.wfile.write(json.dumps(message).encode())
            return
        events = [
            ('message_start', {'type': 'message_start', 'message': {**message, 'content': [], 'stop_reason': None}}),
            (
                'content_block_start', {
                    'type': 'content_block_start', 'index': 0,
                    'content_block': {**block, 'text': ''} if block['type'] == 'text' else {**block, 'input': {}},
                },
            ),
            (
                'content_block_delta', {
                    'type': 'content_block_delta', 'index': 0,
                    'delta': {'type': 'text_delta', 'text': block['text']} if block['type'] == 'text' else
                    {'type': 'input_json_delta', 'partial_json': '{}'},
                },
            ),
            ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
            (
                'message_delta', {
                    'type': 'message_delta', 'delta': {
                        'stop_reason': reason, 'stop_sequence': None,
                    }, 'usage': {'output_tokens': 10},
                },
            ),
            ('message_stop', {'type': 'message_stop'}),
        ]
        for name, event in events:
            self.wfile.write(('event: ' + name + '\ndata: ' + json.dumps(event) + '\n\n').encode())
            self.wfile.flush()


class NativeClaudeMCPTests(NASFixture):
    def test_native_claude_uses_company_mcp_and_replays_owned_result(self):
        executable = os.getenv('COMPANY_MCP_TEST_CLAUDE')
        if not executable:
            self.skipTest('Set COMPANY_MCP_TEST_CLAUDE to an isolated approved Claude test binary.')
        self.assertEqual(subprocess.check_output([executable, '--version'], text=True).strip(), '2.1.231 (Claude Code)')
        authentication = fixture.OIDCTests()
        authentication.setUp()
        self.addCleanup(close_client, authentication.http)
        self.bind()
        workflow = current_resources()
        resources = Resources(fixture.configuration(), self.store, Mock(return_value=workflow))
        service = start(self, resources, authentication.verifier)
        mcp_port = service.port

        calls = []

        model = ThreadingHTTPServer(('127.0.0.1', 0), ModelHandler)
        model.calls = calls
        threading.Thread(target=model.serve_forever, daemon=True).start()
        self.addCleanup(model.server_close)
        self.addCleanup(model.shutdown)
        with tempfile.TemporaryDirectory(prefix='company-claude-mcp-') as temporary:
            root = Path(temporary)
            config = root / 'mcp.json'
            token = authentication.token()
            config.write_text(
                json.dumps({
                    'mcpServers': {
                        'changdar_company': {
                            'type': 'http', 'url': f'http://127.0.0.1:{mcp_port}/company-mcp',
                            'headers': {'Authorization': 'Bearer ' + token, 'Host': 'company.example'},
                        },
                    },
                }),
            )
            config.chmod(0o600)
            env = {
                key: value for key, value in os.environ.items()
                if not key.startswith(('OPENAI_', 'CODEX_', 'ANTHROPIC_', 'CLAUDE_'))
            }
            env.update(
                CLAUDE_CONFIG_DIR=str(root), ANTHROPIC_API_KEY='synthetic-model-only',
                ANTHROPIC_BASE_URL=f'http://127.0.0.1:{model.server_port}',
                CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', DISABLE_AUTOUPDATER='1',
                HTTPS_PROXY='http://127.0.0.1:9', HTTP_PROXY='http://127.0.0.1:9', NO_PROXY='127.0.0.1,localhost',
            )
            result = subprocess.run(
                [
                    executable, '-p', 'Call the company MCP connections_list tool, then reply CLAUDE_COMPANY_MCP_OK.',
                    '--model', 'gemini-3.8-flash', '--mcp-config', str(config), '--strict-mcp-config',
                    '--allowedTools', 'mcp__changdar_company__connections_list', '--output-format', 'json',
                ], cwd=root, env=env, capture_output=True, text=True, timeout=45,
            )
            self.assertEqual(result.returncode, 0, result.stderr[-2000:] + result.stdout[-3000:])
            self.assertIn('CLAUDE_COMPANY_MCP_OK', result.stdout)
            self.assertGreaterEqual(len(calls), 2)
            self.assertNotIn(token, result.stdout + result.stderr)
