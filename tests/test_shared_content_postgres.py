"""Real PostgreSQL singleflight, encrypted pure results and cancellation boundaries."""
from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import time
import unittest
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from threading import Lock
from unittest.mock import Mock
from unittest.mock import patch

import psycopg

from examples.official_document_workflow.domain.documents import CloudRateLimited
from examples.official_document_workflow.domain.documents import CloudTemporaryFailure
from examples.official_document_workflow.domain.documents import ProcessingError
from examples.official_document_workflow.infrastructure.storage.automation_store import (
    AutomationStore,
)
from examples.official_document_workflow.infrastructure.storage.shared_content import (
    SharedContentCache,
)
from examples.official_document_workflow.runtime import WorkflowResources
from examples.official_document_workflow.runtime import WorkflowSettings
from examples.shared.runtime import retention
from examples.shared.runtime.concurrency import ModelNotStarted
from examples.shared.runtime.retention import Retention
from tests.eip_postgres import EIPPostgresTestCase


def process_compute(dsn, schema, key_path):
    store = AutomationStore(Path(key_path), dsn=dsn, schema=schema)
    cache = SharedContentCache(store, 'trusted-eip', 'process-v1')

    def produce():
        with store.db() as db:
            db.execute('INSERT INTO shared_producer_calls DEFAULT VALUES')
        time.sleep(0.15)
        return b'one shared result'
    return cache.get_or_compute(cache.key('ocr', 'identical'), produce)


def paid_process_exit(dsn, schema, key_path):
    store = AutomationStore(Path(key_path), dsn=dsn, schema=schema)
    cache = SharedContentCache(store, 'trusted-eip', 'paid-process-v1')
    cache.get_or_compute(cache.key('paid-evidence'), lambda: os._exit(17), replay_safe=False)


class SharedContentTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.store = self._workflow.store
        self.cache = SharedContentCache(self.store, 'trusted-eip', 'test-v1')

    def expire(self, key):
        with self.store.db() as db:
            db.execute("UPDATE shared_content_cache SET expires_at=CURRENT_TIMESTAMP-INTERVAL '1 second' WHERE key=%s", (key,))

    async def test_threads_on_the_same_content_compute_once_and_different_keys_progress(self):
        calls, guard, entered, release = [], Lock(), Event(), Event()
        key = self.cache.key('ocr', 'same-original-bytes')

        def compute():
            with guard:
                calls.append('producer')
            entered.set()
            if not release.wait(5):
                raise AssertionError('Producer was not released.')
            return {'text': 'private document body'}

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(self.cache.get_or_compute, key, compute) for _ in range(4)]
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                other = pool.submit(self.cache.get_or_compute, self.cache.key('ocr', 'different'), lambda: b'other')
                self.assertEqual(await asyncio.to_thread(other.result, 3), b'other')
            finally:
                release.set()
            values = await asyncio.gather(*(asyncio.to_thread(f.result, 5) for f in futures))
        self.assertEqual(calls, ['producer'])
        self.assertTrue(all(value == {'text': 'private document body'} for value in values))
        with self.store.db() as db:
            row = db.execute(
                'SELECT payload,expires_at-created_at AS ttl FROM shared_content_cache WHERE key=%s', (key,),
            ).fetchone()
        self.assertNotIn(b'private document body', bytes(row['payload']))
        self.assertEqual(row['ttl'].total_seconds(), 604800)

    async def test_independent_worker_processes_share_one_producer(self):
        with self.store.db() as db:
            db.execute('CREATE TABLE shared_producer_calls(id BIGSERIAL PRIMARY KEY)')
        with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context('spawn')) as pool:
            futures = [
                pool.submit(
                    process_compute, self.store.dsn, self.store.schema,
                    str(self._workflow.settings.key),
                ) for _ in range(2)
            ]
            values = await asyncio.gather(*(asyncio.to_thread(future.result, 10) for future in futures))
        self.assertEqual(values, [b'one shared result', b'one shared result'])
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM shared_producer_calls').fetchone()['n'], 1)

    async def test_ttl_validation_scope_revision_and_envelope_are_required_for_reuse(self):
        for value in ({'text': 'synthetic'}, ['synthetic'], b'\0binary\xff'):
            with self.subTest(value_type=type(value).__name__):
                key = self.cache.key('content', type(value).__name__)
                factory = Mock(return_value=value)
                self.assertEqual(self.cache.get_or_compute(key, factory), value)
                self.assertEqual(self.cache.get_or_compute(key, factory), value)
                self.assertEqual(factory.call_count, 1)
                self.expire(key)
                self.assertEqual(self.cache.get_or_compute(key, factory), value)
                self.assertEqual(factory.call_count, 2)
        key = self.cache.key('validated', 'same')
        self.cache.get_or_compute(key, lambda: {'valid': False})

        def validate(value):
            if value.get('valid') is not True:
                raise ValueError('synthetic invalid evidence')
        self.assertEqual(self.cache.get_or_compute(key, lambda: {'valid': True}, validate), {'valid': True})
        other = self.cache.key('validated', 'other')
        self.cache.get_or_compute(other, lambda: {'valid': False})
        with self.store.db() as db:
            db.execute(
                'UPDATE shared_content_cache SET payload=(SELECT payload FROM shared_content_cache WHERE key=%s) WHERE key=%s', (
                    other, key,
                ),
            )
        factory = Mock(return_value={'valid': True})
        self.assertEqual(self.cache.get_or_compute(key, factory, validate), {'valid': True})
        factory.assert_called_once()
        for scope, revision in [('another-eip', 'test-v1'), ('trusted-eip', 'test-v2')]:
            other_cache = SharedContentCache(self.store, scope, revision)
            self.assertNotEqual(other_cache.key('validated', 'same'), key)

    async def test_failed_leader_sets_short_safe_cooldown_for_concurrent_waiters(self):
        key = self.cache.key('failing')
        calls, guard = [], Lock()

        def fail():
            with guard:
                calls.append('attempt')
            raise ValueError('sensitive upstream diagnostics must not persist')

        def invoke():
            try:
                self.cache.get_or_compute(key, fail)
            except Exception as error:
                return error
        with ThreadPoolExecutor(max_workers=4) as pool:
            errors = list(pool.map(lambda _: invoke(), range(4)))
        self.assertEqual(calls, ['attempt'])
        self.assertEqual(sum(isinstance(error, ProcessingError) for error in errors), 3)
        with self.store.db() as db:
            row = db.execute(
                'SELECT state,payload,expires_at-created_at AS ttl FROM shared_content_cache WHERE key=%s', (key,),
            ).fetchone()
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(row['ttl'].total_seconds(), 30)
        envelope = json.loads(self.store.cipher.decrypt(bytes(row['payload'])))
        self.assertEqual(envelope['value'], 'producer_failed')
        self.assertNotIn('sensitive', json.dumps(envelope))
        self.expire(key)
        self.assertEqual(self.cache.get_or_compute(key, lambda: {'recovered': True}), {'recovered': True})

    async def test_fresh_validation_failure_is_not_published_ready(self):
        for asynchronous in (False, True):
            for replay_safe in (False, True):
                with self.subTest(asynchronous=asynchronous, replay_safe=replay_safe):
                    key = self.cache.key('invalid', asynchronous, replay_safe)
                    validate = Mock(side_effect=ValueError('synthetic evidence mismatch'))

                    async def compute():
                        return {'invalid': True}
                    with self.assertRaisesRegex(ValueError, 'synthetic evidence mismatch'):
                        if asynchronous:
                            await self.cache.aget_or_compute(key, compute, validate, replay_safe=replay_safe)
                        else:
                            self.cache.get_or_compute(key, lambda: {'invalid': True}, validate, replay_safe=replay_safe)
                    validate.assert_called_once()
                    with self.store.db() as db:
                        state = db.execute(
                            'SELECT state FROM shared_content_cache WHERE key=%s',
                            (key,),
                        ).fetchone()['state']
                    self.assertEqual(state, 'failed' if replay_safe else 'unknown')

    async def test_ready_encoding_failure_preserves_validated_success_and_paid_unknown(self):
        for asynchronous in (False, True):
            for replay_safe in (False, True):
                for failure in ('json_nan', 'cipher'):
                    with self.subTest(asynchronous=asynchronous, replay_safe=replay_safe, failure=failure):
                        key = self.cache.key('encoding-failure', asynchronous, replay_safe, failure)
                        value = {
                            'verified': True, 'unused_metadata': float(
                                'nan',
                            ),
                        } if failure == 'json_nan' else b'valid bytes'
                        factory = Mock(return_value=value)
                        validate = Mock()
                        encrypt = self.store.cipher.encrypt

                        def reject_ready_bytes(payload):
                            if json.loads(payload)['kind'] == 'bytes':
                                raise OSError('synthetic ready encryption failure')
                            return encrypt(payload)

                        async def compute():
                            return factory()
                        with patch.object(self.store.cipher, 'encrypt', side_effect=reject_ready_bytes):
                            if asynchronous:
                                result = await self.cache.aget_or_compute(key, compute, validate, replay_safe=replay_safe)
                            else:
                                result = self.cache.get_or_compute(key, factory, validate, replay_safe=replay_safe)
                        self.assertIs(result, value)
                        validate.assert_called_once_with(value)
                        factory.assert_called_once()
                        if not replay_safe:
                            with self.assertRaisesRegex(ProcessingError, '尚待核對'):
                                if asynchronous:
                                    await self.cache.aget_or_compute(key, compute, validate, replay_safe=False)
                                else:
                                    self.cache.get_or_compute(key, factory, validate, replay_safe=False)
                            factory.assert_called_once()
                            with self.store.db() as db:
                                self.assertEqual(
                                    db.execute('SELECT state FROM shared_content_cache WHERE key=%s', (key,)).fetchone()[
                                        'state'
                                    ], 'unknown',
                                )
                        else:
                            self.assertEqual(
                                self.cache.get_or_compute(
                                    key, lambda: {'rebuilt': True},
                                ), {'rebuilt': True},
                            )

    async def test_unsupported_fresh_values_still_fail_without_a_custom_validator(self):
        for asynchronous in (False, True):
            for replay_safe in (False, True):
                with self.subTest(asynchronous=asynchronous, replay_safe=replay_safe):
                    key = self.cache.key('unsupported-fresh-value', asynchronous, replay_safe)

                    async def compute():
                        return 42
                    with self.assertRaisesRegex(ValueError, 'JSON objects, arrays or bytes'):
                        if asynchronous:
                            await self.cache.aget_or_compute(key, compute, replay_safe=replay_safe)
                        else:
                            self.cache.get_or_compute(key, lambda: 42, replay_safe=replay_safe)
                    with self.store.db() as db:
                        state = db.execute(
                            'SELECT state FROM shared_content_cache WHERE key=%s',
                            (key,),
                        ).fetchone()['state']
                    self.assertEqual(state, 'failed' if replay_safe else 'unknown')

    async def test_publish_failure_preserves_success_without_recomputing(self):
        key = self.cache.key('successful')
        factory = Mock(return_value={'complete': True})
        with patch.object(self.cache, '_publish', side_effect=OSError('synthetic cache publish failure')):
            self.assertEqual(self.cache.get_or_compute(key, factory), {'complete': True})
        factory.assert_called_once()
        async_factory = Mock()

        async def compute():
            async_factory()
            return b'complete'
        with patch.object(self.cache, '_apublish', side_effect=OSError('synthetic cache publish failure')):
            self.assertEqual(await self.cache.aget_or_compute(key, compute), b'complete')
        async_factory.assert_called_once()

    async def test_cancelled_leader_drains_to_publish_while_cancelled_waiter_stops_immediately(self):
        key = self.cache.key('async-flight')
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def compute():
            calls.append('leader')
            entered.set()
            await release.wait()
            return {'done': True}

        async def must_not_compute():
            self.fail('A waiting caller must reuse the leader result.')
        leader = asyncio.create_task(self.cache.aget_or_compute(key, compute))
        await asyncio.wait_for(entered.wait(), 3)
        waiter = asyncio.create_task(self.cache.aget_or_compute(key, must_not_compute))
        await asyncio.sleep(0.15)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(waiter, 1)
        leader.cancel()
        await asyncio.sleep(0)
        self.assertFalse(leader.done())
        leader.cancel()
        follower = asyncio.create_task(self.cache.aget_or_compute(key, must_not_compute))
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(leader, 3)
        self.assertEqual(await asyncio.wait_for(follower, 3), {'done': True})
        self.assertEqual(calls, ['leader'])

    async def test_session_close_releases_lock_and_cleanup_skips_active_content(self):
        key = self.cache.key('active')
        self.cache.get_or_compute(key, lambda: b'expired')
        self.expire(key)
        with self.store.db() as db:
            db.execute("INSERT INTO processing_chunks VALUES('owner','private',%s,0)", (b'private-ciphertext',))
            db.execute("INSERT INTO writes VALUES('unknown','unknown',0)")
        policy = Retention(self.store.dsn, self.store.schema)
        self.assertEqual(policy.expired_shared_content()['rows'], 1)
        with self.cache._connect() as lease:
            lease.execute('SELECT pg_advisory_lock(hashtextextended(%s,0))', (self.cache._lock_name(key),))
            self.assertEqual(policy.expired_shared_content(apply=True)['rows'], 0)
        self.assertEqual(policy.expired_shared_content(apply=True)['rows'], 1)
        with self.store.db() as db:
            self.assertEqual(
                db.execute('SELECT payload FROM processing_chunks').fetchone()
                ['payload'], b'private-ciphertext',
            )
            self.assertEqual(db.execute('SELECT status FROM writes').fetchone()['status'], 'unknown')

    async def test_lost_leader_connection_cannot_overwrite_a_new_leaders_result(self):
        key = self.cache.key('lost-connection')
        entered, release = asyncio.Event(), asyncio.Event()
        connections = []
        connect = self.cache._aconnect

        async def observe():
            db = await connect()
            connections.append(db)
            return db

        async def old_producer():
            entered.set()
            await release.wait()
            return {'producer': 'old'}

        async def new_producer():
            return {'producer': 'new'}
        with patch.object(self.cache, '_aconnect', side_effect=observe):
            old = asyncio.create_task(self.cache.aget_or_compute(key, old_producer))
            await asyncio.wait_for(entered.wait(), 3)
            with psycopg.connect(self.store.dsn, autocommit=True) as admin:
                admin.execute('SELECT pg_terminate_backend(%s)', (connections[0].info.backend_pid,))
            self.assertEqual(await self.cache.aget_or_compute(key, new_producer), {'producer': 'new'})
            release.set()
            self.assertEqual(await asyncio.wait_for(old, 3), {'producer': 'old'})
        unused = Mock(side_effect=AssertionError('A completed successor must remain cached.'))
        self.assertEqual(self.cache.get_or_compute(key, unused), {'producer': 'new'})

    async def test_missing_key_is_not_replaced_when_only_shared_results_exist(self):
        self.cache.get_or_compute(self.cache.key('only-shared'), lambda: b'private')
        self._workflow.settings.key.unlink()
        with self.assertRaisesRegex(ValueError, '金鑰遺失'):
            AutomationStore(self._workflow.settings.key)
        self.assertFalse(self._workflow.settings.key.exists())

    async def test_paid_success_with_failed_publish_is_returned_once_then_never_replayed(self):
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                key = self.cache.key('paid-publish-failure', asynchronous)
                factory = Mock(return_value={'verified': True})
                if asynchronous:
                    publish = self.cache._apublish

                    async def only_start(db, key, payload, *, state='ready'):
                        if state == 'ready':
                            raise OSError('synthetic ready publication failure')
                        return await publish(db, key, payload, state=state)

                    async def compute():
                        return factory()
                    with patch.object(self.cache, '_apublish', side_effect=only_start):
                        self.assertEqual(await self.cache.aget_or_compute(key, compute, replay_safe=False), {'verified': True})
                else:
                    publish = self.cache._publish

                    def only_start(db, key, payload, *, state='ready'):
                        if state == 'ready':
                            raise OSError('synthetic ready publication failure')
                        return publish(db, key, payload, state=state)
                    with patch.object(self.cache, '_publish', side_effect=only_start):
                        self.assertEqual(self.cache.get_or_compute(key, factory, replay_safe=False), {'verified': True})
                factory.assert_called_once()
                with self.store.db() as db:
                    row = db.execute('SELECT state FROM shared_content_cache WHERE key=%s', (key,)).fetchone()
                    self.assertEqual(row['state'], 'unknown')
                    # Expiration, corruption and accidentally requesting replay-safe
                    # mode must not turn a paid start into a repeatable cache miss.
                    db.execute(
                        "UPDATE shared_content_cache SET payload=%s,expires_at=CURRENT_TIMESTAMP-INTERVAL '1 day' WHERE key=%s", (
                            b'broken-envelope', key,
                        ),
                    )
                with self.assertRaisesRegex(ProcessingError, '尚待核對'):
                    self.cache.get_or_compute(key, factory)
                factory.assert_called_once()
        policy = Retention(self.store.dsn, self.store.schema)
        self.assertEqual(policy.expired_shared_content(apply=True)['rows'], 0)
        report = {row['table']: row for row in policy.report()}
        self.assertEqual(report['shared_content_cache']['category'], 'content_cache_and_paid_receipts')
        self.assertFalse(report['shared_content_cache']['automatic_deletion'])
        with self.store.db() as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) AS n FROM shared_content_cache WHERE state='unknown'",
                ).fetchone()['n'], 2,
            )

    async def test_started_paid_producer_process_death_keeps_durable_unknown(self):
        process = multiprocessing.get_context('spawn').Process(
            target=paid_process_exit,
            args=(self.store.dsn, self.store.schema, str(self._workflow.settings.key)),
        )
        process.start()
        try:
            await asyncio.to_thread(process.join, 10)
        finally:
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 3)
        self.assertEqual(process.exitcode, 17)
        cache = SharedContentCache(self.store, 'trusted-eip', 'paid-process-v1')
        factory = Mock(side_effect=AssertionError('An unconfirmed paid call must never be repeated.'))
        with self.assertRaisesRegex(ProcessingError, '尚待核對'):
            cache.get_or_compute(cache.key('paid-evidence'), factory, replay_safe=False)
        factory.assert_not_called()

    async def test_healthy_paid_leader_and_waiter_share_result_after_reservation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        key = self.cache.key('paid-concurrent')

        async def compute():
            calls.append('paid-call')
            entered.set()
            await release.wait()
            return ['verified']
        leader = asyncio.create_task(self.cache.aget_or_compute(key, compute, replay_safe=False))
        await asyncio.wait_for(entered.wait(), 3)
        follower = asyncio.create_task(self.cache.aget_or_compute(key, compute, replay_safe=False))
        await asyncio.sleep(0.15)
        self.assertFalse(follower.done())
        release.set()
        self.assertEqual(await asyncio.gather(leader, follower), [['verified'], ['verified']])
        self.assertEqual(calls, ['paid-call'])

    async def test_unconfirmed_paid_failure_and_start_write_failure_never_repeat_compute(self):
        key = self.cache.key('paid-failed')
        fail = Mock(side_effect=ValueError('synthetic paid failure'))
        with self.assertRaisesRegex(ValueError, 'synthetic paid failure'):
            self.cache.get_or_compute(key, fail, replay_safe=False)
        with self.assertRaisesRegex(ProcessingError, '尚待核對'):
            self.cache.get_or_compute(key, fail, replay_safe=False)
        fail.assert_called_once()
        unused = Mock()
        with patch.object(self.cache, '_publish', side_effect=OSError('synthetic start failure')):
            with self.assertRaises(ProcessingError):
                self.cache.get_or_compute(self.cache.key('start-failed'), unused, replay_safe=False)
        unused.assert_not_called()

    async def test_confirmed_paid_rate_limit_can_retry_after_cooldown_but_network_failure_cannot(self):
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                key = self.cache.key('paid-rate-limit', asynchronous)
                factory = Mock(side_effect=[CloudRateLimited('synthetic confirmed 429'), {'recovered': True}])

                async def compute():
                    return factory()
                with self.assertRaises(CloudRateLimited):
                    if asynchronous:
                        await self.cache.aget_or_compute(key, compute, replay_safe=False)
                    else:
                        self.cache.get_or_compute(key, factory, replay_safe=False)
                with self.assertRaises(ProcessingError):
                    self.cache.get_or_compute(key, factory, replay_safe=False)
                self.assertEqual(factory.call_count, 1)
                self.expire(key)
                if asynchronous:
                    self.assertEqual(await self.cache.aget_or_compute(key, compute, replay_safe=False), {'recovered': True})
                else:
                    self.assertEqual(self.cache.get_or_compute(key, factory, replay_safe=False), {'recovered': True})
                self.assertEqual(factory.call_count, 2)
        key = self.cache.key('paid-network-unknown')
        uncertain = Mock(side_effect=CloudTemporaryFailure('synthetic network timeout'))
        with self.assertRaises(CloudTemporaryFailure):
            self.cache.get_or_compute(key, uncertain, replay_safe=False)
        self.expire(key)
        with self.assertRaisesRegex(ProcessingError, '尚待核對'):
            self.cache.get_or_compute(key, uncertain, replay_safe=False)
        uncertain.assert_called_once()

    async def test_typed_preflight_rejection_allows_another_owner_after_cooldown(self):
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                key = self.cache.key('paid-preflight-rejected', asynchronous)
                rejection = ModelNotStarted('synthetic caller preflight rejected before provider execution')
                factory = Mock(side_effect=[rejection, {'verified_for_next_owner': True}])

                async def compute():
                    return factory()
                with self.assertRaises(ModelNotStarted) as error:
                    if asynchronous:
                        await self.cache.aget_or_compute(key, compute, replay_safe=False)
                    else:
                        self.cache.get_or_compute(key, factory, replay_safe=False)
                self.assertIs(error.exception, rejection)
                with self.store.db() as db:
                    self.assertEqual(
                        db.execute(
                            'SELECT state FROM shared_content_cache WHERE key=%s',
                            (key,),
                        ).fetchone()['state'], 'failed',
                    )
                with self.assertRaises(ProcessingError):
                    self.cache.get_or_compute(key, factory, replay_safe=False)
                self.assertEqual(factory.call_count, 1)
                self.expire(key)
                if asynchronous:
                    self.assertEqual(await self.cache.aget_or_compute(key, compute, replay_safe=False), {'verified_for_next_owner': True})
                else:
                    self.assertEqual(
                        self.cache.get_or_compute(key, factory, replay_safe=False),
                        {'verified_for_next_owner': True},
                    )
                self.assertEqual(factory.call_count, 2)


