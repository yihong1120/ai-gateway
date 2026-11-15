"""Explicit live Tavily check: one public search and one public page extract.

Uses the existing Open WebUI admin's search permission and configured shared key.
No model calls, NAS access, file publishing or EIP operations. Does use Tavily credits.
Run only with the deployed Open WebUI environment. Never logs secrets/content.
"""
from __future__ import annotations

import asyncio
import json
import time

import company_agent.services.research.web as _web_imports
from examples.shared.runtime.modules import ModuleReference

_webui_users = ModuleReference('open_webui.models.users')


async def main():
    Users = _webui_users.attribute('Users')
    user = next(u for u in (await Users.get_users(limit=100))['users'] if u.role == 'admin')
    config = await _web_imports.web_config(user.id)
    started = time.monotonic()
    search = await _web_imports.perform('search_web', {'query': 'site:docs.tavily.com Tavily Search API', 'count': 3}, config)
    assert search['results'], 'Public search returned no usable results'
    page = await _web_imports.perform('fetch_url', {'url': 'https://www.python.org/about/'}, config)
    assert page['results'] and page['results'][0]['content'], 'Public extract returned no usable text'
    print(
        json.dumps(
            {
                'search_results': len(search['results']), 'page_characters': len(page['results'][0]['content']),
                'page_truncated': page['results'][0]['truncated'],
                'reported_credits': [search['credits'], page['credits']],
                'elapsed_seconds': round(time.monotonic()-started, 2), 'model_calls': 0,
            }, ensure_ascii=False,
        ),
    )


if __name__ == '__main__':
    asyncio.run(main())
