"""Upgrade B's existing access service without re-creating its registry or identity."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
import zipfile
from datetime import datetime
from datetime import timezone
from pathlib import Path


OWNER = 'changdar02'
HOME = Path('/home/changdar02')
SERVICE = 'company-ai-access.service'
CONFIG = HOME / '.config/company-ai-access/production.json'
NGINX = Path('/etc/nginx/snippets/company-ai-production.conf')
DROPIN = Path('/etc/systemd/system/company-ai-access.service.d/20-codex-release.conf')
PYTHON = HOME / '.conda/envs/webui_env/bin/python'
LEGACY = HOME / 'company-ai-production-20261019'
MODELS = 'gemini-3.8-flash,gemini-3.5-flash-lite,gemini-4-argon'


def launcher_route(text):
    """Clone the live helper's exact network/proxy policy; preserve other routes."""
    route = 'location = /company-ai/downloads/Start-CompanyCodex.ps1 {'
    if route in text:
        return text
    matches = list(
        re.finditer(
            r'(?ms)^location = /company-ai/downloads/Get-CompanyAIKey\.ps1 \{\n.*?^\}', text,
        ),
    )
    if len(matches) != 1 or 'proxy_pass http://127.0.0.1:8013;' not in matches[0].group():
        raise RuntimeError('Expected one existing private helper download route.')
    match = matches[0]
    block = match.group().replace('Get-CompanyAIKey.ps1', 'Start-CompanyCodex.ps1')
    return text[:match.end()] + '\n\n' + block + text[match.end():]


