"""Container target contracts and an optional real local BuildKit context audit."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def stages():
    result, current = {}, None
    text = (ROOT / 'Dockerfile').read_text().replace('\\\n', ' ')
    for line in text.splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        instruction, value = line.split(None, 1)
        if instruction == 'FROM':
            image, _, name = value.split()
            current = result[name] = [('FROM', image)]
        else:
            current.append((instruction, value))
    return result


class ContainerBuildTests(unittest.TestCase):
    def test_targets_keep_distinct_dependency_and_runtime_contracts(self):
        targets = stages()
        self.assertEqual(
            set(targets), {
                'uv', 'management-api', 'company-office',
                'eip-office', 'company-cad', 'cad-build', 'eval-preview',
            },
        )
        self.assertEqual(targets['eval-preview'][0], ('FROM', 'company-office'))
        self.assertEqual(list(targets)[-1], 'management-api')
        for target, group, executable in (
            ('company-office', 'office-worker', '/opt/python'),
            ('company-cad', 'cad-worker', '/opt/cad-python'),
        ):
            text = '\n'.join(value for _, value in targets[target])
            self.assertIn('--only-group ' + group, text)
            self.assertIn('UV_PROJECT_ENVIRONMENT=' + executable, text)
            self.assertIn(('USER', '1000:1000'), targets[target])
        self.assertIn(('COPY', '--from=cad-build /app /app'), targets['company-cad'])
        self.assertIn(('ENTRYPOINT', '["/opt/python/bin/python", "/app/office_worker.py"]'), targets['company-office'])
        office = '\n'.join(value for _, value in targets['eip-office'])
        self.assertIn('libreoffice-writer fonts-noto-cjk fonts-arphic-ukai', office)
        self.assertIn('file:///work/profile', office)
        self.assertIn(('USER', 'appuser'), targets['management-api'])
        self.assertIn('--extra identity-services', '\n'.join(value for _, value in targets['management-api']))
        command = json.loads(dict(targets['management-api'])['CMD'])
        self.assertEqual(command, ['uvicorn', 'examples.db_management.app:app', '--host', '0.0.0.0', '--port', '8005'])

    def test_every_local_copy_source_exists_and_stage_copies_resolve(self):
        targets = stages()
        for name, instructions in targets.items():
            for instruction, value in instructions:
                if instruction != 'COPY':
                    continue
                fields = shlex.split(value)
                if fields[0].startswith('--from='):
                    self.assertIn(fields[0].split('=', 1)[1], targets)
                    continue
                sources = [field for field in fields[:-1] if not field.startswith('--')]
                for source in sources:
                    with self.subTest(target=name, source=source):
                        self.assertTrue((ROOT / source).exists())

    def test_management_image_installs_the_canonical_cli_and_shared_configuration(self):
        project = tomllib.loads((ROOT / 'pyproject.toml').read_text())
        self.assertEqual(project['project']['scripts']['ai-gateway'], 'examples.deployment.cli:main')
        management = '\n'.join(value for _, value in stages()['management-api'])
        self.assertIn('examples/deployment/cli.py', management)
        self.assertIn('examples/shared /app/examples/shared', management)
        self.assertNotIn('examples/configuration.py', management)

    def test_sdist_contains_root_build_and_public_compose_inputs(self):
        manifest = (ROOT / 'MANIFEST.in').read_text().splitlines()
        included = {name for line in manifest if line.startswith('include ') for name in line.split()[1:]}
        required = {
            'Dockerfile', '.dockerignore', 'uv.lock', 'docker-compose.yml',
            'examples/litellm/config.yaml', 'examples/litellm/local/vllm.example.yaml',
            'examples/official_document_workflow/deployment/ocr/vllm.example.yaml',
        }
        self.assertLessEqual(required, included)
        for name in required:
            self.assertTrue((ROOT / name).is_file(), name)

    def test_one_dockerfile_and_one_ignore_remain(self):
        for name in (
            'company_agent/deployment/Dockerfile.company-office',
            'company_agent/deployment/Dockerfile.company-office.dockerignore',
            'company_agent/deployment/Dockerfile.company-cad',
            'company_agent/deployment/Dockerfile.company-cad.dockerignore',
            'examples/db_management/Dockerfile', 'examples/db_management/Dockerfile.dockerignore',
            'examples/official_document_workflow/deployment/Dockerfile.office',
            'examples/deployment/diagnostics/Dockerfile.company-data-preview',
        ):
            self.assertFalse((ROOT / name).exists(), name)
        self.assertTrue((ROOT / '.dockerignore').is_file())

    @unittest.skipUnless(os.getenv('TEST_DOCKER_CONTEXT') == '1', 'opt in to a local Docker BuildKit context audit')
    def test_real_buildkit_exports_only_public_context_without_images_or_containers(self):
        if shutil.which('docker') is None:
            self.skipTest('Docker CLI is unavailable')
        context = subprocess.check_output(['docker', 'context', 'show'], text=True).strip()
        endpoint = subprocess.check_output(
            [
                'docker', 'context', 'inspect', context,
                '--format', '{{(index .Endpoints "docker").Host}}',
            ], text=True,
        ).strip()
        if not endpoint.startswith('unix://'):
            self.skipTest('Context auditing requires a local Unix Docker endpoint')
        available = subprocess.run(
            [
                'docker', '--context', context, 'version', '--format',
                '{{.Server.Version}}',
            ], capture_output=True, timeout=10,
        )
        if available.returncode:
            self.skipTest('Local Docker daemon is unavailable')
        with tempfile.TemporaryDirectory(prefix='ai-gateway-context-audit-') as directory:
            temporary = Path(directory)
            dockerfile = temporary / 'Dockerfile'
            dockerfile.write_text('FROM scratch\nCOPY . /context\n')

            def export(source, output):
                command = [
                    'docker', '--context', context, 'buildx', 'build', '--builder', 'default',
                    '--file', str(dockerfile), '--output', f'type=local,dest={output}', '--progress=quiet', str(source),
                ]
                result = subprocess.run(command, text=True, capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                return {path.relative_to(output / 'context').as_posix() for path in (output / 'context').rglob('*') if path.is_file()}

            fixture = temporary / 'fixture'
            fixture.mkdir()
            (fixture / '.dockerignore').write_bytes((ROOT / '.dockerignore').read_bytes())
            allowed = {
                'examples/identity/models/accounts.py', 'company_agent/services/office/worker.py',
                'examples/shared/config.py', 'examples/deployment/cli.py',
            }
            forbidden = {
                'config.local.toml', '.env.local', 'ocr-vllm.local.yaml', '.git/config',
                'examples/deployment/archive/old.py', 'examples/identity/private.json',
                'examples/identity/.env.local', 'examples/identity/keys/signing.key',
                'examples/identity/private/config.py', 'examples/identity/__pycache__/cached.py',
                'company_agent/services/office/other.py',
                'examples/configuration.py', 'examples/deployment/tools/validate.py', 'examples/deployment/dev/run.py',
            }
            for name in allowed | forbidden:
                path = fixture / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('synthetic context fixture\n')
            synthetic = export(fixture, temporary / 'synthetic')
            self.assertLessEqual(allowed, synthetic)
            self.assertFalse(forbidden & synthetic)
            # Audit the checkout only after synthetic private inputs are rejected.
            paths = export(ROOT, temporary / 'actual')
            self.assertIn('examples/identity/models/accounts.py', paths)
            self.assertIn('company_agent/services/office/worker.py', paths)
            self.assertIn('company_agent/services/cad/worker/Program.cs', paths)
            self.assertIn('examples/shared/config.py', paths)
            self.assertIn('examples/deployment/cli.py', paths)
            for name in paths:
                source = Path(name)
                self.assertFalse(
                    any(
                        part in {'.git', 'archive', 'private', 'keys', '__pycache__'}
                        for part in source.parts
                    ), name,
                )
                self.assertFalse(source.name.startswith('.env') or source.name.startswith('config.local.'), name)
                self.assertNotIn(
                    source.suffix, {
                        '.toml', '.yaml', '.yml', '.json', '.key',
                        '.pem', '.pfx', '.p12',
                    } if name != 'pyproject.toml' else set(), name,
                )
