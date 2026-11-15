"""Run offline validation with a disposable PostgreSQL cluster and clean artifacts."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from contextlib import contextmanager
from pathlib import Path

import examples.bff.deployment as _deployment_imports
import examples.company_ai_access.cli.release as _release_imports

ROOT = Path(__file__).resolve().parents[3]


def execute(command, env, log):
    with log.open('w') as output:
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f'Validation failed; see {log}')


@contextmanager
def postgres(output):
    if os.geteuid() == 0:
        raise RuntimeError('PostgreSQL validation requires an unprivileged user.')
    found = shutil.which('initdb')
    candidates = sorted(Path('/usr/lib/postgresql').glob('*/bin/initdb'), key=lambda p: int(p.parts[-3]))
    binary = Path(found) if found else candidates[-1] if candidates else None
    if binary is None:
        raise RuntimeError('Install PostgreSQL server tools (initdb and pg_ctl).')
    data, sockets = output / 'postgres', output / 'socket'
    sockets.mkdir(mode=0o700)
    control = str(binary.with_name('pg_ctl'))
    base = os.environ.copy()
    started = False
    try:
        execute(
            [str(binary), '-D', str(data), '--username=validation', '--auth=trust', '--no-locale', '--encoding=UTF8'],
            base,
            output / 'postgres-init.log',
        )
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        execute(
            [
                control,
                '-D',
                str(data),
                '-l',
                str(output / 'postgres.log'),
                '-o',
                f'-p {port} -h 127.0.0.1 -k {sockets}',
                '-w',
                'start',
            ],
            base,
            output / 'postgres-start.log',
        )
        started = True
        yield f'postgresql://validation@127.0.0.1:{port}/postgres'
    finally:
        if started:
            execute([control, '-D', str(data), '-m', 'fast', '-w', 'stop'], base, output / 'postgres-stop.log')
        if data.exists():
            shutil.rmtree(data)


def pytest_targets():
    targets = []
    for directory in ('tests', 'company_agent/tests', 'examples/deployment/dev'):
        for path in sorted((ROOT / directory).glob('test_*.py')):
            for node in ast.parse(path.read_text()).body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test_'):
                    targets.append(path.relative_to(ROOT).as_posix() + '::' + node.name)
    return targets


def artifacts(output, env):

    source = next((output / 'build').glob('*.tar.gz'))
    with tarfile.open(source) as archive:
        names = {name.split('/', 1)[1] for name in archive.getnames() if '/' in name}
        required_sources = {
            'config.example.toml', 'CONTRIBUTING.md', 'Dockerfile', '.dockerignore', 'docker-compose.yml', 'uv.lock',
            'examples/litellm/config.yaml', 'examples/litellm/profiles/dev.yaml', 'examples/litellm/local/vllm.example.yaml',
            'examples/official_document_workflow/deployment/ocr/vllm.example.yaml',
            'examples/shared/config.py', 'examples/deployment/cli.py',
        }
        if not required_sources <= names or {'config.local.toml', 'vllm.local.yaml', 'ocr-vllm.local.yaml'} & names:
            raise RuntimeError('Source package must include public configuration and exclude private settings.')
        if any(name.startswith('examples/litellm/') and name.endswith('.py') for name in names):
            raise RuntimeError('LiteLLM configuration must not include retired Python entrypoints.')
        if 'examples/identity/adapters/webui.py' not in names or 'examples/openwebui/identity.py' in names:
            raise RuntimeError('Source package must contain only the canonical WebUI identity adapter.')
        if 'examples/configuration.py' in names:
            raise RuntimeError('Source package includes the retired configuration entrypoint.')
    wheel = next((output / 'build').glob('*.whl'))
    target = output / 'installed-wheel'
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if any(
            n.startswith((
                'deploy/', 'gateway_services/', 'gateway_runtime/', 'official_document_workflow/', 'examples/deployment_registry/', 'examples/auth/', 'infra/',
                'examples/litellm/', 'examples/openwebui/pipelines/', 'examples/openwebui/deployment/', 'pipelines/', 'company_ai_access/', 'examples/deployment/archive/',
            ))
            or (n.startswith('examples/deployment/') and n not in {'examples/deployment/__init__.py', 'examples/deployment/cli.py'} and not n.startswith('examples/deployment/registry/'))
            or n == 'examples/configuration.py'
            or n == 'examples/openwebui/identity.py'
            or n.startswith('company_agent/tests/')
            or n.startswith('examples/official_document_workflow/deployment/')
            or '/deployment/production/' in n
            or n.endswith(('.key', '.sqlite3', '.dump', '.zip'))
            for n in names
        ):
            raise RuntimeError('Wheel includes obsolete namespaces, deployment staging or runtime data.')
        required = {
            'examples/shared/runtime/database.py',
            'examples/shared/config.py',
            'examples/deployment/cli.py',
            'company_agent/evals/sandbox.py',
            'company_agent/evals/fixtures.py',
            'examples/identity/runtime.py',
            'examples/identity/sessions/tokens.py',
            'examples/identity/providers/subjects.py',
            'examples/identity/accounts/recovery.py',
            'examples/identity/routers/auth.py',
            'examples/shared/runtime/http_clients.py',
            'examples/openwebui/app.py',
            'examples/openwebui/serve.py',
            'examples/openwebui/smoke.py',
            'examples/identity/adapters/webui.py',
            'examples/ai_usage/config.py',
            'examples/ai_usage/presentation.py',
            'examples/ai_usage/adapters/webui.py',
            'examples/deployment/__init__.py',
            'examples/deployment/registry/router.py',
            'examples/deployment/registry/service.py',
            'examples/official_document_workflow/services/processor.py',
            'examples/official_document_workflow/frontend/assets/app.js',
        }
        if required - set(names):
            raise RuntimeError('Wheel is missing shared runtime or WebUI integration modules.')
        for name in names:
            if name.endswith('.py') and (ROOT / name).is_file():
                if archive.read(name) != (ROOT / name).read_bytes():
                    raise RuntimeError('Wheel does not match source: ' + name)
        archive.extractall(target)
    dependencies = [
        str(Path(p).resolve())
        for p in sys.path
        if p and Path(p).is_dir() and Path(p).resolve() != ROOT and ROOT not in Path(p).resolve().parents
    ]
    code = """import sys,json
