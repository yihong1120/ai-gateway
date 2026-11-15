"""Disposable PostgreSQL schemas for access-registry tests."""
import os
import unittest
import uuid
from unittest.mock import patch

import psycopg
from psycopg import sql


def access_database(case):
    dsn = os.getenv('AI_ACCESS_TEST_DATABASE_URL') or os.getenv('COMPANY_AGENT_TEST_DATABASE_URL')
    if not dsn:
        raise unittest.SkipTest('Set AI_ACCESS_TEST_DATABASE_URL for real PostgreSQL access tests')
    schema = 'company_ai_access_test_' + uuid.uuid4().hex
    environment = patch.dict(os.environ, {'AI_ACCESS_DATABASE_URL': dsn, 'AI_ACCESS_DB_SCHEMA': schema})
    environment.start()
    case.addCleanup(environment.stop)

    def cleanup():
        with psycopg.connect(dsn) as db:
            db.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))
    case.addCleanup(cleanup)
