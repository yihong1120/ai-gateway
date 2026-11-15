"""Shared EIP PostgreSQL configuration and transaction lifetime."""
from __future__ import annotations

import os
import re

from examples.shared.runtime.database import connection  # noqa: F401 - shared transaction lifetime


def configuration():
    dsn = os.getenv('EIP_DATABASE_URL') or os.getenv('DATABASE_URL')
    schema = os.getenv('EIP_DB_SCHEMA', 'eip')
    if not dsn or not dsn.startswith('postgresql://'):
        raise ValueError('EIP 必須設定 PostgreSQL DATABASE_URL。')
    if not re.fullmatch(r'eip(?:_[a-z0-9_]+)?', schema):
        raise ValueError('EIP_DB_SCHEMA 名稱無效。')
    return dsn, schema
