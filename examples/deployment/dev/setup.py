"""Prepare and start isolated local dev services; never read production databases."""
from __future__ import annotations
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import time
import tomllib
import urllib.request

import yaml
import sys
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from examples.shared.config import config_path, read_services
from examples.deployment.dev.files import save
from examples.deployment.dev.identity import provision

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
CONFIG = Path.home() / '.config/ai-gateway-dev'
DATA = Path.home() / '.local/share/ai-gateway-dev'
WEBPY = Path.home() / '.conda/envs/webui_env/bin/python'
LITELLM_VERSION = next(
    requirement.split('==', 1)[1]
    for requirement in tomllib.loads((ROOT / 'pyproject.toml').read_text())['dependency-groups']['litellm']
    if requirement.startswith('litellm[proxy]==')
)
LLMPY = Path(os.getenv('LITELLM_DEV_PYTHON', str(DATA / 'venvs' / ('litellm-' + LITELLM_VERSION) / 'bin/python')))
ORIGIN = 'https://ai-dev.changdar-server.mooo.com'

# Run inside LLMPY after its frozen sync, never in the application environment.
# LiteLLM's drain tracker writes a legacy private field; Prisma 0.15 needs _engine.
PRISMA_INITIALIZATION = '''\
from importlib.metadata import distribution, version
from pathlib import Path
import sys

if version('litellm') != '1.109.1' or version('prisma') != '0.15.0':
    raise RuntimeError('Prisma compatibility fix requires the tested runtime versions.')
package = distribution('litellm')
path = Path(package.locate_file('litellm/proxy/db/prisma_client.py')).resolve()
if not path.is_relative_to(Path(sys.prefix).resolve()):
    raise RuntimeError('Install LiteLLM into this environment before applying the compatibility fix.')
source = path.read_text()
before = '        prisma_client._Prisma__engine = engine'
after = '        prisma_client._engine = engine'
if source.count(before) == 1 and after not in source:
    path.write_text(source.replace(before, after))
elif before in source or source.count(after) != 1:
    raise RuntimeError('Unexpected LiteLLM Prisma engine implementation.')
print(package.locate_file('litellm/proxy/schema.prisma'))
'''


def litellm_config():
    directory = ROOT / 'examples/litellm'
    try:
        config = yaml.safe_load((directory / 'config.yaml').read_text())
        policies = yaml.safe_load((directory / 'profiles/dev.yaml').read_text())
    except yaml.YAMLError:
        raise ValueError('Invalid LiteLLM development configuration YAML.') from None
    if not isinstance(config, dict) or not isinstance(config.get('model_list'), list):
        raise ValueError('Invalid LiteLLM catalog.')
    if any(not isinstance(item, dict) or not isinstance(item.get('model_name'), str)
           or not item['model_name'] or not isinstance(item.get('litellm_params'), dict)
           for item in config['model_list']):
        raise ValueError('Invalid LiteLLM catalog model.')
    if not isinstance(policies, dict):
        raise ValueError('Invalid LiteLLM development policies.')
    for section, values in policies.items():
        if section not in ('general_settings', 'litellm_settings') or not isinstance(values, dict) or not isinstance(config.get(section), dict):
            raise ValueError('Invalid LiteLLM development policies.')
        config[section].update(values)
    return config


def model_environment(config, values):
    references = set()

    def collect(value):
        if isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, str) and value.startswith('os.environ/'):
            references.add(value.removeprefix('os.environ/'))

    collect(config['model_list'])
    # Dev uses the catalog unchanged, with its own explicit local endpoint.
    if 'LOCAL_MODEL_API_BASE' in references:
        if not values.get('LOCAL_MODEL_DEV_API_BASE'):
            raise ValueError('LOCAL_MODEL_DEV_API_BASE is required for the development local model.')
        values = {**values, 'LOCAL_MODEL_API_BASE': values['LOCAL_MODEL_DEV_API_BASE']}
    return {name: str(values[name]) for name in references if values.get(name) not in (None, '')}


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def dev_compose(*arguments):
    return ['/usr/bin/docker', 'compose', '--env-file', '/dev/null',
            '-f', str(ROOT / 'docker-compose.yml'), '-p', 'ai-gateway-dev', '--profile', 'dev-db', *arguments]


