"""Explicit configuration must be ready before an upstream host can load dotenv."""
from __future__ import annotations

import builtins
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from company_agent.ops import deploy_webui, smoke_webui
from examples.company_ai_access.deployment.production import run, smoke
from examples.shared.config import ConfigurationError, read_service


ROOT = Path(__file__).resolve().parents[1]


class HostImportReached(Exception):
    pass


class WebUIEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    def guard(self, events):
        original = builtins.__import__

        def importing(name, *args, **kwargs):
            if name.startswith('open_webui'):
                self.assertEqual(os.environ['PYTHON_DOTENV_DISABLED'], '1')
                self.assertEqual(events, ['webui'])
                events.append('host')
                raise HostImportReached()
            return original(name, *args, **kwargs)

        return patch('builtins.__import__', side_effect=importing)

    async def test_ops_load_only_webui_settings_and_disable_dotenv_before_host_import(self):
        with patch.object(deploy_webui, 'function_source', return_value='public source'):
            for module, operation in ((deploy_webui, deploy_webui.deploy), (smoke_webui, smoke_webui.main)):
                with self.subTest(entry=module.__name__):
                    events = []
                    with patch.dict(os.environ, {}, clear=True), \
                            patch.object(module, 'load_service', side_effect=events.append), self.guard(events):
                        with self.assertRaises(HostImportReached):
                            await operation()
                    self.assertEqual(events, ['webui', 'host'])

    def test_direct_app_factory_disables_dotenv_before_importing_the_host(self):
        spec = importlib.util.spec_from_file_location('isolated_webui_app', ROOT / 'examples/openwebui/app.py')
        app = importlib.util.module_from_spec(spec)
        events = []
        with patch.dict(os.environ, {}, clear=True), \
                patch('examples.shared.config.load_service', side_effect=events.append), self.guard(events):
            with self.assertRaises(HostImportReached):
                spec.loader.exec_module(app)
        self.assertEqual(events, ['webui', 'host'])

    def test_upstream_dotenv_honors_disabled_flag_even_with_a_real_candidate_file(self):
        from dotenv import load_dotenv

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'PYTHON_DOTENV_DISABLED': '1'}, clear=True):
            source = Path(directory) / '.env'
            source.write_text('DOTENV_BOUNDARY_SENTINEL=must-not-load\n')
            self.assertFalse(load_dotenv(source))
            self.assertNotIn('DOTENV_BOUNDARY_SENTINEL', os.environ)


class ProductionConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.root / 'config.local.toml'

    def test_explicit_production_json_snapshot_disables_secondary_configuration(self):
        target = self.root / '.config/company-ai-access/production.json'
        target.parent.mkdir(parents=True)
        values = {
            'AI_ACCESS_ENVIRONMENT': 'production', 'AI_ACCESS_LITELLM_URL': 'http://127.0.0.1:4000',
            'AI_ACCESS_ORIGIN': 'https://changdar-server.mooo.com', 'AI_ACCESS_OIDC_AUDIENCE': 'visionnaire-api',
        }
        target.write_text(json.dumps(values))
        target.chmod(0o600)
        self.config.write_text('[company_ai_access]\nAI_ACCESS_ENVIRONMENT="must-not-load"\n')
        uvicorn = ModuleType('uvicorn')

        def start(*args, **kwargs):
            self.assertEqual(os.environ['PYTHON_DOTENV_DISABLED'], '1')
            self.assertEqual(read_service('company_ai_access'), {})
            self.assertTrue(all(os.environ[key] == value for key, value in values.items()))

        uvicorn.run = Mock(side_effect=start)
        with patch.dict(os.environ, {'AI_GATEWAY_CONFIG': str(self.config)}, clear=True), \
                patch.object(run.Path, 'home', return_value=self.root), \
                patch.object(sys, 'path', list(sys.path)), patch.dict(sys.modules, {'uvicorn': uvicorn}):
            run.main()
        uvicorn.run.assert_called_once()

    def test_smoke_uses_separate_operator_credentials_and_one_cleanup_snapshot(self):
        self.config.write_text(
            '[company_ai_smoke]\nKC_BOOTSTRAP_ADMIN_USERNAME="smoke-admin"\nKC_BOOTSTRAP_ADMIN_PASSWORD="smoke-password"\n'
            '[company_ai_access]\nAI_ACCESS_DIRECTORY_CLIENT="read-only"\nAI_ACCESS_DIRECTORY_SECRET="directory-secret"\n'
            '[litellm]\nLITELLM_MASTER_KEY="cleanup-original"\n',
        )
        configuration = smoke.smoke_configuration(self.config)
        self.config.write_text('[litellm]\nLITELLM_MASTER_KEY="changed-after-start"\n')
        self.assertEqual(set(configuration), {'company_ai_smoke', 'litellm'})
        self.assertEqual(configuration['company_ai_smoke']['KC_BOOTSTRAP_ADMIN_USERNAME'], 'smoke-admin')
        self.assertNotIn('AI_ACCESS_DIRECTORY_SECRET', configuration['company_ai_smoke'])
        self.assertEqual(configuration['litellm']['LITELLM_MASTER_KEY'], 'cleanup-original')

    def test_smoke_missing_operator_pair_never_uses_runtime_credentials_or_legacy_files(self):
        self.config.write_text(
            '[company_ai_access]\nAI_ACCESS_DIRECTORY_CLIENT="read-only"\nAI_ACCESS_DIRECTORY_SECRET="directory-secret"\n',
        )
        with patch.object(sys, 'argv', ['smoke', '--config', str(self.config)]), \
                patch.object(smoke.Path, 'home') as home, patch.object(smoke.httpx, 'Client') as client:
            with self.assertRaisesRegex(ConfigurationError, 'company_ai_smoke') as raised:
                smoke.main()
        home.assert_not_called()
        client.assert_not_called()
        self.assertNotIn('directory-secret', str(raised.exception))
