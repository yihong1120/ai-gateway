"""Bind disposable evaluation schemas to the unittest cleanup lifecycle."""
from __future__ import annotations

import os
import unittest

from company_agent.evals.sandbox import checkpointer
from company_agent.evals.sandbox import isolated_schemas
from company_agent.evals.sandbox import make_state_directory

__all__ = ['PostgreSQLTestCase', 'checkpointer', 'make_state_directory']


class PostgreSQLTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        dsn = os.getenv('COMPANY_AGENT_TEST_DATABASE_URL')
        if not dsn:
            raise unittest.SkipTest('Set COMPANY_AGENT_TEST_DATABASE_URL for real PostgreSQL tests')
        self.enterContext(isolated_schemas(dsn))