def wait(url, headers=None):
    for _ in range(180):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=3) as r:
                return json.load(r)
        except (OSError, ValueError):
            time.sleep(2)
    raise RuntimeError('Dev service did not become healthy: ' + url)


def api(path, key, value):
    req = urllib.request.Request('http://127.0.0.1:14002' + path,
                                 data=json.dumps(value).encode(),
                                 headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def main():
    if os.geteuid() == 0:
        raise RuntimeError('Run as changdar, without sudo.')
    sources = read_services('litellm', 'webui', path=config_path())
    config = litellm_config()
    llm = model_environment(config, sources['litellm'])
    CONFIG.mkdir(mode=0o700, parents=True, exist_ok=True)
    CONFIG.chmod(0o700)
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Sync only the LiteLLM group into its dedicated environment. The root
    # lock owns every version; application and worker dependencies stay out.
    interpreter = os.getenv('LITELLM_BOOTSTRAP_PYTHON', '3.12')
    run(['uv', 'sync', '--project', ROOT, '--frozen', '--only-group', 'litellm',
         '--python', interpreter],
        env={**os.environ, 'UV_PROJECT_ENVIRONMENT': str(LLMPY.parents[1]), 'PYTHONNOUSERSITE': '1'})
    schema_source = Path(subprocess.check_output([str(LLMPY), '-c',
        PRISMA_INITIALIZATION], text=True, cwd=ROOT).strip())
    schema = schema_source.read_text().replace('provider = "prisma-client-py"',
        'provider = "' + str(LLMPY.parent / 'prisma-client-py') + '"')
    save(CONFIG / 'schema.prisma', schema)
    run([LLMPY, '-m', 'prisma', 'generate', '--schema', CONFIG / 'schema.prisma'],
        cwd=CONFIG, env={**os.environ, 'PATH': str(LLMPY.parent) + ':/usr/bin:/bin', 'PYTHONNOUSERSITE': '1'})
    private = CONFIG / 'credentials.json'
    if private.exists():
        credentials = json.loads(private.read_text())
        if credentials.get('environment') != 'ai-gateway-dev-v1':
            raise RuntimeError('Unknown existing configuration.')
    else:
        credentials = {'environment': 'ai-gateway-dev-v1', **{
            name: secrets.token_hex(32) for name in ('postgres', 'webui_db', 'litellm_db', 'webui_secret', 'salt')},
            'master': 'sk-dev-' + secrets.token_hex(32)}
        save(private, credentials)
    print('Preparing dedicated dev identity, databases and data directories.', flush=True)
    client_secret, operator = provision()
    save(CONFIG / 'operator.json', operator)
    save(CONFIG / 'postgres.env', 'POSTGRES_USER=dev_bootstrap\nPOSTGRES_PASSWORD=' + credentials['postgres'] + '\n')
    dsn = lambda name, password: f'postgresql://{name}:{password}@127.0.0.1:15432/{name}'
    llm.update({
        'LITELLM_MASTER_KEY': credentials['master'], 'LITELLM_SALT_KEY': credentials['salt'],
        'LITELLM_DATABASE_URL': dsn('litellm_dev', credentials['litellm_db']),
        'DATABASE_URL': dsn('litellm_dev', credentials['litellm_db']),
        'PATH': str(LLMPY.parent) + ':/usr/local/bin:/usr/bin:/bin', 'DEBUG': 'false', 'TZ': 'Asia/Taipei',
        'PYTHONPATH': str(ROOT), 'CUDA_VISIBLE_DEVICES': '', 'LITELLM_LOG': 'ERROR',
    })
    engines = sorted((Path.home() / 'ai-gateway-migration/prisma').rglob('query-engine-debian-openssl-3.0.x'))
    if engines:
        llm['PRISMA_QUERY_ENGINE_BINARY'] = str(engines[0])
    save(CONFIG / 'litellm.json', llm)
    save(CONFIG / 'litellm.yaml', yaml.safe_dump(config, allow_unicode=True))
    source = sources['webui']
    allowed = ('ENABLE_WEB_SEARCH', 'SEARXNG_QUERY_URL', 'SEARXNG_LANGUAGE', 'WEB_SEARCH_ENGINE',
               'WEB_SEARCH_RESULT_COUNT', 'OAUTH_PROVIDER_NAME', 'OAUTH_SCOPES', 'OAUTH_USERNAME_CLAIM')
    web = {k: str(source[k]) for k in allowed if source.get(k) is not None}
    web.update({
        'DATA_DIR': str(DATA / 'webui'), 'DATABASE_URL': dsn('openwebui_dev', credentials['webui_db']),
        'EIP_DATABASE_URL': dsn('openwebui_dev', credentials['webui_db']),
        'COMPANY_AGENT_DATABASE_URL': dsn('openwebui_dev', credentials['webui_db']),
        'WEBUI_SECRET_KEY': credentials['webui_secret'], 'WEBUI_URL': ORIGIN,
        'WEBUI_NAME': 'Visionnaire AI DEV', 'CORS_ALLOW_ORIGIN': ORIGIN, 'EIP_WEBUI_ORIGIN': ORIGIN,
        'OPENID_PROVIDER_URL': 'https://changdar-server.mooo.com/keycloak/realms/visionnaire/.well-known/openid-configuration',
        'OAUTH_CLIENT_ID': 'open-webui-dev', 'OAUTH_CLIENT_SECRET': client_secret,
        'OPENID_REDIRECT_URI': ORIGIN + '/oauth/oidc/callback', 'OAUTH_ROLES_CLAIM': 'dev_roles',
        'OAUTH_ALLOWED_ROLES': 'dev-user,dev-admin', 'OAUTH_ADMIN_ROLES': 'dev-admin',
        'ENABLE_OAUTH_ROLE_MANAGEMENT': 'true', 'ENABLE_OAUTH_SIGNUP': 'true',
        'ENABLE_OAUTH_EMAIL_FALLBACK': 'true', 'OAUTH_MERGE_ACCOUNTS_BY_EMAIL': 'false',
        'ENABLE_LOGIN_FORM': 'false', 'ENABLE_SIGNUP': 'false', 'ENABLE_PASSWORD_AUTH': 'false',
        'ENABLE_PASSWORD_CHANGE_FORM': 'false', 'DEFAULT_USER_ROLE': 'pending',
        'ENABLE_PERSISTENT_CONFIG': 'false', 'ENABLE_OAUTH_PERSISTENT_CONFIG': 'false',
        'WEBUI_AUTH_COOKIE_SECURE': 'true', 'WEBUI_SESSION_COOKIE_SECURE': 'true',
        'WEBUI_AUTH_COOKIE_SAME_SITE': 'lax', 'WEBUI_SESSION_COOKIE_SAME_SITE': 'lax',
        'FROM_INIT_PY': 'true', 'CUDA_VISIBLE_DEVICES': '', 'USE_CUDA_DOCKER': 'false',
        'MODEL_MAX_CONCURRENT_REQUESTS': '2', 'OCR_MAX_CONCURRENT_REQUESTS': '1',
        'COMPANY_AGENT_CONFIG': str(CONFIG / 'company.json'),
        'EIP_AUTOMATION_KEY_FILE': str(CONFIG / 'eip-automation.key'),
        'OPENAI_API_BASE_URLS': 'http://127.0.0.1:14002/v1',
        'OFFICIAL_DOCUMENT_LITELLM_BASE_URL': 'http://127.0.0.1:14002/v1',
        'LITELLM_USAGE_BASE_URL': 'http://127.0.0.1:14002', 'LITELLM_DAILY_BUDGET_USD': '1.00',
        'PATH': str(WEBPY.parent) + ':/usr/local/bin:/usr/bin:/bin', 'PYTHONPATH': str(ROOT),
        'RAG_EMBEDDING_MODEL_AUTO_UPDATE': 'false', 'RAG_RERANKING_MODEL_AUTO_UPDATE': 'false',
        'WHISPER_MODEL_AUTO_UPDATE': 'false', 'ENABLE_FORWARD_USER_INFO_HEADERS': 'true',
    })
    company = json.loads((ROOT / 'company_agent/configs/config.production.json').read_text())
    company.update({'state_dir': str(DATA / 'company_agent'), 'max_concurrent_jobs': 1,
                    'max_pending_jobs': 20, 'max_pending_per_user': 5})
    save(CONFIG / 'company.json', company)
    # Existing generated virtual keys survive restarts and redeploys.
    web.update(credentials.get('webui_keys', {}))
    existing_web = CONFIG / 'webui.json'
    if existing_web.exists():
        previous_web = json.loads(existing_web.read_text())
        for key in ('AI_ACCESS_ENABLED', 'AI_ACCESS_BRIDGE_URL', 'AI_ACCESS_BRIDGE_KEY', 'AI_ACCESS_FLUTTER_DIR'):
            if key in previous_web:
                web[key] = previous_web[key]
    save(CONFIG / 'webui.json', web)
    units = Path.home() / '.config/systemd/user'
    def unit(name, description, start, after='', extra='', stop='', requires=True):
        body = f'[Unit]\nDescription={description}\nAfter=network-online.target {after}\n'
        if after and requires:
            body += 'Requires=' + after + '\n'
        elif after:
            body += 'Wants=' + after + '\n'
        body += f'\n[Service]\nEnvironment=DEV_CONFIG_DIR={CONFIG}\nEnvironment=PYTHON_DOTENV_DISABLED=1\nEnvironment=PYTHONNOUSERSITE=1\nExecStart={start}\n'
        if stop:
            body += f'ExecStop={stop}\n'
        body += extra + '\nRestart=on-failure\nRestartSec=5\n\n[Install]\nWantedBy=default.target\n'
        save(units / (name + '.service'), body)
    unit('ai-gateway-dev-db', 'AI DEV dedicated PostgreSQL', shlex.join(dev_compose('up', '-d', '--wait', 'postgres')),
         extra='Type=oneshot\nRemainAfterExit=yes\nTimeoutStartSec=180\n', stop=shlex.join(dev_compose('stop', 'postgres')))
    unit('ai-gateway-dev-litellm', 'AI DEV LiteLLM', f'{LLMPY} {HERE}/run.py litellm', 'ai-gateway-dev-db.service')
    unit('ai-gateway-dev-webui', 'AI DEV Open WebUI', f'{WEBPY} {HERE}/run.py webui',
         'ai-gateway-dev-db.service ai-gateway-dev-litellm.service')
    unit('ai-gateway-dev-relay', 'AI DEV B-only private relay', f'/usr/bin/python3 {HERE}/run.py relay',
         'ai-gateway-dev-webui.service', requires=False)
    run(['systemctl', '--user', 'daemon-reload'])
    run(['systemctl', '--user', 'enable', '--now', 'ai-gateway-dev-db.service'])
    for name, password in (('openwebui_dev', credentials['webui_db']), ('litellm_dev', credentials['litellm_db'])):
        sql = rf"""SELECT 'CREATE ROLE {name} LOGIN PASSWORD ''{password}'' NOSUPERUSER NOCREATEDB NOCREATEROLE'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{name}')\gexec
SELECT 'CREATE DATABASE {name} OWNER {name}' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='{name}')\gexec
REVOKE CONNECT ON DATABASE {name} FROM PUBLIC;
"""
        env = {**os.environ, 'DEV_CONFIG_DIR': str(CONFIG)}
        run(dev_compose('exec', '-T', 'postgres',
             'psql', '-v', 'ON_ERROR_STOP=1', '-U', 'dev_bootstrap', '-d', 'postgres'),
            input=sql, text=True, env=env, stdout=subprocess.DEVNULL)
    initialized = subprocess.check_output(dev_compose('exec', '-T', 'postgres', 'psql', '-At', '-U', 'dev_bootstrap', '-d', 'litellm_dev', '-c',
        "SELECT EXISTS(SELECT FROM information_schema.tables WHERE table_name='_prisma_migrations')"),
        env={**os.environ, 'DEV_CONFIG_DIR': str(CONFIG)}, text=True).strip()
    if initialized != 't':
        run(['systemctl', '--user', 'enable', '--now', 'ai-gateway-dev-litellm.service'])
        wait('http://127.0.0.1:14002/health/liveliness')
        run(['systemctl', '--user', 'stop', 'ai-gateway-dev-litellm.service'])
    # The packaged migration set predates the issuer-scoped unique key. Build
    # that constraint explicitly; PostgreSQL validates it without data deletion.
    run(dev_compose('exec', '-T', 'postgres',
         'psql', '-v', 'ON_ERROR_STOP=1', '-U', 'dev_bootstrap', '-d', 'litellm_dev'),
        input='''ALTER TABLE IF EXISTS "LiteLLM_JWTKeyMapping" ADD COLUMN IF NOT EXISTS jwt_issuer TEXT NOT NULL DEFAULT '';
DO $$ BEGIN
IF to_regclass('"LiteLLM_JWTKeyMapping"') IS NOT NULL THEN
CREATE UNIQUE INDEX IF NOT EXISTS "LiteLLM_JWTKeyMapping_jwt_issuer_jwt_claim_name_jwt_claim_v_key"
ON "LiteLLM_JWTKeyMapping" (jwt_issuer, jwt_claim_name, jwt_claim_value);
END IF;
END $$;
''', text=True, env={**os.environ, 'DEV_CONFIG_DIR': str(CONFIG)}, stdout=subprocess.DEVNULL)
    run([LLMPY, '-m', 'prisma', 'db', 'push', '--skip-generate', '--schema', CONFIG / 'schema.prisma'],
        cwd=CONFIG, env={**os.environ, 'DATABASE_URL': llm['DATABASE_URL'],
            'PATH': str(LLMPY.parent) + ':/usr/bin:/bin', 'PYTHONNOUSERSITE': '1'})
    run(['systemctl', '--user', 'enable', 'ai-gateway-dev-litellm.service'])
    run(['systemctl', '--user', 'restart', 'ai-gateway-dev-litellm.service'])
    wait('http://127.0.0.1:14002/health/liveliness')
    if not credentials.get('webui_keys'):
        # Budget may already exist after an interrupted first setup.
        current = api('/budget/info', credentials['master'], {'budgets': ['openwebui-dev-daily-usd-1']})
        if not current:
            api('/budget/new', credentials['master'], {'budget_id': 'openwebui-dev-daily-usd-1',
                'max_budget': 1, 'budget_duration': '1d', 'max_parallel_requests': 2})
        keys = {}
        for label, variable in (('chat', 'OPENAI_API_KEYS'), ('documents', 'OFFICIAL_DOCUMENT_LITELLM_API_KEY'),
                                 ('usage', 'LITELLM_USAGE_READER_KEY')):
            request = {'key_alias': 'dev-' + label, 'max_parallel_requests': 2,
                       'metadata': {'environment': 'dev'}}
            if label == 'usage':
                api('/user/new', credentials['master'], {'user_id': 'ai-gateway-dev-usage',
                    'user_role': 'proxy_admin_viewer', 'auto_create_key': False})
                request.update({'user_id': 'ai-gateway-dev-usage', 'allowed_routes': ['/spend/logs/v2']})
            else:
                request.update({'key_type': 'llm_api', 'max_budget': 1, 'budget_duration': '1d',
                                'end_user_budget_id': 'openwebui-dev-daily-usd-1'})
            keys[variable] = api('/key/generate', credentials['master'], request)['key']
        credentials['webui_keys'] = keys
        save(private, credentials)
        web.update(keys)
        save(CONFIG / 'webui.json', web)
    run(['systemctl', '--user', 'enable', '--now', 'ai-gateway-dev-webui.service'])
    wait('http://127.0.0.1:18082/health')
    run([WEBPY, HERE / 'seed.py'])
    run(['systemctl', '--user', 'restart', 'ai-gateway-dev-webui.service'])
    wait('http://127.0.0.1:18082/health')
    run(['systemctl', '--user', 'enable', '--now', 'ai-gateway-dev-relay.service'])
    print('A dev services ready. Independent databases, OIDC client, keys and data; B HTTPS deployment is next.')


if __name__ == '__main__':
    main()
