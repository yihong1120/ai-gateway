"""Crash recovery and deduplication across filesystem and PostgreSQL boundaries."""
from __future__ import annotations

import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import patch

import company_agent.api.service as _service_imports
from company_agent.api.queue import WorkQueue
from company_agent.api.service import Service
from company_agent.api.source_snapshots import SourceSnapshots
from company_agent.config import Settings
from company_agent.runtime.storage import Store
from company_agent.tests.test_agent import Fixture


class SubmissionTests(Fixture):
    def setUp(self):
        super().setUp()
        self.queue = WorkQueue(self.private, self.base / 'uploads')

    def submit(self, request='turn'):
        return self.queue.submit('alice', 'subject', 'chat', request, '處理來源', [('source.txt', b'owned source')])

    def age(self, directory):
        old = time.time() - 3 * 86400
        os.utime(directory, (old, old))
        marker = self.queue.snapshots.marker(directory.name.removeprefix('.preparing_'))
        if marker.exists():
            os.utime(marker, (old, old))

    def test_every_filesystem_sync_precedes_the_database_mutation_lock(self):
        sync = os.fsync
        observed = []

        def probe(descriptor):
            with self.store.db() as db:
                available = db.execute(
                    'SELECT pg_try_advisory_xact_lock(%s)',
                    (self.store.lock_id('mutations'),),
                ).fetchone()[0]
            observed.append(available)
            return sync(descriptor)
        with patch('company_agent.api.source_snapshots.os.fsync', side_effect=probe):
            key = self.submit()
        self.assertTrue(observed)
        self.assertTrue(all(observed))
        self.assertEqual((self.queue.sources / key / 'input_1.txt').read_bytes(), b'owned source')
        self.assertFalse((self.queue.sources / key / '.snapshot').exists())

    def test_concurrent_duplicates_publish_one_job_and_one_source_directory(self):
        with ThreadPoolExecutor(max_workers=4) as workers:
            ids = list(workers.map(lambda _: self.submit(), range(4)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual([path.name for path in self.queue.sources.iterdir() if path.name != '.pending'], [ids[0]])
        self.assertEqual(len(self.queue.list('alice')), 1)

    def test_failed_file_sync_leaves_no_partial_sources_or_registered_job(self):
        with patch('company_agent.api.source_snapshots.os.fsync', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):
                self.submit()
        self.assertEqual([p for p in self.queue.sources.iterdir() if p.name != '.pending'], [])
        self.assertEqual(self.queue.list('alice'), [])

    def test_recovery_preserves_active_producer_then_removes_unregistered_ready_files(self):
        with self.queue.snapshots.prepare([('source.txt', b'prepared')]) as (key, _):
            directory = self.queue.sources / key
            self.age(directory)
            self.assertEqual(self.queue.snapshots.sweep()['removed'], 0)
            self.assertTrue(directory.exists())
        self.assertEqual(self.queue.snapshots.sweep()['removed'], 1)
        self.assertFalse(directory.exists())

    def test_commit_timeout_preserves_sources_and_recovery_checks_committed_database_state(self):
        original = self.queue.store.db

        @contextmanager
        def uncertain(*args, **kwargs):
            with original(*args, **kwargs) as db:
                yield db
                committed = db.execute('SELECT 1 FROM web_jobs LIMIT 1').fetchone()
            if committed:
                raise OSError('commit response lost')
        with patch.object(self.queue.store, 'db', uncertain):
            with self.assertRaises(OSError):
                self.submit()
        key = self.queue.list('alice')[0]['id']
        directory = self.queue.sources / key
        self.age(directory)
        self.assertTrue(self.queue.snapshots.marker(key).exists())
        self.assertEqual(self.queue.snapshots.sweep()['removed'], 0)
        self.assertEqual((directory / 'input_1.txt').read_bytes(), b'owned source')
        self.assertFalse(self.queue.snapshots.marker(key).exists())
        self.assertEqual(self.submit(), key)

    def test_rollback_orphans_are_reclaimed_but_legacy_and_recent_directories_are_protected(self):
        original = self.queue.store.db

        @contextmanager
        def rollback(*args, **kwargs):
            with original(*args, **kwargs) as db:
                yield db
                if db.execute('SELECT 1 FROM web_jobs LIMIT 1').fetchone():
                    raise ValueError('rollback after insert')
        with patch.object(self.queue.store, 'db', rollback):
            with self.assertRaises(ValueError):
                self.submit()
        orphan = next(p for p in self.queue.sources.iterdir() if p.name != '.pending')
        self.assertEqual(self.queue.snapshots.sweep()['removed'], 0)
        self.age(orphan)
        legacy = self.queue.sources / '00000000-0000-0000-0000-000000000000'
        legacy.mkdir()
        self.age(legacy)
        self.assertEqual(self.queue.snapshots.sweep()['removed'], 1)
        self.assertTrue(legacy.exists())

    def test_same_message_returns_existing_result_even_when_disk_preparation_is_unavailable(self):
        key = self.submit()
        self.queue.update(key, status='done')
        with patch.object(self.queue.snapshots, 'prepare', side_effect=OSError('disk full')):
            self.assertEqual(self.submit(), key)

    def test_capacity_rejection_removes_prepared_files_immediately(self):
        with patch.object(self.queue, 'check_capacity', side_effect=ValueError('full')):
            with self.assertRaises(ValueError):
                self.submit()
        self.assertEqual([p for p in self.queue.sources.iterdir() if p.name != '.pending'], [])

    def test_recovery_skips_an_inflight_registration_transaction(self):
        with self.queue.snapshots.prepare([('source.txt', b'owned')]) as (key, _):
            directory = self.queue.sources / key
            self.age(directory)
        with self.store.db() as db:
            self.store.lock(db)
            self.assertEqual(self.queue.snapshots.sweep()['removed'], 0)
        self.assertTrue(directory.exists())
        self.assertEqual(self.queue.snapshots.sweep()['removed'], 1)

    def test_another_database_namespace_cannot_reclaim_the_same_directory(self):
        with self.queue.snapshots.prepare([('source.txt', b'owned')]) as (key, _):
            directory = self.queue.sources / key
            self.age(directory)
        other = SourceSnapshots(
            self.queue.sources, SimpleNamespace(
                dsn=self.store.dsn, schema='company_agent_other', db=lambda: self.fail('must not query a different database'),
            ),
        )
        self.assertEqual(other.sweep()['removed'], 0)
        self.assertTrue(directory.exists())
        self.assertEqual(self.queue.snapshots.sweep()['removed'], 1)

    def test_version_three_upgrade_preserves_jobs_sources_and_chat_receipts(self):
        key = self.submit()
        with self.store.db() as db:
            db.execute('DROP TRIGGER web_job_chat_changed ON web_jobs')
            db.execute('ALTER TABLE web_jobs DROP COLUMN error_code')
            db.execute(
                'CREATE TRIGGER web_job_chat_changed BEFORE UPDATE OF status ON web_jobs FOR EACH ROW EXECUTE FUNCTION bump_web_job_chat_version()',
            )
            db.execute('DELETE FROM schema_migrations WHERE version>=4')
            db.execute('DROP TABLE mcp_grants,mcp_workspaces,mcp_operations,mcp_request_limits')
            db.execute('DROP INDEX nas_bindings_owner_revision')
            db.execute('ALTER TABLE nas_bindings DROP CONSTRAINT nas_bindings_pkey')
            db.execute('ALTER TABLE nas_bindings DROP COLUMN binding_id')
            db.execute('ALTER TABLE nas_bindings ADD PRIMARY KEY(owner)')
        restored = Store(self.private)
        with restored.db() as db:
            self.assertEqual(db.execute('SELECT max(version) FROM schema_migrations').fetchone()[0], 6)
        self.assertEqual(self.queue.get('alice', key)['status'], 'queued')
        self.assertEqual((self.queue.sources / key / 'input_1.txt').read_bytes(), b'owned source')


class DurableFailureTests(Fixture):
    def test_safe_failure_survives_service_recreation_and_authorizes_before_query(self):
        queue = WorkQueue(self.private, self.base / 'uploads')
        key = queue.submit('alice', 'subject', 'chat', 'turn', '工作', [])
        queue.update(key, status='error', error_code='execution_failed', progress='執行未完成')
        first, restored = Service(queue, None, None, None), Service(queue, None, None, None)
        self.assertEqual(first.result('alice', key), restored.result('alice', key))
        self.assertEqual(restored.result('alice', key)['error'], {'code': 'execution_failed', 'message': '執行未完成'})
        with self.assertRaises(ValueError):
            restored.result('bob', key)
        self.assertFalse(hasattr(restored, 'failures'))

    def test_error_code_change_requeues_delivery_without_replaying_work(self):
        queue = WorkQueue(self.private, self.base / 'uploads')
        key = queue.submit('alice', 'subject', 'chat', 'turn', '工作', [])
        revision = queue.chat_pending()[0]['revision']
        queue.chat_delivered(key, revision)
        queue.update(key, error_code='execution_failed')
        pending = queue.chat_pending()
        self.assertEqual([row['id'] for row in pending], [key])
        self.assertGreater(pending[0]['revision'], revision)


class DeploymentSnapshotTests(Fixture):
    def test_application_captures_database_gateway_and_model_settings_without_environment_mutation(self):
        data = self.base / 'app'
        data.mkdir()
        host = ModuleType('open_webui.env')
        host.DATA_DIR = data
        config = self.base / 'application.json'
        config.write_text(Settings(state_dir=str(self.private), model_name='first').model_dump_json())
        dsn = os.environ['COMPANY_AGENT_DATABASE_URL']
        with patch.dict(
            os.environ, {
                'DATABASE_URL': dsn, 'COMPANY_AGENT_CONFIG': str(config),
                'OPENAI_API_BASE_URLS': 'http://127.0.0.1:14002/v1', 'OPENAI_API_KEYS': 'synthetic-first',
            },
        ), \
                patch.dict(sys.modules, {'open_webui.env': host}):
            os.environ.pop('COMPANY_AGENT_DATABASE_URL')
            current = _service_imports.create_service()
            self.assertNotIn('COMPANY_AGENT_DATABASE_URL', os.environ)
            self.assertEqual(current.queue.store.dsn, dsn)
            config.write_text(Settings(state_dir=str(self.private), model_name='second').model_dump_json())
            os.environ.update(OPENAI_API_KEYS='synthetic-second', COMPANY_AGENT_MODEL='second')
            selected = current.settings_factory({'id': 'job', 'owner': 'alice', 'payload': {}})
            self.assertEqual(selected.model_name, 'first')
            with patch(
                'company_agent.api.service.gateway_for_verified_user',
                return_value=SimpleNamespace(owner='alice', subject='sub', client=None),
            ) as model:
                current.model_factory({'id': 'alice'}, selected.model_name)
            self.assertEqual(model.call_args.args[1:], ('http://127.0.0.1:14002/v1', 'synthetic-first', 'first'))


class BoundedRecoveryTests(Fixture):
    def test_invalid_candidates_and_large_history_do_not_starve_later_pending_files(self):
        queue = WorkQueue(self.private, self.base / 'bounded-uploads')
        self.addCleanup(queue.snapshots.close)
        history = []
        for _ in range(200):
            directory = queue.sources / str(uuid.uuid4())
            directory.mkdir()
            history.append(directory)
        for _ in range(40):
            marker = queue.snapshots.pending / (str(uuid.uuid4()) + '.json')
            marker.write_text('corrupt marker')
            os.utime(marker, (0, 0))
        with queue.snapshots.prepare([('source.txt', b'pending')]) as (key, _):
            pass
        os.utime(queue.snapshots.marker(key), (0, 0))
        observations = []
        # Directory enumeration order is filesystem-dependent. Supply the
        # adversarial order with the valid candidate behind every bad marker.
        original_scan = os.scandir
        with original_scan(queue.snapshots.pending) as scan:
            entries = sorted(scan, key=lambda entry: entry.name == key + '.json')

        class OrderedScan:
            def __init__(self):
                self.entries = iter(entries)

            def __next__(self):
                return next(self.entries)

            def close(self):
                pass

        def scandir(path):
            return OrderedScan() if path == queue.snapshots.pending else original_scan(path)
        with patch('company_agent.api.source_snapshots.os.scandir', side_effect=scandir):
            for _ in range(50):
                result = queue.snapshots.sweep(limit=6, max_seconds=1)
                observations.append(result)
                self.assertLessEqual(result['visited'], 6)
                if result['removed']:
                    break
        self.assertTrue(any(result['removed'] == 1 for result in observations))
        self.assertGreater(len(observations), 1)
        self.assertFalse((queue.sources / key).exists())
        self.assertTrue(all(path.exists() for path in history))
