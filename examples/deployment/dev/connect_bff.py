"""Connect the existing company dev BFF to A's isolated AI access service."""
from __future__ import annotations

import json
from pathlib import Path

import sys
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from examples.deployment.dev.files import save

CONFIG = Path.home() / '.config/ai-gateway-dev'


def configure():
    company_config = Path.home() / '.config/construction-dev/environment.json'
    access_config = CONFIG / 'ai-access.json'
    company = json.loads(company_config.read_text())
    access = json.loads(access_config.read_text())
    origin = 'https://dev.changdar-server.mooo.com'
    audience = 'visionnaire-dev-api'
    issuer = 'https://changdar-server.mooo.com/keycloak/realms/visionnaire'
    if (company.get('APP_PUBLIC_URL') != origin
            or company.get('OIDC_AUDIENCE') != audience
            or company.get('OIDC_ISSUER_URL') != issuer
            or company.get('BFF_SESSION_COOKIE_NAME') != '__Host-vn_dev_session'
            or access.get('AI_ACCESS_ENVIRONMENT') != 'dev'
            or access.get('AI_ACCESS_ORIGIN') != 'https://ai-dev.changdar-server.mooo.com'
            or access.get('AI_ACCESS_ISSUER') != issuer
            or access.get('AI_ACCESS_LITELLM_URL') != 'http://127.0.0.1:14002'
            or access.get('AI_ACCESS_OIDC_AUDIENCE', '') not in ('', audience)):
        raise RuntimeError('Refusing to connect mismatched or non-dev environments.')
    for path in (company_config, access_config):
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise RuntimeError('Configuration must be private and must not be a symlink.')
        backup = path.with_name(path.stem + '.before-company-ai-bff.json')
        if not backup.exists():
            save(backup, json.loads(path.read_text()))
    company['BFF_AI_ACCESS_API_URL'] = 'http://127.0.0.1:18083'
    access['AI_ACCESS_OIDC_AUDIENCE'] = audience
    origins = [item.strip() for item in access.get('AI_ACCESS_CONTROL_ORIGINS', '').split(',') if item.strip()]
    if origin not in origins:
        origins.append(origin)
    access['AI_ACCESS_CONTROL_ORIGINS'] = ','.join(origins)
    save(access_config, access)
    save(company_config, company)
    print('Dev BFF upstream and verified OIDC audience configured; private backups retained.')
    print('Restart ai-gateway-dev-ai-access and construction-dev-management to apply.')


if __name__ == '__main__':
    configure()
