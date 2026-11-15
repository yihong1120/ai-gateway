"""A separate deployment must not read production connection credentials."""
from unittest.mock import patch
import pytest
from company_agent.api.service import ServiceConfiguration


def test_explicit_connection_ignores_repository_production_defaults(monkeypatch):
    monkeypatch.setenv('OPENAI_API_BASE_URLS', 'http://127.0.0.1:14002/v1')
    monkeypatch.setenv('OPENAI_API_KEYS', 'dev-key')
    with patch('examples.shared.config.read_service') as defaults, \
         patch.object(ServiceConfiguration, 'configured_settings', return_value=None), \
         patch('company_agent.api.service.gateway_for_verified_user') as gateway:
        ServiceConfiguration.model({'id': 'verified-user'})
    defaults.assert_not_called()
    assert gateway.call_args.args[1:3] == ('http://127.0.0.1:14002/v1', 'dev-key')


@pytest.mark.parametrize('name', ['OPENAI_API_BASE_URLS', 'OPENAI_API_KEYS'])
def test_incomplete_explicit_connection_fails_without_production_fallback(monkeypatch, name):
    for key in ('OPENAI_API_BASE_URLS', 'OPENAI_API_KEYS'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(name, 'only-one-setting')
    with patch('examples.shared.config.read_service') as defaults, pytest.raises(ValueError, match='一起設定'):
        ServiceConfiguration.model({'id': 'verified-user'})
    defaults.assert_not_called()


def test_service_configuration_supplies_the_webui_connection(monkeypatch):
    for key in ('OPENAI_API_BASE_URLS', 'OPENAI_API_KEYS'):
        monkeypatch.delenv(key, raising=False)
    values = {'OPENAI_API_BASE_URLS': 'http://127.0.0.1:4000/v1', 'OPENAI_API_KEYS': 'existing-key'}
    with patch('examples.shared.config.read_service', return_value=values) as defaults, \
         patch.object(ServiceConfiguration, 'configured_settings', return_value=None), \
         patch('company_agent.api.service.gateway_for_verified_user') as gateway:
        ServiceConfiguration.model({'id': 'verified-user'})
    defaults.assert_called_once_with('webui')
    assert gateway.call_args.args[1:3] == ('http://127.0.0.1:4000/v1', 'existing-key')


def test_job_settings_snapshot_selects_model_without_rereading_changed_file(tmp_path, monkeypatch):
    from company_agent.config import Settings
    from types import SimpleNamespace
    path = tmp_path / 'settings.json'
    path.write_text(Settings(state_dir=str(tmp_path), model_name='first').model_dump_json())
    monkeypatch.setenv('COMPANY_AGENT_CONFIG', str(path))
    monkeypatch.setenv('OPENAI_API_BASE_URLS', 'http://127.0.0.1:14002/v1')
    monkeypatch.setenv('OPENAI_API_KEYS', 'synthetic-key')
    configuration = ServiceConfiguration(tmp_path, tmp_path / 'sources')
    with patch.object(ServiceConfiguration, 'configured_settings', wraps=ServiceConfiguration.configured_settings) as read:
        snapshot = configuration.settings({'id': 'job', 'owner': 'alice', 'payload': {}})
        path.write_text(Settings(state_dir=str(tmp_path), model_name='second').model_dump_json())
        with patch('company_agent.api.service.gateway_for_verified_user', return_value=SimpleNamespace(owner='alice', subject='sub', client=SimpleNamespace())) as gateway:
            configuration.model({'id': 'alice'}, snapshot.model_name)
        assert read.call_count == 1
    assert snapshot.model_name == 'first'
    assert gateway.call_args.args[3] == 'first'
