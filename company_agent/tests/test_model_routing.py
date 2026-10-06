"""Provider projection, verified per-role billing and checkpoint policy tests."""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import httpx
from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from langchain_core.messages import ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from openai import RateLimitError
from pydantic import ValidationError

from company_agent.config import ModelRoute
from company_agent.config import Settings
from company_agent.runtime.execution import DocumentAgent
from company_agent.runtime.gemini import gemini_schema
from company_agent.runtime.gemini import provider_tools
from company_agent.runtime.gemini import signed_history
from company_agent.runtime.gemini import valid_provider_id
from company_agent.runtime.models import close_model
from company_agent.runtime.models import gateway_for_verified_user
from company_agent.runtime.models import model_policy
from company_agent.runtime.models import routed_model
from company_agent.runtime.models import select_model
from company_agent.runtime.models import WebBilledModel
from company_agent.services.files.tools import file_tools
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_user_billing import response
from company_agent.tests.test_user_billing import verified


class SchemaTests(unittest.TestCase):
    def test_signed_history_survives_serial_batches_without_mutating_receipts(self):
        signature = 'ABcd+/' * 100 + '=='
        self.assertTrue(valid_provider_id('call_provider__thought__' + signature))
        self.assertFalse(valid_provider_id('x' * 129))
        self.assertFalse(valid_provider_id('call_a__thought__' + 'A' * 32768))
        mapping = {
            'call_first': 'call_provider__thought__' + signature,
            'call_second': 'call_provider2',
        }
        first = AIMessage(
            content='', tool_calls=[{'name': 'read_file', 'args': {}, 'id': 'call_first'}],
            response_metadata={'provider_tool_ids_by_call': mapping},
        )
        second = AIMessage(
            content='', tool_calls=[{'name': 'read_file', 'args': {}, 'id': 'call_second'}],
            response_metadata={'provider_tool_ids_by_call': mapping},
        )
        receipt = ToolMessage(content='ok', tool_call_id='call_first')
        projected = signed_history([first, receipt, second])
        self.assertEqual(projected[0].tool_calls[0]['id'], 'call_first__thought__' + signature)
        self.assertEqual(projected[1].tool_call_id, projected[0].tool_calls[0]['id'])
        self.assertEqual(projected[2].tool_calls[0]['id'], 'call_second')
        self.assertEqual(first.tool_calls[0]['id'], 'call_first')
        self.assertEqual(receipt.tool_call_id, 'call_first')

    def test_signature_mapping_and_authoritative_empty_mapping(self):
        signature = 'ABcd+/' * 100 + '=='
        ai = AIMessage(
            content='', tool_calls=[
                {'name': 'read_file', 'args': {}, 'id': 'canonical'},
                {'name': 'read_file', 'args': {}, 'id': 'unsigned'},
            ], response_metadata={'provider_tool_ids_by_call': {'canonical': 'provider__thought__' + signature}},
        )
        receipt = ToolMessage(content='ok', tool_call_id='canonical')
        projected = signed_history([ai, receipt])
        self.assertEqual(projected[0].tool_calls[0]['id'], 'canonical__thought__' + signature)
        self.assertEqual(projected[1].tool_call_id, projected[0].tool_calls[0]['id'])
        self.assertEqual(projected[0].tool_calls[1]['id'], 'unsigned')
        self.assertEqual(ai.tool_calls[0]['id'], 'canonical')
        ai.response_metadata['provider_tool_ids_by_call'] = {}
        self.assertEqual(signed_history([ai, receipt])[1].tool_call_id, 'canonical')

    def test_projection_preserves_names_unions_and_local_limits(self):
        original = {
            'type': 'object', 'properties': {
                'maxItems': {
                    'type': 'array', 'minItems': 1, 'maxItems': 2,
                    'items': {'anyOf': [{'type': 'string'}, {'type': 'number'}]},
                },
            },
            'required': ['maxItems'],
        }
        projected = gemini_schema(original)
        value = projected['properties']['maxItems']
        self.assertNotIn('maxItems', value)
        self.assertIn('maxItems=2', value['description'])
        self.assertEqual(value['items'], original['properties']['maxItems']['items'])
        self.assertEqual(original['properties']['maxItems']['maxItems'], 2)
        self.assertEqual(projected['required'], ['maxItems'])

    def test_original_tools_still_reject_oversized_arrays(self):
        tool = next(t for t in file_tools() if t.name == 'create_cad')
        before = convert_to_openai_tool(tool)
        provider_tools([tool], 'gemini-3.5-flash-lite')
        self.assertEqual(convert_to_openai_tool(tool), before)
        with self.assertRaises(ValidationError):
            tool.args_schema.model_validate({
                'name': 'draft.dxf', 'units': 'mm',
                'entities': [{'kind': 'line', 'point': [0, 0, 0]}], 'runtime': None,
            })
        self.assertEqual(provider_tools([tool], 'Kimi-K2.5'), [tool])

    def test_configuration_rejects_unknown_roles_purposes_and_effort(self):
        for fields in (
            {'role_models': {'typo': {'model': 'x'}}},
            {'purpose_models': {'typo': {'model': 'x'}}},
            {'role_models': {'data': {'model': 'x', 'reasoning_effort': 'typo'}}},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                Settings(state_dir='/tmp/private', **fields)


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_http_models_effort_and_identity(self):
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return response(content='{}')
        base = gateway_for_verified_user(
            verified(), 'https://gateway.example/v1', 'key',
            'Kimi-K2.5', httpx.MockTransport(handle),
        )
        self.addAsyncCleanup(close_model, base)
        settings = Settings(
            state_dir='/tmp/private', role_models={
                'data': ModelRoute(model='gemini-3.5-flash-lite', reasoning_effort='medium', max_tokens=8192),
            },
            purpose_models={
                name: ModelRoute(model='purpose-'+name, max_tokens=3072)
                for name in ('summary', 'vision', 'attachment', 'attachment_plan')
            },
        )
        root = routed_model(base, settings)
        tools = [t for t in file_tools() if t.name == 'create_spreadsheet']
        await select_model(root, 'data').bind_tools(tools).ainvoke([HumanMessage(content='test')])
        await select_model(root, 'coordinator').bind_tools(tools).ainvoke([HumanMessage(content='test')])
        for name in settings.purpose_models:
            model = select_model(root, name)
            if name == 'summary':
                await model.summarize('test')
            else:
                await model.analyze([HumanMessage(content='test')])
        self.assertEqual(
            [r['model'] for r in requests],
            [
                'gemini-3.5-flash-lite', 'Kimi-K2.5',
                'purpose-summary', 'purpose-vision', 'purpose-attachment', 'purpose-attachment_plan',
            ],
        )
        self.assertTrue(all(r['user'] == 'oidc-alice' for r in requests))
        self.assertTrue(all(r['num_retries'] == 0 and r['fallbacks'] == [] for r in requests))
        self.assertEqual(requests[0]['reasoning_effort'], 'medium')
        self.assertEqual(requests[0].get('max_completion_tokens', requests[0].get('max_tokens')), 8192)
        self.assertNotIn('maxItems"', json.dumps(requests[0]['tools']))
        self.assertIn('maxItems"', json.dumps(requests[1]['tools']))
        self.assertEqual(base.client.model_name, 'Kimi-K2.5')
        self.assertIs(select_model(root, 'data').client.http_async_client, base.client.http_async_client)

    async def test_web_routes_reverify_identity_before_invocation(self):
        base = gateway_for_verified_user(
            verified(), 'https://gateway.example/v1', 'key', 'default',
            httpx.MockTransport(lambda _: response()),
        )
        self.addAsyncCleanup(close_model, base)
        web = WebBilledModel(base.owner, base.subject, base.client)
        root = routed_model(
            web, Settings(
                state_dir='/tmp/private', purpose_models={
                    'vision': ModelRoute(model='gemini-3.8-flash'),
                },
            ),
        )
        model = select_model(root, 'vision')
        self.assertIsInstance(model, WebBilledModel)
        with patch('company_agent.runtime.context.verified_user', AsyncMock(return_value=verified('changed'))):
            with self.assertRaises(ValueError):
                await model.analyze([HumanMessage(content='test')])

    async def test_all_web_invocation_paths_recheck_identity_each_time(self):
        requests = []
        base = gateway_for_verified_user(
            verified(), 'https://gateway.example/v1', 'key',
            'gemini-3.5-flash-lite', httpx.MockTransport(
                lambda request: (requests.append(request), response(content='{}'))[1],
            ),
        )
        self.addAsyncCleanup(close_model, base)
        model = WebBilledModel(base.owner, base.subject, base.client)
        tool_call = model.bind_tools([t for t in file_tools() if t.name == 'read_file']).ainvoke
        for invoke in (tool_call, model.analyze, model.summarize):
            with self.subTest(path=invoke.__name__), patch(
                'company_agent.runtime.context.verified_user',
                AsyncMock(side_effect=[verified(), verified('changed')]),
            ) as check:
                before = len(requests)
                await invoke([HumanMessage(content='test')])
                with self.assertRaises(ValueError):
                    await invoke([HumanMessage(content='test')])
                self.assertEqual(check.await_count, 2)
                self.assertEqual(len(requests), before + 1)


class RoutingGraphTests(unittest.IsolatedAsyncioTestCase, Fixture):
    async def test_attachment_adapter_selects_parse_and_plan_routes_and_counts_both(self):
        from company_agent.runtime.context import Context
        from company_agent.services.eip.processing import BilledPipeAdapter

        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return response(content='{}')
        base = gateway_for_verified_user(
            verified(), 'https://gateway.example/v1', 'key', 'default',
            httpx.MockTransport(handle),
        )
        self.addAsyncCleanup(close_model, base)
        self.config.purpose_models = {
            'attachment': ModelRoute(model='DeepSeek-V4-Flash'),
            'attachment_plan': ModelRoute(model='gemini-3.5-flash-lite', reasoning_effort='medium'),
        }
        ctx = Context('alice', self.job, self.config, self.store, routed_model(base, self.config))
        pipe = SimpleNamespace(
            valves=SimpleNamespace(cloud_ocr_text_max_input_characters=1000),
            _cloud_json_object=json.loads, _cloud_ocr_text_prompt=lambda kind: 'extract',
        )
        adapter = BilledPipeAdapter(ctx, pipe, SimpleNamespace(INSTRUCTION_PLANNER_PROMPT='plan'))
        with patch('company_agent.services.eip.processing.binding', AsyncMock()):
            await adapter.analyze(SimpleNamespace(text='synthetic document', kind='pdf'))
            await adapter.plan('整理附件')
        self.assertEqual([r['model'] for r in requests], ['DeepSeek-V4-Flash', 'gemini-3.5-flash-lite'])
        self.assertEqual({r['user'] for r in requests}, {'oidc-alice'})
        self.assertEqual(self.store.attempts(self.job), 2)

    async def test_signed_call_roundtrip_uses_canonical_id_and_original_signature(self):
        signature = 'ABcd+/' * 80 + '=='
        requests = []

        def handle(request):
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                result = response('delegate_knowledge', {'task': '確認缺少資料'})
                payload = result.json()
                payload['choices'][0]['message']['tool_calls'][0]['id'] += '__thought__' + signature
                return httpx.Response(200, json=payload)
            if len(requests) == 2:
                return response(content='請提供來源。')
            calls = [m for m in body['messages'] if m.get('tool_calls')]
            receipts = [m for m in body['messages'] if m['role'] == 'tool']
            wire_id = calls[0]['tool_calls'][0]['id']
            self.assertTrue(wire_id.startswith('call_'))
            self.assertTrue(wire_id.endswith('__thought__' + signature))
            self.assertEqual(receipts[0]['tool_call_id'], wire_id)
            return response(content='請提供來源。')
        base = gateway_for_verified_user(
            verified(), 'https://gateway.example/v1', 'key',
            'gemini-3.5-flash-lite', httpx.MockTransport(handle),
        )
        self.addAsyncCleanup(close_model, base)
        result = await DocumentAgent(self.config, base).run('alice', '查詢來源')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(len(requests), 3)

    async def test_graph_routes_and_resume_rejects_changed_policy(self):
        requests = []
        script = [
            response('delegate_knowledge', {'task': '確認來源'}),
            httpx.Response(429, json={'error': {'message': 'limited'}}),
        ]

        def handle(request):
            requests.append(json.loads(request.content))
            return script.pop(0)
        base = gateway_for_verified_user(
            verified(), 'https://gateway.example/v1', 'key', 'default',
            httpx.MockTransport(handle),
        )
        self.addAsyncCleanup(close_model, base)
        self.config.role_models = {'knowledge': ModelRoute(model='gemini-3.5-flash-lite', reasoning_effort='medium')}
        agent = DocumentAgent(self.config, base)
        result = await self.failed_job(agent.run('alice', '查詢來源'), RateLimitError)
        self.assertEqual([r['model'] for r in requests], ['default', 'gemini-3.5-flash-lite'])
        original_policy = model_policy(agent.model)
        self.config.role_models = {'knowledge': ModelRoute(model='gemini-3.8-flash')}
        changed = DocumentAgent(self.config, base)
        with self.assertRaisesRegex(ValueError, '模型分流設定已變更'):
            await changed.resume('alice', result['job_id'])
        self.assertEqual(len(requests), 2)
        self.assertEqual(original_policy['routes']['knowledge']['model'], 'gemini-3.5-flash-lite')
