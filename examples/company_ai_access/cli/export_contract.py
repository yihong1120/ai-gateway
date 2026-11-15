"""Generate a stable, credential-free OpenAPI contract for the Flutter team."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi import Request

from examples.company_ai_access.routers.control import create_router


def contract():
    async def authenticated_company_user(request: Request):
        raise RuntimeError('Schema generation only; not an authentication implementation.')

    app = FastAPI(
        title='Company AI Access Control API',
        version='1.0.0',
        description=(
            'ai-dev: same-origin Open WebUI login cookie. Company Flutter site: existing BFF session and CSRF. '
            'Mutations require an explicitly configured Origin and X-Company-AI-Action: 1. '
            'device/start,poll,ack are native Windows client endpoints; Flutter must never call poll. '
            'All integer timestamps are UTC Unix seconds.'
        ),
    )
    app.include_router(create_router(authenticated_company_user))
    result = app.openapi()
    result['servers'] = [
        {
            'url': 'https://ai-dev.changdar-server.mooo.com',
            'description': 'Same-origin dev browser session',
        },
        {
            'url': 'https://dev.changdar-server.mooo.com/bff/ai_access',
            'description': 'Company dev BFF session and CSRF',
        },
        {
            'url': 'https://changdar-server.mooo.com/bff/ai_access',
            'description': 'Production BFF after deployment',
        },
    ]
    return result


if __name__ == '__main__':
    path = Path(__file__).resolve().parents[3] / 'docs/company-ai-access-openapi.json'
    path.write_text(json.dumps(contract(), ensure_ascii=False, indent=2) + '\n')
    print(path)
