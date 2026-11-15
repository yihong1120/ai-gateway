"""Expose one reviewed unsigned EXE signing input only to the signing VM."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import zipfile


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,required=True);parser.add_argument('--sha256',required=True)
    args=parser.parse_args()
    if os.geteuid()!=0 or socket.gethostname()!='changdar02-System-Product-Name':raise ValueError('Run as B root.')
    if hashlib.sha256(args.archive.read_bytes()).hexdigest()!=args.sha256:raise ValueError('Input archive checksum mismatch.')
    with zipfile.ZipFile(args.archive) as z:
        if len(z.namelist())!=3 or set(z.namelist())!={'changdar-ai-setup.exe','signing-input.json','Sign-ChangdarAIInstaller.ps1'}:
            raise ValueError('Unexpected signing input files.')
        settings=json.loads(z.read('signing-input.json'))
        if hashlib.sha256(z.read('changdar-ai-setup.exe')).hexdigest()!=settings['unsigned_sha256']:
            raise ValueError('Unsigned EXE hash differs.')
        public=json.loads(Path('/var/lib/company-ai-code-signing-public/pki.json').read_text())
        if settings['root_sha256']!=public['root_sha256'] or settings['publisher_sha256']!=public['signer']['sha256']:
            raise ValueError('Signing input must pin the active publisher.')
    target=Path('/var/lib/company-ai-signing-tools/changdar-ai-installer-input.zip')
    if target.is_symlink():raise ValueError('Signing input must not be a link.')
    shutil.copyfile(args.archive,target);target.chmod(0o644)
    snippet=Path('/etc/nginx/snippets/company-ai-production.conf')
    previous=snippet.read_bytes()
    route='''
# BEGIN changdar-ai EXE signing input
location = /company-ai/signing/changdar-ai-installer-input.zip {
    allow 127.0.0.1;
    allow 192.168.1.98/32;
    deny all;
    alias /var/lib/company-ai-signing-tools/changdar-ai-installer-input.zip;
    default_type application/zip;
    limit_except GET { deny all; }
    add_header Cache-Control "no-store" always;
    add_header X-Content-Type-Options nosniff always;
}
# END changdar-ai EXE signing input
'''
    if 'location = /company-ai/signing/changdar-ai-installer-input.zip {' not in previous.decode():
        backup=args.archive.parent/'nginx-before-exe-input.conf'
        if backup.exists():raise ValueError('Preserve the existing input-route backup.')
        backup.write_bytes(previous);backup.chmod(0o600)
        snippet.write_bytes(previous+route.encode())
        try:
            subprocess.run(['/usr/sbin/nginx','-t'],check=True,capture_output=True)
            subprocess.run(['systemctl','reload','nginx'],check=True)
        except BaseException:
            snippet.write_bytes(previous)
            subprocess.run(['/usr/sbin/nginx','-t'],check=True,capture_output=True)
            subprocess.run(['systemctl','reload','nginx'],check=True)
            raise
    print(json.dumps({'signing_input_sha256':args.sha256,'vm_ip':'192.168.1.98','production_download_published':False}))


if __name__=='__main__':main()
