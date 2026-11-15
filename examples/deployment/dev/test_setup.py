from __future__ import annotations

import importlib.metadata
import shlex
import sys
import tomllib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from examples.deployment.dev import setup
from examples.shared.config import read_service


def test_invalid_catalog_is_rejected_before_deployment_side_effects(tmp_path, monkeypatch):
    source = tmp_path / 'synthetic.toml'
    read = Mock(return_value={'litellm': {}, 'webui': {}})
    execute = Mock()
    provision = Mock()
    monkeypatch.setattr(setup.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(setup, 'config_path', lambda: source)
    monkeypatch.setattr(setup, 'read_services', read)
    monkeypatch.setattr(setup, 'litellm_config', Mock(side_effect=ValueError('Invalid LiteLLM catalog.')))
    monkeypatch.setattr(setup, 'run', execute)
    monkeypatch.setattr(setup, 'provision', provision)
    monkeypatch.setattr(setup, 'CONFIG', tmp_path / 'generated')
    monkeypatch.setattr(setup, 'DATA', tmp_path / 'data')
    with pytest.raises(ValueError, match='Invalid LiteLLM catalog'):
        setup.main()
    read.assert_called_once_with('litellm', 'webui', path=source)
    execute.assert_not_called()
    provision.assert_not_called()
    assert not setup.CONFIG.exists()
    assert not setup.DATA.exists()


@pytest.mark.parametrize('arguments', [('up', '-d', '--wait', 'postgres'), ('stop', 'postgres'), ('exec', '-T', 'postgres', 'psql')])
def test_dev_compose_always_selects_root_file_and_isolated_database_project(tmp_path, monkeypatch, arguments):
    repository = tmp_path / 'repository with spaces'
    monkeypatch.setattr(setup, 'ROOT', repository)
    command = setup.dev_compose(*arguments)
    assert command[:4] == ['/usr/bin/docker', 'compose', '--env-file', '/dev/null']
    assert command[command.index('-f') + 1] == str(repository / 'docker-compose.yml')
    assert command[command.index('-p') + 1] == 'ai-gateway-dev'
    assert command[command.index('--profile') + 1] == 'dev-db'
    assert command[-len(arguments):] == list(arguments)
    assert shlex.split(shlex.join(command)) == command


def write_catalog(tmp_path, monkeypatch, catalog, policies):
    directory = tmp_path / 'examples/litellm'
    (directory / 'profiles').mkdir(parents=True)
    (directory / 'config.yaml').write_text(yaml.safe_dump(catalog))
    (directory / 'profiles/dev.yaml').write_text(yaml.safe_dump(policies))
    monkeypatch.setattr(setup, 'ROOT', tmp_path)
    return directory


def test_dev_policies_preserve_every_catalog_route_and_rate():
    catalog_path = setup.ROOT / 'examples/litellm/config.yaml'
    original = yaml.safe_load(catalog_path.read_text())
    dev = setup.litellm_config()
    assert dev['model_list'] == original['model_list']
    assert dev['general_settings'] == {**original['general_settings'], 'global_max_parallel_requests': 2}
    assert dev['litellm_settings'] == {
        **original['litellm_settings'], 'max_end_user_budget_id': 'openwebui-dev-daily-usd-1',
    }
    assert {k: v for k, v in dev.items() if k not in ('general_settings', 'litellm_settings')} == {
        k: v for k, v in original.items() if k not in ('general_settings', 'litellm_settings')
    }
    dev['general_settings']['global_max_parallel_requests'] = 999
    assert setup.litellm_config()['general_settings']['global_max_parallel_requests'] == 2
    assert yaml.safe_load(catalog_path.read_text()) == original


def test_dev_overlay_preserves_duplicate_model_routes(tmp_path, monkeypatch):
    catalog = {
        'model_list': [
            {'model_name': 'shared', 'litellm_params': {'model': 'openai/first', 'input_cost_per_token': 0.000001}},
            {'model_name': 'shared', 'litellm_params': {'model': 'openai/second', 'input_cost_per_token': 0.000002}},
        ],
        'general_settings': {'global_max_parallel_requests': 500},
        'router_settings': {'routing_strategy': 'simple-shuffle'},
    }
    write_catalog(tmp_path, monkeypatch, catalog, {'general_settings': {'global_max_parallel_requests': 2}})
    dev = setup.litellm_config()
    assert dev == {**catalog, 'general_settings': {'global_max_parallel_requests': 2}}


def test_dev_model_environment_excludes_production_management_and_database_credentials():
    config = setup.litellm_config()
    values = {
        'LITELLM_MASTER_KEY': 'never-copy', 'LITELLM_DATABASE_URL': 'never-copy',
        'DATABASE_URL': 'never-copy', 'UNRELATED_SECRET': 'never-copy',
        'AZURE_AI_API_KEY': 'synthetic-model-key',
        'LOCAL_MODEL_API_BASE': 'http://production.test/v1',
        'LOCAL_MODEL_DEV_API_BASE': 'http://development.test/v1',
    }
    assert setup.model_environment(config, values) == {
        'AZURE_AI_API_KEY': 'synthetic-model-key',
        'LOCAL_MODEL_API_BASE': 'http://development.test/v1',
    }
    assert values['LOCAL_MODEL_API_BASE'] == 'http://production.test/v1'


def test_nested_provider_references_are_selected_without_unrelated_configuration():
    config = {
        'model_list': [{
            'litellm_params': {
                'provider': [{'credentials': 'os.environ/VERTEX_CREDENTIALS'}],
                'api_key': 'os.environ/PROVIDER_KEY', 'unset': 'os.environ/UNSET',
            },
        }], 'general_settings': {'master_key': 'os.environ/LITELLM_MASTER_KEY'},
    }
    assert setup.model_environment(
        config, {
            'VERTEX_CREDENTIALS': 'synthetic-credentials', 'PROVIDER_KEY': 'synthetic-provider-key',
            'UNSET': '', 'LITELLM_MASTER_KEY': 'never-copy', 'OTHER': 'never-copy',
        },
    ) == {'VERTEX_CREDENTIALS': 'synthetic-credentials', 'PROVIDER_KEY': 'synthetic-provider-key'}


def test_template_covers_model_environment_and_litellm_versioned_environment():
    values = read_service('litellm', setup.ROOT / 'config.example.toml')
    assert set(setup.model_environment(setup.litellm_config(), values)) == {
        'AZURE_AI_API_KEY', 'AZURE_AI_API_BASE', 'LOCAL_MODEL_API_KEY', 'LOCAL_MODEL_API_BASE',
        'VERTEX_PROJECT', 'VERTEX_LOCATION', 'VERTEX_CREDENTIALS',
    }
    project = tomllib.loads((setup.ROOT / 'pyproject.toml').read_text())
    assert 'litellm[proxy]==' + setup.LITELLM_VERSION in project['dependency-groups']['litellm']
    assert setup.LITELLM_VERSION == '1.109.1'


@pytest.mark.parametrize('dev_endpoint', [None, ''])
def test_missing_dev_local_endpoint_fails_before_deployment(tmp_path, monkeypatch, dev_endpoint):
    values = {'LOCAL_MODEL_API_BASE': 'http://production.test/v1', 'LOCAL_MODEL_DEV_API_BASE': dev_endpoint}
    execute, provision = Mock(), Mock()
    monkeypatch.setattr(setup.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(setup, 'read_services', Mock(return_value={'litellm': values, 'webui': {}}))
    monkeypatch.setattr(setup, 'CONFIG', tmp_path / 'generated')
    monkeypatch.setattr(setup, 'DATA', tmp_path / 'data')
    monkeypatch.setattr(setup, 'run', execute)
    monkeypatch.setattr(setup, 'provision', provision)
    with pytest.raises(ValueError, match='LOCAL_MODEL_DEV_API_BASE is required'):
        setup.main()
    execute.assert_not_called()
    provision.assert_not_called()
    assert not setup.CONFIG.exists()
    assert not setup.DATA.exists()


@pytest.mark.parametrize(
    'catalog', [
        [], {}, {'model_list': {}}, {'model_list': [None]},
        {'model_list': [{'model_name': 'shared', 'litellm_params': []}]},
    ],
)
def test_invalid_catalog_structure_is_rejected(tmp_path, monkeypatch, catalog):
    write_catalog(tmp_path, monkeypatch, catalog, {})
    with pytest.raises(ValueError, match='Invalid LiteLLM catalog'):
        setup.litellm_config()


@pytest.mark.parametrize(
    'policies', [
        [], {'models': {'shared': {}}}, {'general_settings': []}, {'unknown_section': {}},
    ],
)
def test_dev_accepts_only_native_policy_sections(tmp_path, monkeypatch, policies):
    catalog = {'model_list': [{'model_name': 'shared', 'litellm_params': {}}], 'general_settings': {}}
    write_catalog(tmp_path, monkeypatch, catalog, policies)
    with pytest.raises(ValueError, match='Invalid LiteLLM development policies') as error:
        setup.litellm_config()
    assert 'shared' not in str(error.value)
    assert 'unknown_section' not in str(error.value)


@pytest.mark.parametrize('filename', ['config.yaml', 'profiles/dev.yaml'])
def test_invalid_yaml_does_not_echo_setting_values(tmp_path, monkeypatch, filename):
    directory = write_catalog(tmp_path, monkeypatch, {'model_list': []}, {})
    (directory / filename).write_text('setting: [synthetic-secret: [')
    with pytest.raises(ValueError, match='Invalid LiteLLM development configuration YAML') as error:
        setup.litellm_config()
    assert 'synthetic-secret' not in str(error.value)


@pytest.fixture
def prisma_runtime(tmp_path, monkeypatch):
    source = tmp_path / 'litellm/proxy/db/prisma_client.py'
    source.parent.mkdir(parents=True)
    source.write_text('        prisma_client._Prisma__engine = engine\n')
    versions = {'litellm': '1.109.1', 'prisma': '0.15.0'}
    monkeypatch.setattr(sys, 'prefix', str(tmp_path))
    monkeypatch.setattr(importlib.metadata, 'version', versions.__getitem__)
    monkeypatch.setattr(
        importlib.metadata, 'distribution',
        lambda _: SimpleNamespace(locate_file=lambda name: tmp_path / name),
    )
    return source, versions


def test_prisma_initialization_is_idempotent_and_returns_schema(prisma_runtime, capsys):
    source, _ = prisma_runtime
    exec(setup.PRISMA_INITIALIZATION, {})
    patched = source.read_bytes()
    assert patched == b'        prisma_client._engine = engine\n'
    assert capsys.readouterr().out.strip() == str(source.parent.parent / 'schema.prisma')
    exec(setup.PRISMA_INITIALIZATION, {})
    assert source.read_bytes() == patched
    assert capsys.readouterr().out.strip() == str(source.parent.parent / 'schema.prisma')


@pytest.mark.parametrize('package,version', [('litellm', '1.105.1'), ('prisma', '0.14.0')])
def test_prisma_initialization_rejects_unreviewed_versions(prisma_runtime, package, version):
    source, versions = prisma_runtime
    before = source.read_bytes()
    versions[package] = version
    with pytest.raises(RuntimeError, match='tested runtime versions'):
        exec(setup.PRISMA_INITIALIZATION, {})
    assert source.read_bytes() == before


def test_prisma_initialization_does_not_modify_foreign_environment(prisma_runtime, monkeypatch):
    source, _ = prisma_runtime
    before = source.read_bytes()
    monkeypatch.setattr(sys, 'prefix', str(source.parent / 'other-environment'))
    with pytest.raises(RuntimeError, match='this environment'):
        exec(setup.PRISMA_INITIALIZATION, {})
    assert source.read_bytes() == before


@pytest.mark.parametrize(
    'source_text', [
        'unexpected implementation\n',
        '        prisma_client._Prisma__engine = engine\n' * 2,
        '        prisma_client._Prisma__engine = engine\n        prisma_client._engine = engine\n',
    ],
)
def test_prisma_initialization_rejects_unknown_source_without_writing(prisma_runtime, source_text):
    source, _ = prisma_runtime
    source.write_text(source_text)
    with pytest.raises(RuntimeError, match='Unexpected LiteLLM Prisma engine implementation'):
        exec(setup.PRISMA_INITIALIZATION, {})
    assert source.read_text() == source_text


def test_setup_initializes_prisma_only_inside_selected_litellm_environment(tmp_path, monkeypatch):
    class StopBeforeGenerate(Exception):
        pass

    commands = []

    def execute(command, **kwargs):
        commands.append((command, kwargs))
        if command[1:4] == ['-m', 'prisma', 'generate']:
            raise StopBeforeGenerate

    schema = tmp_path / 'source.prisma'
    schema.write_text('provider = "prisma-client-py"\n')
    selected_python = tmp_path / 'dedicated-venv/bin/python'
    check_output = Mock(return_value=str(schema) + '\n')
    provision = Mock()
    monkeypatch.setattr(setup.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(
        setup, 'read_services', Mock(
            return_value={
                'litellm': {'LOCAL_MODEL_DEV_API_BASE': 'http://dev.test/v1'}, 'webui': {},
            },
        ),
    )
    monkeypatch.setattr(setup, 'CONFIG', tmp_path / 'generated')
    monkeypatch.setattr(setup, 'DATA', tmp_path / 'data')
    monkeypatch.setattr(setup, 'LLMPY', selected_python)
    monkeypatch.setattr(setup, 'run', execute)
    monkeypatch.setattr(setup.subprocess, 'check_output', check_output)
    monkeypatch.setattr(setup, 'provision', provision)
    with pytest.raises(StopBeforeGenerate):
        setup.main()
    assert commands[0][0][:7] == ['uv', 'sync', '--project', setup.ROOT, '--frozen', '--only-group', 'litellm']
    assert commands[0][1]['env']['UV_PROJECT_ENVIRONMENT'] == str(selected_python.parents[1])
    check_output.assert_called_once_with(
        [str(selected_python), '-c', setup.PRISMA_INITIALIZATION], text=True, cwd=setup.ROOT,
    )
    assert commands[1][0][0] == selected_python
    assert str(selected_python.parent / 'prisma-client-py') in (setup.CONFIG / 'schema.prisma').read_text()
    provision.assert_not_called()
