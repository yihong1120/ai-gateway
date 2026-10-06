"""Multi-source handoffs over the real graph, with isolated external fixtures."""
from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock
from unittest.mock import patch

from langchain_core.messages import ToolMessage
from pydantic import ValidationError

from company_agent.runtime.completion import Completion
from company_agent.runtime.execution import DocumentAgent
from company_agent.tests.test_agent import call
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_enterprise_completion import finish
from company_agent.tests.test_multi_agent import RawRoleModel
from company_agent.tests.test_nas import NASFixture
from company_agent.tests.test_safe_batches import batch


class CrossSourceTests(unittest.IsolatedAsyncioTestCase, NASFixture):
    async def test_web_nas_eip_then_cad_followup_and_combined_answer(self):
        self.config.structured_completion = True
        self.config.enable_web_research = True
        self.config.enable_eip_read = True
        self.config.enable_cad = True
        self.config.nas_binding_revision = self.bind()['revision']
        root = self.nas_root(self.remote(), 'home')
        request = {
            'role': 'engineering', 'task': '核對本案圖面範圍',
            'context': f'root={root}; path=repair.dwg；確認是否包含橋面工程。',
        }
        seen = []

        def followup(messages):
            receipts = {
                m.name: json.loads(m.content) for m in messages
                if isinstance(m, ToolMessage) and m.name.startswith('delegate_')
            }
            self.assertEqual(set(receipts), {'delegate_research', 'delegate_knowledge', 'delegate_eip'})
            self.assertEqual(receipts['delegate_knowledge']['evidence_requests'], [request])
            self.assertIn('公開案例', receipts['delegate_research']['summary'])
            self.assertIn('核定', receipts['delegate_eip']['summary'])
            return call('delegate_engineering', {'task': request['task'], 'context': request['context']}, 'cad')

        def public_search(messages):
            self.assertNotIn('alice secret', str(messages))
            self.assertNotIn('alice-password', str(messages))
            return call('search_web', {'query': '公共橋梁復建 施工工期'}, 'web')

        def cad_read(box, args):
            seen.append((args.root, args.path))
            source = {'root': args.root, 'path': args.path, 'sha256': 'a' * 64}
            box.sources[(args.root, args.path)] = source
            return {**source, 'rows': [{'text': '橋面修復範圍'}], 'next_start': None}

        def final(messages):
            receipt = json.loads(messages[-1].content)
            self.assertEqual(receipt['status'], 'completed')
            self.assertIn('橋面', receipt['summary'])
            return finish(summary='NAS 契約與 EIP 核定資料為本案依據；CAD 顯示橋面範圍；公開案例僅作參考，不能當本案工期。')

        model = RawRoleModel(
            coordinator=[
                batch(
                    call('delegate_research', {'task': '查公開案例'}, 'r'),
                    call('delegate_knowledge', {'task': '查本案 NAS 紀錄與圖檔位置'}, 'k'),
                    call('delegate_eip', {'task': '查本案核定資料'}, 'e'),
                ), followup, final,
            ],
            research=[public_search, finish('complete_assignment', summary='公開案例僅供比較 https://example.com/repair')],
            knowledge=[
                call('read_file', {'root': root, 'path': 'a.txt'}, 'nas'),
                call(
                    'complete_assignment', {
                        'outcome': 'completed', 'summary': 'NAS 契約已讀，需 CAD 核對圖面。',
                        'artifact_ids': [], 'evidence_requests': [request],
                    }, 'handoff',
                ),
            ],
            eip=[
                call('eip_query', {'folder': 'inbox', 'start': '2026-09-01', 'end': '2026-09-21'}, 'eip'),
                finish('complete_assignment', summary='核定資料日期為 2026-09-10，清單不是附件全文。'),
            ],
            engineering=[
                call('read_cad', {'root': root, 'path': 'repair.dwg'}, 'read'),
                finish('complete_assignment', summary='圖面文字顯示橋面修復範圍，非完整尺寸驗證。'),
            ],
        )
        with (
            patch('company_agent.services.research.web.web_config', AsyncMock(return_value={})),
            patch(
                'company_agent.services.research.web.perform', AsyncMock(
                    return_value={
                        'results': [{'url': 'https://example.com/repair', 'kind': 'search_snippet', 'content': '公開案例'}],
                    },
                ),
            ),
            patch('company_agent.services.eip.client.binding', AsyncMock()),
            patch('company_agent.services.eip.client.query', AsyncMock(return_value={'documents': []})),
            patch('company_agent.services.files.operations.ToolBox.read_cad', cad_read),
        ):
            result = await DocumentAgent(self.config, model).run('alice', '整合本案資料與公開案例說明復建範圍及工期依據。')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(seen, [(root, 'repair.dwg')])
        self.assertEqual({d['agent'] for d in result['delegations']}, {'research', 'knowledge', 'eip', 'engineering'})
        self.assertEqual({s['path'] for s in result['sources']}, {'a.txt', 'repair.dwg'})
        self.assertEqual(len(result['web_sources']), 1)
        self.assertFalse(result['files'])
        self.assertFalse(result['proposals'])
        self.assertFalse(self.dsm.sessions)


class SourceGapTests(unittest.IsolatedAsyncioTestCase, Fixture):
    async def test_unavailable_source_keeps_other_findings_without_completion_loop(self):
        self.config.structured_completion = True
        self.file('source.txt', b'contract evidence')
        model = RawRoleModel(
            coordinator=[
                batch(
                    call('delegate_eip', {'task': '查核定函'}, 'e'),
                    call('delegate_knowledge', {'task': '查契約'}, 'k'),
                ),
                finish(outcome='blocked', summary='已讀契約；EIP 未綁定，無法確認最新核定日期。'),
            ],
            eip=[finish('complete_assignment', outcome='blocked', summary='缺少 EIP 綁定，未查詢。')],
            knowledge=[
                call('read_file', {'root': 'project', 'path': 'source.txt'}, 'read'),
                finish('complete_assignment', summary='契約已讀。'),
            ],
        )
        result = await DocumentAgent(self.config, model).run('alice', '比對契約與核定日期')
        self.assertEqual(result['status'], 'blocked', result)
        self.assertNotIn('errors', result)
        self.assertEqual(len(result['sources']), 1)
        self.assertIn('已讀契約', result['answer'])
        self.assertEqual(model.invocations.count('coordinator'), 2)

    def test_followups_are_optional_and_cannot_inject_runtime_or_unknown_roles(self):
        original = dict(outcome='completed', summary='已讀', artifact_ids=[])
        self.assertEqual(Completion(**original).evidence_requests, [])
        for request in ({'role': 'shell', 'task': 'x'}, {'role': 'knowledge', 'task': 'x', 'owner': 'admin'}):
            with self.assertRaises(ValidationError):
                Completion(**original, evidence_requests=[request])
