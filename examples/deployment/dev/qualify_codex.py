"""Dev-only real Codex/LiteLLM qualification using a disposable local gateway.

Uses the existing dev operator's membership and shared budget. Creates, revokes
and deletes one narrowly scoped dev key. Never changes live service settings,
registries, model routes or budget limits. Real model requests consume dev quota.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from importlib.metadata import version
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sys
import tempfile
import time

import httpx
import uvicorn
import uuid
import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from examples.deployment.dev.run import CONFIG, load_environment
from examples.company_ai_access.routers.inference import inference_router
from examples.company_ai_access.services.inference import InferenceProxy
from examples.company_ai_access.storage.repository import Repository
from examples.company_ai_access.protocols.codex_catalog import candidate
from examples.company_ai_access.config import Settings
from examples.company_ai_access.services.devices import Service, challenge
from examples.company_ai_access.storage.registry import Store, digest
from examples.company_ai_access.adapters.upstream import Directory, LiteLLM
from fastapi import FastAPI


@contextmanager
def temporary_registry_schema():
    schema = 'company_ai_access_qualify_' + uuid.uuid4().hex
    previous = os.environ.get('AI_ACCESS_DB_SCHEMA')
    os.environ['AI_ACCESS_DB_SCHEMA'] = schema
    try:
        yield
    finally:
        with psycopg.connect(os.environ['AI_ACCESS_DATABASE_URL']) as db:
            db.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))
        if previous is None:
            os.environ.pop('AI_ACCESS_DB_SCHEMA', None)
        else:
            os.environ['AI_ACCESS_DB_SCHEMA'] = previous


async def qualify(args):
    load_environment('ai-access')
    settings = Settings.from_env()
    operator = json.loads((CONFIG / 'operator.json').read_text())
    if (
        settings.environment != 'dev'
        or settings.litellm_url != 'http://127.0.0.1:14002'
        or args.model not in settings.models
    ):
        raise RuntimeError('Only the existing isolated dev gateway/model allowlist may be used.')
    executable = shutil.which(args.codex)
    if not executable:
        raise RuntimeError('Install the Codex CLI to qualify first.')
    proc = await asyncio.create_subprocess_exec(executable, '--version', stdout=asyncio.subprocess.PIPE)
    stdout, _ = await proc.communicate()
    release = stdout.decode().strip().removeprefix('codex-cli ')
    started = datetime.now(timezone.utc)
    report = {
        'environment': 'dev',
        'at': started.isoformat(),
        'model': args.model,
        'codex_release': release,
        'litellm_package_in_runner': version('litellm'),
        'available_models': list(settings.models),
        'default_model': settings.models[0],
        'selection': 'codex exec -m',
        'checks': {},
        'success': False,
    }
    cache = Path.home() / '.cache'
    with (
        temporary_registry_schema(),
        tempfile.TemporaryDirectory(prefix='company-ai-codex-qualification-', dir=cache) as temporary,
    ):
        directory = Path(temporary)
        catalog = directory / 'models.json'
        catalog.write_text(json.dumps(candidate(settings.models, 32768)))
        settings = replace(
            settings,
            state_dir=directory / 'registry',
            codex_models=settings.models,
            codex_catalog_path=catalog,
            codex_release=release,
            inference_cidrs=('127.0.0.1/32',),
            inference_deny_cidrs=(),
        )
        calls = []

        async def observed_request(request):
            if request.url.path == '/v1/responses':
                body = json.loads(request.content)
                calls.append(
                    {
                        'attribution': body.get('user') == operator['id']
                        and body.get('metadata', {}).get('user_id') == operator['id']
                        and request.headers.get('x-litellm-customer-id') == operator['id'],
                        'tool_results': sum(
                            item.get('type') in {'function_call_output', 'custom_tool_call_output'}
                            for item in body.get('input', [])
                            if isinstance(item, dict)
                        ),
                        'started': time.monotonic(),
                        'request_shape': {
                            'stream': body.get('stream'),
                            'max_output_tokens': body.get('max_output_tokens'),
                            'reasoning_effort': (body.get('reasoning') or {}).get('effort'),
                            'input_items': len(body.get('input', [])),
                            'tools': len(body.get('tools', [])),
                        },
                    }
                )
                request.extensions['qualification_call'] = calls[-1]
                print(
                    json.dumps(
                        {
                            'progress': 'request',
                            'model': args.model,
                            'shape': calls[-1]['request_shape'],
                        }
                    ),
                    flush=True,
                )

        async def observed_response(response):
            if response.request.url.path == '/v1/responses':
                call = response.request.extensions['qualification_call']
                call['status'] = response.status_code
                call['header_seconds'] = round(time.monotonic() - call['started'], 3)
                call['route_model'] = response.headers.get('x-litellm-model-group', '')
                call['gateway_version'] = response.headers.get('x-litellm-version', '')
                call['retries'] = response.headers.get('x-litellm-attempted-retries', '')
                print(
                    json.dumps(
                        {
                            'progress': 'response',
                            'model': args.model,
                            'status': call['status'],
                            'header_seconds': call['header_seconds'],
                            'retries': call['retries'],
                        }
                    ),
                    flush=True,
                )

        registry = await asyncio.to_thread(Store, settings.state_dir)
        async with (
            Repository(registry) as repository,
            httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                timeout=120,
                event_hooks={
                    'request': [observed_request],
                    'response': [observed_response],
                },
            ) as client,
        ):
            llm = LiteLLM(settings, client)
            state = Service(settings, repository, llm, Directory(settings, client))
            verifier = secrets.token_urlsafe(48)
            row = await state.start('DEV-CODEX-QUALIFY', challenge(verifier), None, '127.0.0.1')
            ready = None
            try:
                approved = await state.approve(operator['id'], row['request_id'], row['user_code'])
                ready = await state.poll(row['request_id'], verifier)
                await llm.ensure_key(
                    (await state.repository.get('device', approved['device_id'])),
                    ready['key'],
                )
                report['checks']['key_route_reconciliation'] = True
                await state.ack(row['request_id'], verifier)
                report['checks']['membership_and_enrollment'] = True

                async def lifespan(app):
                    yield

                app = FastAPI(lifespan=asynccontextmanager(lifespan))
                app.state.company_ai_access = state
                app.state.company_ai_inference = InferenceProxy(settings, client)
                app.include_router(inference_router())
                sock = socket.socket()
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
                server = uvicorn.Server(
                    uvicorn.Config(
                        app,
                        log_level='critical',
                        access_log=False,
                        ws='none',
                        timeout_graceful_shutdown=5,
                    )
                )
                task = asyncio.create_task(server.serve(sockets=[sock]))
                home = directory / 'codex-home'
                home.mkdir(mode=0o700)
                helper = home / 'helper.py'
                helper.write_text('print(' + repr(ready['key']) + ')\n')
                helper.chmod(0o600)
                (home / 'config.toml').write_text(
                    '\n'.join(
                        [
                            'model = ' + json.dumps(settings.models[0]),
                            'model_provider = "company_ai"',
                            'web_search = "disabled"',
                            'model_catalog_json = ' + json.dumps(str(catalog)),
                            'approval_policy = "never"',
                            'sandbox_mode = "workspace-write"',
                            '[model_providers.company_ai]',
                            'name = "Company AI Qualification"',
                            f'base_url = "http://127.0.0.1:{port}/company-ai/v1"',
                            'wire_api = "responses"',
                            'supports_websockets = false',
                            '[model_providers.company_ai.auth]',
                            'command = ' + json.dumps(sys.executable),
                            'args = [' + json.dumps(str(helper)) + ']',
                            'timeout_ms = 10000',
                            'refresh_interval_ms = 300000',
                        ]
                    )
                    + '\n'
                )
                work = directory / 'work'
                work.mkdir()
                (work / 'calc.py').write_text('def add(a, b):\n    return a - b\n')
                env = {
                    key: value
                    for key, value in os.environ.items()
                    if key not in {'OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL'}
                    and not key.startswith('AI_ACCESS_')
                }
                env['CODEX_HOME'] = str(home)
                try:
                    while not server.started and not task.done():
                        await asyncio.sleep(0.02)
                    if task.done():
                        await task
                        raise RuntimeError('Disposable dev gateway failed to start.')
                    proc = await asyncio.create_subprocess_exec(
                        executable,
                        'exec',
                        '--strict-config',
                        '--skip-git-repo-check',
                        '--ephemeral',
                        '-C',
                        str(work),
                        '--json',
                        '-m',
                        args.model,
                        'Fix calc.py so add(a,b) adds its two arguments. Use a local shell tool to verify add(2,3)==5 and print COMPANY_CODEX_TOOL_OK. Do not delegate or access the network. Finally reply CODEX_GATEWAY_OK.',
                        env=env,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    try:
                        stdout, stderr = await asyncio.wait_for(proc.communicate(), args.timeout)
                    except TimeoutError:
                        proc.kill()
                        stdout, stderr = await proc.communicate()
                        report['failure'] = 'cli_timeout'
                    else:
                        report['cli_exit_code'] = proc.returncode
                    transcript = stdout.decode(errors='replace')
                    diagnostic = stderr.decode(errors='replace')
                    report['checks']['credential_not_in_cli_output'] = ready['key'] not in transcript + diagnostic
                    report['checks']['cli_final_answer'] = 'CODEX_GATEWAY_OK' in transcript and proc.returncode == 0
                    report['checks']['tool_output_replayed'] = any(call['tool_results'] > 0 for call in calls)
                    report['checks']['all_requests_attributed'] = bool(calls) and all(
                        call['attribution'] for call in calls
                    )
                    # Run only the one-line arithmetic fixture, with no imports/network.
                    fixed = (work / 'calc.py').read_text()
                    namespace = {}
                    if len(fixed) < 200 and fixed.strip() in {
                        'def add(a, b):\n    return a + b',
                        'def add(a,b):\n    return a+b',
                    }:
                        exec(fixed, {'__builtins__': {}}, namespace)
                        report['checks']['fixture_fixed'] = namespace['add'](2, 3) == 5
                    else:
                        report['checks']['fixture_fixed'] = False
                    report['request_count'] = len(calls)
                    report['request_shapes'] = [call['request_shape'] for call in calls]
                    report['header_seconds'] = [call.get('header_seconds') for call in calls]
                    report['upstream_statuses'] = [call.get('status') for call in calls]
                    report['gateway_versions'] = sorted(
                        {call.get('gateway_version') for call in calls if call.get('gateway_version')}
                    )
                    if not report['checks']['cli_final_answer']:
                        report['diagnostic_markers'] = [
                            marker
                            for marker in (
                                'ai_access_invalid_input',
                                'ai_access_hosted_tools_not_allowed',
                                'ai_access_upstream_rejected',
                                'ai_access_budget_exceeded',
                                'ai_access_stateless_responses_required',
                                '401',
                                '403',
                                '429',
                                'unknown field',
                                'failed to parse',
                            )
                            if marker in transcript + diagnostic
                        ]
                    # Inspect only this temporary key's own persisted records.
                    end_date = str((started + timedelta(days=1)).date())
                    deadline = time.monotonic() + 35
                    logs = []
                    while calls and time.monotonic() < deadline:
                        response = await client.get(
                            settings.litellm_url + '/spend/logs/v2',
                            params={
                                'api_key': digest(ready['key']),
                                'start_date': str(started.date()),
                                'end_date': end_date,
                            },
                            headers={'Authorization': 'Bearer ' + settings.master_key},
                        )
                        if response.status_code != 200:
                            report['spend_check_status'] = response.status_code
                            break
                        payload = response.json()
                        rows = payload if isinstance(payload, list) else payload.get('data', [])
                        logs = [item for item in rows if item.get('api_key') == digest(ready['key'])]
                        if len(logs) >= len(calls):
                            break
                        await asyncio.sleep(2)
                    report['checks']['persisted_employee_attribution'] = bool(logs) and all(
                        item.get('end_user') == operator['id'] for item in logs
                    )
                    report['checks']['persisted_nonzero_cost'] = bool(logs) and all(
                        float(item.get('spend') or 0) > 0 for item in logs
                    )
                    report['persisted_model_routes'] = sorted({item.get('model') for item in logs if item.get('model')})
                    report['checks']['selected_model_routed'] = bool(logs) and all(
                        item.get('model') == 'vertex_ai/' + args.model for item in logs
                    )
                    report['spend_rows'] = len(logs)
                    report['spend_usd'] = sum(float(item.get('spend') or 0) for item in logs)
                finally:
                    server.should_exit = True
                    await task
                    sock.close()
                await state.revoke(operator['id'], ready['device_id'])
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url=settings.origin) as native:
                    denied = await native.post(
                        '/company-ai/v1/responses',
                        headers={'Authorization': 'Bearer ' + ready['key']},
                        json={'model': args.model, 'input': 'Should be denied.'},
                    )
                report['checks']['revoked_key_denied'] = denied.status_code == 401
            except Exception as exc:
                report['failure'] = type(exc).__name__
                if hasattr(exc, 'status_code'):
                    report['failure_status'] = exc.status_code
                    report['failure_code'] = getattr(exc, 'detail', {}).get('code')
            finally:
                device = (
                    (await state.repository.get('device', approved['device_id'])) if 'approved' in locals() else None
                )
                raw = (
                    ready['key']
                    if ready
                    else state.repository.open(device['key_box'])
                    if device and device.get('key_box')
                    else None
                )
                if device and raw:
                    await state.revoke(operator['id'], device['id'])
                    await llm.call('/key/delete', {'keys': [raw]})
                    report['checks']['temporary_key_deleted'] = True
    report['success'] = not report.get('failure') and bool(report['checks']) and all(report['checks'].values())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--codex', default='codex')
    parser.add_argument(
        '--timeout',
        type=int,
        default=300,
        help='Whole coding task deadline, including all model/tool turns and provider retries',
    )
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output = args.output.resolve()
    report = asyncio.run(qualify(args))
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report['success'] else 1)


if __name__ == '__main__':
    main()
