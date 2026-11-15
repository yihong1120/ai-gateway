"""Resolve current identity through the WebUI host, never caller claims."""
from __future__ import annotations

from examples.shared.runtime.modules import ModuleReference

_webui_auth = ModuleReference('open_webui.utils.auth')


async def verified_user(owner):
    get_verified_user_by_id = _webui_auth.attribute('get_verified_user_by_id')

    user = await get_verified_user_by_id(owner)
    if not user:
        raise ValueError('請先以已啟用帳號登入。')
    return user.model_dump()
