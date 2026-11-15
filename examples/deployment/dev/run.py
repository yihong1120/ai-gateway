"""Start one dev service with explicit private configuration."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import uvicorn

from examples.shared.runtime.modules import ModuleReference

_litellm = ModuleReference('litellm')

ROOT = Path(__file__).resolve().parents[3]
CONFIG = Path.home() / '.config/ai-gateway-dev'


async def relay():
    async def handle(reader, writer):
        if writer.get_extra_info('peername')[0] != '100.93.178.78':
            writer.close()
            await writer.wait_closed()
            return
        remote = None
        try:
            upstream, remote = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', 18082), 10)

            async def copy(source, destination):
                while data := await source.read(65536):
                    destination.write(data)
                    await destination.drain()
                if destination.can_write_eof():
                    destination.write_eof()

            await asyncio.wait_for(asyncio.gather(copy(reader, remote), copy(upstream, writer)), 3600)
        except (OSError, TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()
            if remote:
                remote.close()

    async with await asyncio.start_server(handle, '100.96.17.117', 18082) as server:
        await server.serve_forever()


def load_environment(service):
    values = json.loads((CONFIG / f'{service}.json').read_text())

    def dev_database(url, name):
        parsed = urlparse(url)
        return parsed.hostname == '127.0.0.1' and parsed.port == 15432 and parsed.path == '/' + name and parsed.username == name
    if service == 'webui':
        expected = str(Path.home() / '.local/share/ai-gateway-dev/webui')
        if values.get('DATA_DIR') != expected or not dev_database(values.get('DATABASE_URL', ''), 'openwebui_dev'):
            raise RuntimeError('Refusing a non-dev WebUI database/data directory.')
        if any(values.get(key) != values['DATABASE_URL'] for key in ('EIP_DATABASE_URL', 'COMPANY_AGENT_DATABASE_URL')):
            raise RuntimeError('Refusing shared EIP/company state.')
    elif service == 'litellm':
        database = values.get('LITELLM_DATABASE_URL', '')
        if (
            not dev_database(database, 'litellm_dev')
            or values.get('DATABASE_URL') != database
        ):
            raise RuntimeError('Refusing a non-dev LiteLLM database or mismatched Prisma database.')
    elif service == 'ai-access':
        if (
            not dev_database(values.get('AI_ACCESS_DATABASE_URL', ''), 'openwebui_dev')
            or values.get('AI_ACCESS_DB_SCHEMA') != 'company_ai_access_dev'
            or values.get('AI_ACCESS_ENVIRONMENT') != 'dev'
            or values.get('AI_ACCESS_LITELLM_URL') != 'http://127.0.0.1:14002'
            or values.get('AI_ACCESS_STATE_DIR') != str(Path.home() / '.local/share/ai-gateway-dev/company-ai-access')
        ):
            raise RuntimeError('Refusing non-dev AI access state or LiteLLM.')
    os.environ.update(values)
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    os.chdir(CONFIG)
    sys.path.insert(0, str(ROOT))
    return values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('service', choices=['webui', 'litellm', 'relay', 'ai-access'])
    service = parser.parse_args().service
    if service == 'relay':
        asyncio.run(relay())
        return
    load_environment(service)
    if service == 'litellm':
        run_server = _litellm.attribute('run_server')
        sys.argv = [
            'litellm', '--config', str(CONFIG / 'litellm.yaml'),
            '--host', '127.0.0.1', '--port', '14002',
        ]
        run_server()
    elif service == 'ai-access':
        uvicorn.run(
            'examples.company_ai_access.app:create_app', factory=True,
            host='127.0.0.1', port=18083, workers=1, proxy_headers=False,
        )
    else:
        uvicorn.run(
            'examples.deployment.dev.app:create_app', factory=True,
            host='127.0.0.1', port=18082, workers=1, proxy_headers=True,
            forwarded_allow_ips='127.0.0.1', ws_per_message_deflate=False,
        )


if __name__ == '__main__':
    main()
