"""Applications load only their service settings before building resources."""

import os
import unittest
from unittest.mock import patch


class ServiceConfigurationTests(unittest.TestCase):
    def test_ai_usage_loads_its_section_before_validation(self):
        from examples.ai_usage.config import AiUsageSettings

        values = {
            'AI_USAGE_LITELLM_BASE_URL': 'http://usage.test',
            'LITELLM_USAGE_READER_KEY': 'synthetic-reader',
            'AI_USAGE_OIDC_ISSUER_URL': 'https://issuer.test/realms/test',
            'AI_USAGE_OIDC_JWKS_URL': 'https://issuer.test/certs',
            'AI_USAGE_OIDC_AUDIENCE': 'test-api',
        }
        with patch.dict(os.environ, {'PYTHON_DOTENV_DISABLED': '1'}, clear=True), \
                patch('examples.ai_usage.config.load_service', side_effect=lambda service: os.environ.update(values)) as load:
            settings = AiUsageSettings.from_environment()
        load.assert_called_once_with('ai_usage')
        self.assertEqual(settings.litellm_base_url, 'http://usage.test')

    def test_company_ai_loads_its_section_before_database_validation(self):
        from examples.company_ai_access.config import Settings

        values = {
            'AI_ACCESS_DATABASE_URL': 'postgresql://synthetic@127.0.0.1/test',
            'AI_ACCESS_ORIGIN': 'https://access.test',
            'AI_ACCESS_STATE_DIR': '/tmp/synthetic-state',
            'AI_ACCESS_LITELLM_URL': 'http://127.0.0.1:4000',
            'AI_ACCESS_MASTER_KEY': 'synthetic-access-master',
            'AI_ACCESS_MODELS': 'test-model',
            'AI_ACCESS_ISSUER': 'https://issuer.test/realms/test',
            'AI_ACCESS_DIRECTORY_CLIENT': 'read-only',
            'AI_ACCESS_DIRECTORY_SECRET': 'synthetic-directory',
            'AI_ACCESS_ROLE_CLIENT_UUID': 'test-roles',
        }
        with patch.dict(os.environ, {'PYTHON_DOTENV_DISABLED': '1'}, clear=True), \
                patch('examples.shared.config.load_service', side_effect=lambda service: os.environ.update(values)) as load:
            settings = Settings.from_env()
        load.assert_called_once_with('company_ai_access')
        self.assertEqual(settings.models, ('test-model',))

    def test_identity_explicit_settings_skip_configuration_file(self):
        with patch.dict(os.environ, {'PYTHON_DOTENV_DISABLED': '1', 'JWT_SECRET_KEY': 'synthetic'}, clear=True):
            from examples.db_management.app import create_app
            from examples.identity.config import Settings

            explicit = Settings()
            with patch('examples.shared.config.load_service') as load:
                app = create_app(explicit)
            load.assert_not_called()
            self.assertIs(app.state.identity.settings, explicit)

    def test_identity_default_settings_load_before_building_resources(self):
        with patch.dict(os.environ, {'PYTHON_DOTENV_DISABLED': '1', 'JWT_SECRET_KEY': 'synthetic'}, clear=True):
            from examples.db_management.app import create_app

            def configure(service):
                os.environ['APP_PUBLIC_URL'] = 'https://identity.test'

            with patch('examples.shared.config.load_service', side_effect=configure) as load:
                app = create_app()
            load.assert_called_once_with('identity')
            self.assertEqual(app.state.identity.settings.app_public_url, 'https://identity.test')