from pathlib import Path
sys.path[:0]=[sys.argv[1],*json.loads(sys.argv[3])]
from examples.identity.adapters.webui import verified_user
assert callable(verified_user)
assert not any(name == 'open_webui' or name.startswith('open_webui.') for name in sys.modules)
from examples.shared.runtime.database import PoolScope
from examples.shared.config import read_service, read_service_snapshot
from examples.deployment.cli import main
from importlib.metadata import distribution
assert callable(read_service) and callable(read_service_snapshot) and callable(main)
from examples.openwebui.smoke import main as webui_smoke
assert callable(webui_smoke)
assert not any(name.startswith('examples.litellm') for name in sys.modules)
assert not any(name == 'open_webui' or name.startswith('open_webui.') for name in sys.modules)
assert next(e.value for e in distribution('ai-gateway').entry_points if e.group=='console_scripts' and e.name=='ai-gateway')=='examples.deployment.cli:main'
from company_agent.ops.deploy_webui import function_source
assert 'class Pipe' in function_source()
from importlib.resources import files
from examples.ai_usage.config import UsageQuerySettings
from examples.ai_usage.adapters.webui import install_webui_usage
from examples.identity.providers.subjects import linked_subject
assert linked_subject({'oidc': {'sub': 'verified'}}) == 'verified'
for filename, entry in (('my_daily_usage_pipe.py','Pipe'),('keycloak_subject_budget_filter.py','Filter')):
 source=files('examples.openwebui.functions').joinpath(filename).read_text()
 namespace={'__name__':'isolated_function_entry'}
 exec(compile(source,filename,'exec'),namespace)
 assert callable(getattr(namespace[entry](), 'pipe' if entry=='Pipe' else 'inlet'))
