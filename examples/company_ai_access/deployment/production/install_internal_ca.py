"""Install B's private CA maintenance and public-only signing handoff endpoints."""
import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import socket
import subprocess
import zipfile


SNIPPET = Path('/etc/nginx/snippets/company-ai-production.conf')
CODE = Path('/opt/company-ai-code-signing/internal_ca.py')
STATE = Path('/var/lib/company-ai-code-signing')
PUBLIC = Path('/var/lib/company-ai-code-signing-public')
TOOLS = Path('/var/lib/company-ai-signing-tools')
INCOMING = Path('/var/lib/company-ai-signing-incoming')
BEGIN = '# BEGIN company-ai internal code signing'
END = '# END company-ai internal code signing'


def routes(original, vm_ip):
    ipaddress.IPv4Address(vm_ip)
    match = re.search(r'(?ms)^location = /company-ai/downloads/Get-CompanyAIKey\.ps1 \{\n.*?^\}', original)
    if not match:
        raise ValueError('Expected the existing private helper download location.')
    rules = re.findall(r'(?m)^\s*(?:allow|deny)\s+[^;\n]+;', match.group())
    if not rules or rules[-1].strip() != 'deny all;':
        raise ValueError('The existing network allowlist must terminate with deny all.')
    read_rules = '\n'.join('    ' + value.strip() for value in rules)
    blocks = []
    for name, content_type in (
        ('CompanyAI-CodeSigning-Root.cer', 'application/pkix-cert'),
        ('CompanyAI-CodeSigning-Publisher.cer', 'application/pkix-cert'),
        ('CompanyAI-CodeSigning.crl', 'application/pkix-crl'), ('pki.json', 'application/json'),
    ):
        blocks.append(f'''location = /company-ai/pki/{name} {{
{read_rules}
    alias {PUBLIC}/{name};
    default_type {content_type};
    limit_except GET {{ deny all; }}
    add_header Cache-Control "no-store" always;
    add_header X-Content-Type-Options nosniff always;
}}''')
    blocks.append(f'''location = /company-ai/signing/CompanyAI-SigningTools.zip {{
{read_rules}
    alias {TOOLS}/CompanyAI-SigningTools.zip;
    default_type application/zip;
    limit_except GET {{ deny all; }}
    add_header Cache-Control "no-store" always;
    add_header X-Content-Type-Options nosniff always;
}}''')
    for name, size in (('request.csr', '16k'), ('signed-release.zip', '2m')):
        blocks.append(f'''location = /company-ai/signing/{name} {{
    allow 127.0.0.1;
    allow {vm_ip}/32;
    deny all;
    alias {INCOMING}/{name};
    dav_methods PUT;
    dav_access user:rw group:rw;
    create_full_put_path off;
    client_max_body_size {size};
    limit_except PUT {{ deny all; }}
    add_header Cache-Control "no-store" always;
}}''')
    clean = re.sub(re.escape(BEGIN) + r'.*?' + re.escape(END) + r'\n?', '', original, flags=re.S).rstrip()
    return clean + '\n\n' + BEGIN + '\n' + '\n\n'.join(blocks) + '\n' + END + '\n'


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT)


