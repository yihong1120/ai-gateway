"""Invalid historical-version requests must not crash an otherwise valid job."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from company_agent.runtime.context import Context
from company_agent.runtime.execution import DocumentAgent
from company_agent.services.delivery.tools import supersede
from company_agent.services.files.errors import RecoverableToolError
from company_agent.services.office.documents import create_word
from company_agent.tests.test_agent import call
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_enterprise_completion import finish
from company_agent.tests.test_multi_agent import RawRoleModel


class VersionTransitionRecoveryTests(unittest.IsolatedAsyncioTestCase, Fixture):
    def versions(self):
        files = [self.store.artifact('alice', self.job, 'summary.docx', create_word(str(i), [])) for i in range(3)]
        for a, b in zip(files, files[1:]):
            self.store.supersede_artifact('alice', self.job, a['id'], a['sha256'], b['id'], b['sha256'])
        return files

    async def invoke(self, old, new, owner='alice'):
        runtime = SimpleNamespace(context=Context(owner, self.job, self.config, self.store), tool_call_id='supersede')
        return await supersede(runtime, old_id=old['id'], old_sha256=old['sha256'], new_id=new['id'], new_sha256=new['sha256'])

    async def test_skipping_an_existing_chain_is_recoverable_and_does_not_rewrite_it(self):
        a, b, c = self.versions()
        with self.assertRaisesRegex(RecoverableToolError, 'invalid_version_transition'):
            await self.invoke(a, c)
        by_id = {f['id']: f for f in self.store.files('alice', self.job)}
        self.assertEqual(by_id[a['id']]['superseded_by'], b['id'])
        self.assertEqual(by_id[b['id']]['superseded_by'], c['id'])
        self.assertEqual([f['id'] for f in self.store.deliverable_files('alice', self.job)], [c['id']])
        await self.invoke(b, c)  # Existing exact relation remains idempotent.

    async def test_real_graph_uses_active_version_after_invalid_transition(self):
        a, _b, c = self.versions()
        self.config.structured_completion = True
        self.config.require_artifact_review = True
        self.config.max_steps = 12
        model = RawRoleModel(
            coordinator=[
                call(
                    'supersede_artifact', {
                        'old_id': a['id'], 'old_sha256': a['sha256'],
                        'new_id': c['id'], 'new_sha256': c['sha256'],
                    }, 'invalid',
                ),
                call('delegate_reviewer', {'task': '審核有效最新版', 'context': c['id']}, 'review'),
                finish(ids=[c['id']]),
            ], reviewer=[
                call('inspect_artifact', {'artifact_id': c['id']}, 'inspect'), finish(
                    'complete_assignment', ids=[c['id']],
                ),
            ],
        )
        with patch.object(self.store, 'job', return_value=self.job):
            result = await DocumentAgent(self.config, model, store=self.store).run('alice', '交付本工作已有的有效最新版，不新增文件。')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual([f['id'] for f in result['files']], [c['id']])
        self.assertIn('invalid_version_transition', result['tool_feedback'][0]['message'])

    async def test_cycle_request_recovers_but_ownership_and_hash_failures_stay_hard(self):
        a, _b, c = self.versions()
        with self.assertRaisesRegex(RecoverableToolError, 'invalid_version_transition'):
            await self.invoke(c, a)
        with self.assertRaises(ValueError):
            await self.invoke(a, c, owner='bob')
        with self.assertRaises(ValueError):
            await self.invoke({**a, 'sha256': '0'*64}, c)
        self.assertEqual([f['id'] for f in self.store.deliverable_files('alice', self.job)], [c['id']])
