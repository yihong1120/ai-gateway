"""Regressions for the artifact and upstream failures found by live evaluation."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import httpx
from openai import APIConnectionError
from openai import APITimeoutError
from openai import RateLimitError

from company_agent.runtime.execution import DocumentAgent
from company_agent.tests.test_agent import call
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_enterprise_completion import finish
from company_agent.tests.test_multi_agent import RawRoleModel


class FailureRecoveryTests(unittest.IsolatedAsyncioTestCase, Fixture):
    def setUp(self):
        super().setUp()
        self.config.structured_completion = True
        self.config.require_artifact_review = True

    async def test_failure_inside_child_resumes_saved_child_without_replaying_create(
        self,
    ):
        request = httpx.Request('POST', 'https://gateway.example/v1')
        failure = RateLimitError(
            'PRIVATE', response=httpx.Response(429, request=request), body=None,
        )

        def fail(_):
            raise failure

        model = RawRoleModel(
            coordinator=[call('delegate_document', {'task': '建立草稿'}, 'd')],
            document=[
                call('create_text', {'name': 'saved.txt', 'content': 'keep'}, 'create'),
                fail,
            ],
        )
        failed = await self.failed_job(
            DocumentAgent(self.config, model).run('alice', '建立草稿'), RateLimitError,
        )
        ids = [file['id'] for file in failed['files']]
        self.assertEqual(len(ids), 1)
        self.assertTrue(failed['resumable'])
        model = RawRoleModel(
            document=[lambda _: finish('complete_assignment', ids=ids)],
            coordinator=[
                call('delegate_reviewer', {'task': '核對草稿'}, 'r'),
                lambda _: finish(ids=ids),
            ],
            reviewer=[
                lambda _: call('inspect_artifact', {'artifact_id': ids[0]}, 'read'),
                lambda _: finish('complete_assignment', ids=ids),
            ],
        )
        with patch(
            'company_agent.services.files.operations.ToolBox.create_text',
            side_effect=AssertionError('must not replay'),
        ):
            result = await DocumentAgent(self.config, model).resume(
                'alice', failed['job_id'],
            )
        self.assertEqual(result['status'], 'done')
        self.assertEqual([file['id'] for file in result['files']], ids)

    async def test_failure_projection_error_preserves_original_sdk_exception(self):
        request = httpx.Request('POST', 'https://gateway.example/v1')
        failure = RateLimitError(
            'PRIVATE', response=httpx.Response(429, request=request), body=None,
        )

        def fail(_):
            raise failure

        with patch(
            'company_agent.runtime.execution.finish_run',
            side_effect=ValueError('projection failed'),
        ):
            with self.assertRaises(RateLimitError) as raised:
                await DocumentAgent(self.config, RawRoleModel(coordinator=[fail])).run(
                    'alice', '回答',
                )
        self.assertIs(raised.exception, failure)

    async def artifact_workflow(self, reviewer_steps):
        ids = []

        def created(messages):
            ids.append(json.loads(messages[-1].content)['id'])
            return finish('complete_assignment', ids=ids)

        model = RawRoleModel(
            coordinator=[
                call('delegate_document', {'task': '建立草稿'}, 'd'),
                call('delegate_reviewer', {'task': '讀取並核對草稿'}, 'r'),
                lambda _: finish(ids=ids),
            ],
            document=[
                call(
                    'create_text',
                    {'name': 'draft.txt', 'content': 'private draft'},
                    'create',
                ),
                created,
            ],
            reviewer=reviewer_steps(ids)
            + [lambda _: finish('complete_assignment', ids=ids)],
        )
        result = await DocumentAgent(self.config, model).run('alice', '製作並核對草稿')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(len(result['files']), 1)
        self.assertFalse(result['artifact_review']['unreviewed_ids'])
        self.assertFalse(any(model.scripts.values()))
        return result

    async def test_wrong_id_is_correctable_without_reading_foreign_artifact(self):
        foreign_job = self.store.job('bob')
        foreign = self.store.artifact(
            'bob', foreign_job, 'secret-name.txt', b'foreign-private-text',
        )

        def steps(ids):
            def correct(messages):
                self.assertEqual(messages[-1].status, 'error')
                self.assertIn(ids[0], messages[-1].content)
                self.assertNotIn('secret-name', messages[-1].content)
                self.assertNotIn('foreign-private-text', messages[-1].content)
                self.assertIn(ids[0], messages[0].content)
                return call('inspect_artifact', {'artifact_id': ids[0]}, 'correct')

            return [
                call('inspect_artifact', {'artifact_id': foreign['id']}, 'wrong'),
                correct,
            ]

        result = await self.artifact_workflow(steps)
        self.assertEqual(len(result['tool_feedback']), 1)
        self.assertNotIn('foreign-private-text', str(result))

    async def test_missing_source_draft_routes_to_inspection_without_claiming_read(
        self,
    ):
        def steps(ids):
            def routed(messages):
                receipt = json.loads(messages[-1].content)
                self.assertEqual(receipt['status'], 'needs_artifact_reader')
                self.assertFalse(receipt['executed'])
                self.assertEqual(receipt['artifacts'][0]['id'], ids[0])
                return call('inspect_artifact', {'artifact_id': ids[0]}, 'inspect')

            return [
                call(
                    'read_file',
                    {'root': 'project', 'path': 'draft.txt'},
                    'wrong-reader',
                ),
                routed,
            ]

        result = await self.artifact_workflow(steps)
        self.assertFalse(result['sources'])
        self.assertFalse(list(self.root.iterdir()))

    async def test_existing_same_name_source_is_read_before_artifact_routing(self):
        self.file('draft.txt', b'existing source')

        def steps(ids):
            def read_source(messages):
                receipt = json.loads(messages[-1].content)
                self.assertNotIn('needs_artifact_reader', str(receipt))
                self.assertIn('existing source', str(receipt))
                self.assertNotIn('private draft', str(receipt))
                return call('inspect_artifact', {'artifact_id': ids[0]}, 'inspect')

            return [
                call(
                    'read_file', {'root': 'project', 'path': 'draft.txt'}, 'read-source',
                ),
                read_source,
            ]

        result = await self.artifact_workflow(steps)
        self.assertEqual(result['sources'][0]['path'], 'draft.txt')

    async def test_unknown_missing_path_returns_feedback_and_unauthorized_root_still_fails(self):
        for root, path, error in [
            ('project', 'unknown.txt', None),
            ('secret', 'draft.txt', ValueError),
        ]:
            with self.subTest(root=root):
                model = RawRoleModel(
                    coordinator=[
                        call('delegate_reviewer', {'task': '讀取草稿'}, 'r'),
                        finish(outcome='blocked', summary='必要來源未提供。'),
                    ],
                    reviewer=[
                        call('read_file', {'root': root, 'path': path}, 'read'), finish(
                            'complete_assignment', outcome='blocked', summary='必要來源未提供。',
                        ),
                    ],
                )
                operation = DocumentAgent(self.config, model).run('alice', '讀取資料')
                if error:
                    await self.failed_job(operation, error)
                    self.assertEqual(model.invocations, ['coordinator', 'reviewer'])
                else:
                    result = await operation
                    self.assertEqual(result['status'], 'blocked')
                    self.assertEqual(len(result['tool_feedback']), 1)
                    self.assertFalse(result['sources'])

    async def test_inconsistent_completed_outcome_returns_validation_feedback(self):
        for field, outcome in [
            ('missing_inputs', 'needs_input'),
            ('blockers', 'blocked'),
        ]:
            with self.subTest(field=field):
                args = {
                    'outcome': 'completed',
                    'summary': '尚缺必要資訊',
                    'artifact_ids': [],
                    field: ['結束時間或來源權限'],
                }

                def correct(messages):
                    self.assertEqual(messages[-1].status, 'error')
                    return call('complete_job', {**args, 'outcome': outcome}, 'correct')

                model = RawRoleModel(
                    coordinator=[call('complete_job', args, 'wrong'), correct],
                )
                result = await DocumentAgent(self.config, model).run(
                    'alice', '處理要求',
                )
                self.assertEqual(result['status'], outcome)
                self.assertEqual(len(model.invocations), 2)

    async def test_child_missing_inputs_are_explicit_in_coordinator_receipt(self):
        def complete(messages):
            receipt = json.loads(messages[-1].content)
            self.assertEqual(receipt['missing_inputs'], ['會議結束時間'])
            self.assertEqual(receipt['status'], 'needs_input')
            return finish(outcome='needs_input', summary='請提供會議結束時間')

        model = RawRoleModel(
            coordinator=[call('delegate_eip', {'task': '檢查必要資訊'}, 'd'), complete],
            eip=[
                call(
                    'complete_assignment',
                    {
                        'outcome': 'needs_input',
                        'summary': '缺結束時間',
                        'artifact_ids': [],
                        'missing_inputs': ['會議結束時間'],
                    },
                    'finish',
                ),
            ],
        )
        result = await DocumentAgent(self.config, model).run('alice', '準備會議')
        self.assertEqual(result['status'], 'needs_input')

    async def test_sdk_failures_keep_artifact_and_resume_without_recreating_it(self):
        request = httpx.Request('POST', 'https://gateway.example/v1')
        failures = [
            RateLimitError(
                'PRIVATE provider body',
                response=httpx.Response(429, request=request),
                body=None,
            ),
            APITimeoutError(request=request),
            APIConnectionError(request=request),
            TimeoutError('PRIVATE timeout'),
        ]
        for failure in failures:
            with self.subTest(error=type(failure).__name__):
                ids = []

                def created(messages):
                    ids.append(json.loads(messages[-1].content)['id'])
                    return finish('complete_assignment', ids=ids)

                def fail(_):
                    raise failure

                model = RawRoleModel(
                    coordinator=[
                        call('delegate_document', {'task': '建立草稿'}, 'd'),
                        fail,
                    ],
                    document=[
                        call(
                            'create_text',
                            {'name': 'saved.txt', 'content': 'preserve me'},
                            'create',
                        ),
                        created,
                    ],
                )
                failed = await self.failed_job(
                    DocumentAgent(self.config, model).run('alice', '建立並核對'),
                    type(failure),
                )
                self.assertIs(self.native_error, failure)
                self.assertEqual(failed['status'], 'error')
                self.assertTrue(failed['resumable'])
                self.assertEqual(failed['artifact_review']['unreviewed_ids'], ids)
                self.assertNotIn(
                    'PRIVATE', str(self.store.result('alice', failed['job_id'])),
                )
                used = failed['budget']['used']
                resumed_model = RawRoleModel(
                    coordinator=[
                        call('delegate_reviewer', {'task': '核對保留草稿'}, 'r'),
                        lambda _: finish(ids=ids),
                    ],
                    reviewer=[
                        lambda _: call(
                            'inspect_artifact', {'artifact_id': ids[0]}, 'read',
                        ),
                        lambda _: finish('complete_assignment', ids=ids),
                    ],
                )
                result = await DocumentAgent(self.config, resumed_model).resume(
                    'alice', failed['job_id'],
                )
                self.assertEqual(result['status'], 'done')
                self.assertEqual([file['id'] for file in result['files']], ids)
                self.assertGreater(result['budget']['used'], used)
                self.assertNotIn('document', resumed_model.invocations)
                self.assertEqual(
                    self.store.read_artifact('alice', ids[0], failed['job_id'])[2],
                    b'preserve me',
                )
