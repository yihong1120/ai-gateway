"""Password hashing policy, shared by account services and ORM compatibility methods."""
from __future__ import annotations

import asyncio

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError

password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return password_hash.hash(password)


async def hash_password_async(password: str) -> str:
    return await asyncio.to_thread(hash_password, password)


async def verify_password(password: str, encoded: str) -> bool:
    try:
        return await asyncio.to_thread(password_hash.verify, password, encoded)
    except UnknownHashError:
        return False
