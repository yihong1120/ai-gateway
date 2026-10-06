from __future__ import annotations

import json
import unittest

from langchain_core.messages import AIMessage
from langchain_core.messages import messages_to_dict

from company_agent.agents.registry import capability_catalog
from company_agent.runtime.execution import DocumentAgent
from company_agent.tests.test_agent import call
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_multi_agent import RawRoleModel


class IsolationTests(unittest.IsolatedAsyncioTestCase, Fixture):
    async def test_raw_tool_text_not_shared_with_coordinator_or_next_specialist(self):
        self.file('private.txt', b'RAW_SECRET_SENTINEL_4729')
        seen = {}

        def coordinate(messages):
            seen['coordinator'] = json.dumps(messages_to_dict(messages))
            value = json.loads(messages[-1].content)
            self.assertEqual(value['status'], 'completed')
            self.assertEqual(value['summary'], '已讀取，僅轉交這份必要摘要。')
            return call('delegate_document', {'task': '建立摘要', 'context': '明確選取的摘要，不含原始全文。'}, 'next')

        def document(messages):
            seen['document'] = json.dumps(messages_to_dict(messages))
            return AIMessage(content='已接收選定摘要。')
        model = RawRoleModel(
            coordinator=[
                call('delegate_knowledge', {'task': '讀 private.txt'}, 'start'),
                coordinate, AIMessage(content='完成。'),
            ],
            knowledge=[
                call('read_file', {'root': 'project', 'path': 'private.txt'}, 'read'),
                AIMessage(content='已讀取，僅轉交這份必要摘要。'),
            ], document=[document],
        )
        result = await DocumentAgent(self.config, model).run('alice', '檢索後準備摘要')
        self.assertEqual(result['status'], 'done', result)
        self.assertTrue(all('RAW_SECRET_SENTINEL_4729' not in text for text in seen.values()))
        self.assertNotIn('tool_call_id', seen['document'])
        self.assertEqual(result['context_mode'], 'per_delegation')

    async def test_redelegation_to_same_role_starts_clean(self):
        seen = []

        def second(messages):
            seen.extend(messages)
            return AIMessage(content='第二份完成。')
        model = RawRoleModel(
            coordinator=[
                call('delegate_knowledge', {'task': '第一份'}, 'one'),
                call('delegate_knowledge', {'task': '第二份'}, 'two'), AIMessage(content='完成。'),
            ],
            knowledge=[AIMessage(content='FIRST_PRIVATE_REASONING'), second],
        )
        result = await DocumentAgent(self.config, model).run('alice', '做兩項查詢')
        self.assertEqual(result['status'], 'done', result)
        self.assertNotIn('FIRST_PRIVATE_REASONING', json.dumps(messages_to_dict(seen)))

    async def test_reviewer_reads_actual_current_job_artifact(self):
        def review_handoff(messages):
            result = json.loads(messages[-1].content)
            artifact = result['artifacts'][0]
            return call('delegate_reviewer', {'task': '核對草稿', 'context': artifact['id']}, 'review')

        def inspect(messages):
            task = json.loads(messages[-1].content)
            return call('inspect_artifact', {'artifact_id': task['selected_context']}, 'inspect')

        def report(messages):
            value = json.loads(messages[-1].content)
            self.assertEqual(value['blocks'][0]['text'], 'VERIFY_ACTUAL_ARTIFACT')
            return AIMessage(content='文字與要求一致，視覺排版未驗證。')
        model = RawRoleModel(
            coordinator=[
                call('delegate_document', {'task': '產生草稿'}, 'write'),
                review_handoff, AIMessage(content='已完成草稿及文字校核。'),
            ],
            document=[
                call('create_text', {'name': 'out.txt', 'content': 'VERIFY_ACTUAL_ARTIFACT'}, 'create'),
                AIMessage(content='草稿已建立。'),
            ], reviewer=[inspect, report],
        )
        result = await DocumentAgent(self.config, model).run('alice', '建立草稿並校核')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(result['review_status'], 'advisory_returned')
        self.assertEqual(len(result['files']), 1)
        self.assertFalse((self.root/'out.txt').exists())

    async def test_reviewer_cannot_read_other_job_artifact(self):
        artifact = self.store.artifact('alice', self.job, 'other.txt', b'PRIVATE_OTHER_JOB')
        model = RawRoleModel(
            coordinator=[call('delegate_reviewer', {'task': '核對'}, 'review'), AIMessage(content='無法核對。')],
            reviewer=[call('inspect_artifact', {'artifact_id': artifact['id']}, 'read'), AIMessage(content='權限不足。')],
        )
        result = await DocumentAgent(self.config, model).run('alice', '核對產出')
        self.assertEqual(result['answer'], '無法核對。')
        self.assertFalse(result['artifact_review']['read_by_reviewer'])
        self.assertEqual(result['tool_feedback'][0]['tool'], 'inspect_artifact')
        self.assertFalse(result['files'])
        self.assertNotIn('other.txt', str(result))
        self.assertNotIn('PRIVATE_OTHER_JOB', str(result))

    async def test_eip_signing_not_exposed_as_callable_tool(self):
        model = RawRoleModel(
            coordinator=[call('delegate_eip', {'task': '自行簽核'}, 'delegate'), AIMessage(content='未簽核。')],
            eip=[call('eip_sign', {}, 'forbidden'), AIMessage(content='沒有簽核工具。')],
        )
        result = await DocumentAgent(self.config, model).run('alice', '查看能力')
        self.assertEqual([r['tool'] for r in result['tool_feedback']], ['eip_sign'])
        self.assertEqual(capability_catalog()['eip']['status'], 'implemented')
        self.assertEqual(capability_catalog()['research']['status'], 'implemented')

    async def test_data_agent_has_no_publish_tool(self):
        model = RawRoleModel(
            coordinator=[call('delegate_data', {'task': '分析'}, 'analysis'), AIMessage(content='未存檔。')],
            data=[
                call(
                    'propose_publish', {
                        'artifact_id': 'fake', 'root': 'project',
                        'path': 'x.xlsx',
                    }, 'forbidden',
                ), AIMessage(content='沒有存檔工具。'),
            ],
        )
        result = await DocumentAgent(self.config, model).run('alice', '分析資料')
        self.assertEqual([r['tool'] for r in result['tool_feedback']], ['propose_publish'])
        self.assertFalse(result['proposals'])
        self.assertFalse(list(self.root.iterdir()))
        self.assertFalse(result['files'])

    async def test_real_xlsx_analysis_creation_and_independent_review(self):
        from company_agent.services.office.documents import create_spreadsheet
        self.file('source.xlsx', create_spreadsheet([{'name': '工程', 'rows': [[10], [20]]}]))

        def create(messages):
            stats = json.loads(messages[-1].content)
            self.assertEqual(stats['sum'], 30)
            return call(
                'create_spreadsheet', {
                    'name': '統計.xlsx',
                    'sheets': [{'name': '結果', 'rows': [['合計', stats['sum']]]}],
                }, 'create',
            )

        def handoff(messages):
            result = json.loads(messages[-1].content)
            return call('delegate_reviewer', {'task': '檢查工作簿', 'context': result['artifacts'][0]['id']}, 'review')

        def inspect(messages):
            task = json.loads(messages[-1].content)
            return call('inspect_artifact', {'artifact_id': task['selected_context']}, 'inspect')

        def report(messages):
            result = json.loads(messages[-1].content)
            self.assertEqual(result['spreadsheet']['rows'][0]['cells'][1]['value'], 30)
            return AIMessage(content='產出工作簿合計是30；未執行公式。')
        model = RawRoleModel(
            coordinator=[
                call('delegate_data', {'task': '統計 project/source.xlsx 並建立結果工作簿'}, 'data'),
                handoff, AIMessage(content='完成工作簿與校核。'),
            ],
            data=[
                call(
                    'analyze_spreadsheet', {
                        'root': 'project', 'path': 'source.xlsx',
                        'sheet': '工程', 'column': 1, 'start_row': 1, 'end_row': 2,
                    }, 'analyze',
                ), create,
                AIMessage(content='合計30，產出統計.xlsx。'),
            ], reviewer=[inspect, report],
        )
        result = await DocumentAgent(self.config, model).run('alice', '統計工程數據，另產 Excel 並校核')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(result['review_status'], 'advisory_returned')
        self.assertEqual(result['model_steps'], 8)
        self.assertEqual(result['files'][0]['name'], '統計.xlsx')
        self.assertEqual(result['sources'][0]['path'], 'source.xlsx')
