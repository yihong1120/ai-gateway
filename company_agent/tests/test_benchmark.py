from __future__ import annotations

import unittest

from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage

from company_agent.evals.benchmark import Budget
from company_agent.evals.benchmark import MeteredModel
from company_agent.runtime.execution import DocumentAgent
from company_agent.tests import coordinator_tools
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_agent import ScriptedModel


class BudgetTests(unittest.TestCase):
    def test_reserve_settle_and_stop(self):
        budget = Budget(.1)
        budget.reserve(.08)
        with self.assertRaises(ValueError):
            budget.reserve(.03)
        budget.settle(.08, .005)
        self.assertAlmostEqual(budget.charged, .005)


class MeterTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_safe_metrics_leave_wrapper(self):
        class Fake:
            def bind_tools(self, *args, **kwargs): return self

            async def ainvoke(self, *args):
                return AIMessage(
                    content='ok', tool_calls=[
                        {'id': 'one', 'name': 'delegate_document', 'args': {'task': 'PRIVATE task'}},
                        {'id': 'two', 'name': 'PRIVATE unknown tool', 'args': {}},
                    ], usage_metadata={'input_tokens': 100, 'output_tokens': 5, 'total_tokens': 105},
                    response_metadata={
                        'headers': {'x-litellm-response-cost': '0.001', 'secret-provider-header': 'PRIVATE'},
                        'finish_reason': 'stop',
                    },
                )
        budget = Budget(1)
        model = MeteredModel(Fake(), 'test', {'input': .000001, 'output': .000001, 'cache': .0000001}, budget)
        response = await model.bind_tools(coordinator_tools()).ainvoke([HumanMessage(content='test')])
        self.assertNotIn('headers', response.response_metadata)
        self.assertNotIn('PRIVATE', str(model.calls))
        self.assertEqual(model.calls[0]['tool_names'], ['delegate_document', 'unknown_tool'])
        self.assertAlmostEqual(budget.charged, .001)


class CompactTests(unittest.IsolatedAsyncioTestCase, Fixture):
    async def test_compact_prompt_retains_safety_and_is_smaller(self):
        prompts = []
        for enabled in (False, True):
            self.config.compact_prompts = enabled
            model = ScriptedModel([AIMessage(content='請提供檔案。')])
            result = await DocumentAgent(self.config, model).run('alice', '你能做什麼？')
            self.assertEqual(result['status'], 'done')
            prompts.append(model.messages[0][0].content)
        self.assertLess(len(prompts[1]), len(prompts[0]))
        for phrase in ('不可信資料', '等待本人確認', '沒有期限不建立行事曆', '有資料依賴'):
            self.assertIn(phrase, prompts[1])
