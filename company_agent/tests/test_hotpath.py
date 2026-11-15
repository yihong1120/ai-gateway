"""Hot-path refactor contracts: live ACLs, bounded blobs and atomic budgets."""
from __future__ import annotations

import asyncio
import json
import re
import threading
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage

from company_agent.runtime.execution import DocumentAgent
from company_agent.runtime.middleware import visible_sources
from company_agent.tests.test_agent import call
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_enterprise_completion import finish
from company_agent.tests.test_multi_agent import RawRoleModel
from company_agent.tests.test_nas import NASFixture


class SourceDiscoveryTests(NASFixture):
    def prepare(self):
        self.config.nas_binding_revision = self.bind()['revision']
        remote = self.remote()
        roots = remote.visible_roots()
        return SimpleNamespace(
            settings=self.config, store=self.store, owner='alice',
        ), roots

    def test_multiple_accessed_nas_roots_share_one_fresh_discovery_session(self):
        ctx, roots = self.prepare()
        before = len(self.dsm.calls)
        found = visible_sources(
            ctx, {'accessed_roots': [r['name'] for r in roots]}, 'knowledge', {'root'},
        )
        calls = self.dsm.calls[before:]
        self.assertEqual(sum(c['method'] == 'login' for c in calls), 1)
        self.assertEqual(sum(c['method'] == 'list_share' for c in calls), 1)
        self.assertEqual(
            [r for r in found if r.get('source') == 'NAS'],
            [{**r, 'label': 'alice / ' + r['label'], 'connection': 'alice'} for r in roots],
        )
        self.assertFalse(self.dsm.sessions)

    def test_next_discovery_rechecks_share_revocation(self):
        ctx, roots = self.prepare()
        shared = next(r['name'] for r in roots if r['label'] == 'shared')
        visible_sources(ctx, {'accessed_roots': [shared]}, 'knowledge', {'root'})
        self.dsm.no_shared = True
        with self.assertRaisesRegex(ValueError, 'NAS 資料夾'):
            visible_sources(ctx, {'accessed_roots': [shared]}, 'knowledge', {'root'})
        self.assertFalse(self.dsm.sessions)

    def test_local_acl_failure_stops_before_any_nas_request(self):
        ctx, _ = self.prepare()
        self.config.roots[0].readers = []
        before = len(self.dsm.calls)
        with self.assertRaisesRegex(ValueError, '權限'):
            visible_sources(ctx, {'accessed_roots': ['project']}, 'knowledge', {'root'})
        self.assertEqual(len(self.dsm.calls), before)

    def test_unlinked_binding_stops_discovery_without_login(self):
        ctx, roots = self.prepare()
        self.vault.unlink('alice')
        before = len(self.dsm.calls)
        with self.assertRaises(ValueError):
            visible_sources(
                ctx, {'accessed_roots': [roots[0]['name']]}, 'knowledge', {'root'},
            )
        self.assertEqual(len(self.dsm.calls), before)


class ArtifactValidationTests(Fixture):
    def test_batch_uses_one_connection_and_checks_ownership_and_current_bytes(self):
        artifacts = [
            self.store.artifact('alice', self.job, f"{i}.txt", b'verified' * 1000)
            for i in range(4)
        ]
        ids = [artifact['id'] for artifact in artifacts]
        with patch.object(self.store, 'db', wraps=self.store.db) as opened:
            self.store.verify_artifacts('alice', self.job, ids)
            self.assertEqual(opened.call_count, 1)
        foreign_job = self.store.job('alice')
        foreign = self.store.artifact('alice', foreign_job, 'other.txt', b'private')
        for owner, job, keys in [
            ('bob', self.job, ids),
            ('alice', foreign_job, ids),
            ('alice', self.job, ids + [foreign['id']]),
            ('alice', self.job, ids + ['missing']),
        ]:
            with self.subTest(owner=owner, job=job), self.assertRaises(ValueError):
                self.store.verify_artifacts(owner, job, keys)
        with self.store.db() as db:
            db.execute('UPDATE artifacts SET data=%s WHERE id=%s', (b'changed', ids[0]))
        with self.assertRaisesRegex(ValueError, '校驗失敗'):
            self.store.verify_artifacts('alice', self.job, ids)
        # Failed server cursors release their connection and transaction.
        self.store.verify_artifacts('alice', self.job, ids[1:])

    def test_validation_streams_one_blob_at_a_time(self):
        artifact = self.store.artifact('alice', self.job, 'large.txt', b'x' * 1_000_000)
        itersizes = []
        original = self.store.db

        @contextmanager
        def spy():
            with original() as db:
                original_cursor = db.cursor

                @contextmanager
                def track(rows):
                    with rows:
                        yield rows
                        itersizes.append(rows.itersize)

                def cursor(*args, **kwargs):
                    rows = original_cursor(*args, **kwargs)
                    return track(rows) if kwargs.get('name') else rows

                with patch.object(db, 'cursor', side_effect=cursor):
                    yield db

        with patch.object(self.store, 'db', side_effect=spy):
            self.store.verify_artifacts('alice', self.job, [artifact['id']])
        self.assertEqual(itersizes, [1])


