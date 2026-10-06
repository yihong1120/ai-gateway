"""Shared EIP PostgreSQL configuration and transaction lifetime."""
from __future__ import annotations

import os
import re
from contextlib import closing
from contextlib import contextmanager

import psycopg


def configuration():
    dsn = os.getenv('EIP_DATABASE_URL') or os.getenv('DATABASE_URL')
    schema = os.getenv('EIP_DB_SCHEMA', 'eip')
    if not dsn or not dsn.startswith('postgresql://'):
        raise ValueError('EIP 必須設定 PostgreSQL DATABASE_URL。')
    if not re.fullmatch(r'eip(?:_[a-z0-9_]+)?', schema):
        raise ValueError('EIP_DB_SCHEMA 名稱無效。')
    return dsn, schema


@contextmanager
def connection(dsn, schema, *, row_factory=None):
    with closing(
        psycopg.connect(
            dsn, row_factory=row_factory, connect_timeout=5,
            options=f'-c search_path={schema} -c lock_timeout=5000 -c statement_timeout=30000',
        ),
    ) as db:
        with db:
            yield db
