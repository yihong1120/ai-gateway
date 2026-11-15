from __future__ import annotations

import unittest

from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from pydantic import ValidationError

from company_agent.runtime.execution import DocumentAgent
from company_agent.tests.test_agent import call
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_enterprise_completion import finish


class Sequence:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def bind_tools(self, tools, **kwargs):
        outer = self
        names = {t.name for t in tools}

        class Bound:
            async def ainvoke(self, messages):
                outer.calls.append(names)
                item = outer.steps.pop(0)
                return item(messages) if callable(item) else item
        return Bound()


class BudgetTests(unittest.IsolatedAsyncioTestCase, Fixture):
    async def test_limit_raises_native_error_and_keeps_artifacts(self):
        self.config.structured_completion = True
        self.config.max_steps = 2
        model = Sequence([
            call('delegate_document', {'task': '製作文字'}, 'a'), call(
                'create_text', {'name': 'draft.txt', 'content': '一次產製'}, 'b',
            ),
        ])
        result = await self.failed_job(DocumentAgent(self.config, model).run('alice', '製作文字'), ModelCallLimitExceededError)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(self.store.attempts(result['job_id']), 2)
        self.assertEqual([f['name'] for f in result['files']], ['draft.txt'])
        self.assertTrue(result['checkpoint'].next)
        self.assertEqual(self.native_error.thread_count, 2)

    async def test_config_change_cannot_increase_existing_budget(self):
        self.config.max_steps = 1
        self.config.structured_completion = True
        result = await self.failed_job(DocumentAgent(self.config, Sequence([call('delegate_document', {'task': '閱讀'}, 'a')])).run('alice', '閱讀'), ModelCallLimitExceededError)
        self.config.max_steps = 80
        model = Sequence([])
        with self.assertRaises(ModelCallLimitExceededError):
            await DocumentAgent(self.config, model).resume('alice', result['job_id'])
        self.assertEqual(self.store.attempts(result['job_id']), 1)
        self.assertEqual(model.calls, [])

    async def test_remaining_calls_do_not_remove_role_tools(self):
        self.config.max_steps = 8
        self.config.structured_completion = True
        self.file('a.txt', b'source')
        ids = []

        def created(messages):
            import json
            ids.append(json.loads(messages[-1].content)['id'])
            return finish('complete_assignment', ids=ids)
        model = Sequence([
            call('delegate_document', {'task': '讀取並產生草稿'}, 'a'),
            call('read_file', {'root': 'project', 'path': 'a.txt'}, 'b'),
            call('create_text', {'name': 'draft.txt', 'content': 'new'}, 'c'),
            created, lambda _:finish(ids=ids),
        ])
        result = await DocumentAgent(self.config, model).run('alice', '閱讀並建立草稿')
        self.assertEqual(result['status'], 'done', result)
        self.assertIn('create_text', model.calls[2])
        self.assertIn('delegate_document', model.calls[-1])
        self.assertEqual(result['budget']['used'], 5)
        self.assertNotIn('paused', result['budget'])
        self.assertEqual(len(result['files']), 1)

    async def test_blocked_outcome_does_not_pretend_call_limit_was_reached(self):
        self.config.max_steps = 8
        self.config.structured_completion = True
        model = Sequence([
            call('delegate_document', {'task': '讀取'}, 'a'),
            finish('complete_assignment', outcome='blocked', summary='缺少必要來源。'),
            finish(outcome='blocked', summary='請提供來源。'),
        ])
        result = await DocumentAgent(self.config, model).run('alice', '閱讀')
        self.assertEqual(result['status'], 'blocked', result)
        self.assertNotIn('paused', result['budget'])
        self.assertEqual(result['budget']['used'], 3)

    async def test_eighty_is_hard_ceiling(self):
        from company_agent.config import Settings
        with self.assertRaises(ValidationError):
            Settings(state_dir=str(self.private), max_steps=81)
