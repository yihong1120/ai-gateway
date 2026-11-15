"""One Compose entry selects configuration before any container can start."""
from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from examples.deployment.cli import compose_command
from examples.deployment.cli import main

ROOT = Path(__file__).resolve().parents[1]


class ComposeEntryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = Path(temporary.name) / 'config.toml'
        self.config.write_text(
            '[litellm]\nLITELLM_DATABASE_URL="postgresql://unused/db"\n'
            'LITELLM_MASTER_KEY="synthetic-master"\nVERTEX_CREDENTIALS="/synthetic/vertex.json"\n'
            '[local_inference]\nLOCAL_INFERENCE_IMAGE="synthetic-vllm:1"\n'
            'LOCAL_INFERENCE_MODEL_PATH="/synthetic/model with spaces"\n'
            'LOCAL_INFERENCE_CONFIG_PATH="/synthetic/model.yaml"\n'
            '[webui]\nWEBUI_SECRET_KEY="must-not-export"\n',
        )
        self.enterContext(patch.dict(os.environ, {'PATH': os.environ['PATH']}, clear=True))
        self.enterContext(patch('examples.deployment.cli.Path.cwd', return_value=ROOT))

    def test_selected_profile_uses_one_root_source_and_only_its_section(self):
        with patch('examples.deployment.cli.os.execvpe') as execute:
            main(['--config', str(self.config), 'compose', 'local-model', 'up', '-d'])
        executable, argv, env = execute.call_args.args
        self.assertEqual(executable, 'docker')
        self.assertEqual(
            argv, [
                'docker', 'compose', '--env-file', '/dev/null', '-f',
                str(ROOT / 'docker-compose.yml'), '-p', 'local-model',
                '--profile', 'local-model', 'up', '-d',
            ],
        )
        self.assertEqual(env['LOCAL_INFERENCE_MODEL_PATH'], '/synthetic/model with spaces')
        self.assertEqual(env['PYTHON_DOTENV_DISABLED'], '1')
        self.assertNotIn('LITELLM_MASTER_KEY', env)
        self.assertNotIn('WEBUI_SECRET_KEY', env)

    def test_missing_selected_settings_refuse_start_before_docker_without_secrets(self):
        self.config.write_text('[litellm]\nLITELLM_MASTER_KEY="never-print-this"\n')
        error = StringIO()
        with patch('examples.deployment.cli.os.execvpe') as execute, redirect_stderr(error):
            with self.assertRaises(SystemExit) as raised:
                main(['--config', str(self.config), 'compose', 'litellm', 'up', '-d'])
        self.assertEqual(raised.exception.code, 2)
        execute.assert_not_called()
        self.assertIn('LITELLM_DATABASE_URL', error.getvalue())
        self.assertIn('VERTEX_CREDENTIALS', error.getvalue())
        self.assertNotIn('never-print-this', error.getvalue())

    def test_building_images_does_not_require_or_read_private_service_configuration(self):
        with patch('examples.shared.config._read_configuration', side_effect=AssertionError('Must not read secrets.')):
            argv, env = compose_command('images', ['build'])
        self.assertEqual(argv[-3:], ['--profile', 'images', 'build'])
        self.assertNotIn('LITELLM_MASTER_KEY', env)
        self.assertNotIn('LOCAL_INFERENCE_MODEL_PATH', env)

    def test_project_override_and_dev_project_preserve_independent_resources(self):
        argv, _ = compose_command('local-model', ['ps'], project='another-model', path=self.config)
        self.assertEqual(argv[argv.index('-p') + 1], 'another-model')
        with patch.dict(os.environ, {'DEV_CONFIG_DIR': '/synthetic/dev'}):
            argv, env = compose_command('dev-db', ['up', '-d', 'postgres'])
        self.assertEqual(argv[argv.index('-p') + 1], 'ai-gateway-dev')
        self.assertEqual(env['DEV_CONFIG_DIR'], '/synthetic/dev')
        self.assertNotIn('LITELLM_MASTER_KEY', env)

    def test_caller_override_wins_and_config_does_not_require_other_profiles(self):
        with patch.dict(os.environ, {'LITELLM_MASTER_KEY': 'explicit-caller-key'}):
            argv, env = compose_command('litellm', ['up', '-d'], path=self.config)
        self.assertEqual(env['LITELLM_MASTER_KEY'], 'explicit-caller-key')
        self.assertEqual(argv[-4:], ['--profile', 'litellm', 'up', '-d'])
        self.assertNotIn('LOCAL_INFERENCE_MODEL_PATH', env)

    def test_read_only_inspection_needs_no_startup_credentials(self):
        self.config.write_text('[litellm]\n')
        argv, env = compose_command('litellm', ['ps'], path=self.config)
        self.assertEqual(argv[-1], 'ps')
        self.assertNotIn('LITELLM_MASTER_KEY', env)

    def test_prefix_options_cannot_bypass_selected_startup_validation(self):
        error = StringIO()
        with patch('examples.deployment.cli.os.execvpe') as execute, redirect_stderr(error):
            with self.assertRaises(SystemExit):
                main(['--config', str(self.config), 'compose', 'litellm', '--parallel', '1', 'up'])
        execute.assert_not_called()
        self.assertIn('immediately after the profile', error.getvalue())