def atomic(path, payload, mode=0o644):
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('Deployment paths must not be links.')
    temporary = path.with_name('.' + path.name + '.' + str(os.getpid()))
    try:
        with temporary.open('xb') as stream:
            stream.write(payload)
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def install_tools(archive, expected):
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        raise ValueError('Signing toolkit checksum mismatch.')
    public = json.loads((PUBLIC / 'pki.json').read_text())
    allowed = {'New-CompanySigningRequest.ps1','Complete-CompanySigning.ps1','CompanyAI.Signing.ps1',
               'Sign-CompanyAIRelease.ps1','Install-CompanyAITrust.ps1','signing-settings.json','README.txt',
               'scripts/Setup-CompanyAI.ps1','scripts/Get-CompanyAIKey.ps1','scripts/Start-CompanyCodex.ps1'}
    with zipfile.ZipFile(archive) as source:
        if set(source.namelist()) != allowed or len(source.namelist()) != len(allowed):
            raise ValueError('Unexpected toolkit files.')
        settings = json.loads(source.read('signing-settings.json'))
        if settings['root_sha256'] != public['root_sha256'] or settings['portal'] != 'https://changdar-server.mooo.com':
            raise ValueError('Toolkit does not pin the actual production CA.')
        for entry in settings['files']:
            payload = source.read('scripts/' + entry['name'])
            if hashlib.sha256(payload).hexdigest() != entry['sha256']:
                raise ValueError('Reviewed signing source hash mismatch.')
    TOOLS.mkdir(mode=0o755, exist_ok=True)
    TOOLS.chmod(0o755)
    atomic(TOOLS / 'CompanyAI-SigningTools.zip', archive.read_bytes())
    return {'toolkit_sha256': expected, 'root_sha256': public['root_sha256'], 'public_only': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ca-source', type=Path)
    parser.add_argument('--ca-sha256')
    parser.add_argument('--vm-ip', default='192.168.1.98')
    parser.add_argument('--tools', type=Path)
    parser.add_argument('--tools-sha256')
    args = parser.parse_args()
    if os.geteuid() != 0 or socket.gethostname() != 'changdar02-System-Product-Name':
        raise SystemExit('Run as root on the verified production B host.')
    if args.tools:
        print(json.dumps(install_tools(args.tools, args.tools_sha256), indent=2))
        return
    if not args.ca_source or hashlib.sha256(args.ca_source.read_bytes()).hexdigest() != args.ca_sha256:
        raise ValueError('An exact reviewed CA source/checksum is required.')
    if '--with-http_dav_module' not in run('/usr/sbin/nginx','-V'):
        raise ValueError('Nginx DAV is required for the two source-IP-restricted upload files.')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = Path('/var/backups/company-ai-code-signing') / stamp
    backup.mkdir(mode=0o700, parents=True)
    backup.parent.chmod(0o700)
    original = SNIPPET.read_bytes()
    (backup / 'nginx-before.conf').write_bytes(original)
    (backup / 'nginx-before.conf').chmod(0o600)
    CODE.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if CODE.exists():
        shutil.copy2(CODE, backup / 'internal_ca-before.py')
    atomic(CODE, args.ca_source.read_bytes())
    summary = json.loads(run('/usr/bin/python3',str(CODE),'init'))
    # Root material is backed up only into the root-private backup, never served.
    shutil.copytree(STATE, backup / 'authority', copy_function=shutil.copy2)
    (backup / 'authority').chmod(0o700)
    identity = pwd.getpwnam('www-data')
    INCOMING.mkdir(mode=0o730, exist_ok=True)
    if INCOMING.is_symlink():
        raise ValueError('Incoming handoff directory must not be a link.')
    os.chown(INCOMING, 0, identity.pw_gid)
    INCOMING.chmod(0o730)
    TOOLS.mkdir(mode=0o755, exist_ok=True)
    TOOLS.chmod(0o755)
    generated = routes(original.decode(), args.vm_ip).encode()
    atomic(SNIPPET, generated)
    try:
        run('/usr/sbin/nginx','-t')
        run('systemctl','reload','nginx')
    except BaseException:
        atomic(SNIPPET, original)
        run('/usr/sbin/nginx','-t')
        run('systemctl','reload','nginx')
        raise
    service = '''[Unit]
Description=Refresh Company AI code-signing certificate revocation list
[Service]
Type=oneshot
User=root
UMask=0077
ExecStart=/usr/bin/python3 /opt/company-ai-code-signing/internal_ca.py refresh-crl
NoNewPrivileges=true
PrivateTmp=true
PrivateNetwork=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/company-ai-code-signing /var/lib/company-ai-code-signing-public
CapabilityBoundingSet=
'''
    timer = '''[Unit]
Description=Daily Company AI code-signing CRL refresh
[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=300
Unit=company-ai-code-signing-crl.service
[Install]
WantedBy=timers.target
'''
    atomic(Path('/etc/systemd/system/company-ai-code-signing-crl.service'), service.encode())
    atomic(Path('/etc/systemd/system/company-ai-code-signing-crl.timer'), timer.encode())
    run('systemctl','daemon-reload')
    run('systemctl','enable','--now','company-ai-code-signing-crl.timer')
    run('systemctl','start','company-ai-code-signing-crl.service')
    receipt = {'authority':summary,'backup':str(backup),'ca_source_sha256':args.ca_sha256,
               'vm_upload_ip':args.vm_ip,'downloaded_scripts_still_unsigned':True,
               'employee_trust_changed':False,'windows_vm_key_request':'pending'}
    (backup / 'installation.json').write_text(json.dumps(receipt, indent=2)+'\n')
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()
