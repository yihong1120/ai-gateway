"""Outbox races and pooled transaction isolation against disposable PostgreSQL."""
from __future__ import annotations


from psycopg.rows import dict_row

from company_agent.api.queue import WorkQueue
from company_agent.tests.test_agent import Fixture


class OutboxTests(Fixture):
    def setUp(self):
        super().setUp()
        self.queue = WorkQueue(self.private, self.base / 'uploads')
        self.key = self.queue.submit('alice', 'subject', 'chat', 'turn', '處理文件', [])

    def acknowledge(self):
        row = self.queue.chat_pending()[0]
        self.queue.chat_delivered(row['id'], row['revision'])
        return row['revision']

    def test_old_acknowledgement_cannot_consume_new_progress(self):
        old = self.acknowledge()
        self.queue.update(self.key, progress='新的處理進度')
        self.queue.chat_delivered(self.key, old)
        pending = self.queue.chat_pending()
        self.assertEqual([r['id'] for r in pending], [self.key])
        self.assertGreater(pending[0]['revision'], old)
        self.acknowledge()
        self.queue.chat_delivered(self.key, old)
        self.assertEqual(self.queue.chat_pending(), [])

    def test_unchanged_progress_does_not_requeue_delivery(self):
        self.acknowledge()
        self.queue.update(self.key, progress='等待執行')
        self.assertEqual(self.queue.chat_pending(), [])

    def test_engine_result_update_is_transactionally_visible_in_outbox(self):
        engine = self.store.job('alice')
        self.queue.update(self.key, engine_job=engine)
        self.acknowledge()
        with self.store.db() as db:
            db.execute('UPDATE jobs SET result=%s WHERE id=%s', ('{"answer":"完成"}', engine))
        self.assertEqual([r['id'] for r in self.queue.chat_pending()], [self.key])
        self.assertEqual(self.queue.public('alice', self.key)['answer'], '完成')

    def test_rolled_back_result_does_not_publish_a_change(self):
        engine = self.store.job('alice')
        self.queue.update(self.key, engine_job=engine)
        self.acknowledge()
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with self.store.db() as db:
                db.execute('UPDATE jobs SET result=%s WHERE id=%s', ('{"answer":"未提交"}', engine))
                raise RuntimeError('rollback')
        self.assertEqual(self.queue.chat_pending(), [])

    def test_queue_reinitialization_preserves_acknowledged_versions(self):
        self.acknowledge()
        restored = WorkQueue(self.private, self.base / 'uploads')
        self.assertEqual(restored.chat_pending(), [])
        self.assertEqual(restored.get('alice', self.key)['status'], 'queued')

    def test_newer_schema_is_refused_without_rewriting_job_data(self):
        from company_agent.runtime.storage import Store

        with self.store.db() as db:
            db.execute('INSERT INTO schema_migrations(version) VALUES(999)')
        with self.assertRaisesRegex(ValueError, 'newer'):
            Store(self.private)
        self.assertEqual(self.queue.get('alice', self.key)['status'], 'queued')

    def test_pool_restores_row_factory_and_rolls_back_failed_transactions(self):
        with self.assertRaises(RuntimeError):
            with self.store.db() as db:
                db.row_factory = dict_row
                db.execute('UPDATE web_jobs SET progress=%s WHERE id=%s', ('未提交', self.key))
                raise RuntimeError()
        with self.store.db() as db:
            self.assertIsInstance(db.execute('SELECT 1').fetchone(), tuple)
        self.assertEqual(self.queue.get('alice', self.key)['progress'], '等待執行')

    def test_session_lease_survives_unrelated_pooled_transactions(self):
        with self.store.mutex('test-worker'):
            for _ in range(12):
                with self.store.db() as db:
                    self.assertEqual(db.execute('SELECT 1').fetchone(), (1,))
            with self.assertRaisesRegex(ValueError, 'worker'):
                with self.store.mutex('test-worker'):
                    self.fail('a second worker acquired the same lease')
        with self.store.mutex('test-worker'):
            pass
