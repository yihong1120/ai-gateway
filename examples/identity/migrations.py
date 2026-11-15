"""Explicit identity upgrades; no application import or HTTP request runs DDL."""
from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import text

from examples.identity.config import Settings
from examples.identity.database import Database
from examples.identity.models import AccountActionToken

VERSION = 1


async def upgrade(engine):
    async with engine.begin() as db:
        await db.execute(
            text('SELECT pg_advisory_xact_lock(hashtext(current_schema() || :suffix))'),
            {'suffix': ':identity_migrations'},
        )
        await db.execute(
            text(
                'CREATE TABLE IF NOT EXISTS identity_schema_migrations '
                '(version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)',
            ),
        )
        version = await db.scalar(
            text('SELECT COALESCE(MAX(version),0) FROM identity_schema_migrations'),
        )
        if version > VERSION:
            raise ValueError('Identity schema is newer than this service.')
        if version < 1:
            width = await db.scalar(
                text(
                    'SELECT character_maximum_length FROM information_schema.columns '
                    "WHERE table_schema=current_schema() AND table_name='users' AND column_name='status'",
                ),
            )
            if width is not None and width < 32:
                await db.execute(
                    text('ALTER TABLE users ALTER COLUMN status TYPE VARCHAR(32)'),
                )
            await db.run_sync(
                lambda conn: AccountActionToken.__table__.create(conn, checkfirst=True),
            )
            await db.execute(
                text('INSERT INTO identity_schema_migrations(version) VALUES(1)'),
            )


async def require_schema(engine):
    async with engine.connect() as db:
        exists = await db.scalar(
            text(
                "SELECT to_regclass(current_schema() || '.identity_schema_migrations')",
            ),
        )
        version = (
            await db.scalar(text('SELECT max(version) FROM identity_schema_migrations'))
            if exists
            else None
        )
        if version != VERSION:
            raise RuntimeError(
                'Run python -m examples.identity.migrations before starting the identity service.',
            )


async def main():

    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    database = Database(Settings())
    database.start()
    try:
        await upgrade(database.engine)
    finally:
        await database.close()


if __name__ == '__main__':
    asyncio.run(main())
