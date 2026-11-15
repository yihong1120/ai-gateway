import asyncio
import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from examples.ai_usage.adapters.webui import install_webui_usage
from examples.deployment.dev import run, verify
from examples.deployment.dev.app import dev_role


@pytest.mark.parametrize('claims', [{}, {'dev_roles': []}, {'dev_roles': ['admin']}, {'dev_roles': 'dev-admin'}])
def test_shared_identity_roles_do_not_grant_dev_access(claims):
    with pytest.raises(HTTPException) as error:
        asyncio.run(dev_role(None, claims))
    assert error.value.status_code == 403


@pytest.mark.parametrize('role,expected', [('dev-user', 'user'), ('dev-admin', 'admin')])
def test_explicit_dev_membership(role, expected):
    assert asyncio.run(dev_role(None, {'dev_roles': [role]})) == expected


@pytest.mark.parametrize('role,expected', [('dev-user', 'user'), ('dev-admin', 'admin')])
def test_current_webui_role_hook_keyword(role, expected):
    assert asyncio.run(dev_role(None, {'dev_roles': [role]}, token_claims=None)) == expected
    assert asyncio.run(dev_role(None, {}, token_claims={'dev_roles': [role]})) == expected


@pytest.mark.parametrize('roles', [[], ['admin'], 'dev-admin', [{'role': 'dev-admin'}]])
def test_userinfo_roles_cannot_be_overridden_by_token_claims(roles):
    with pytest.raises(HTTPException) as error:
        asyncio.run(dev_role(None, {'dev_roles': roles}, token_claims={'dev_roles': ['dev-admin']}))
    assert error.value.status_code == 403


def test_token_roles_do_not_grant_shared_realm_admin_dev_access():
    with pytest.raises(HTTPException) as error:
        asyncio.run(dev_role(None, {}, token_claims={'realm_access': {'roles': ['admin']}}))
    assert error.value.status_code == 403


def usage_module():
    path = Path(__file__).resolve().parents[3] / 'examples/openwebui/functions/my_daily_usage_pipe.py'
    spec = importlib.util.spec_from_file_location('dev_usage_pipe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('port,budget', [(14002, '1.00'), (4000, '4.00')])
def test_usage_queries_selected_host_only_with_verified_subject(monkeypatch, port, budget):
    monkeypatch.setenv('LITELLM_USAGE_BASE_URL', f'http://127.0.0.1:{port}/')
    monkeypatch.setenv('LITELLM_DAILY_BUDGET_USD', budget)
    monkeypatch.setenv('LITELLM_USAGE_READER_KEY', 'selected-reader')
    pipe = usage_module().Pipe()
    app = FastAPI()
    install_webui_usage(app)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'data': [], 'total_pages': 1})

    client_type = httpx.AsyncClient

    def create(**options):
        return client_type(**options, transport=httpx.MockTransport(handler))

    async def query():
        with patch('examples.ai_usage.adapters.webui.httpx.AsyncClient', side_effect=create):
            async with app.router.lifespan_context(app):
                return await pipe.pipe(
                    {'user': 'forged-sub'}, {'oauth': {'oidc': {'sub': 'verified-sub'}}},
                    Request({'type': 'http', 'app': app}),
                )

    result = asyncio.run(query())
    assert len(requests) == 1
    assert str(requests[0].url).startswith(f'http://127.0.0.1:{port}/spend/logs/v2?')
    assert requests[0].url.params['end_user'] == 'verified-sub'
    assert requests[0].headers['authorization'] == 'Bearer selected-reader'
    assert f'/ US${budget}' in result


def test_usage_missing_identity_does_not_query_backend():
    pipe = usage_module().Pipe()
    with patch('examples.ai_usage.adapters.webui.httpx.AsyncClient') as query:
        result = asyncio.run(pipe.pipe({'user': 'forged-sub'}, __user__={'oauth': {}}))
    assert 'Keycloak' in result
    query.assert_not_called()


