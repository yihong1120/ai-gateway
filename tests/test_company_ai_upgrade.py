"""Production upgrade boundaries: existing network policy, source and rollback."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import zipfile
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    'company_ai_upgrade', Path(__file__).resolve().parents[1] /
    'examples/company_ai_access/deployment/production/upgrade.py',
)
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)


def test_launcher_route_preserves_live_network_and_other_locations():
    helper = '''location = /company-ai/downloads/Get-CompanyAIKey.ps1 {
    deny 192.168.1.1/32;
    allow 10.99.18.2/32;
    deny all;
    proxy_pass http://127.0.0.1:8013;
    proxy_set_header X-Forwarded-For $remote_addr;
}'''
    original = 'location /unrelated { return 404; }\n' + helper + '\n'
    result = upgrade.launcher_route(original)
    assert original.replace(helper, '') == result.replace(helper, '').replace(
        '\n\n' + helper.replace('Get-CompanyAIKey.ps1', 'Start-CompanyCodex.ps1'), '',
    )
    assert helper.replace('Get-CompanyAIKey.ps1', 'Start-CompanyCodex.ps1') in result
    assert result.count('allow 10.99.18.2/32;') == 2
    assert upgrade.launcher_route(result) == result
    with pytest.raises(RuntimeError):
        upgrade.launcher_route(original.replace('proxy_pass http://127.0.0.1:8013;', 'proxy_pass http://elsewhere;'))


def test_installer_route_clones_existing_download_provenance_and_is_idempotent():
    original = '''location = /company-ai/downloads/Get-CompanyAIKey.ps1 {
    deny 192.168.1.1/32;
    allow 192.168.1.0/24;
    deny all;
    proxy_pass http://127.0.0.1:8013;
    proxy_set_header X-Forwarded-For $remote_addr;
}'''
    updated = upgrade.installer_route(original)
    assert original in updated
    assert original.replace('Get-CompanyAIKey.ps1', 'changdar-ai-setup.exe') in updated
    assert upgrade.installer_route(updated) == updated


def make_archive(path, entries, version='1.1.3'):
    manifest = {
        'version': version, 'portal': 'https://changdar-server.mooo.com',
        'files': [
            {'name': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            for name, data in entries.items()
        ],
    }
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        archive.writestr('release.json', json.dumps(manifest))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_release_under_private_umask_is_readable_by_service_and_rejects_tampering(tmp_path):
    archive = tmp_path / 'source.zip'
    checksum = make_archive(archive, {'examples/company_ai_access/app.py': b'# source'})
    release = tmp_path / 'releases' / 'new'
    previous = os.umask(0o077)
    try:
        upgrade.unpack(archive, release, checksum)
    finally:
        os.umask(previous)
    for directory in (release.parent, release, release / 'examples/company_ai_access'):
        assert directory.stat().st_mode & 0o777 == 0o755
    assert (release / 'examples/company_ai_access/app.py').read_bytes() == b'# source'
    assert (release / 'examples/company_ai_access/app.py').stat().st_mode & 0o777 == 0o644
    with pytest.raises(RuntimeError):
        upgrade.unpack(archive, release, checksum)
    with pytest.raises(RuntimeError):
        upgrade.unpack(archive, tmp_path / 'bad', '0' * 64)


def test_archive_cannot_write_outside_release(tmp_path):
    archive = tmp_path / 'unsafe.zip'
    checksum = make_archive(archive, {'../escape.py': b'unsafe'})
    with pytest.raises(RuntimeError):
        upgrade.unpack(archive, tmp_path / 'release', checksum)
    assert not (tmp_path / 'escape.py').exists()
    assert not (tmp_path / 'release').exists()


def test_upgrade_requires_explicit_matching_source_version(tmp_path):
    archive = tmp_path / 'hotfix.zip'
    checksum = make_archive(archive, {'examples/company_ai_access/app.py': b'# hotfix'}, version='1.1.4')
    with pytest.raises(RuntimeError):
        upgrade.unpack(archive, tmp_path / 'wrong-version', checksum)
    assert not (tmp_path / 'wrong-version').exists()
    manifest = upgrade.unpack(archive, tmp_path / 'hotfix', checksum, '1.1.4')
    assert manifest['version'] == '1.1.4'


def test_rollback_restores_config_bytes_and_permissions_and_removes_new_dropin(tmp_path):
    backup = tmp_path / 'backup'
    backup.mkdir(mode=0o700)
    config = tmp_path / 'production.json'
    original = b'{"private":"synthetic-token", "unrelated":"keep"}\n'
    config.write_bytes(original)
    config.chmod(0o600)
    dropin = tmp_path / 'unit.d' / 'new.conf'
    receipts = upgrade.save_receipts((config, dropin), backup)
    for path in (config, dropin):
        upgrade.atomic(path, b'changed', uid=os.getuid(), gid=os.getgid(), mode=0o644)
    upgrade.restore(receipts)
    assert config.read_bytes() == original
    assert config.stat().st_mode & 0o777 == 0o600
    assert not dropin.exists()
    assert all(path.stat().st_mode & 0o077 == 0 for path in backup.iterdir() if path.name != 'receipt.json')
