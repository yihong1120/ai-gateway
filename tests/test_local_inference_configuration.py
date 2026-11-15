"""One deployment accepts native model configurations without model-specific code."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / 'docker-compose.yml'


@unittest.skipUnless(shutil.which('docker'), 'Requires Docker Compose; no daemon or model is used')
class LocalInferenceConfigurationTests(unittest.TestCase):
    def render(self, models, config, *, project='local-check', **overrides):
        environment = {
            'PATH': os.environ['PATH'],
            'LOCAL_INFERENCE_IMAGE': 'vllm/vllm-openai:v0.31.0',
            'LOCAL_INFERENCE_MODEL_PATH': str(models),
            'LOCAL_INFERENCE_CONFIG_PATH': str(config),
            'UNRELATED_SERVICE_SECRET': 'must-not-reach-container',
            **overrides,
        }
        result = subprocess.run(
            [
                'docker', 'compose', '--env-file', '/dev/null', '-p', project,
                '-f', str(COMPOSE), '--profile', 'local-model', 'config', '--format', 'json',
            ],
            env=environment, capture_output=True, text=True, check=True, timeout=30,
        )
        self.assertNotIn('must-not-reach-container', result.stdout)
        return json.loads(result.stdout)

    def test_switching_only_native_config_changes_model_without_changing_deployment(self):
        with tempfile.TemporaryDirectory(prefix='local models ') as folder:
            root = Path(folder)
            models = root / 'weights'
            models.mkdir()
            for name in ('alpha', 'beta'):
                (models / name).mkdir()
            native_configs = [
                {
                    'model': '/models/model/alpha', 'served-model-name': 'alpha',
                    'host': '0.0.0.0', 'port': 8000, 'dtype': 'auto',
                },
                {
                    'model': '/models/model/beta', 'served-model-name': ['beta', 'second-alias'],
                    'host': '0.0.0.0', 'port': 8000, 'limit-mm-per-prompt': {'image': 2},
                    'enable-auto-tool-choice': True, 'tool-call-parser': 'hermes',
                },
            ]
            deployments = []
            for index, native in enumerate(native_configs):
                config = root / f'model config {index}.yaml'
                config.write_text(yaml.safe_dump(native))
                deployed = self.render(models, config)['services']['vllm']
                mounts = {item['target']: item for item in deployed['volumes']}
                self.assertEqual(mounts['/models/model']['source'], str(models))
                self.assertTrue(mounts['/models/model']['read_only'])
                self.assertFalse(mounts['/models/model']['bind'].get('create_host_path', False))
                mounted = mounts['/etc/vllm/config.yaml']
                self.assertTrue(mounted['read_only'])
                self.assertFalse(mounted['bind'].get('create_host_path', False))
                self.assertEqual(yaml.safe_load(Path(mounted['source']).read_text()), native)
                self.assertNotIn('container_name', deployed)
                self.assertEqual(deployed['command'], ['--config', '/etc/vllm/config.yaml'])
                self.assertEqual(deployed['ports'][0]['host_ip'], '127.0.0.1')
                self.assertEqual(deployed['ports'][0]['target'], 8000)
                self.assertEqual(deployed['environment'], {'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'})
                # The only deployment difference is which native YAML is mounted.
                mounted['source'] = '<operator-selected-config>'
                deployments.append(deployed)
            self.assertEqual(deployments[0], deployments[1])

    def test_projects_and_resources_are_selected_per_instance(self):
        with tempfile.TemporaryDirectory() as folder:
            models = Path(folder)
            config = ROOT / 'examples/litellm/local/vllm.example.yaml'
            first = self.render(models, config, project='first-local')
            second = self.render(
                models, config, project='second-local',
                LOCAL_INFERENCE_HOST_PORT='15003', LOCAL_INFERENCE_GPU_ID='2',
            )
            self.assertNotEqual(first['name'], second['name'])
            self.assertEqual(first['services']['vllm']['ports'][0]['published'], '5003')
            service = second['services']['vllm']
            self.assertEqual(service['ports'][0]['published'], '15003')
            self.assertEqual(service['ports'][0]['host_ip'], '127.0.0.1')
            self.assertEqual(service['deploy']['resources']['reservations']['devices'][0]['device_ids'], ['2'])
            self.assertNotIn('container_name', service)
