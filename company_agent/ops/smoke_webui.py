"""Administrator-only read-only deployment smoke check; no model calls."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta

from examples.shared.config import load_service


async def main():
    load_service('webui')
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    os.environ['FROM_INIT_PY'] = 'true'
    from open_webui.models.users import Users
    from open_webui.models.config import Config
    from open_webui.utils.auth import create_token
    from company_agent.api.service import service
    from company_agent.runtime.models import close_model
    import httpx
    users = (await Users.get_users(limit=100))['users']
    from company_agent.ops.deploy_webui import ENTRIES
    banners = await Config.get('ui.banners') or []
    assert set(ENTRIES).issubset({b.get('id') for b in banners})
    checks = []
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8080', timeout=45, trust_env=False) as client:
        for role in ('admin', 'user'):
            user = next((u for u in users if u.role == role), None)
            if not user:
                checks.append({'role': role, 'available': False})
                continue
            # Token remains in memory and is used only against loopback checks.
            headers = {'Authorization': 'Bearer '+create_token({'id': user.id}, timedelta(minutes=2))}
            codes = {}
            for path in (
                '/agents', '/agents/jobs', '/agents/nas', '/agents/app.js', '/agents/app.css',
                '/connections', '/connections/app.js', '/connections/app.css', '/eip', '/api/v1/eip/status', '/api/models',
            ):
                response = await client.get(path, headers=headers)
                codes[path] = response.status_code
                assert response.status_code == 200, (role, path, response.status_code)
                if path == '/agents/nas':
                    assert not {'password', 'account', 'sid', 'ciphertext'} & response.json().keys()
                    assert response.json()['read_only'] is True
                if path == '/api/models':
                    assert any(m['id'] == 'company_work_assistant' for m in response.json()['data']), role
            model = service().model_factory(user.model_dump())
            try:
                assert model.subject == user.oauth['oidc']['sub']
            finally:
                await close_model(model)
            # Invalid preview/confirm requests raise native ValueError before
            # EIP I/O; the host returns 500, matching the route regression tests.
            # These never stage or commit requests.
            csrf = {**headers, 'Origin': os.environ['EIP_WEBUI_ORIGIN'], 'X-Company-Action': '1'}
            for path, payload in [
                ('/agents/calendar/preview', {'artifact_id': 'not-an-artifact'}),
                ('/agents/calendar/confirm', {'token': 'invalid', 'confirm': False}),
            ]:
                response = await client.post(path, headers=csrf, json=payload)
                assert response.status_code == 500, (path, response.status_code)
                codes[path+' invalid'] = response.status_code
            response = await client.post('/agents/calendar/preview', headers=headers, json={'artifact_id': 'none'})
            assert response.status_code == 403, response.status_code
            codes['calendar CSRF'] = response.status_code
            response = await client.post('/agents/jobs/invalid/resume', headers=headers, json={})
            assert response.status_code == 403, response.status_code
            codes['resume CSRF'] = response.status_code
            checks.append({'role': role, 'http': codes, 'model_visible': True, 'billing_identity_valid': True})
    print(json.dumps({'paid_model_requests': 0, 'connection_banners_present': True, 'checks': checks}, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main())
