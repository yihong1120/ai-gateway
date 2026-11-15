"""Identity database resources are created and closed by the application lifespan."""
from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import Request
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import DeclarativeBase

from examples.shared.runtime.modules import ModuleReference

_identity_config = ModuleReference('examples.identity.config')


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, settings=None):
        self.settings = settings
        self.engine = None
        self.sessions = None

    def start(self):
        if self.engine is not None:
            raise RuntimeError('Identity database is already started.')
        Settings = _identity_config.attribute('Settings')

        options = (self.settings or Settings()).database
        self.engine = create_async_engine(
            options.database_uri,
            pool_size=options.pool_size,
            max_overflow=options.max_overflow,
            pool_timeout=options.pool_timeout_seconds,
            pool_pre_ping=True,
            pool_recycle=options.pool_recycle_seconds,
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    def session(self):
        if self.sessions is None:
            raise RuntimeError('Identity database lifespan has not started.')
        return self.sessions()

    async def close(self):
        engine, self.engine, self.sessions = self.engine, None, None
        if engine is not None:
            await engine.dispose()


async def get_db(request: Request) -> AsyncGenerator[AsyncSession]:
    async with request.app.state.identity.database.session() as session:
        yield session
