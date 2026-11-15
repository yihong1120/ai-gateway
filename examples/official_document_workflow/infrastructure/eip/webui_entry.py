"""Install an EIP entry through WebUI's native banner configuration."""
from __future__ import annotations

import time

from open_webui.models.config import Config

ENTRY_ID = 'ai-gateway-eip-workspace'


async def ensure_entry(config=None):
    if config is None:
        config = Config
    banners = await config.get('ui.banners') or []
    # Keep existing notices and later administrator customizations intact.
    if any(b.get('id') == ENTRY_ID for b in banners):
        return
    await config.upsert({
        'ui.banners': [
            *banners, {
                'id': ENTRY_ID,
                'type': 'info',
                'title': 'EIP 公文工作台',
                'content': '**[EIP 公文工作台：處理狀態與下載](/eip)**　公文排程、PDF／Word／CSV；帳號請至[帳號與連線](/connections)管理。',
                'url': '',
                'dismissible': False,
                'timestamp': int(time.time()),
            },
        ],
    })
