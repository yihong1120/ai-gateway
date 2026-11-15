"""Shared administrator policy without database or JWT configuration imports."""
from __future__ import annotations

from typing import Protocol


SUPER_ADMIN_NAME = 'ChangDar'


class NamedRoleUser(Protocol):
    """Minimal account attributes needed by the administrator policy."""

    username: str
    role: str


def is_super_admin(user: NamedRoleUser) -> bool:
    """Recognise the configured administrator using the management policy."""
    return (
        user.username.casefold() == SUPER_ADMIN_NAME.casefold()
        and user.role in {'admin', 'super_admin'}
    )