def unpack(archive, output, expected, expected_version='1.1.3'):
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        raise RuntimeError('Source archive checksum mismatch.')
    if output.exists():
        raise RuntimeError('Use a new release directory.')
    if output.parent.is_symlink():
        raise RuntimeError('Release parent must not be a link.')
    if not output.parent.exists():
        output.parent.mkdir(mode=0o755, parents=True)
        output.parent.chmod(0o755)
    with zipfile.ZipFile(archive) as source:
        manifest = json.loads(source.read('release.json'))
        if manifest['version'] != expected_version or manifest['portal'] != 'https://changdar-server.mooo.com':
            raise RuntimeError('Unexpected production source release.')
        names = source.namelist()
        if len(names) != len(set(names)):
            raise RuntimeError('Duplicate archive path.')
        for name in names:
            path = Path(name)
            if path.is_absolute() or '..' in path.parts or '\\' in name or not name:
                raise RuntimeError('Unsafe archive path.')
            mode = source.getinfo(name).external_attr >> 16
            if mode & 0o170000 == 0o120000:
                raise RuntimeError('Release must not contain links.')
        for entry in manifest['files']:
            data = source.read(entry['name'])
            if len(data) != entry['bytes'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
                raise RuntimeError('Release manifest mismatch.')
        output.mkdir(mode=0o755, parents=True)
        for name in names:
            path = output / name
            path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            path.write_bytes(source.read(name))
            path.chmod(0o644)
        # Deployment runs with a private umask; source must remain traversable
        # by the non-root service account, while private backups stay 0700.
        output.chmod(0o755)
        for directory in output.rglob('*'):
            if directory.is_dir():
                directory.chmod(0o755)
    return manifest


def installer_route(text):
    route = 'location = /company-ai/downloads/changdar-ai-setup.exe {'
    if route in text:
        return text
    matches = list(re.finditer(r'(?ms)^location = /company-ai/downloads/Get-CompanyAIKey\.ps1 \{\n.*?^\}', text))
    if len(matches) != 1 or 'proxy_pass http://127.0.0.1:8013;' not in matches[0].group():
        raise RuntimeError('Expected the existing private helper download route.')
    match = matches[0]
    block = match.group().replace('Get-CompanyAIKey.ps1', 'changdar-ai-setup.exe')
    return text[:match.end()]+'\n\n'+block+text[match.end():]


def atomic(path, data, *, uid, gid, mode):
    if path.is_symlink():
        raise RuntimeError('Deployment target must not be a link.')
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.company-ai-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(name, uid, gid)
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def save_receipts(paths, backup):
    receipts = []
    for index, path in enumerate(paths):
        if path.is_symlink():
            raise RuntimeError('Deployment target must not be a link.')
        item = {'path': str(path), 'exists': path.exists()}
        if path.exists():
            info = path.stat()
            saved = backup / str(index)
            shutil.copyfile(path, saved)
            saved.chmod(0o600)
            item.update(backup=str(saved), uid=info.st_uid, gid=info.st_gid, mode=info.st_mode & 0o777)
        receipts.append(item)
    (backup / 'receipt.json').write_text(json.dumps(receipts, indent=2) + '\n')
    return receipts


def restore(receipts):
    failures = []
    for item in reversed(receipts):
        try:
            path = Path(item['path'])
            if item['exists']:
                atomic(path, Path(item['backup']).read_bytes(), uid=item['uid'], gid=item['gid'], mode=item['mode'])
            else:
                path.unlink(missing_ok=True)
        except Exception:
            failures.append(item['path'])
    if failures:
        raise RuntimeError('Deployment file rollback was incomplete.')


def healthy():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(40):
        try:
            with opener.open('http://127.0.0.1:8013/health', timeout=2) as response:
                if json.load(response) == {'status': True, 'environment': 'production'}:
                    return
        except OSError:
            pass
        time.sleep(0.5)
    raise RuntimeError('Production access service failed its health check.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--codex-release', required=True)
    parser.add_argument('--source-version', default='1.1.3')
    parser.add_argument('--smoke-source', type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or socket.gethostname() != 'changdar02-System-Product-Name':
        raise RuntimeError('Run as root on the verified production B host.')
    if not re.fullmatch(r'\d+\.\d+\.\d+', args.codex_release):
        raise RuntimeError('Use the stable Codex release qualified for this deployment.')
    if not re.fullmatch(r'\d+\.\d+\.\d+', args.source_version):
        raise RuntimeError('Use an explicit source release version.')
    os.umask(0o077)
    identity = pwd.getpwnam(OWNER)
    if CONFIG.is_symlink() or CONFIG.stat().st_mode & 0o077:
        raise RuntimeError('Expected the existing private production configuration.')
    values = json.loads(CONFIG.read_text())
    if (
        values.get('AI_ACCESS_ENVIRONMENT') != 'production' or
        values.get('AI_ACCESS_ORIGIN') != 'https://changdar-server.mooo.com' or
        values.get('AI_ACCESS_LITELLM_URL') != 'http://127.0.0.1:4000' or
        values.get('AI_ACCESS_MODELS') != MODELS
    ):
        raise RuntimeError('Production environment/model policy differs from the prepared deployment.')
    if not values.get('AI_ACCESS_DATABASE_URL'):
        raise RuntimeError(
            'Migrate the access registry to local PostgreSQL and configure AI_ACCESS_DATABASE_URL before upgrading.',
        )
    state = Path(values['AI_ACCESS_STATE_DIR'])
    if state != HOME / '.local/share/company-ai-access' or not (state / 'access.key').is_file():
        raise RuntimeError('Expected the existing production registry and encryption key.')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    release = HOME / 'company-ai-access-releases' / (args.source_version + '-codex-' + args.codex_release + '-' + stamp)
    backup = Path('/var/backups/company-ai-access') / stamp
    backup.mkdir(mode=0o700, parents=True)

    def run(*command):
        result = subprocess.run([str(x) for x in command], capture_output=True, text=True, timeout=180)
        if result.returncode:
            with (backup / 'private-error.log').open('a') as log:
                log.write(result.stdout + result.stderr)
            raise RuntimeError(Path(str(command[0])).name + ' failed; private diagnostics retained in backup.')
        return result.stdout

    if run('systemctl', 'is-active', SERVICE).strip() != 'active':
        raise RuntimeError('Expected the existing access service to be active.')
    manifest = unpack(args.archive, release, args.sha256, args.source_version)
    # The public release is immutable; operational smoke source is retained
    # separately, with its own deployment checksum in the private receipt.
    smoke = backup / 'smoke.py'
    shutil.copyfile(args.smoke_source, smoke)
    smoke.chmod(0o600)
    validation_dir = HOME / '.local/state/company-ai-deployments' / stamp
    for directory in (validation_dir.parent.parent, validation_dir.parent, validation_dir):
        if not directory.exists():
            directory.mkdir(mode=0o700)
            os.chown(directory, identity.pw_uid, identity.pw_gid)
    smoke_user = validation_dir / 'smoke.py'
    atomic(smoke_user, smoke.read_bytes(), uid=identity.pw_uid, gid=identity.pw_gid, mode=0o600)
    new_values = dict(values)
    new_values.update(
        AI_ACCESS_CODEX_RELEASE=args.codex_release,
        AI_ACCESS_CODEX_CATALOG=str(release / 'examples/company_ai_access/codex-models.example.json'),
    )
    candidate = validation_dir / 'candidate-config.json'
    atomic(
        candidate, (json.dumps(new_values, indent=2) + '\n').encode(),
        uid=identity.pw_uid, gid=identity.pw_gid, mode=0o600,
    )
    validation_code = (
        'import sys,os,json;sys.path.insert(0,sys.argv[1]);'
        'os.environ.update(json.load(open(sys.argv[2])));'
        'from examples.company_ai_access.config import Settings;'
        's=Settings.from_env();assert s.codex_models==s.models and len(s.models)==3;'
        'from examples.company_ai_access.app import create_app;create_app()'
    )
    run('runuser', '-u', OWNER, '--', str(PYTHON), '-B', '-c', validation_code, str(release), str(candidate))
    updated_nginx = installer_route(launcher_route(NGINX.read_text())).encode()
    unit = (
        '[Service]\nWorkingDirectory=' + str(release) + '\nExecStart=\nExecStart=' +
        str(PYTHON) + ' ' + str(release / 'examples/company_ai_access/deployment/production/run.py') + '\n'
    )
    receipts = save_receipts((CONFIG, NGINX, DROPIN), backup)
    original_key = hashlib.sha256((state / 'access.key').read_bytes()).digest()
    stopped = False
    try:
        atomic(NGINX, updated_nginx, uid=0, gid=0, mode=0o644)
        run('/usr/sbin/nginx', '-t')
        run('systemctl', 'stop', SERVICE)
        stopped = True
        shutil.copytree(state, backup / 'registry')
        # PostgreSQL rows are no longer in the local registry directory.
        backup_code = (
            'import os,json,subprocess,sys;from psycopg import pq;from psycopg.conninfo import conninfo_to_dict;'
            'v=json.load(open(sys.argv[1]));'
            'e={k:x for k,x in os.environ.items() if not k.startswith("PG")};'
            'm={i.keyword.decode():i.envvar.decode() for i in pq.Conninfo.get_defaults() if i.envvar};'
            'e.update({m[k]:x for k,x in conninfo_to_dict(v["AI_ACCESS_DATABASE_URL"]).items()});'
            'r=subprocess.run(["pg_dump","--format=custom","--schema",'
            'v.get("AI_ACCESS_DB_SCHEMA","company_ai_access")],env=e,stdout=subprocess.PIPE,stderr=subprocess.PIPE);'
            'r.check_returncode();open(sys.argv[2],"xb").write(r.stdout)'
        )
        run(str(PYTHON), '-c', backup_code, str(CONFIG), str(backup / 'registry.dump'))
        atomic(CONFIG, candidate.read_bytes(), uid=identity.pw_uid, gid=identity.pw_gid, mode=0o600)
        atomic(DROPIN, unit.encode(), uid=0, gid=0, mode=0o644)
        run('systemctl', 'daemon-reload')
        run('systemctl', 'start', SERVICE)
        healthy()
        run('systemctl', 'reload', 'nginx')
        output = run(
            'runuser', '-u', OWNER, '--', str(PYTHON), '-B', str(smoke_user),
            '--require-codex', '--model-call', '--source-root', str(release),
            '--operator-file', str(LEGACY / 'operator.json'), '--output-dir', str(validation_dir),
        )
        result = json.loads(output)
        if (
            not result['disposable_device_revoked_and_key_deleted'] or
            not all(result['checks'].values()) or
            hashlib.sha256((state / 'access.key').read_bytes()).digest() != original_key
        ):
            raise RuntimeError('Production acceptance or registry preservation failed.')
        receipt = {
            'version': manifest['version'], 'codex_release': args.codex_release,
            'release': str(release), 'backup': str(backup), 'validation': str(validation_dir),
            'source_sha256': args.sha256, 'registry_encryption_key_preserved': True,
            'smoke_sha256': hashlib.sha256(smoke.read_bytes()).hexdigest(),
            'checks': result['checks'], 'model_calls': result['model_calls'],
            'windows_native_acceptance': 'pending', 'windows_scripts_signed': manifest['signed'],
        }
        (backup / 'deployment.json').write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps(receipt, indent=2), flush=True)
    except BaseException:
        restore(receipts)
        run('systemctl', 'daemon-reload')
        if stopped:
            run('systemctl', 'restart', SERVICE)
            healthy()
        run('/usr/sbin/nginx', '-t')
        run('systemctl', 'reload', 'nginx')
        print(json.dumps({'deployment': 'rolled_back', 'backup': str(backup)}), flush=True)
        raise
    finally:
        candidate.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        raise SystemExit(
            'Company AI upgrade failed: ' +
            (str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__),
        ) from None
