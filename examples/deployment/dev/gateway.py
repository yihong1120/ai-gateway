"""Run with sudo on B after local dev health checks have passed."""
from __future__ import annotations
import argparse
import configparser
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
import urllib.request

HOST = 'ai-dev.changdar-server.mooo.com'
ORIGIN = 'https://' + HOST
MARKER = '# Managed by ai-gateway-dev-v1'
DEST = Path('/etc/nginx/sites-available/ai-gateway-dev.conf')
LINK = Path('/etc/nginx/sites-enabled/ai-gateway-dev.conf')
CERT = Path('/etc/letsencrypt/live') / HOST


def run(*args):
    subprocess.run(args, check=True)


def configuration(tls):
    result = f'''{MARKER}
server {{
    listen 80;
    listen [::]:80;
    server_name {HOST};
    location /.well-known/acme-challenge/ {{ root /var/www/html; }}
    location / {{ return 308 https://{HOST}$request_uri; }}
}}
'''
    if tls:
        result += f'''
server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name {HOST};
    ssl_certificate {CERT}/fullchain.pem;
    ssl_certificate_key {CERT}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    add_header X-Content-Type-Options nosniff always;
    add_header X-AI-Gateway dev-b always;
    client_max_body_size 100M;
    proxy_connect_timeout 15s;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    location / {{
        proxy_pass http://100.96.17.117:18082;
        proxy_http_version 1.1;
        proxy_redirect off;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Port 443;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_cache off;
    }}
}}
'''
    return result


def install(text):
    previous = DEST.read_bytes() if DEST.exists() else None
    existed = LINK.is_symlink()
    temporary = DEST.with_suffix('.tmp')
    temporary.write_text(text)
    temporary.chmod(0o644)
    temporary.replace(DEST)
    if not LINK.exists():
        LINK.symlink_to(DEST)
    try:
        run('/usr/sbin/nginx', '-t')
    except BaseException:
        if previous is None:
            DEST.unlink(missing_ok=True)
        else:
            DEST.write_bytes(previous)
        if not existed:
            LINK.unlink(missing_ok=True)
        raise
    run('/usr/bin/systemctl', 'reload', 'nginx')


def wait_gateway(timeout=45):
    """Reload signals the master; wait for new workers with verified dev TLS."""
    deadline = time.monotonic() + timeout
    last_error = 'No healthy dev response.'
    while True:
        result = subprocess.run([
            '/usr/bin/curl', '--noproxy', '*', '--fail', '--silent', '--show-error',
            '--connect-timeout', '3', '--max-time', '5', '--dump-header', '-',
            '--resolve', HOST + ':443:127.0.0.1', ORIGIN + '/health',
        ], capture_output=True, text=True)
        if result.returncode == 0:
            headers, _, body = result.stdout.partition('\n\n')
            try:
                ready = json.loads(body).get('status') is True
            except (ValueError, AttributeError):
                ready = False
            lines = {line.strip().lower() for line in headers.splitlines()}
            if ready and {'x-ai-gateway: dev-b', 'x-ai-environment: dev'} <= lines:
                return
            last_error = 'HTTPS response has not reached the dev gateway and app yet.'
        else:
            last_error = result.stderr.strip() or 'curl exit ' + str(result.returncode)
        if time.monotonic() >= deadline:
            raise RuntimeError('Dev HTTPS did not become ready after reload: ' + last_error)
        time.sleep(1)


def verify():
    wait_gateway()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for url in (ORIGIN + '/health', 'https://ai.changdar-server.mooo.com/health',
                'https://changdar-server.mooo.com/', 'https://wiki.changdar-server.mooo.com/'):
        with opener.open(url, timeout=30) as response:
            if response.status != 200:
                raise RuntimeError('Public URL check failed.')
            if url == ORIGIN + '/health' and (response.headers.get('X-AI-Gateway') != 'dev-b'
                    or response.headers.get('X-AI-Environment') != 'dev'):
                raise RuntimeError('Public dev URL reaches another gateway or application.')
    run('/usr/bin/systemctl', 'is-enabled', 'nginx')
    run('/usr/bin/systemctl', 'is-active', 'certbot.timer')
    print('Passed: ai-dev HTTPS on B reaches isolated dev on A; all three production URLs remain healthy.')
    print('Open ' + ORIGIN + ' and sign in using Keycloak. Only ChangDar currently has dev access.')


def main():
    if os.geteuid() != 0 or socket.gethostname() != 'changdar02-System-Product-Name':
        raise RuntimeError('Run with sudo on B (changdar02).')
    if DEST.exists() and not DEST.read_text().startswith(MARKER):
        raise RuntimeError('Refusing to replace an unowned virtual host.')
    if LINK.exists() and (not LINK.is_symlink() or LINK.resolve() != DEST):
        raise RuntimeError('Unexpected existing enabled dev site.')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://100.96.17.117:18082/health', timeout=15) as response:
        if response.headers.get('X-AI-Environment') != 'dev' or not json.load(response).get('status'):
            raise RuntimeError('A dev service did not pass the private health check.')
    backup = Path('/etc/nginx/ai-gateway-dev-backups') / str(time.time_ns())
    backup.mkdir(parents=True, mode=0o700)
    if DEST.exists():
        shutil.copy2(DEST, backup / DEST.name)
    (Path('/var/www/html/.well-known/acme-challenge')).mkdir(parents=True, exist_ok=True)
    install(configuration(CERT.joinpath('fullchain.pem').exists()))
    renewal = configparser.ConfigParser()
    renewal.read_string('[certificate]\n' + Path('/etc/letsencrypt/renewal/ai.changdar-server.mooo.com.conf').read_text())
    account = renewal.get('renewalparams', 'account')
    server = renewal.get('renewalparams', 'server')
    print('A dev health verified. Obtaining a separate HTTPS certificate for ai-dev.', flush=True)
    run('/usr/bin/certbot', 'certonly', '--webroot', '-w', '/var/www/html', '-d', HOST,
        '--cert-name', HOST, '--non-interactive', '--agree-tos', '--keep-until-expiring',
        '--account', account, '--server', server)
    install(configuration(True))
    hook = Path('/etc/letsencrypt/renewal-hooks/deploy/ai-gateway-dev-nginx')
    hook.parent.mkdir(parents=True, exist_ok=True)
    content = '#!/bin/sh\n# Managed by ai-gateway-dev-v1\ncase " $RENEWED_DOMAINS " in\n'
    content += f'  *" {HOST} "*) /usr/sbin/nginx -t && /usr/bin/systemctl reload nginx ;;\nesac\n'
    if hook.exists() and 'Managed by ai-gateway-dev-v1' not in hook.read_text():
        raise RuntimeError('Existing unowned certificate hook.')
    hook.write_text(content)
    hook.chmod(0o755)
    run('/bin/sh', '-n', str(hook))
    verify()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify-only', action='store_true', help='Check the deployed gateway without sudo or reload.')
    if parser.parse_args().verify_only:
        verify()
    else:
        main()