class ModelPreparationTests(unittest.IsolatedAsyncioTestCase, Fixture):
    async def test_parallel_child_calls_get_distinct_remaining_counts(self):

        self.config.structured_completion = True
        self.config.max_steps = 4
        remaining = []

        def child(messages):
            remaining.append(
                int(re.search(r'剩餘模型呼叫 (\d+) 次', messages[0].content)[1]),
            )
            return finish('complete_assignment', outcome='needs_input')

        batch = AIMessage(
            content='',
            tool_calls=[
                {'name': 'delegate_knowledge', 'args': {'task': task}, 'id': task}
                for task in ('first', 'second')
            ],
        )
        result = await DocumentAgent(
            self.config,
            RawRoleModel(
                coordinator=[batch, finish(outcome='needs_input')],
                knowledge=[child, child],
            ),
        ).run('alice', '處理兩项資料')
        self.assertEqual(sorted(remaining), [2, 3])
        self.assertEqual(result['budget']['used'], 4)

    async def test_remaining_calls_follow_atomic_reservation_without_extra_read(self):
        self.config.structured_completion = True
        self.config.max_steps = 4

        def first(messages):
            self.assertIn('剩餘模型呼叫 4 次', messages[0].content)
            return call('delegate_knowledge', {'task': '指出缺少資料'}, 'd')

        def child(messages):
            self.assertIn('剩餘模型呼叫 3 次', messages[0].content)
            return finish('complete_assignment', outcome='needs_input')

        def final(messages):
            self.assertIn('剩餘模型呼叫 2 次', messages[0].content)
            return finish(outcome='needs_input')

        original = self.store.attempts
        with patch.object(self.store, 'attempts', wraps=original) as reads:
            result = await DocumentAgent(
                self.config,
                RawRoleModel(coordinator=[first, final], knowledge=[child]),
                store=self.store,
            ).run('alice', '處理資料')
        self.assertEqual(result['budget']['used'], 3)
        self.assertEqual(
            reads.call_count, 1,
        )  # Final result only, none before requests.

    async def test_slow_artifact_validation_does_not_block_event_loop(self):
        self.config.structured_completion = True
        entered, release = threading.Event(), threading.Event()
        validate = self.store.verify_artifacts

        def slow(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Event loop did not release the worker')
            return validate(*args)

        ids = []

        def created(messages):
            ids.append(json.loads(messages[-1].content)['id'])
            return finish('complete_assignment', ids=ids)

        model = RawRoleModel(
            coordinator=[
                call('delegate_document', {'task': '建立草稿'}, 'd'),
                lambda _: finish(ids=ids),
            ],
            document=[
                call('create_text', {'name': 'out.txt', 'content': 'private'}, 'c'),
                created,
            ],
        )
        with patch.object(self.store, 'verify_artifacts', side_effect=slow):
            task = asyncio.create_task(
                DocumentAgent(self.config, model, store=self.store).run(
                    'alice', '建立草稿',
                ),
            )
            try:
                async with asyncio.timeout(5):
                    while not entered.is_set():
                        await asyncio.sleep(0.01)
                release.set()  # This coroutine ran while the validator waited.
                result = await asyncio.wait_for(task, 10)
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(result['status'], 'done')
