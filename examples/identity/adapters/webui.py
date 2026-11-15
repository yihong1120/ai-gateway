"""Resolve current identity through the WebUI host, never caller claims."""
from __future__ import annotations


async def verified_user(owner):
    from open_webui.utils.auth import get_verified_user_by_id

    user = await get_verified_user_by_id(owner)
    if not user:
        raise ValueError('請先以已啟用帳號登入。')
    return user.model_dump()
