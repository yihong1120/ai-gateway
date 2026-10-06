from __future__ import annotations

import json
import unittest

from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from langchain_core.messages import messages_to_dict
from langchain_core.messages import ToolMessage
from pydantic import ValidationError

from company_agent.runtime.completion import review_evidence
from company_agent.runtime.execution import DocumentAgent
from company_agent.services.office.schemas import Slides
from company_agent.services.office.schemas import Spreadsheet
from company_agent.services.office.schemas import Word
from company_agent.tests.test_agent import call
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_multi_agent import RawRoleModel
from company_agent.tests.test_safe_batches import batch


class ProtocolTests(unittest.IsolatedAsyncioTestCase, Fixture):
    async def test_provider_ids_can_repeat_across_invocations_without_overwrite(self):
        # Providers can reuse BOTH message IDs and tool IDs across separate requests.
        def response(name, args):
            return call(name, args, '0').model_copy(update={'id': 'provider-message'})
        model = RawRoleModel(
            coordinator=[response('delegate_document', {'task': '兩份草稿'}), AIMessage(content='完成')],
            document=[
                response('create_text', {'name': 'a.txt', 'content': 'a'}),
                response('create_text', {'name': 'b.txt', 'content': 'b'}), AIMessage(content='完成'),
            ],
        )
        result = await DocumentAgent(self.config, model).run('alice', '兩份草稿')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual({f['name'] for f in result['files']}, {'a.txt', 'b.txt'})
        self.assertEqual(result['model_steps'], 5)

    async def test_duplicate_provider_ids_within_response_have_distinct_receipts(self):
        def done(messages):
            receipts = [m for m in messages if isinstance(m, ToolMessage)]
            self.assertEqual(len(receipts), 2)
            self.assertEqual(len({m.tool_call_id for m in receipts}), 2)
            return AIMessage(content='完成')
        model = RawRoleModel(
            coordinator=[call('delegate_document', {'task': '草稿'}, 'go'), AIMessage(content='完成')],
            document=[
                batch(
                    call('create_text', {'name': 'a.txt', 'content': 'a'}, 'duplicate'),
                    call('create_text', {'name': 'b.txt', 'content': 'b'}, 'duplicate'),
                ), done,
            ],
        )
        result = await DocumentAgent(self.config, model).run('alice', '工作')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual({f['name'] for f in result['files']}, {'a.txt', 'b.txt'})

    async def test_batch_dispatch_preserves_results_and_isolates_raw_context(self):
        self.file('a.txt', b'PRIVATE_FIRST_ROLE_SENTINEL')

        def document(messages):
            text = json.dumps(messages_to_dict(messages))
            self.assertNotIn('PRIVATE_FIRST_ROLE_SENTINEL', text)
            self.assertNotIn('ONLY_KNOWLEDGE_THOUGHT', text)
            return call('create_text', {'name': 'b.txt', 'content': 'SECOND_ROLE_RESULT'}, '0')

        def coordinate(messages):
            receipts = [json.loads(m.content) for m in messages if isinstance(m, ToolMessage)]
            self.assertEqual([r['status'] for r in receipts], ['completed', 'completed'])
            self.assertNotIn('PRIVATE_FIRST_ROLE_SENTINEL', json.dumps(messages_to_dict(messages)))
            return AIMessage(content='完成')
        model = RawRoleModel(
            coordinator=[
                batch(
                    call('delegate_knowledge', {'task': '讀 a.txt'}, '0'),
                    call('delegate_document', {'task': '獨立建立 b.txt'}, '1'),
                ), coordinate,
            ],
            knowledge=[
                call('read_file', {'root': 'project', 'path': 'a.txt'}, '0'),
                AIMessage(content='ONLY_KNOWLEDGE_THOUGHT'),
            ],
            document=[document, AIMessage(content='完成')],
        )
        result = await DocumentAgent(self.config, model).run('alice', '兩件獨立工作')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(model.invocations[0], 'coordinator')
        self.assertEqual(model.invocations[-1], 'coordinator')
        self.assertCountEqual(model.invocations[1:-1], ['knowledge', 'knowledge', 'document', 'document'])
        self.assertEqual([t['status'] for t in result['delegations']], ['completed', 'completed'])

    async def test_batch_bad_delegation_does_not_discard_other_role(self):
        model = RawRoleModel(
            coordinator=[
                batch(
                    call('delegate_knowledge', {'task': 'x', 'owner': 'admin'}, '0'),
                    call('delegate_document', {'task': '建立草稿'}, '1'),
                ), AIMessage(content='部分完成'),
            ],
            document=[call('create_text', {'name': 'a.txt', 'content': 'a'}, '0'), AIMessage(content='完成')],
        )
        result = await DocumentAgent(self.config, model).run('alice', '工作')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(len(result['tool_feedback']), 1)
        self.assertEqual(len(result['files']), 1)
        self.assertNotIn('knowledge', model.invocations)

    async def test_same_role_batch_has_distinct_assignments_and_contexts(self):
        def work(messages):
            task = json.loads(messages[1].content)['task']
            text = json.dumps(messages_to_dict(messages))
            if task == 'second':
                self.assertNotIn('FIRST_PRIVATE_VALUE', text)
                self.assertNotIn('FIRST_PRIVATE_THOUGHT', text)
            if isinstance(messages[-1], HumanMessage):
                return call(
                    'create_text', {
                        'name': task + '.txt',
                        'content': 'FIRST_PRIVATE_VALUE' if task == 'first' else 'SECOND',
                    }, '0',
                )
            return AIMessage(content='FIRST_PRIVATE_THOUGHT' if task == 'first' else '完成 second')

        def complete(messages):
            results = [json.loads(m.content) for m in messages if isinstance(m, ToolMessage)]
            self.assertEqual([r['task'] for r in results], ['first', 'second'])
            self.assertEqual([r['status'] for r in results], ['completed', 'completed'])
            return AIMessage(content='完成')
        model = RawRoleModel(
            coordinator=[
                batch(
                    call('delegate_document', {'task': 'first'}, '0'),
                    call('delegate_document', {'task': 'second'}, '1'),
                ), complete,
            ],
            document=[work] * 4,
        )
        result = await DocumentAgent(self.config, model).run('alice', '兩份獨立草稿')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual({f['name'] for f in result['files']}, {'first.txt', 'second.txt'})
        self.assertEqual([t['status'] for t in result['delegations']], ['completed', 'completed'])

    async def test_same_role_completed_sibling_is_not_replayed_after_approval(self):
        def work(messages):
            task = json.loads(messages[1].content)['task']
            if task == 'second':
                return AIMessage(content='第二項完成')
            if isinstance(messages[-1], HumanMessage):
                return call('create_text', {'name': 'one.txt', 'content': 'one'}, '0')
            artifact = json.loads(messages[-1].content)
            return call('propose_publish', {'artifact_id': artifact['id'], 'root': 'project', 'path': 'one.txt'}, '0')
        model = RawRoleModel(
            coordinator=[
                batch(
                    call('delegate_document', {'task': 'first'}, '0'),
                    call('delegate_document', {'task': 'second'}, '1'),
                ),
            ],
            document=[work] * 3,
        )
        pending = await DocumentAgent(self.config, model).run('alice', '存 one.txt，另做獨立第二項')
        self.assertEqual(pending['status'], 'awaiting_approval', pending)
        self.assertEqual([t['status'] for t in pending['delegations']], ['awaiting_approval', 'completed'])
        resumed = RawRoleModel(document=[AIMessage(content='第一項完成')], coordinator=[AIMessage(content='完成')])
        done = await DocumentAgent(self.config, resumed).resume(
            'alice', pending['job_id'],
            {'proposal_id': pending['proposals'][0]['proposal_id'], 'decision': 'approve'},
        )
        self.assertEqual(done['status'], 'done', done)
        self.assertEqual(resumed.invocations, ['document', 'coordinator'])
        self.assertEqual((self.root/'one.txt').read_text(), 'one')
        self.assertEqual(len(done['files']), 1)
        self.assertEqual([t['status'] for t in done['delegations']], ['completed', 'completed'])

    async def test_batch_survives_approval_resume_without_replaying_first_role(self):
        def propose(messages):
            artifact = json.loads(messages[-1].content)
            return call('propose_publish', {'artifact_id': artifact['id'], 'root': 'project', 'path': 'approved.txt'}, '0')
        model = RawRoleModel(
            coordinator=[
                batch(
                    call('delegate_document', {'task': '製作並申請存檔'}, '0'),
                    call('delegate_knowledge', {'task': '回報查詢限制'}, '1'),
                ),
            ],
            document=[call('create_text', {'name': 'approved.txt', 'content': 'approved'}, '0'), propose],
            knowledge=[AIMessage(content='僅限授權根目錄')],
        )
        pending = await DocumentAgent(self.config, model).run('alice', '製作 approved.txt 並存回，另回報查詢限制')
        self.assertEqual(pending['status'], 'awaiting_approval', pending)
        self.assertEqual([t['status'] for t in pending['delegations']], ['awaiting_approval', 'completed'])
        self.assertFalse((self.root/'approved.txt').exists())
        resumed = RawRoleModel(
            document=[AIMessage(content='存檔已核准')],
            coordinator=[AIMessage(content='完成')],
        )
        done = await DocumentAgent(self.config, resumed).resume(
            'alice', pending['job_id'],
            {'proposal_id': pending['proposals'][0]['proposal_id'], 'decision': 'approve'},
        )
        self.assertEqual(done['status'], 'done', done)
        self.assertEqual(resumed.invocations, ['document', 'coordinator'])
        self.assertEqual((self.root/'approved.txt').read_text(), 'approved')
        self.assertEqual(len(done['files']), 1)
        self.assertEqual([t['status'] for t in done['delegations']], ['completed', 'completed'])

    async def test_claiming_review_without_reading_does_not_pass(self):
        self.config.require_artifact_review = True
        model = RawRoleModel(
            coordinator=[
                call('delegate_document', {'task': '產生'}, '0'),
                call('delegate_reviewer', {'task': '審核'}, '0'), AIMessage(content='全部合格'),
            ],
            document=[call('create_text', {'name': 'a.txt', 'content': 'a'}, '0'), AIMessage(content='完成')],
            reviewer=[AIMessage(content='全部合格，已核對')],
        )
        result = await self.failed_job(DocumentAgent(self.config, model).run('alice', '建立並審核'), ValueError)
        self.assertEqual(result['status'], 'running', result)
        self.assertFalse(review_evidence(result))
        self.assertEqual(len(result['files']), 1)

    async def test_required_review_has_actual_read_and_no_extra_rights(self):
        self.config.require_artifact_review = True

        def handoff(messages):
            artifact = json.loads(messages[-1].content)['artifacts'][0]
            return call('delegate_reviewer', {'task': '核對', 'context': artifact['id']}, '0')

        def inspect(messages):
            return call('inspect_artifact', {'artifact_id': json.loads(messages[-1].content)['selected_context']}, '0')
        model = RawRoleModel(
            coordinator=[call('delegate_document', {'task': '產生'}, '0'), handoff, AIMessage(content='完成')],
            document=[call('create_text', {'name': 'a.txt', 'content': 'a'}, '0'), AIMessage(content='完成')],
            reviewer=[inspect, AIMessage(content='已讀取草稿，排版未驗證')],
        )
        result = await DocumentAgent(self.config, model).run('alice', '建立並審核')
        self.assertEqual(result['status'], 'done', result)
        self.assertFalse(result['artifact_review']['unreviewed_ids'])
        self.assertEqual(result['artifact_review']['read_by_reviewer'][0]['sha256'], result['files'][0]['sha256'])
        self.assertFalse(result['proposals'])


class SchemaTests(unittest.TestCase):

    def test_output_names_are_declared_and_checked_before_execution(self):
        for cls, other, suffix in [
            (Word, {'title': 'a', 'paragraphs': []}, '.docx'),
            (Slides, {'slides': [{'title': 'a', 'bullets': []}]}, '.pptx'),
            (Spreadsheet, {'sheets': [{'name': '結果', 'rows': [[30]]}]}, '.xlsx'),
        ]:
            self.assertIn('pattern', cls.model_json_schema()['properties']['name'])
            cls.model_validate({'name': 'result'+suffix, **other})
            for name in ['folder/result'+suffix, 'result', '../result'+suffix]:
                with self.assertRaises(ValidationError):
                    cls.model_validate({'name': name, **other})
