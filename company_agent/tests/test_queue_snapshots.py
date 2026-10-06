"""Real owned database snapshots, bounded query cost and shared Store lifetime."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from company_agent.api.queue import WorkQueue
from company_agent.runtime.execution import DocumentAgent
from company_agent.tests.test_agent import Fixture


class QueueSnapshotTests(Fixture):
    def setUp(self):
        super().setUp()
        self.queue = WorkQueue(self.private, self.base / 'uploads')

    def complete(self, owner, instruction):
        key = self.queue.submit(owner, 'subject-' + owner, 'chat', instruction, instruction, [])
        job = self.store.job(owner)
        artifact = self.store.artifact(owner, job, instruction + '.txt', b'private')
        self.store.finish(owner, job, 'done', {'answer': instruction, 'resumable': False, 'budget': {'used': 99}})
        self.store.step_budget(owner, job, 40)
        self.store.reserve_model_attempt(owner, job, 40)
        self.queue.update(key, status='done', engine_job=job)
        return key, job, artifact

    def test_list_and_detail_share_current_metadata_with_one_connection(self):
        keys = [self.complete('alice', 'file-' + str(index)) for index in range(20)]
        foreign, _, _ = self.complete('bob', 'PRIVATE foreign')
        with self.store.db() as db:
            db.execute(
                'INSERT INTO eip_calendar_sources VALUES (%s,%s,%s,%s)',
                ('alice', keys[0][1], keys[0][2]['id'], 'source'),
            )
        real_db, connections = self.queue.store.db, []

        @contextmanager
        def count(**kwargs):
            connections.append(1)
            with real_db(**kwargs) as db:
                yield db
        with patch.object(self.queue.store, 'db', count):
            listing = self.queue.list('alice')
            self.assertEqual(len(connections), 1)
            detail = self.queue.public('alice', keys[0][0])
            self.assertEqual(len(connections), 2)
        self.assertEqual(len(listing), 20)
        self.assertEqual(detail, next(row for row in listing if row['id'] == detail['id']))
        self.assertEqual(detail['budget']['used'], 1)
        self.assertEqual(detail['files'][0]['id'], keys[0][2]['id'])
        self.assertTrue(detail['files'][0]['calendar_preview'])
        self.assertNotIn('PRIVATE', str(listing))
        self.assertNotIn('payload', detail)
        self.assertNotIn('engine_job', detail)
        with self.assertRaises(ValueError):
            self.queue.public('alice', foreign)

    def test_foreign_engine_or_file_never_leaks_into_owned_job(self):
        key, job, _ = self.complete('alice', 'own')
        _, foreign_job, foreign_artifact = self.complete('bob', 'PRIVATE foreign')
        with self.store.db() as db:
            db.execute('UPDATE artifacts SET job=%s WHERE id=%s', (job, foreign_artifact['id']))
        detail = self.queue.public('alice', key)
        self.assertEqual(len(detail['files']), 1)
        self.assertNotIn('PRIVATE', str(detail))
        self.queue.update(key, engine_job=foreign_job)
        with self.assertRaises(ValueError):
            self.queue.public('alice', key)
        with self.assertRaises(ValueError):
            self.queue.list('alice')

    def test_counting_authorizes_without_loading_results_or_artifacts(self):
        # Ownership and limits remain valid even before a result is serialized.
        with self.store.db() as db:
            db.execute("UPDATE jobs SET result='not a completed JSON result' WHERE id=%s", (self.job,))
        self.assertEqual(self.store.reserve_model_attempt('alice', self.job, 3), 1)
        self.assertEqual(self.store.step_budget('alice', self.job, 3), 3)
        for action in (self.store.reserve_model_attempt, self.store.step_budget):
            with self.assertRaises(ValueError):
                action('bob', self.job, 80)
        self.assertEqual(self.store.attempts(self.job), 1)
        self.assertEqual(self.store.step_budget('alice', self.job, 80), 3)

    def test_agent_reuses_store_and_rejects_a_different_state_directory(self):
        with patch('company_agent.runtime.execution.Store') as constructor:
            agent = DocumentAgent(self.config, None, store=self.store)
        self.assertIs(agent.store, self.store)
        constructor.assert_not_called()
        other = self.config.model_copy(update={'state_dir': str(Path(self.private).with_name('other'))})
        with self.assertRaisesRegex(ValueError, '儲存目錄不符'):
            DocumentAgent(other, None, store=self.store)
