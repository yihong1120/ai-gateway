"""Verified identity and injected execution dependencies; never model arguments."""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any

from company_agent.config import Settings
from company_agent.runtime.storage import Store


@dataclass(frozen=True)
class Context:
    owner: str
    job: str
    settings: Settings
    store: Store
    model: Any = None  # Runtime only; never serialized into graph/checkpoints.
    delegation_id: str | None = None
    source_errors: dict[str, str] = field(default_factory=dict)  # This run only; no credentials.


async def verified_user(owner):
    from open_webui.utils.auth import get_verified_user_by_id

    user = await get_verified_user_by_id(owner)
    if not user:
        raise ValueError('請先以已啟用帳號登入。')
    return user.model_dump()