@pytest.mark.parametrize('url', [
    'postgresql://litellm_dev:x@100.93.178.78:5432/litellm_dev',
    'postgresql://litellm:x@127.0.0.1:15432/litellm',
    'postgresql://litellm_dev:x@127.0.0.1:5432/litellm_dev',
])
def test_runner_rejects_production_or_shared_database(tmp_path, monkeypatch, url):
    monkeypatch.setattr(run, 'CONFIG', tmp_path)
    (tmp_path / 'litellm.json').write_text(json.dumps({'LITELLM_DATABASE_URL': url}))
    with pytest.raises(RuntimeError, match='non-dev'):
        run.load_environment('litellm')


@pytest.mark.parametrize('prisma', [
    None,
    'postgresql://production:synthetic@127.0.0.1:5432/litellm',
    'postgresql://litellm_dev:different@127.0.0.1:15432/litellm_dev',
])
def test_runner_rejects_mismatched_prisma_before_environment_changes(tmp_path, monkeypatch, prisma):
    values = {
        'LITELLM_DATABASE_URL': 'postgresql://litellm_dev:synthetic@127.0.0.1:15432/litellm_dev',
        'UNRELATED_SETTING': 'must-not-appear',
    }
    if prisma is not None:
        values['DATABASE_URL'] = prisma
    (tmp_path / 'litellm.json').write_text(json.dumps(values))
    monkeypatch.setattr(run, 'CONFIG', tmp_path)
    monkeypatch.setattr(os, 'environ', {'EXISTING': 'unchanged'})
    with patch.object(run.os, 'chdir') as change_directory:
        with pytest.raises(RuntimeError, match='mismatched Prisma'):
            run.load_environment('litellm')
    assert dict(os.environ) == {'EXISTING': 'unchanged'}
    change_directory.assert_not_called()


def test_runner_accepts_one_dev_database_for_litellm_and_prisma(tmp_path, monkeypatch):
    database = 'postgresql://litellm_dev:synthetic@127.0.0.1:15432/litellm_dev'
    values = {'LITELLM_DATABASE_URL': database, 'DATABASE_URL': database}
    (tmp_path / 'litellm.json').write_text(json.dumps(values))
    monkeypatch.setattr(run, 'CONFIG', tmp_path)
    monkeypatch.setattr(os, 'environ', {})
    monkeypatch.setattr(run.sys, 'path', run.sys.path.copy())
    with patch.object(run.os, 'chdir') as change_directory:
        assert run.load_environment('litellm') == values
    assert os.environ['LITELLM_DATABASE_URL'] == os.environ['DATABASE_URL'] == database
    assert os.environ['PYTHON_DOTENV_DISABLED'] == '1'
    change_directory.assert_called_once_with(tmp_path)


@pytest.mark.parametrize('expected_parallel', [3, 11])
def test_litellm_verification_checks_database_and_selected_runtime_limit(expected_parallel):
    requests = []

    def respond(request):
        requests.append(request)
        assert str(request.url).startswith('http://127.0.0.1:14002/')
        assert request.headers['authorization'] == 'Bearer dev-synthetic-master'
        if request.url.path == '/health/readiness':
            return httpx.Response(200, json={'status': 'connected'})
        assert request.url.path == '/config/field/info'
        assert request.url.params['field_name'] == 'global_max_parallel_requests'
        return httpx.Response(200, json={'field_value': expected_parallel})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        verify.verify_litellm(client, 'dev-synthetic-master', expected_parallel)
    assert [request.url.path for request in requests] == ['/health/readiness', '/config/field/info']


def test_litellm_verification_stops_when_database_is_not_ready():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(503, json={'status': 'disconnected'})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(AssertionError, match='database readiness failed: HTTP 503'):
            verify.verify_litellm(client, 'dev-synthetic-master', 7)
    assert [request.url.path for request in requests] == ['/health/readiness']


def test_litellm_verification_rejects_runtime_limit_from_another_configuration():
    def respond(request):
        if request.url.path == '/health/readiness':
            return httpx.Response(200, json={'status': 'connected'})
        return httpx.Response(200, json={'field_value': 500})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(AssertionError, match='runtime concurrency differs from generated configuration'):
            verify.verify_litellm(client, 'dev-synthetic-master', 7)
