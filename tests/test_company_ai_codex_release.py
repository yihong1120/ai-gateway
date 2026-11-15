"""Release boundary: reproducible bytes, exact files, no runtime credentials."""
from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import zipfile
from pathlib import Path

import pytest

from examples.company_ai_access.cli.release import build
from examples.company_ai_access.cli.release import EXTRA_FILES


def test_release_omits_runtime_secrets_and_has_verifiable_manifest(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / 'source'
    package = root / 'examples/company_ai_access'
    public = [path for path in package.rglob('*.py') if 'deployment' not in path.relative_to(package).parts]
    public.extend(package / name for name in ('README.md', 'codex-models.example.json'))
    for directory, pattern in (('windows', '*.ps1'), ('ui', '*.html'), ('ui', '*.js')):
        public.extend((package / directory).glob(pattern))
    for path in public:
        target = source / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    for name in EXTRA_FILES:
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
    secret = ('sk-' + secrets.token_hex(24)).encode()
    for name in ('.env', 'credential.dpapi', 'production.json', 'access.key', 'access.sqlite3'):
        (source / 'examples/company_ai_access' / name).write_bytes(secret)
    (source / 'config.local.toml').write_bytes(secret)
    first = tmp_path / 'first.zip'
    manifest = build(source, first, '1.1.0', 'https://changdar-server.mooo.com')
    with zipfile.ZipFile(first) as archive:
        for entry in manifest['files']:
            data = archive.read(entry['name'])
            assert hashlib.sha256(data).hexdigest() == entry['sha256']
            assert len(data) == entry['bytes']
        assert json.loads(archive.read('release.json')) == manifest
        assert not manifest['signed']
        release_readme = archive.read('README-release.txt').decode()
        assert 'uv sync --frozen --no-dev --extra company-ai-access' in release_readme
        assert 'not the complete monorepo' in release_readme
        assert 'Sign all three Windows scripts' in release_readme
        assert {
            'pyproject.toml', 'uv.lock', 'README.md', 'CONTRIBUTING.md', 'LICENSE', 'config.example.toml',
            'examples/shared/config.py', 'examples/deployment/cli.py',
        } <= set(archive.namelist())
        assert 'examples/configuration.py' not in archive.namelist()
        assert not any(name.startswith('examples/deployment/tools/') for name in archive.namelist())
        assert not any(
            name.endswith(('requirements.txt', 'requirements.lock.txt', '.env.example'))
            for name in archive.namelist()
        )
        assert b"[string]$Portal = 'https://changdar-server.mooo.com'" in archive.read(
            'examples/company_ai_access/windows/Setup-CompanyAI.ps1',
        )
        assert all(secret not in archive.read(name) for name in archive.namelist())
        assert 'examples/company_ai_access/windows/Start-CompanyCodex.ps1' in archive.namelist()
        assert 'examples/company_ai_access/cli/export_handoff.py' not in archive.namelist()
        assert not any(name.startswith('docs/') and '-2026-' in name for name in archive.namelist())
        assert 'tests/test_company_ai_device_state.py' in archive.namelist()
        for obsolete in ('prepare.py', 'activate.py', 'rollback.py', 'nginx.conf', 'policy.json'):
            assert 'examples/company_ai_access/deployment/production/' + obsolete not in archive.namelist()
    second = tmp_path / 'second.zip'
    build(source, second, '1.1.0', 'https://changdar-server.mooo.com')
    assert first.read_bytes() == second.read_bytes()
    with pytest.raises(FileExistsError):
        build(source, first, '1.1.0', 'https://changdar-server.mooo.com')


def test_release_rejects_noncompany_portal(tmp_path):
    with pytest.raises(ValueError):
        build(tmp_path, tmp_path / 'no.zip', '1.1.0', 'https://other.example')