class SharedContentSettingsTests(unittest.TestCase):
    def test_disabled_cache_does_not_open_storage_and_defaults_keep_old_constructor(self):
        settings = WorkflowSettings('postgresql://synthetic', 'eip_test', Path('/unused'))
        self.assertEqual(settings.shared_content_cache_ttl_seconds, 604800)
        resources = WorkflowResources(WorkflowSettings(settings.dsn, settings.schema, settings.key, False))
        self.assertIsNone(resources.shared_content)
        self.assertIsNone(resources._store)

    def test_invalid_flags_and_ttls_fail_without_echoing_values(self):
        for enabled, ttl in [('misspelled-secret', '604800'), ('true', 'bad-secret'), ('true', '0'), ('true', '2592001')]:
            with self.subTest(enabled=enabled, ttl=ttl), patch.dict(
                'os.environ', {
                    'EIP_SHARED_CONTENT_CACHE_ENABLED': enabled, 'EIP_SHARED_CONTENT_CACHE_TTL_SECONDS': ttl,
                }, clear=True,
            ):
                with self.assertRaises(ValueError) as error:
                    WorkflowSettings.from_environment()
                self.assertNotIn('secret', str(error.exception))

    def test_database_failure_before_producer_is_safe_and_never_computes(self):
        cache = SharedContentCache(
            lambda: (_ for _ in ()).throw(
                psycopg.OperationalError('sensitive diagnostics'),
            ), 'scope', 'v1',
        )
        factory = Mock()
        with self.assertRaises(ProcessingError) as error:
            cache.get_or_compute(cache.key('test'), factory)
        self.assertNotIn('sensitive', str(error.exception))
        factory.assert_not_called()

    def test_shared_cleanup_requires_explicit_scope_and_apply_flags(self):
        for arguments in [
            ['retention', '--apply-shared-content-cleanup'],
            ['retention', '--scope', 'company', '--shared-content-cache'],
        ]:
            with self.subTest(arguments=arguments), patch('sys.argv', arguments):
                with self.assertRaises(SystemExit) as error:
                    retention.main()
                self.assertEqual(error.exception.code, 2)
        for limit in (0, 1001, True):
            with self.assertRaises(ValueError):
                Retention('unused', 'eip').expired_shared_content(limit=limit)