from examples.identity.database import Database
from examples.identity.models import Base
from sqlalchemy.orm import configure_mappers
configure_mappers()
assert len(Base.metadata.tables)==24 and Database().engine is None
assert 'account_action_tokens' in Base.metadata.tables
import examples.company_ai_access.app
import company_agent.evals.benchmark
import company_agent.evals.scenario_suite
import company_agent.evals.comparison
import company_agent.evals.data_replacement
import company_agent.evals.smoke
import company_agent.evals.smoke_vision
import company_agent.evals.capacity
assert not any(n=='company_agent.tests' or n.startswith('company_agent.tests.') for n in sys.modules)
from examples.company_ai_access.cli.export_contract import contract
assert contract()==json.loads(Path(sys.argv[2]).read_text())
assert not any(n in {'deploy','examples.configuration'} or n.startswith(('deploy.','gateway_services.','gateway_runtime.','examples.auth.','official_document_workflow.','company_ai_access.','src.','open_webui.')) for n in sys.modules)
"""
    # -I plus a cwd outside the checkout prevents source-tree imports masking a
    # broken wheel. Third-party dependency roots are reused, never repo paths.
    with (output / 'wheel-import.log').open('w') as log:
        result = subprocess.run(
            [
                sys.executable,
                '-I',
                '-c',
                code,
                str(target),
                str(ROOT / 'docs/company-ai-access-openapi.json'),
                json.dumps(dependencies),
            ],
            cwd=output,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    if result.returncode:
        raise RuntimeError('Isolated wheel import failed; see wheel-import.log')
    _release_imports.build(ROOT, output / 'company-ai-source.zip', '1.1.9', 'https://changdar-server.mooo.com')
    with zipfile.ZipFile(output / 'company-ai-source.zip') as archive:
        names = set(archive.namelist())
        if not {'examples/shared/config.py', 'examples/deployment/cli.py'} <= names or 'examples/configuration.py' in names:
            raise RuntimeError('Company AI source bundle is missing canonical configuration or CLI modules.')
        if any(name.startswith('examples/deployment/tools/') for name in names):
            raise RuntimeError('Company AI source bundle includes unrelated deployment tools.')
    _deployment_imports.build(output / 'identity-bundle')
    verified = _deployment_imports.read_bundle(output / 'identity-bundle')
    manifest, payloads = verified
    if 'examples/identity/adapters/webui.py' not in payloads or 'examples/openwebui/identity.py' in payloads:
        raise RuntimeError('Identity bundle must contain only the canonical WebUI identity adapter.')
    if not {'examples/shared/config.py', 'examples/deployment/cli.py'} <= payloads.keys() or 'examples/configuration.py' in payloads:
        raise RuntimeError('Identity bundle must contain only the canonical configuration and CLI modules.')
    verified.verify(verified.stage(output / 'identity-releases'))
    assert hashlib.sha256(payloads['examples/bff/proxy.py']).hexdigest() == next(
        row['sha256'] for row in manifest['files'] if row['name'] == 'examples/bff/proxy.py'
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='New private directory for reports and build artifacts.')
    parser.add_argument(
        '--with-archive', action='store_true',
        help='Also require and verify local historical archives.',
    )
    parser.add_argument('--archive-dir', type=Path, help='Verify historical archives restored outside the checkout.')
    args = parser.parse_args()
    output = args.output.resolve() if args.output else Path(tempfile.mkdtemp(prefix='ai-gateway-validation-'))
    if args.output:
        output.mkdir(parents=True, mode=0o700)
    report = {'phases': [], 'output': str(output)}
    private_prefixes = (
        'AI_ACCESS_', 'COMPANY_AGENT_', 'EIP_', 'OPENAI_', 'LITELLM_', 'OIDC_',
        'BFF_', 'KEYCLOAK_', 'JWT_', 'DATABASE_', 'REDIS_', 'BREVO_', 'GOOGLE_',
        'ANTHROPIC_', 'VERTEX_', 'CODEX_', 'PG',
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith(private_prefixes)}
    env.update(
        PYTHON_DOTENV_DISABLED='1',
        JWT_SECRET_KEY='validation-only-not-a-runtime-key',
        WEBUI_SECRET_KEY='validation-only-not-a-runtime-key',
        DATA_DIR=str(output / 'webui'),
    )
    for key in tuple(env):
        if key.endswith('_LIVE'):
            env[key] = '0'

    def phase(name, operation):
        print(name + '...', flush=True)
        start = time.monotonic()
        try:
            operation()
        except Exception:
            report['phases'].append({'name': name, 'status': 'failed'})
            raise
        report['phases'].append({'name': name, 'status': 'passed', 'seconds': round(time.monotonic() - start, 2)})

    try:
        phase(
            'static checks',
            lambda: execute(
                [
                    sys.executable,
                    '-m',
                    'ruff',
                    'check',
                    '--select',
                    'F,RUF059',
                    'company_agent',
                    'examples/official_document_workflow',
                    'examples/identity',
                    'examples/bff',
                    'examples/db_management',
                    'examples/deployment/registry',
                    'examples/ai_usage',
                    'examples/shared',
                    'examples/openwebui',
                    'examples/deployment/cli.py',
                    'examples/company_ai_access',
                    '--exclude',
                    '**/company_ai_access/deployment/**,**/official_document_workflow/deployment/**,**/ai_usage/deployment/**,**/company_agent/deployment/**',
                ],
                env,
                output / 'ruff.log',
            ),
        )
        for name, command in (
            ('candidate settings', [sys.executable, '-m', 'company_agent.configs.build', '--check']),
            ('dependency lock', ['uv', 'lock', '--check']),
            ('archive manifest', [sys.executable, '-m', 'examples.deployment.tools.verify_archive', '--manifest-only']),
        ):
            phase(
                name,
                lambda command=command, name=name: execute(command, env, output / (name.replace(' ', '-') + '.log')),
            )
        if args.with_archive or args.archive_dir is not None:
            command = [sys.executable, '-m', 'examples.deployment.tools.verify_archive']
            if args.archive_dir is not None:
                command.extend(['--archive-dir', str(args.archive_dir.resolve())])
            phase('archive integrity', lambda: execute(command, env, output / 'archive-integrity.log'))
        else:
            report['phases'].append({
                'name': 'archive integrity', 'status': 'skipped',
                'reason': 'Local historical archives are optional; use --with-archive or --archive-dir to verify bytes.',
            })
        with postgres(output) as dsn:
            for key in (
                'COMPANY_AGENT_TEST_DATABASE_URL',
                'EIP_TEST_DATABASE_URL',
                'AI_ACCESS_TEST_DATABASE_URL',
                'COMPANY_AGENT_DATABASE_URL',
                'EIP_DATABASE_URL',
                'AI_ACCESS_DATABASE_URL',
                'DATABASE_URL',
            ):
                env[key] = dsn
            code = """import unittest,json,sys
