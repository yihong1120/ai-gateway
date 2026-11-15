"""Offline registry bootstrap, key validation and migration access. Runtime uses Repository."""
from __future__ import annotations

import hashlib
import os
import re
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from examples.company_ai_access.storage.migrations import initialize
from examples.shared.runtime.database import connection


def digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def database_config():
    dsn = os.getenv('AI_ACCESS_DATABASE_URL')
    schema = os.getenv('AI_ACCESS_DB_SCHEMA', 'company_ai_access')
    if not dsn:
        raise ValueError('Set AI_ACCESS_DATABASE_URL; SQLite fallback is not supported.')
    options = conninfo_to_dict(dsn)
    if options.get('service'):
        raise ValueError('Use an explicit local PostgreSQL DSN, not a libpq service.')
    if not options.get('host'):
        dsn = make_conninfo(dsn, host='localhost')
    hosts = conninfo_to_dict(dsn).get('host', '').split(',')
    if any(host not in {'', 'localhost', '127.0.0.1', '::1'} and not host.startswith('/') for host in hosts):
        raise ValueError('AI access PostgreSQL must use loopback or a local Unix socket.')
    if conninfo_to_dict(dsn).get('hostaddr', os.getenv('PGHOSTADDR') or None) not in {
        None,
        '127.0.0.1',
        '::1',
    }:
        raise ValueError('AI access PostgreSQL hostaddr must be loopback.')
    if not re.fullmatch(r'company_ai_access(?:_[a-z0-9_]+)?', schema) or len(schema) > 63:
        raise ValueError('Invalid AI access PostgreSQL schema.')
    return dsn, schema


class Store:
    def __init__(self, directory: Path, *, legacy_import=False):
        if directory.is_symlink():
            raise ValueError('AI access state must not be a symlink.')
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if directory.stat().st_mode & 0o077:
            raise ValueError('AI access state must be mode 0700.')
        key_path = directory / 'access.key'
        legacy = directory / 'access.sqlite3'
        for path in (legacy, key_path):
            if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_mode & 0o077)):
                raise ValueError('AI access state files must be private regular files.')
        self.dsn, self.schema = database_config()
        self.cipher = initialize(self.dsn, self.schema, key_path, legacy_import=legacy_import)

    @contextmanager
    def lease(self):
        # Dedicated session: serialize service instances and offline imports,
        # including instances with different local state directories.
        with psycopg.connect(make_conninfo(self.dsn, connect_timeout=5), autocommit=True) as db:
            key = self.schema + ':runtime'
            if not db.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0))', (key,)).fetchone()[0]:
                raise RuntimeError('AI access registry is already in use; stop the service before migration.')
            try:
                yield
            finally:
                if not db.closed:
                    db.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))', (key,))

    def db(self):
        return connection(self.dsn, self.schema, row_factory=dict_row)
