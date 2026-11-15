"""Build a reproducible public-source release; no runtime state or credentials."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path


EXTRA_FILES = (
    'pyproject.toml',
    'uv.lock',
    'README.md',
    'CONTRIBUTING.md',
    'LICENSE',
    'config.example.toml',
    'examples/shared/config.py',
    'examples/deployment/cli.py',
    'examples/company_ai_access/deployment/windows/changdar-ai/ChangdarAI.csproj',
    'examples/company_ai_access/deployment/windows/changdar-ai/Program.cs',
    'examples/company_ai_access/deployment/windows/changdar-ai/app.manifest',
    'examples/company_ai_access/deployment/windows/changdar-ai/build.py',
    'examples/company_ai_access/deployment/windows/changdar-ai/Sign-ChangdarAIInstaller.ps1',
    'examples/company_ai_access/deployment/production/signed_release.py',
    'examples/company_ai_access/deployment/production/publish_installer_input.py',
    'tests/test_company_ai_signed_release.py',
    'docs/company-ai-codex-design.md',
    'docs/company-ai-access-openapi.json',
    'docs/company-ai-access-flutter-handoff.md',
    'examples/company_ai_access/deployment/windows/Sign-CompanyAIRelease.ps1',
    'examples/company_ai_access/deployment/production/run.py',
    'examples/company_ai_access/deployment/production/upgrade.py',
    'examples/company_ai_access/deployment/production/smoke.py',
    'examples/deployment/dev/qualify_codex.py',
    'examples/deployment/dev/run.py',
    'examples/__init__.py',
    'examples/deployment/__init__.py',
    'examples/deployment/dev/__init__.py',
    'examples/company_ai_access/deployment/__init__.py',
    'examples/company_ai_access/deployment/production/__init__.py',
    'examples/shared/__init__.py',
    'examples/shared/runtime/__init__.py',
    'examples/shared/runtime/database.py',
    'examples/shared/runtime/installation.py',
    'tests/ai_access_postgres.py',
    'tests/test_ai_access_postgres.py',
    'tests/test_ai_access_runtime.py',
    'tests/test_company_ai_access.py',
    'tests/test_company_ai_device_state.py',
    'tests/test_company_ai_codex.py',
    'tests/test_company_ai_codex_cli.py',
    'tests/test_company_ai_codex_release.py',
    'tests/test_company_ai_upgrade.py',
    'tests/test_company_ai_setup.py',
    'tests/__init__.py',
    'tests/windows/Test-CompanyAI.ps1',
    'tests/windows/Test-CompanyAIPath.ps1',
    'tests/windows/Test-CompanyAIModels.ps1',
    'tests/windows/Test-CompanyAIInstall.ps1',
    'tests/windows/Test-ChangdarAIRecovery.ps1',
    'tests/windows/Test-CompanyCodexDefault.ps1',
    'tests/windows/Test-CompanyMcp.ps1',
    'tests/windows/Test-CompanyAICodex.ps1',
    'tests/windows/Test-CompanyAICodexDownload.ps1',
)
PORTALS = (
    'https://ai-dev.changdar-server.mooo.com', 'https://ai.changdar-server.mooo.com',
    'https://changdar-server.mooo.com',
)


def build(root: Path, output: Path, version: str, portal: str):
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?', version) or portal not in PORTALS:
        raise ValueError('Use a release version and approved exact HTTPS portal.')
    package = root / 'examples/company_ai_access'
    files = sorted(
        set([path for path in package.rglob('*.py') if path.is_file() and 'deployment' not in path.relative_to(package).parts] +
            [package / name for name in ('README.md', 'codex-models.example.json')] +
            list((package / 'windows').glob('*.ps1')) + list((package / 'ui').glob('*.html')) +
            list((package / 'ui').glob('*.js')) + [root / name for name in EXTRA_FILES]),
    )
    payloads = {}
    for path in files:
        if path.is_symlink():
            raise ValueError('Release source must not be a symlink.')
        name = path.relative_to(root).as_posix()
        data = path.read_bytes()
        if name == 'examples/company_ai_access/windows/Setup-CompanyAI.ps1':
            original = b"[string]$Portal = 'https://ai-dev.changdar-server.mooo.com'"
            if data.count(original) != 1:
                raise ValueError('Unexpected setup template; refusing to guess its default portal.')
            data = data.replace(original, ("[string]$Portal = '" + portal + "'").encode())
        payloads[name] = data
    manifest = {
        'version': version, 'portal': portal, 'signed': False,
        'files': [
            {'name': name, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
            for name, data in payloads.items()
        ],
    }
    payloads['release.json'] = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode()
    payloads['README-release.txt'] = (
        b'Single-product Company AI source bundle; this is not the complete monorepo.\n'
        b'The root project metadata and lock support this product profile.\n'
        b'Install from the extracted root: uv sync --frozen --no-dev --extra company-ai-access\n'
        b'Start with examples/company_ai_access/README.md and docs/company-ai-codex-design.md.\n'
        b'Sign all three Windows scripts using the included signing workflow before distribution.\n'
        b'No runtime configuration, enrollment credential, registry or provider secret is included.\n'
        b'Codex remains disabled until a qualified release/catalog/model policy is configured.\n'
    )
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in payloads.items():
            entry = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--portal', choices=PORTALS, required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    manifest = build(Path(__file__).resolve().parents[3], args.output, args.version, args.portal)
    print(
        json.dumps({
            'output': str(args.output), 'source_files': len(manifest['files']),
            'sha256': hashlib.sha256(args.output.read_bytes()).hexdigest(),
        }),
    )


if __name__ == '__main__':
    main()