from examples.shared.runtime.database import PoolScope
pools=PoolScope()
pool_binding=pools.bind()
pool_binding.__enter__()
loader=unittest.TestLoader()
suite=unittest.TestSuite([loader.discover('tests',top_level_dir='.'),loader.discover('company_agent/tests',top_level_dir='.')])
try:r=unittest.TextTestRunner(verbosity=1).run(suite)
finally:
 pool_binding.__exit__(None,None,None)
 pools.close()
with open(sys.argv[1],'w') as f:json.dump({'run':r.testsRun,'failed':len(r.failures),'errors':len(r.errors),'skipped':len(r.skipped),'passed':r.testsRun-len(r.failures)-len(r.errors)-len(r.skipped),'skipped_tests':[{'id':test.id(),'reason':reason} for test,reason in r.skipped]},f)
raise SystemExit(not r.wasSuccessful())
"""
            phase(
                'unittest',
                lambda: execute(
                    [sys.executable, '-c', code, str(output / 'unittest.json')], env, output / 'unittest.log',
                ),
            )
            phase(
                'pytest and native CLI',
                lambda: execute(
                    [
                        sys.executable,
                        '-m',
                        'pytest',
                        '-q',
                        '--junitxml=' + str(output / 'pytest.xml'),
                        *pytest_targets(),
                    ],
                    env,
                    output / 'pytest.log',
                ),
            )
        phase(
            'clean sdist and wheel',
            lambda: execute(
                [sys.executable, '-m', 'build', '--outdir', str(output / 'build')], env, output / 'build.log',
            ),
        )
        phase('isolated imports and deployment artifacts', lambda: artifacts(output, env))
    finally:
        if (output / 'unittest.json').is_file():
            report['unittest'] = json.loads((output / 'unittest.json').read_text())
        if (output / 'pytest.xml').is_file():
            totals = {key: 0 for key in ('tests', 'failures', 'errors', 'skipped')}
            for suite in ET.parse(output / 'pytest.xml').getroot().iter('testsuite'):
                for key in totals:
                    totals[key] += int(suite.get(key, '0'))
            totals['passed'] = totals['tests'] - totals['failures'] - totals['errors'] - totals['skipped']
            totals['skipped_tests'] = [
                {
                    'id': case.get('classname', '') + '.' + case.get('name', ''),
                    'reason': case.find('skipped').get('message', ''),
                }
                for case in ET.parse(output / 'pytest.xml').getroot().iter('testcase') if case.find('skipped') is not None
            ]
            report['pytest'] = totals
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print('Report: ' + str(output / 'report.json'), flush=True)


if __name__ == '__main__':
    main()
