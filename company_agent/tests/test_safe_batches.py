from __future__ import annotations

import json
import unittest

from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage

from company_agent.runtime.execution import DocumentAgent
from company_agent.tests.test_agent import call
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_multi_agent import RawRoleModel


def batch(*messages):
    return AIMessage(content='', tool_calls=[c for m in messages for c in m.tool_calls])


class BatchTests(unittest.IsolatedAsyncioTestCase, Fixture):
    async def test_more_than_four_reads_merge_all_sources(self):
        for i in range(6):
            self.file(f'{i}.txt', str(i).encode())
        reads = batch(*[call('read_file', {'root': 'project', 'path': f'{i}.txt'}, str(i)) for i in range(6)])
        model = RawRoleModel(
            coordinator=[call('delegate_knowledge', {'task': '讀四檔'}, 'go'), AIMessage(content='完成')],
            knowledge=[reads, AIMessage(content='完成')],
        )
        result = await DocumentAgent(self.config, model).run('alice', '讀四檔')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(len(result['sources']), 6)

    async def test_two_reads_merge_sources_without_loss(self):
        self.file('a.txt', b'a')
        self.file('b.txt', b'b')
        model = RawRoleModel(
            coordinator=[call('delegate_knowledge', {'task': '讀兩檔'}, 'go'), AIMessage(content='完成')],
            knowledge=[
                batch(
                    call('read_file', {'root': 'project', 'path': 'a.txt'}, 'a'),
                    call('read_file', {'root': 'project', 'path': 'b.txt'}, 'b'),
                ), AIMessage(content='完成'),
            ],
        )
        result = await DocumentAgent(self.config, model).run('alice', '讀兩檔')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual({s['path'] for s in result['sources']}, {'a.txt', 'b.txt'})

    async def test_two_drafts_do_not_publish(self):
        model = RawRoleModel(
            coordinator=[call('delegate_document', {'task': '兩份草稿'}, 'go'), AIMessage(content='完成')],
            document=[
                batch(
                    call('create_text', {'name': 'a.txt', 'content': 'a'}, 'a'),
                    call('create_text', {'name': 'b.txt', 'content': 'b'}, 'b'),
                ), AIMessage(content='完成'),
            ],
        )
        result = await DocumentAgent(self.config, model).run('alice', '兩份草稿')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual({f['name'] for f in result['files']}, {'a.txt', 'b.txt'})
        self.assertFalse((self.root/'a.txt').exists())

    async def test_publish_in_native_batch_still_requires_approval(self):
        self.file('source.txt', b'source')

        def propose_and_read(messages):
            artifact = json.loads(messages[-1].content)
            return batch(
                call('propose_publish', {'artifact_id': artifact['id'], 'root': 'project', 'path': 'a.txt'}, 'publish'),
                call('read_file', {'root': 'project', 'path': 'source.txt'}, 'read'),
            )
        model = RawRoleModel(
            coordinator=[call('delegate_document', {'task': '工作'}, 'go')],
            document=[call('create_text', {'name': 'a.txt', 'content': 'a'}, 'draft'), propose_and_read],
        )
        pending = await DocumentAgent(self.config, model).run('alice', '製作並存檔，另讀取 source.txt')
        self.assertEqual(pending['status'], 'awaiting_approval', pending)
        self.assertEqual([s['path'] for s in pending['sources']], ['source.txt'])
        self.assertFalse((self.root / 'a.txt').exists())
        resumed = RawRoleModel(document=[AIMessage(content='已存檔')], coordinator=[AIMessage(content='完成')])
        done = await DocumentAgent(self.config, resumed).resume(
            'alice', pending['job_id'],
            {'proposal_id': pending['proposals'][0]['proposal_id'], 'decision': 'approve'},
        )
        self.assertEqual(done['status'], 'done', done)
        self.assertEqual((self.root / 'a.txt').read_text(), 'a')
        self.assertEqual(len(done['files']), 1)

    async def test_five_delegations_each_create_a_draft(self):
        def work(messages):
            task = json.loads(messages[1].content)['task']
            if isinstance(messages[-1], HumanMessage):
                return call('create_text', {'name': task + '.txt', 'content': task}, 'draft')
            return AIMessage(content='完成')
        model = RawRoleModel(
            coordinator=[
                batch(
                    *[
                        call('delegate_document', {'task': str(i)}, str(i))
                        for i in range(5)
                    ],
                ), AIMessage(content='完成'),
            ],
            document=[work] * 10,
        )
        result = await DocumentAgent(self.config, model).run('alice', '五份草稿')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(len(result['files']), 5)

    async def test_more_than_two_drafts(self):
        model = RawRoleModel(
            coordinator=[call('delegate_document', {'task': '三份草稿'}, 'go'), AIMessage(content='完成')],
            document=[
                batch(
                    *[
                        call('create_text', {'name': f'{i}.txt', 'content': str(i)}, str(i))
                        for i in range(3)
                    ],
                ), AIMessage(content='完成'),
            ],
        )
        result = await DocumentAgent(self.config, model).run('alice', '三份草稿')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(len(result['files']), 3)
