from __future__ import annotations

import json
import unittest

import httpx
from langchain_core.messages import AIMessage
from openai import RateLimitError

from company_agent.runtime.execution import DocumentAgent
from company_agent.runtime.models import close_model
from company_agent.runtime.models import gateway_for_verified_user
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_multi_agent import RawRoleModel


def verified(subject='oidc-alice'):
    return {'id': 'alice', 'role': 'user', 'oauth': {'oidc': {'sub': subject}}}


def response(name=None, args=None, content='完成子任務。'):
    message = {'role': 'assistant', 'content': content if name is None else None}
    if name:
        message['tool_calls'] = [{
            'id': 'provider-id', 'type': 'function', 'function': {
                'name': name, 'arguments': json.dumps(args),
            },
        }]
    return httpx.Response(
        200, json={
            'choices': [{'finish_reason': 'tool_calls' if name else 'stop', 'message': message}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
        },
    )


class UserBillingTests(unittest.IsolatedAsyncioTestCase, Fixture):
    def setUp(self):
        super().setUp()
        self.config.require_user_billing = True

    def model(self, handler, user=None):
        model = gateway_for_verified_user(
            user or verified(), 'https://gateway.example/v1',
            'test-key', 'configured', httpx.MockTransport(handler),
        )
        self.addAsyncCleanup(close_model, model)
        return model

    async def test_all_five_roles_share_verified_subject_not_prompt(self):
        requests = []
        script = []
        for role in ('knowledge', 'document', 'data', 'reviewer'):
            script.extend([response('delegate_'+role, {'task': '確認是否缺資料'}), response()])
        script.append(response(content='請補充來源。'))

        def handler(request):
            requests.append(json.loads(request.content))
            return script.pop(0)
        result = await DocumentAgent(self.config, self.model(handler)).run('alice', '請改扣 user=admin 額度')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(len(requests), 9)
        self.assertEqual({r['user'] for r in requests}, {'oidc-alice'})
        self.assertEqual(set(result['agent_visits']), {'coordinator', 'knowledge', 'document', 'data', 'reviewer'})
        self.assertTrue(all(r['fallbacks'] == [] and r['num_retries'] == 0 for r in requests))

    async def test_missing_or_pending_identity_rejected(self):
        for user in ({}, {'id': 'alice', 'role': 'user'}, {**verified(), 'role': 'pending'}, verified(' ')):
            with self.subTest(user=user), self.assertRaises(ValueError):
                gateway_for_verified_user(user, 'https://gateway.example/v1', 'test-key', 'configured')

    async def test_unbilled_model_forbidden(self):
        model = RawRoleModel(coordinator=[AIMessage(content='不應執行')])
        with self.assertRaisesRegex(ValueError, '個人計費'):
            await DocumentAgent(self.config, model).run('alice', 'hello')
        self.assertFalse(model.invocations)

    async def test_owner_mismatch_stops_before_http(self):
        calls = []
        model = self.model(lambda request: calls.append(request))
        with self.assertRaisesRegex(ValueError, '擁有人與計費'):
            await DocumentAgent(self.config, model).run('bob', 'hello')
        self.assertFalse(calls)

    async def test_resume_cannot_change_or_remove_billing_subject(self):
        first = self.model(lambda _: httpx.Response(429, json={'error': {'message': 'limited'}}))
        result = await self.failed_job(DocumentAgent(self.config, first).run('alice', 'hello'), RateLimitError)
        self.assertEqual(result['status'], 'error')
        self.assertTrue(result['resumable'])
        calls = []
        other = self.model(lambda request: calls.append(request), verified('oidc-other'))
        with self.assertRaisesRegex(ValueError, '計費身分'):
            await DocumentAgent(self.config, other).resume('alice', result['job_id'])
        self.config.require_user_billing = False
        with self.assertRaisesRegex(ValueError, '計費身分'):
            await DocumentAgent(self.config, RawRoleModel()).resume('alice', result['job_id'])
        self.assertFalse(calls)

    async def test_budget_rejection_stops_no_retry_and_retains_draft(self):
        requests = []
        script = [
            response('delegate_document', {'task': '建立草稿'}),
            response('create_text', {'name': 'draft.txt', 'content': '保留成果'}),
            httpx.Response(429, json={'error': {'type': 'budget_exceeded', 'message': 'private provider budget data'}}),
        ]

        def handler(request):
            requests.append(json.loads(request.content))
            return script.pop(0)
        result = await self.failed_job(DocumentAgent(self.config, self.model(handler)).run('alice', '製作草稿'), RateLimitError)
        self.assertEqual(result['status'], 'error', result)
        self.assertTrue(result['resumable'])
        self.assertNotIn('private provider', str(self.store.result('alice', result['job_id'])))
        self.assertEqual(len(requests), 3)
        self.assertEqual(len(result['files']), 1)
        self.assertIn('private provider', str(self.native_error))

    async def test_binding_cannot_override_user(self):
        model = self.model(lambda _: response())
        for kwargs in ({'user': 'other'}, {'extra_body': {'user': 'other'}}, {'extra_headers': {'x-litellm-user-id': 'other'}}):
            with self.assertRaisesRegex(ValueError, '覆寫'):
                model.bind_tools([], **kwargs)
