from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import httpx
from langchain_core.messages import HumanMessage
from openai import RateLimitError
from pydantic import ValidationError

from company_agent.config import ModelProfile
from company_agent.config import ModelRoute
from company_agent.config import Settings
from company_agent.runtime.context import Context
from company_agent.runtime.execution import DocumentAgent
from company_agent.runtime.models import analysis_model
from company_agent.runtime.models import billed_analysis
from company_agent.runtime.models import close_model
from company_agent.runtime.models import gateway_for_verified_user
from company_agent.runtime.models import routed_model
from company_agent.runtime.models import select_profile
from company_agent.runtime.models import WebBilledModel
from company_agent.services.eip.processing import BilledPipeAdapter
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_user_billing import response
from company_agent.tests.test_user_billing import verified
"""Fixed role models and agent-selected auxiliary analysis profiles."""


def profiles():
    return {
        'small': ModelProfile(
            model='gemini-3.5-flash-lite', reasoning_effort='medium',
            description='較便宜，明確任務及圖片', supports_images=True,
        ),
        'text': ModelProfile(model='DeepSeek-V4-Flash', description='低價文字'),
        'strong': ModelProfile(model='Kimi-K2.5', description='較複雜整合', supports_images=True),
    }


class DynamicModelTests(unittest.IsolatedAsyncioTestCase, Fixture):
    def model(self, handler):
        base = gateway_for_verified_user(
            verified(), 'https://gateway.example/v1', 'key',
            'Kimi-K2.5', httpx.MockTransport(handler),
        )
        self.addAsyncCleanup(close_model, base)
        self.config.model_profiles = profiles()
        return base

    async def test_fixed_role_model_persists_for_all_subtask_calls(self):
        requests = []
        script = [
            response('delegate_document', {'task': '建立草稿'}),
            response('create_text', {'name': 'draft.txt', 'content': 'synthetic'}),
            response(content='完成'), response(content='完成'),
        ]

        def handle(request):
            body = json.loads(request.content)
            requests.append(body)
            self.assertIn('每個 Agent 固定使用', body['messages'][0]['content'])
            for tool in body.get('tools', []):
                if tool['function']['name'].startswith('delegate_'):
                    self.assertNotIn('model_profile', tool['function']['parameters'].get('properties', {}))
            return script.pop(0)
        base = self.model(handle)
        self.config.role_models = {'document': ModelRoute(model='gemini-3.5-flash-lite', reasoning_effort='medium')}
        agent = DocumentAgent(self.config, base)
        result = await agent.run('alice', '建立草稿')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(
            [r['model'] for r in requests],
            ['Kimi-K2.5', 'gemini-3.5-flash-lite', 'gemini-3.5-flash-lite', 'Kimi-K2.5'],
        )
        self.assertEqual({r['user'] for r in requests}, {'oidc-alice'})
        self.assertEqual(len(result['files']), 1)

    async def test_even_listed_profile_rejected_before_delegation_or_file_creation(self):
        requests = []

        def handle(request):
            requests.append(request)
            return (
                response('delegate_document', {'task': '建立草稿', 'model_profile': 'small'})
                if len(requests) == 1 else response(content='無法完成')
            )
        result = await DocumentAgent(self.config, self.model(handle)).run('alice', '建立草稿')
        self.assertTrue(result['tool_feedback'])
        self.assertFalse(result['files'])
        self.assertEqual(len(requests), 2)

    async def test_profile_choice_overrides_fixed_purpose_and_invalid_image_stops_before_budget(self):
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return response(content='{}')
        base = self.model(handle)
        self.config.purpose_models = {'attachment': ModelRoute(model='legacy')}
        root = routed_model(base, self.config)
        ctx = Context('alice', self.job, self.config, self.store, root)
        for purpose in ('attachment', 'attachment_plan', 'vision'):
            await billed_analysis(
                ctx, [HumanMessage(content='synthetic')],
                purpose=purpose, model_profile='small',
            )
        self.assertEqual({r['model'] for r in requests}, {'gemini-3.5-flash-lite'})
        self.assertEqual(self.store.attempts(self.job), 3)
        self.assertEqual(analysis_model(root, None, 'attachment').client.model_name, 'Kimi-K2.5')
        with self.assertRaisesRegex(ValueError, '圖片能力'):
            await billed_analysis(ctx, [HumanMessage(content='image')], model_profile='text')
        self.assertEqual(self.store.attempts(self.job), 3)
        self.assertEqual(len(requests), 3)

    async def test_eip_adapter_uses_agent_choice_for_parse_and_plan(self):
        requests = []
        base = self.model(lambda request: (requests.append(json.loads(request.content)), response(content='{}'))[1])
        ctx = Context('alice', self.job, self.config, self.store, routed_model(base, self.config))
        pipe = SimpleNamespace(
            valves=SimpleNamespace(cloud_ocr_text_max_input_characters=1000),
            _cloud_json_object=json.loads, _cloud_ocr_text_prompt=lambda kind: 'extract',
        )
        adapter = BilledPipeAdapter(ctx, pipe, SimpleNamespace(INSTRUCTION_PLANNER_PROMPT='plan'), 'text')
        with patch('company_agent.services.eip.processing.binding', AsyncMock()):
            await adapter.analyze(SimpleNamespace(text='synthetic', kind='pdf'))
            await adapter.plan('整理')
        self.assertEqual([r['model'] for r in requests], ['DeepSeek-V4-Flash'] * 2)
        self.assertEqual({r['user'] for r in requests}, {'oidc-alice'})

    async def test_eip_native_schema_and_output_budget_reach_billed_gateway(self):
        from official_document_workflow.domain.calendar.contracts import GROUP_ANALYSIS_SCHEMA
        requests = []
        base = self.model(lambda request: (requests.append(json.loads(request.content)), response(content='{}'))[1])
        ctx = Context('alice', self.job, self.config, self.store, routed_model(base, self.config))
        pipe = SimpleNamespace(
            valves=SimpleNamespace(cloud_ocr_text_max_input_characters=1000),
            _cloud_json_object=json.loads, _cloud_ocr_text_prompt=lambda kind: 'extract',
        )
        adapter = BilledPipeAdapter(ctx, pipe, SimpleNamespace(), 'small')
        with patch('company_agent.services.eip.processing.binding', AsyncMock()):
            await adapter.analyze(
                SimpleNamespace(text='synthetic', kind='pdf'),
                schema=GROUP_ANALYSIS_SCHEMA, max_output_tokens=8192,
            )
        self.assertEqual(len(requests), 1)
        self.assertEqual(
            requests[0]['response_format'], {
                'type': 'json_schema', 'json_schema': {'name': 'analysis', 'schema': GROUP_ANALYSIS_SCHEMA},
            },
        )
        self.assertEqual(requests[0]['model'], 'gemini-3.5-flash-lite')
        self.assertEqual(requests[0].get('max_completion_tokens', requests[0].get('max_tokens')), 8192)
        self.assertEqual(requests[0]['user'], 'oidc-alice')
        self.assertEqual(self.store.attempts(self.job), 1)

    async def test_web_profile_retains_identity_reverification(self):
        base = self.model(lambda _: response())
        root = routed_model(WebBilledModel(base.owner, base.subject, base.client), self.config)
        selected = select_profile(root, 'small')
        self.assertIsInstance(selected, WebBilledModel)
        with patch('company_agent.runtime.context.verified_user', AsyncMock(return_value=verified('changed'))):
            with self.assertRaises(ValueError):
                await selected.analyze([HumanMessage(content='synthetic')])

    async def test_resume_rejects_changed_catalog(self):
        base = self.model(lambda _: httpx.Response(429, json={'error': {'message': 'limited'}}))
        agent = DocumentAgent(self.config, base)
        result = await self.failed_job(agent.run('alice', 'test'), RateLimitError)
        self.config.model_profiles['small'].model = 'changed-model'
        changed = DocumentAgent(self.config, base)
        with self.assertRaisesRegex(ValueError, '模型分流設定已變更'):
            await changed.resume('alice', result['job_id'])

    async def test_resume_retains_fixed_role_model_without_redelegation(self):
        requests = []
        script = [
            response('delegate_data', {'task': '確認缺少資料'}),
            httpx.Response(429, json={'error': {'message': 'limited'}}),
            response(content='請提供資料。'), response(content='請提供資料。'),
        ]

        def handle(request):
            requests.append(json.loads(request.content))
            return script.pop(0)
        agent = DocumentAgent(self.config, self.model(handle))
        self.config.role_models = {'data': ModelRoute(model='DeepSeek-V4-Flash')}
        agent = DocumentAgent(self.config, agent.model)
        first = await self.failed_job(agent.run('alice', '查詢未提供的數據'), RateLimitError)
        self.assertEqual(first['status'], 'error')
        self.assertTrue(first['resumable'])
        self.assertIn('429', first['answer'])
        self.assertEqual(len(requests), 2)
        resumed = await agent.resume('alice', first['job_id'])
        self.assertEqual(resumed['status'], 'done', resumed)
        self.assertEqual(
            [r['model'] for r in requests],
            ['Kimi-K2.5', 'DeepSeek-V4-Flash', 'DeepSeek-V4-Flash', 'Kimi-K2.5'],
        )

    async def test_same_role_batch_uses_one_fixed_model(self):
        requests = []
        first = response('delegate_knowledge', {'task': '第一項'}).json()
        second = response('delegate_knowledge', {'task': '第二項'}).json()
        second_call = second['choices'][0]['message']['tool_calls'][0]
        second_call['id'] = 'provider-second'
        first['choices'][0]['message']['tool_calls'].append(second_call)
        script = [
            httpx.Response(200, json=first), response(content='第一項缺來源。'),
            response(content='第二項缺來源。'), response(content='請補充來源。'),
        ]

        def handle(request):
            requests.append(json.loads(request.content))
            return script.pop(0)
        base = self.model(handle)
        self.config.role_models = {'knowledge': ModelRoute(model='gemini-3.5-flash-lite', reasoning_effort='medium')}
        result = await DocumentAgent(self.config, base).run('alice', '兩項查詢')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(
            [r['model'] for r in requests],
            ['Kimi-K2.5', 'gemini-3.5-flash-lite', 'gemini-3.5-flash-lite', 'Kimi-K2.5'],
        )


class CatalogTests(unittest.TestCase):
    def test_profile_names_and_descriptions_are_validated(self):
        for value in (
            {'Bad Name': profiles()['small']},
            {'small': {'model': 'model', 'description': ''}},
        ):
            with self.assertRaises(ValidationError):
                Settings(state_dir='/tmp/private', model_profiles=value)
