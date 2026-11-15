"""Application ports required by the protocol server; no host/global service."""
from __future__ import annotations

from typing import Protocol


class ResourceServices(Protocol):
    async def authorize(self, person) -> None: ...

    async def audit(self, person, name) -> None: ...

    async def execute(self, person, name, value) -> dict: ...

    async def validate_result(self, person, name, value, output=None) -> None: ...
