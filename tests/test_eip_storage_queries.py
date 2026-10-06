"""Exercise transaction lifetime, batched receipts and scoped file queries on PostgreSQL."""
from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from psycopg.types.json import Json
from sqlalchemy import Column
from sqlalchemy import JSON
from sqlalchemy import MetaData
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import create_async_engine

from official_document_workflow.infrastructure.eip.calendar_sync import verified_events
from official_document_workflow.infrastructure.storage import webui_files
from official_document_workflow.infrastructure.storage.automation_store import (
    AutomationStore,
)
from official_document_workflow.infrastructure.storage.state import Journal
from tests.eip_postgres import EIPPostgresTestCase
from tests.test_eip_connector import remote
from tests.test_eip_connector import row


class StorageTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    def test_preview_checks_all_wire_aliases_with_one_query(self):
        journal = Journal()
        rows = [{**row(), 'Subject': 'A+B'}, {**row(), 'Subject': '另一行程'}]
        initial = verified_events(rows, [remote(r) for r in rows], '17', journal)
        legacy = initial[0]['aliases'][-1]
        journal.reserve_event(legacy)
        with patch.object(journal, 'connect', wraps=journal.connect) as connect:
            events = verified_events(rows, [remote(r) for r in rows], '17', journal)
            connect.assert_called_once()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]['key'], legacy)
        self.assertEqual(journal.reserve_event(events[0]['key'], events[0]['aliases']), 'unknown')

    def test_both_stores_commit_rollback_and_close_with_their_row_formats(self):
        journal = Journal()
        with TemporaryDirectory() as directory:
            store = AutomationStore(Path(directory) / 'key')
            for connect, expected_row in ((journal.connect, (7,)), (store.db, {'value': 7})):
                with self.subTest(store=connect.__name__):
                    with connect() as db:
                        self.assertEqual(db.execute('SELECT 7 AS value').fetchone(), expected_row)
                        db.execute("INSERT INTO writes VALUES (%s,'done',0)", (connect.__name__,))
                    self.assertTrue(db.closed)
                    self.assertEqual(journal.status(connect.__name__), 'done')
                    with self.assertRaises(KeyboardInterrupt):
                        with connect() as aborted:
                            aborted.execute("UPDATE writes SET status='unknown'")
                            raise KeyboardInterrupt()
                    self.assertTrue(aborted.closed)
                    self.assertEqual(journal.status(connect.__name__), 'done')

    def test_batch_receipts_preserve_unknown_and_need_one_connection(self):
        journal = Journal()
        journal.reserve_event('done')
        journal.done('done')
        journal.reserve_event('unknown')
        with patch.object(journal, 'connect', wraps=journal.connect) as connect:
            self.assertEqual(
                journal.statuses(['done', 'unknown', 'missing', 'done']),
                {'done': 'done', 'unknown': 'unknown'},
            )
            connect.assert_called_once()
        with patch.object(journal, 'connect') as connect:
            self.assertEqual(journal.statuses([]), {})
            connect.assert_not_called()

    async def test_csv_query_is_owner_scoped_case_insensitive_and_not_truncated(self):
        journal = Journal()
        with journal.connect() as db:
            db.execute('CREATE TABLE file (id TEXT PRIMARY KEY,user_id TEXT,filename TEXT,meta JSON)')
            data = [(str(i), 'alice', 'history.CSV', {'eip_group_source_ids': ['b', 'a']}) for i in range(150)]
            data += [
                ('new', 'alice', 'new.csv', {'eip_group_source_ids': ['a', 'b']}),
                ('other-owner', 'bob', 'private.csv', {'eip_group_source_ids': ['a', 'b']}),
                ('pdf', 'alice', 'source.pdf', {'eip_group_source_ids': ['a', 'b']}),
                ('unrelated', 'alice', 'other.csv', {'eip_group_source_ids': ['c']}),
                ('bad', 'alice', 'bad.csv', {'eip_group_source_ids': [{}]}),
                ('duplicate', 'alice', 'duplicate.csv', {'eip_group_source_ids': ['a', 'a', 'b']}),
            ]
            with db.cursor() as cursor:
                cursor.executemany(
                    'INSERT INTO file VALUES (%s,%s,%s,%s)',
                    [(i, owner, name, Json(meta)) for i, owner, name, meta in data],
                )
        table = Table(
            'file', MetaData(), Column('id', String), Column('user_id', String),
            Column('filename', String), Column('meta', JSON),
        )
        engine = create_async_engine(
            self._eip_test_dsn.replace('postgresql://', 'postgresql+psycopg://', 1),
            connect_args={'options': f'-c search_path={self._eip_test_schema}'},
        )

        @asynccontextmanager
        async def context():
            async with engine.connect() as db:
                yield db

        try:
            with patch.object(webui_files, 'File', table.c), patch.object(webui_files, 'get_async_db_context', context):
                result = await webui_files.prior_csv_ids('alice', 'new', ['a', 'b'])
            self.assertEqual(set(result), {str(i) for i in range(150)})
        finally:
            await engine.dispose()
