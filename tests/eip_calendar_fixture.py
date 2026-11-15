"""Read-boundary default for tests focused on other EIP responsibilities."""
from __future__ import annotations


async def new_calendar_matches(lines):
    return [{'status': 'new'} for _ in lines]
