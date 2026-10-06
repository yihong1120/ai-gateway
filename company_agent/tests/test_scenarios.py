from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage

from company_agent.evals.benchmark import Budget
from company_agent.evals.benchmark import verify
from company_agent.evals.scenario_suite import BilledMeter
from company_agent.evals.scenario_suite import checks_for
from company_agent.evals.scenario_suite import prepare
from company_agent.runtime.models import UserBilledModel
from company_agent.tests import coordinator_tools
from company_agent.tests.test_agent import Fixture


class ScenarioMeterTests(unittest.IsolatedAsyncioTestCase):
    async def test_role_routing_preserves_shared_budget_identity_and_analysis_options(self):
        from company_agent.config import ModelRoute, Settings
        from company_agent.runtime.models import WebBilledModel, routed_model, select_model
        from langchain_core.runnables import RunnableLambda
        from unittest.mock import AsyncMock, patch
        seen = []

        class Client:
            model_name = 'base'
            reasoning_effort = None
            max_tokens = 4096

            def model_copy(self, update):
                clone = Client()
                for key, value in update.items():
                    setattr(clone, key, value)
                return clone

            def bind(self, **kwargs):
                seen.append(kwargs)
                return RunnableLambda(self.ainvoke)

            def bind_tools(self, tools, **kwargs):
                seen.append(kwargs)
                return RunnableLambda(self.ainvoke)

            async def ainvoke(self, messages, config=None):
                return AIMessage(content='ok', usage_metadata={'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12})
        model = WebBilledModel('owner', 'verified-subject', Client())
        budget = Budget(.1)
        rate = {'input': .000001, 'output': .000001, 'cache': .000001}
        meter = BilledMeter(model, 'base', rate, budget)
        meter.meter.rate_catalog = {'base': rate, 'role-model': rate}
        configured = routed_model(
            meter, Settings(
                state_dir='/unused-test-state',
                role_models={'coordinator': ModelRoute(model='role-model', reasoning_effort='medium')},
            ),
        )
        role = select_model(configured, 'coordinator')
        self.assertEqual(role.client.model_name, 'role-model')
        self.assertEqual(role.client.reasoning_effort, 'medium')
        self.assertIs(role.meter.calls, meter.meter.calls)
        self.assertIs(role.meter.budget, budget)
        self.assertIsInstance(role.meter.model, WebBilledModel)
        user = {'oauth': {'oidc': {'sub': 'verified-subject'}}}
        with patch('company_agent.runtime.context.verified_user', new=AsyncMock(return_value=user)) as verified:
            await role.bind_tools(coordinator_tools()).ainvoke([HumanMessage(content='test')])
            await role.analyze([HumanMessage(content='test')], schema={'type': 'object'}, max_output_tokens=12)
            await role.summarize([HumanMessage(content='test')])
        self.assertEqual(verified.await_count, 3)
        self.assertEqual(len(meter.meter.calls), 3)
        self.assertEqual(seen[1]['max_tokens'], 12)
        self.assertEqual(seen[1]['response_format']['type'], 'json_schema')

    async def test_meter_preserves_verified_billing_and_budget(self):
        seen = []

        class Client:
            model_name = 'test-model'
            reasoning_effort = None
            max_tokens = 4096

            def bind(self, **kwargs):
                seen.append(kwargs)
                return self

            def bind_tools(self, tools, **kwargs):
                seen.append(kwargs)
                return self

            async def ainvoke(self, messages, config=None):
                return AIMessage(content='ok', usage_metadata={'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12})
        inner = UserBilledModel('owner', 'verified-subject', Client())
        meter = BilledMeter(inner, 'test', {'input': .000001, 'output': .000001, 'cache': .000001}, Budget(.1))
        self.assertIsInstance(meter, UserBilledModel)
        await meter.bind_tools(coordinator_tools()).ainvoke([HumanMessage(content='test')])
        self.assertEqual(seen, [{'user': 'verified-subject'}])
        with self.assertRaises(ValueError):
            meter.bind_tools(coordinator_tools(), user='someone-else')
        await meter.analyze([HumanMessage(content='synthetic image question')])
        self.assertEqual(seen[-1], {'user': 'verified-subject', 'response_format': {'type': 'json_object'}})
        self.assertEqual(len(meter.meter.calls), 2)
        self.assertGreater(meter.meter.budget.charged, 0)

    def test_missing_input_is_expected_outcome_not_fake_success(self):
        result = {'files': [], 'status': 'needs_input', 'answer': '請提供上月營收表。', 'proposals': []}
        self.assertTrue(all(checks_for(None, result, 'missing_source', 'owner').values()))
        result['status'] = 'done'
        self.assertFalse(all(checks_for(None, result, 'missing_source', 'owner').values()))


class ScenarioPostgresTests(Fixture):
    def test_numbered_word_paragraphs_do_not_merge_total_with_next_item(self):
        from company_agent.services.office.documents import create_word
        fake = SimpleNamespace(settings=self.config, store=self.store)
        self.store.audit('alice', self.job, 'analyze_spreadsheet', 'done')
        for total, expected in [(30, True), (300, False)]:
            artifact = self.store.artifact(
                'alice', self.job, 'summary.docx', create_word(
                    '統計', [
                        {'text': f'1. 數值合計：{total}', 'heading': False},
                        {'text': '2. 數值筆數：2 筆', 'heading': False},
                        {'text': '排除非數值文字。', 'heading': False},
                    ],
                ),
            )
            result = {
                'files': [artifact], 'status': 'done', 'job_id': self.job, 'proposals': [],
                'artifact_review': {'unreviewed_ids': []}, 'agent_visits': ['data', 'document', 'reviewer'],
            }
            self.assertEqual(checks_for(fake, result, 'data_to_document', 'alice')['numbers'], expected)

    def test_difficult_statistics_rejects_formula_inclusion_and_missing_tool_evidence(self):
        from company_agent.services.office.documents import create_spreadsheet
        fake = SimpleNamespace(settings=self.config, store=self.store)

        def result(total):
            artifact = self.store.artifact(
                'alice', self.job, 'checked.xlsx', create_spreadsheet([
                    {'name': '結果', 'rows': [['合計', total], ['筆數', 3]]},
                ]),
            )
            return {
                'files': [artifact], 'status': 'done', 'job_id': self.job, 'proposals': [],
                'artifact_review': {'unreviewed_ids': []}, 'answer': '排除公式、文字與空白。',
            }
        missing = checks_for(fake, result(15), 'mixed_statistics', 'alice')
        self.assertTrue(missing['exact_values'])
        self.assertFalse(missing['statistics_tool_used'])
        self.store.audit('alice', self.job, 'analyze_spreadsheet', 'done')
        wrong = checks_for(fake, result(30), 'mixed_statistics', 'alice')
        self.assertFalse(wrong['exact_values'])
        self.assertTrue(wrong['statistics_tool_used'])

    def test_legacy_verifier_uses_postgresql_and_explicit_owner(self):
        result = {
            'files': [], 'status': 'done', 'sources': [{'path': 'source.txt'}],
            'job_id': self.job, 'proposals': [],
        }
        fake = SimpleNamespace(settings=self.config, store=self.store)
        checks = verify(fake, result, 'documents', 'alice')
        self.assertTrue(checks['no_external_write_proposal'])
        self.assertFalse(checks['report.docx'])

    def test_test_input_is_synthetic_and_chart_fixture_is_real(self):
        from openpyxl import load_workbook
        with tempfile.TemporaryDirectory() as temporary:
            docs = Path(temporary)/'sources'
            before = prepare(docs, 'excel_edit')
            self.assertEqual(len(before), 4)
            book = load_workbook(docs/'source.xlsx')
            self.assertEqual(book['工程']['B4'].value, '=SUM(B2:B3)')
            self.assertEqual(len(book['工程']._charts), 1)


class ScenarioStopTests(unittest.IsolatedAsyncioTestCase, Fixture):
    async def test_rate_limit_stops_remaining_scenarios_without_calls(self):
        from unittest.mock import AsyncMock, patch
        from company_agent.evals.scenario_suite import run
        import httpx
        from openai import RateLimitError

        class Client:
            model_name = 'test-model'
            reasoning_effort = None
            max_tokens = 4096
            model_name = 'Kimi-K2.5'
            calls = 0
            def bind_tools(self, *args, **kwargs): return self

            async def ainvoke(self, messages, config=None):
                self.calls += 1
                raise RateLimitError(
                    'secret provider response', response=httpx.Response(
                        429,
                        request=httpx.Request('POST', 'https://gateway.example/v1'),
                    ), body=None,
                )
        client = Client()
        model = UserBilledModel('alice', 'verified', client)
        output = self.base/'scenario-output'
        with patch('company_agent.api.service.service', return_value=SimpleNamespace(model_factory=lambda _: model)), \
            patch('company_agent.runtime.context.verified_user', new=AsyncMock(return_value={'id': 'alice'})), \
                patch('company_agent.evals.scenario_suite.close_model', new=AsyncMock()):
            await run('alice', output, ['missing_source', 'documents', 'word_edit'], .5)
        report = json.loads((output/'report.json').read_text())
        self.assertEqual(client.calls, 1)
        self.assertEqual([r['status'] for r in report['runs']], ['error', 'not_run', 'not_run'])
        self.assertNotIn('secret provider response', json.dumps(report))
        self.assertEqual(report['runs'][0]['error_type'], 'RateLimitError')
        result = report['runs'][0]['diagnostics']['result']
        self.assertEqual(report['runs'][0]['diagnostics']['job_status'], 'error')
        self.assertTrue(result['resumable'])
        self.assertIn('429', result['answer'])
