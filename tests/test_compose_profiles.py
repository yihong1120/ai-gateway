"""One Compose selects isolated services without starting Docker or loading models."""
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
PROFILES = {
    'litellm': {'litellm'}, 'local-model': {'vllm'}, 'ocr': {'paddleocr-vl16-vllm'},
    'dev-db': {'postgres'}, 'files': {'fileserver'},
    'images': {'management-api', 'company-office', 'eip-office', 'company-cad', 'eval-preview'},
}


@unittest.skipUnless(shutil.which('docker'), 'Requires Docker Compose; no daemon is used')
class ComposeProfileTests(unittest.TestCase):
    def render(self, profile, *, project=None, **values):
        command = ['docker', 'compose', '--env-file', '/dev/null', '-f', str(COMPOSE)]
        if project:
            command.extend(['-p', project])
        command.extend(['--profile', profile, 'config', '--format', 'json'])
        result = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, check=True, timeout=30,
            env={'PATH': os.environ['PATH'], 'UNRELATED_SERVICE_SECRET': 'must-not-reach-container', **values},
        )
        self.assertNotIn('must-not-reach-container', result.stdout)
        return json.loads(result.stdout)

    def test_profiles_only_include_their_services_and_no_global_container_names(self):
        for profile, expected in PROFILES.items():
            if profile == 'dev-db':
                continue  # Its generated bootstrap file is tested separately.
            with self.subTest(profile=profile):
                rendered = self.render(profile)
                self.assertEqual(rendered['name'], 'ai-gateway')
                self.assertEqual(set(rendered['services']), expected)
                for service in rendered['services'].values():
                    self.assertNotIn('container_name', service)

    def test_bind_mounts_are_read_only_and_never_create_missing_host_paths(self):
        document = yaml.safe_load(COMPOSE.read_text())
        for name, service in document['services'].items():
            for mount in service.get('volumes', []):
                if isinstance(mount, dict):
                    with self.subTest(service=name, target=mount['target']):
                        self.assertEqual(mount['type'], 'bind')
                        self.assertTrue(mount['read_only'])
                        self.assertIs(mount['bind']['create_host_path'], False)
        litellm = self.render('litellm')['services']['litellm']
        self.assertEqual(litellm['network_mode'], 'host')
        self.assertEqual(litellm['command'][-4:], ['--host', '127.0.0.1', '--port', '4000'])
        self.assertEqual(litellm['volumes'][0]['source'], str(ROOT / 'examples/litellm/config.yaml'))
        self.assertIn('@sha256:', litellm['image'])

    def test_dev_database_keeps_its_project_volume_and_generated_credentials(self):
        with tempfile.TemporaryDirectory(prefix='dev bootstrap ') as folder:
            Path(folder, 'postgres.env').write_text(
                'POSTGRES_USER=dev_bootstrap\nPOSTGRES_PASSWORD=generated-test-password\n',
            )
            rendered = self.render(
                'dev-db', project='ai-gateway-dev', DEV_CONFIG_DIR=folder,
                POSTGRES_PASSWORD='unrelated-shell-password',
            )
        self.assertEqual(set(rendered['services']), {'postgres'})
        self.assertEqual(rendered['volumes']['postgres']['name'], 'ai-gateway-dev_postgres')
        service = rendered['services']['postgres']
        self.assertNotIn('container_name', service)
        self.assertEqual(
            service['environment'], {
                'POSTGRES_USER': 'dev_bootstrap',
                'POSTGRES_PASSWORD': 'generated-test-password',
            },
        )
        self.assertEqual(service['ports'][0]['host_ip'], '127.0.0.1')
        self.assertEqual(service['ports'][0]['published'], '15432')
        self.assertEqual(service['ports'][0]['target'], 5432)
        self.assertEqual(service['volumes'][0]['source'], 'postgres')
        self.assertEqual(service['volumes'][0]['target'], '/var/lib/postgresql')
        self.assertEqual(service['mem_limit'], '1073741824')
        self.assertEqual(service['shm_size'], '134217728')
        self.assertEqual(service['cpus'], 1)

    def test_ocr_uses_native_config_read_only_with_independent_gpu_and_loopback_port(self):
        with tempfile.TemporaryDirectory(prefix='ocr models ') as folder:
            config = ROOT / 'examples/official_document_workflow/deployment/ocr/vllm.example.yaml'
            rendered = self.render(
                'ocr', OCR_MODEL_PATH=folder, OCR_INFERENCE_CONFIG_PATH=str(config),
                OCR_GPU_ID='2', OCR_HOST_PORT='18115',
            )
            service = rendered['services']['paddleocr-vl16-vllm']
            mounts = {mount['target']: mount for mount in service['volumes']}
            self.assertEqual(mounts['/models/model']['source'], folder)
            self.assertEqual(mounts['/etc/vllm/config.yaml']['source'], str(config))
            self.assertEqual(service['command'], ['--config', '/etc/vllm/config.yaml'])
            self.assertEqual(service['environment'], {'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'})
            self.assertEqual(service['ports'][0]['host_ip'], '127.0.0.1')
            self.assertEqual(service['ports'][0]['published'], '18115')
            self.assertEqual(service['deploy']['resources']['reservations']['devices'][0]['device_ids'], ['2'])
            native = yaml.safe_load(config.read_text())
            self.assertEqual(native['model'], '/models/model')
            self.assertEqual(native['max-num-seqs'], 1)
            self.assertEqual(native['gpu-memory-utilization'], 0.20)
            self.assertEqual(native['kv-cache-memory-bytes'], 536870912)

    def test_fileserver_preserves_public_defaults_and_allows_explicit_host_and_root(self):
        default = self.render('files')['services']['fileserver']
        self.assertEqual(default['ports'][0]['host_ip'], '0.0.0.0')
        self.assertEqual(default['ports'][0]['published'], '18080')
        self.assertEqual(default['volumes'][0]['source'], '/mnt/data')
        with tempfile.TemporaryDirectory() as folder:
            selected = self.render(
                'files', FILESERVER_ROOT=folder, FILESERVER_HOST='127.0.0.1',
                FILESERVER_PORT='18081',
            )['services']['fileserver']
        self.assertEqual(selected['ports'][0]['host_ip'], '127.0.0.1')
        self.assertEqual(selected['ports'][0]['published'], '18081')
        self.assertEqual(selected['volumes'][0]['source'], folder)
        self.assertTrue(selected['volumes'][0]['read_only'])

    def test_images_only_build_existing_tags_from_root_targets(self):
        expected = {
            'management-api': 'ai-gateway/management-api:1', 'company-office': 'ai-gateway/company-office:1',
            'eip-office': 'ai-gateway/eip-office:1', 'company-cad': 'ai-gateway/company-cad:1',
            'eval-preview': 'ai-gateway/company-eval-preview:20261018',
        }
        for name, service in self.render('images')['services'].items():
            with self.subTest(target=name):
                self.assertEqual(service['image'], expected[name])
                self.assertEqual(service['build'], {'context': str(ROOT), 'dockerfile': 'Dockerfile', 'target': name})
                self.assertNotIn('environment', service)
                self.assertNotIn('volumes', service)
                self.assertNotIn('ports', service)
