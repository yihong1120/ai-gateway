from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from examples.official_document_workflow.infrastructure.eip.task_times import document_recorded_at
from examples.official_document_workflow.infrastructure.storage.automation_store import AutomationStore
from tests.eip_postgres import EIPPostgresTestCase


class TaskTimesTests(EIPPostgresTestCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = AutomationStore(self.root/'key')
        self.doc = {'id': '1', 'folder': 'inbox', 'date': '2026-09-03 16:26:59.497'}

    def history(self):
        return self.store.task_page('alice')[0][0]

    def test_first_detection_survives_poll_save_and_explicit_task_replacement(self):
        with patch('examples.official_document_workflow.infrastructure.storage.automation_store.time.time', return_value=100):
            self.store.discover('alice', '17', [self.doc])
        task = self.history()
        self.assertEqual(task['first_detected_at'], 100)
        self.assertIsNone(task['last_processed_at'])
        with patch('examples.official_document_workflow.infrastructure.storage.automation_store.time.time', return_value=200):
            self.store.discover('alice', '17', [self.doc])
        self.assertEqual(self.history()['first_detected_at'], 100)
        self.assertIsNone(self.history()['last_processed_at'])
        with patch('examples.official_document_workflow.infrastructure.storage.automation_store.time.time', return_value=300):
            self.store.save_task(task, 'done', task['payload'])
        self.assertEqual(self.history()['last_processed_at'], 300)
        with self.store.db() as db:
            db.execute('DELETE FROM tasks WHERE id=%s', (task['id'],))
        with patch('examples.official_document_workflow.infrastructure.storage.automation_store.time.time', return_value=400):
            self.store.discover('alice', '17', [self.doc])
        self.assertEqual(self.history()['first_detected_at'], 100)
        self.assertEqual(self.history()['last_processed_at'], 300)

    def test_legacy_history_does_not_fabricate_first_detection_or_processing(self):
        self.store.discover('alice', '17', [self.doc])
        with self.store.db() as db:
            # An installation predating timeline tracking is also unversioned.
            db.execute('DROP TABLE schema_migrations')
            db.execute('DROP TABLE task_timeline')
            db.execute('UPDATE tasks SET updated=123')
        self.store = AutomationStore(self.root/'key')
        self.store.discover('alice', '17', [self.doc])
        self.assertIsNone(self.history()['first_detected_at'])
        self.assertIsNone(self.history()['last_processed_at'])

    def test_wrong_owner_cannot_update_timeline(self):
        self.store.discover('alice', '17', [self.doc])
        task = self.history()
        self.store.save_task({**task, 'owner': 'bob'}, 'done', {})
        self.assertIsNone(self.history()['last_processed_at'])
        self.assertEqual(self.store.task_page('bob')[0], [])

    def test_recorded_time_normalizes_taipei_without_inventing_receipt(self):
        self.assertEqual(document_recorded_at(self.doc), '2026-09-03T16:26:59+08:00')
        self.assertEqual(document_recorded_at({'date': '2026-09-03T08:26:59Z'}), '2026-09-03T16:26:59+08:00')
        for value in (None, '', '2026-09-03', '<script>'):
            self.assertIsNone(document_recorded_at({'date': value}))
        for value in (True, 123):
            with self.assertRaises(TypeError):
                document_recorded_at({'date': value})

        with self.assertRaises(ValueError):
            document_recorded_at({'date': '2026-02-30 12:00:00'})
