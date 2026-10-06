"""Native child checkpoints, concurrent isolation and independent approvals."""
from __future__ import annotations

import asyncio
import json
import unittest

from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from langchain_core.messages import messages_to_dict

from company_agent.graph import build_graph
from company_agent.runtime.execution import DocumentAgent
from company_agent.tests.postgres_fixture import checkpointer
from company_agent.tests.test_agent import call
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_multi_agent import RawRoleModel
from company_agent.tests.test_safe_batches import batch


class SubagentTests(unittest.IsolatedAsyncioTestCase, Fixture):
    async def test_same_role_runs_concurrently_in_separate_native_checkpoints(self):
        both_entered = asyncio.Event()
        entered = set()
        for task in ('first', 'second'):
            self.file(task + '.txt', ('PRIVATE_' + task).encode())

        async def read(messages):
            task = json.loads(messages[-1].content)['task']
            entered.add(task)
            if len(entered) == 2:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), 5)
            return call('read_file', {'root': 'project', 'path': task + '.txt'}, 'read')

        def finish(messages):
            task = json.loads(messages[1].content)['task']
            text = str(messages_to_dict(messages))
            self.assertIn('PRIVATE_' + task, text)
            other = 'second' if task == 'first' else 'first'
            self.assertNotIn('PRIVATE_' + other, text)
            return AIMessage(content='完成 ' + task)

        model = RawRoleModel(
            coordinator=[
                batch(
                    *[
                        call('delegate_knowledge', {'task': task}, task)
                        for task in ('first', 'second')
                    ],
                ), AIMessage(content='完成'),
            ],
            knowledge=[read, read, finish, finish],
        )
        agent = DocumentAgent(self.config, model)
        result = await agent.run('alice', '兩項獨立閱讀工作')
        self.assertEqual(result['status'], 'done')
        self.assertEqual({s['path'] for s in result['sources']}, {'first.txt', 'second.txt'})
        async with checkpointer(self.store) as saver:
            parent = await build_graph(model, saver).aget_state({'configurable': {'thread_id': result['job_id']}})
            children = {}
            async for saved in saver.alist({'configurable': {'thread_id': result['job_id']}}):
                namespace = saved.config['configurable']['checkpoint_ns']
                if namespace:
                    children.setdefault(namespace, saved.checkpoint['channel_values'])
        self.assertEqual(len(children), 2)
        self.assertEqual({m.name for m in parent.values['messages'] if isinstance(m, AIMessage)}, {'coordinator'})
        self.assertNotIn('PRIVATE_', str(messages_to_dict(parent.values['messages'])))
        self.assertEqual(len(parent.values['reports']), 2)
        for child in children.values():
            self.assertEqual(len([m for m in child['messages'] if isinstance(m, HumanMessage)]), 1)
            self.assertEqual({m.name for m in child['messages'] if isinstance(m, AIMessage)}, {'knowledge'})
        self.assertEqual(result['model_steps'], 6)

    async def test_two_child_approvals_can_be_resumed_one_at_a_time(self):
        def metered(message):
            return message.model_copy(
                update={
                    'usage_metadata': {
                        'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12,
                    },
                },
            )

        def work(messages):
            task = json.loads(messages[1].content)['task']
            if isinstance(messages[-1], HumanMessage):
                return metered(call('create_text', {'name': task + '.txt', 'content': task}, 'draft'))
            artifact = json.loads(messages[-1].content)
            return metered(
                call(
                    'propose_publish', {
                        'artifact_id': artifact['id'], 'root': 'project', 'path': task + '.txt',
                    }, 'publish',
                ),
            )
        model = RawRoleModel(
            coordinator=[
                metered(
                    batch(
                        *[
                            call('delegate_document', {'task': task}, task)
                            for task in ('first', 'second')
                        ],
                    ),
                ),
            ],
            document=[work] * 4,
        )
        pending = await DocumentAgent(self.config, model).run('alice', '建立 first.txt 與 second.txt 並申請存檔')
        self.assertEqual(pending['status'], 'awaiting_approval')
        self.assertEqual(len(pending['proposals']), 2)
        self.assertEqual(len(pending['files']), 2)
        self.assertEqual(pending['usage'], {'input_tokens': 50, 'output_tokens': 10})
        self.assertEqual(pending['agent_visits'], ['coordinator', *['document'] * 4])
        self.assertFalse(list(self.root.iterdir()))
        first, second = pending['proposals']
        resumed = RawRoleModel(
            document=[metered(AIMessage(content='已處理')), metered(AIMessage(content='已處理'))],
            coordinator=[metered(AIMessage(content='完成'))],
        )
        agent = DocumentAgent(self.config, resumed)
        partial = await agent.resume(
            'alice', pending['job_id'],
            {'proposal_id': first['proposal_id'], 'decision': 'approve'},
        )
        self.assertEqual(partial['status'], 'awaiting_approval')
        self.assertEqual(partial['usage'], {'input_tokens': 60, 'output_tokens': 12})
        self.assertEqual([p['proposal_id'] for p in partial['proposals']], [second['proposal_id']])
        self.assertTrue((self.root / first['path']).exists())
        self.assertFalse((self.root / second['path']).exists())
        done = await agent.resume(
            'alice', pending['job_id'],
            {'proposal_id': second['proposal_id'], 'decision': 'reject'},
        )
        self.assertEqual(done['status'], 'done')
        self.assertEqual({f['id'] for f in done['files']}, {f['id'] for f in pending['files']})
        self.assertEqual(resumed.invocations, ['document', 'document', 'coordinator'])
        self.assertFalse((self.root / second['path']).exists())
        self.assertEqual(done['model_steps'], 8)
        self.assertEqual(done['usage'], {'input_tokens': 80, 'output_tokens': 16})
        self.assertEqual(done['notices'], ['使用者拒絕存檔；草稿保留，不要重新提出相同申請。'])
