"""Central configuration remains explicit in tools and restricted in containers."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from examples.shared.config import read_service_snapshot

ROOT = Path(__file__).resolve().parents[1]


class DeploymentConfigurationTests(unittest.TestCase):
    def test_tools_read_selected_toml_even_in_an_exec_child(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'config.toml'
            config.write_text('[litellm]\nTOKEN = "litellm-test"\n[webui]\nTOKEN = "webui-test"\n')
            with patch.dict(os.environ, {'AI_GATEWAY_CONFIG': str(config), 'PYTHON_DOTENV_DISABLED': '1'}):
                self.assertEqual(read_service_snapshot('litellm'), {'TOKEN': 'litellm-test'})
                self.assertEqual(read_service_snapshot('webui'), {'TOKEN': 'webui-test'})
            snapshot = Path(folder) / 'snapshot.json'
            snapshot.write_text('{"TOKEN":"snapshot-test"}')
            self.assertEqual(read_service_snapshot('litellm', snapshot), {'TOKEN': 'snapshot-test'})

    @unittest.skipUnless(shutil.which('docker'), 'Requires the Docker Compose CLI; no daemon is used')
    def test_compose_receives_only_explicit_service_variables(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'config.toml'
            config.write_text(
                '[litellm]\nLITELLM_MASTER_KEY="test-master"\n'
                'LITELLM_DATABASE_URL="postgresql://unused/test"\n'
                'VERTEX_CREDENTIALS="/tmp/synthetic-vertex-credentials.json"\n'
                'UNRELATED_LITELLM_SETTING="must-not-reach-container"\n'
                '[webui]\nWEBUI_SECRET_KEY="must-not-reach-container"\n',
            )
            result = subprocess.run(
                [
                    sys.executable, '-m', 'examples.deployment.cli', '--config', str(config),
                    'compose', 'litellm', 'config', '--format', 'json',
                ],
                cwd=ROOT, env={'PATH': os.environ['PATH'], 'CALLER_SECRET': 'must-not-reach-container'},
                capture_output=True, text=True, check=True, timeout=30,
            )
            rendered = json.loads(result.stdout)['services']['litellm']
            environment = rendered['environment']
            self.assertEqual(environment['LITELLM_MASTER_KEY'], 'test-master')
            self.assertEqual(environment['LITELLM_DATABASE_URL'], 'postgresql://unused/test')
            self.assertEqual(environment['PYTHON_DOTENV_DISABLED'], '1')
            self.assertNotIn('WEBUI_SECRET_KEY', environment)
            self.assertNotIn('UNRELATED_LITELLM_SETTING', environment)
            self.assertNotIn('CALLER_SECRET', environment)
            self.assertNotIn('must-not-reach-container', result.stdout)
            self.assertNotIn('env_file', rendered)
