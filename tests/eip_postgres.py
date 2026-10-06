"""Isolated PostgreSQL schema for EIP persistence tests."""
from __future__ import annotations

import os
import unittest
import uuid
from unittest.mock import patch

import psycopg
from psycopg import sql


class EIPPostgresTestCase:
    """Mix into unittest cases that create an EIP Journal or AutomationStore.

    Production state is PostgreSQL-only.  Tests therefore use a disposable
    schema rather than pretending a temporary directory is a database.
    """

    def setUp(self):
        super().setUp()
        dsn = os.getenv('EIP_TEST_DATABASE_URL')
        if not dsn:
            raise unittest.SkipTest('EIP_TEST_DATABASE_URL is required for PostgreSQL EIP tests')
        self._eip_test_dsn = dsn
        self._eip_test_schema = f'eip_test_{uuid.uuid4().hex}'
        self._eip_test_env = patch.dict(
            os.environ, {
                'EIP_DATABASE_URL': dsn,
                'EIP_DB_SCHEMA': self._eip_test_schema,
            },
        )
        self._eip_test_env.start()

    def tearDown(self):
        try:
            with psycopg.connect(self._eip_test_dsn, autocommit=True) as db:
                db.execute(
                    sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(
                        sql.Identifier(self._eip_test_schema),
                    ),
                )
        finally:
            self._eip_test_env.stop()
            super().tearDown()
