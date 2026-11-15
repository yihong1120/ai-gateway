"""Exercise the installed Codex binary through a real HTTP Company AI gateway.

The deterministic upstream emits a local shell call, verifies its replayed result,
then completes the answer. No external model, user credential or child agent is used.
"""

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

from fastapi import FastAPI
import httpx
import pytest
import uvicorn
import unittest
from tests.ai_access_postgres import access_database

from examples.company_ai_access.routers.inference import inference_router
from examples.company_ai_access.services.inference import InferenceProxy
from examples.company_ai_access.storage.repository import Repository
from examples.company_ai_access.protocols.codex_catalog import candidate, COMPANY_MODELS
from examples.company_ai_access.config import Settings
from examples.company_ai_access.services.devices import Service, challenge
from examples.company_ai_access.storage.registry import Store
from tests.test_company_ai_access import FakeDirectory, FakeLiteLLM


@pytest.mark.skipif(
    os.name != 'posix',
    reason='Uses a POSIX PTY; Windows terminal acceptance is separate.',
)
def test_installed_codex_interactive_startup_with_long_generated_company_home(tmp_path):
    executable, powershell = shutil.which('codex'), shutil.which('pwsh')
    if not executable or not powershell:
        pytest.skip('Approved Codex and portable PowerShell are needed for the interactive regression.')
    import fcntl
    import pty
    import select
    import signal
    import struct
    import termios
    import tomllib

    home = tmp_path / ('Company AI user with spaces ' * 4)
    home.mkdir()
    assert len(os.fsencode(home / 'app-server-control/app-server-control.sock')) > 108
    catalog = home / 'models.json'
    catalog.write_text(json.dumps(candidate(COMPANY_MODELS, 32768)))
    auth = tmp_path / 'synthetic-auth.sh'
    auth.write_text("#!/bin/sh\nprintf 'synthetic-test-key'\n")
    auth.chmod(0o700)
    generated = subprocess.check_output(
        [
            powershell,
            '-NoProfile',
            '-NonInteractive',
            '-Command',
            r"""
$ast = [Management.Automation.Language.Parser]::ParseFile($env:COMPANYAI_TEST_SOURCE, [ref]$null, [ref]$null)
foreach ($function in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]}, $true)) {
    . ([ScriptBlock]::Create($function.Extent.Text))
}
$policy = [pscustomobject]@{ default_model = 'gemini-3.8-flash'; base_url = 'http://127.0.0.1:9/v1' }
New-CompanyCodexConfig $policy 'synthetic-helper.ps1' $env:COMPANYAI_TEST_CATALOG $env:COMPANYAI_TEST_AUTH
""",
        ],
        env={
            **os.environ,
            'COMPANYAI_TEST_SOURCE': str(Path('examples/company_ai_access/windows/Setup-CompanyAI.ps1').resolve()),
            'COMPANYAI_TEST_CATALOG': str(catalog),
            'COMPANYAI_TEST_AUTH': str(auth),
        },
        text=True,
        timeout=15,
    )
    parsed = tomllib.loads(generated)
    assert parsed['features']['daemon_auto_start'] is False
    assert parsed['model_provider'] == 'company_ai'
    assert parsed['sandbox_mode'] == 'workspace-write'
    assert parsed['approval_policy'] == 'on-request'
    (home / 'config.toml').write_text(generated)
    env = {**os.environ, 'CODEX_HOME': str(home), 'TERM': 'xterm-256color'}
    features = subprocess.check_output([executable, 'features', 'list'], env=env, text=True, timeout=10)
    assert re.search(r'^daemon_auto_start\s+stable\s+false$', features, re.MULTILINE)
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
    process = subprocess.Popen(
        [executable, '--no-alt-screen'],
        cwd=tmp_path,
        env=env,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    output = b''
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                output += chunk
                if b'\x1b[6n' in chunk:
                    os.write(master, b'\x1b[1;1R')
                if b'\x1b[?u' in chunk:
                    os.write(master, b'\x1b[?0u')
            if b'/permissions' in output and b'Gemini' in output:
                break
            if process.poll() is not None:
                break
        assert process.poll() is None, output.decode(errors='replace')[-3000:]
        assert b'/permissions' in output and b'Gemini' in output, 'Interactive CLI did not become ready.'
        assert b'SUN_LEN' not in output and b'app server did not become ready' not in output
        assert not (home / 'app-server-control').exists(), 'Interactive CLI attempted daemon startup.'
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
        os.close(master)


def test_installed_codex_picker_contains_exactly_the_three_company_models(tmp_path):
    executable = shutil.which('codex')
    if not executable:
        pytest.skip('Codex CLI is not installed.')
    catalog = tmp_path / 'models.json'
    catalog.write_text(json.dumps(candidate(COMPANY_MODELS, 32768)))
    home = tmp_path / 'picker-home'
    home.mkdir()
    (home / 'config.toml').write_text(
        '\n'.join(
            [
                'model = ' + json.dumps(COMPANY_MODELS[0]),
                'model_provider = "company_ai"',
                'model_catalog_json = ' + json.dumps(str(catalog)),
                '[model_providers.company_ai]',
                'name = "Company AI"',
                'base_url = "http://127.0.0.1:9/v1"',
                'wire_api = "responses"',
            ]
        )
        + '\n'
    )

    async def listing():
        proc = await asyncio.create_subprocess_exec(
            executable,
            'app-server',
            env={**os.environ, 'CODEX_HOME': str(home)},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            for request in (
                {
                    'id': 0,
                    'method': 'initialize',
                    'params': {
                        'clientInfo': {
                            'name': 'company_catalog_test',
                            'version': '1.0.0',
                        },
                        'capabilities': {'experimentalApi': True},
                    },
                },
                {'method': 'initialized'},
                {'id': 1, 'method': 'model/list', 'params': {'includeHidden': False}},
            ):
                proc.stdin.write((json.dumps(request) + '\n').encode())
            await proc.stdin.drain()
            while line := await asyncio.wait_for(proc.stdout.readline(), 10):
                response = json.loads(line)
                if response.get('id') == 1:
                    return response['result']['data']
            raise AssertionError('Codex exited before returning its model list.')
        finally:
            proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except TimeoutError:
                proc.kill()
                await proc.wait()

    models = asyncio.run(listing())
    assert [model['model'] for model in models] == list(COMPANY_MODELS)
    assert [model['displayName'] for model in models] == [
        'Gemini 3.8 Flash',
        'Gemini 3.5 Flash Lite',
        'Gemini 4 Argon',
    ]
    assert [model['defaultReasoningEffort'] for model in models] == [
        'low',
        'none',
        'medium',
    ]


@pytest.mark.parametrize('model', COMPANY_MODELS)
def test_installed_codex_tool_loop(tmp_path, model, request, mcp_config='', mcp_tool_suffix=None):
    executable = shutil.which('codex')
    if not executable:
        pytest.skip('Codex CLI is not installed; run this qualification in the CLI acceptance environment.')
    release = subprocess.check_output([executable, '--version'], text=True).strip().removeprefix('codex-cli ')
    catalog = tmp_path / 'models.json'
    catalog.write_text(json.dumps(candidate(COMPANY_MODELS, 32768)))
    settings = Settings(
        origin='https://ai-dev.changdar-server.mooo.com',
        state_dir=tmp_path / 'registry',
        litellm_url='http://127.0.0.1:14002',
        master_key='synthetic-master',
        models=COMPANY_MODELS,
        inference_cidrs=('127.0.0.1/32',),
        environment='test',
        codex_models=COMPANY_MODELS,
        codex_release=release,
        codex_catalog_path=catalog,
    )

    class Cleanup:
        addCleanup = staticmethod(request.addfinalizer)

    try:
        access_database(Cleanup())
    except unittest.SkipTest as exc:
        pytest.skip(str(exc))
    store = Store(settings.state_dir)
    service = Service(settings, Repository(store), FakeLiteLLM(), FakeDirectory())

    async def enroll():
        verifier = 'x' * 43
        started = await service.start('CLI-TEST', challenge(verifier), None, '127.0.0.1')
        await service.approve('cli-test-user', started['request_id'], started['user_code'])
        ready = await service.poll(started['request_id'], verifier)
        await service.ack(started['request_id'], verifier)
        return ready

    ready = {}
    forwarded = []
    chunks_sent = []

    class Stream(httpx.AsyncByteStream):
        def __init__(self, events):
            self.events = events

        async def __aiter__(self):
            for event in self.events:
                chunks_sent.append(event['type'])
                yield ('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n').encode()
                await asyncio.sleep(0.02)

    async def upstream(request):
        body = json.loads(request.content)
        forwarded.append((body, dict(request.headers)))
        assert body['model'] == model
        assert body['user'] == 'cli-test-user'
        assert body['metadata']['user_id'] == 'cli-test-user'
        assert request.headers['x-litellm-customer-id'] == 'cli-test-user'
        assert body['store'] is False
        results = [item for item in body['input'] if item.get('type') == 'function_call_output']
        if not results:
            def functions_in(tools, namespace=None):
                for tool in tools:
                    if tool.get('type') == 'function':
                        yield {**tool, 'test_namespace': namespace}
                    elif tool.get('type') == 'namespace':
                        yield from functions_in(tool.get('tools', []), tool['name'])
            functions = list(functions_in(body.get('tools', [])))
            shell = next((tool for tool in functions if (
                tool['name'].endswith(mcp_tool_suffix) if mcp_tool_suffix else
                tool['name'] in {'shell_command', 'exec_command', 'shell'}
            )), None)
            assert shell is not None, 'CLI did not offer a local shell tool: ' + ','.join(
                tool['name'] for tool in functions
            )
            args = {'command': 'printf COMPANY_CODEX_TOOL_OK', 'workdir': str(tmp_path)}
            if shell['name'] == 'exec_command':
                args = {'cmd': 'printf COMPANY_CODEX_TOOL_OK', 'workdir': str(tmp_path)}
            if mcp_tool_suffix:
                args = {}
            item = {
                'type': 'function_call',
                'id': 'fc_test',
                'call_id': 'call_test',
                'name': shell['name'],
                'arguments': json.dumps(args),
                'status': 'completed',
            }
            if shell.get('test_namespace'):
                item['namespace'] = shell['test_namespace']
        else:
            assert results[-1]['call_id'] == 'call_test'
            assert ('all_owned_bindings' if mcp_tool_suffix else 'COMPANY_CODEX_TOOL_OK') in results[-1]['output']
            item = {
                'type': 'message',
                'id': 'msg_test',
                'role': 'assistant',
                'status': 'completed',
                'content': [
                    {
                        'type': 'output_text',
                        'text': 'CODEX_GATEWAY_OK',
                        'annotations': [],
                    }
                ],
            }
        response = {
            'id': 'resp_' + str(len(forwarded)),
            'object': 'response',
            'created_at': int(time.time()),
            'status': 'completed',
            'model': model,
            'output': [item],
            'usage': {'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110},
        }
        events = [
            {
                'type': 'response.created',
                'response': {**response, 'status': 'in_progress', 'output': []},
            },
            {
                'type': 'response.output_item.added',
                'output_index': 0,
                'item': {**item, 'status': 'in_progress'},
            },
            {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
            {'type': 'response.completed', 'response': response},
        ]
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Stream(events))

    @asynccontextmanager
    async def lifespan(app):
        async with (
            service.repository,
            httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client,
        ):
            app.state.company_ai_inference = InferenceProxy(settings, client)
            ready.update(await enroll())
            try:
                yield
            finally:
                await service.revoke('cli-test-user', ready['device_id'])

    app = FastAPI(lifespan=lifespan)
    app.state.company_ai_access = service
    app.include_router(inference_router())
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False, ws='none'))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()

    def stop_server():
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()

    request.addfinalizer(stop_server)
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    home = tmp_path / 'codex-home'
    home.mkdir()
    helper = home / 'credential-helper.py'
    helper.write_text('print(' + repr(ready['key']) + ')\n')
    helper.chmod(0o600)
    (home / 'config.toml').write_text(
        '\n'.join(
            [
                'model = ' + json.dumps(COMPANY_MODELS[0]),
                'model_provider = "company_ai"',
                'web_search = "disabled"',
                'model_catalog_json = ' + json.dumps(str(catalog)),
                'approval_policy = "never"',
                'sandbox_mode = "workspace-write"',
                '[features]',
                'plugins = false',
                'apps = false',
                '[model_providers.company_ai]',
                'name = "Company AI Test"',
                f'base_url = "http://127.0.0.1:{port}/company-ai/v1"',
                'wire_api = "responses"',
                'supports_websockets = false',
                '[model_providers.company_ai.auth]',
                'command = ' + json.dumps(sys.executable),
                'args = [' + json.dumps(str(helper)) + ']',
                'timeout_ms = 5000',
                'refresh_interval_ms = 300000',
            ]
        )
        + '\n' + mcp_config
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(('OPENAI_', 'CODEX_'))
    }
    env['CODEX_HOME'] = str(home)
    env.update(HTTPS_PROXY='http://127.0.0.1:9', HTTP_PROXY='http://127.0.0.1:9', NO_PROXY='127.0.0.1,localhost')
    try:
        result = subprocess.run(
            [
                executable,
                'exec',
                '--strict-config',
                '--skip-git-repo-check',
                '--ephemeral',
                '-C',
                str(tmp_path),
                '--json',
                '-m',
                model,
                'Call the company MCP connections_list tool, then reply CODEX_GATEWAY_OK.' if mcp_tool_suffix else
                'Run a local shell command to print COMPANY_CODEX_TOOL_OK, then reply CODEX_GATEWAY_OK.',
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert ready['key'] not in result.stdout + result.stderr, 'Credential appeared in CLI diagnostics.'
        assert result.returncode == 0, result.stderr[-5000:] + result.stdout[-3000:]
        assert 'CODEX_GATEWAY_OK' in result.stdout, result.stdout[-3000:]
        assert len(forwarded) >= 2
        assert chunks_sent.count('response.completed') >= 2
    finally:
        stop_server()
