"""Producer ownership, concurrent duplicate dispatch and native approval resume."""
from __future__ import annotations

import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from company_agent.config import Settings
from company_agent.api.service import ServiceConfiguration
from company_agent.graph import build_graph
from company_agent.runtime.context import Context
from company_agent.runtime.execution import DocumentAgent
from company_agent.services.delivery.validation import prepare_output
from company_agent.services.files.errors import RecoverableToolError
from company_agent.services.office.documents import create_spreadsheet
from company_agent.tests.test_agent import Fixture, call
from company_agent.tests.test_delivery_optimization import delivery_plan
from company_agent.tests.test_enterprise_completion import finish
from company_agent.tests.test_multi_agent import RawRoleModel


def payload(task='製作報告', outputs=None, role='document'):
    return {'role': role, 'task': task, 'context': '', 'outputs': outputs or [],
            'requires_evidence': [], 'resolves_assignments': [], 'revises_assignments': []}


class OfficeConfigurationTests(unittest.TestCase):
    def test_specialist_requires_typed_completion_before_model_or_storage(self):
        settings = Settings(state_dir='/tmp/unused', office_execution_mode='specialist')
        with patch('company_agent.runtime.execution.routed_model') as model, \
             patch('company_agent.runtime.execution.Store') as store:
            with self.assertRaisesRegex(ValueError, 'structured_completion'):
                DocumentAgent(settings, None)
        model.assert_not_called()
        store.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'structured_completion'):
            build_graph(None, InMemorySaver(), False, 'hybrid', 'specialist')

    def test_specialist_hybrid_coordinator_can_read_but_not_produce_or_calculate(self):
        with patch('company_agent.graph.create_agent', wraps=create_agent) as factory:
            build_graph(None, InMemorySaver(), True, 'hybrid', 'specialist')
        tools = {t.name for t in factory.call_args.kwargs['tools']}
        self.assertTrue({'search_files', 'read_file', 'read_spreadsheet', 'inspect_artifact',
                         'delegate_data', 'delegate_document', 'delegate_reviewer'} <= tools)
        self.assertFalse({'analyze_spreadsheet', 'create_spreadsheet', 'create_spreadsheet_report',
                          'edit_spreadsheet', 'create_word', 'create_slides', 'create_text',
                          'edit_text_run', 'edit_artifact_text_run'} & tools)

    def test_examples_select_specialist_and_keep_mixed_model_explicit(self):
        folder = Path(__file__).parents[1] / 'configs'
        for name, model in [('config.optimized.example.json', 'gemini-3.5-flash-lite'),
                            ('config.mixed-candidate.example.json', 'gemini-4-argon')]:
            settings = Settings.model_validate_json((folder / name).read_text())
            self.assertEqual(settings.office_execution_mode, 'specialist')
            self.assertEqual(settings.orchestration_mode, 'hybrid')
            self.assertEqual(settings.role_models['data'].model, model)
            self.assertTrue(all(route.model == 'gemini-3.5-flash-lite'
                                for role, route in settings.role_models.items() if role != 'data'))
        self.assertEqual(Settings(state_dir='/tmp/unused').office_execution_mode, 'flexible')

    def test_service_preserves_office_policy_and_role_models(self):
        path = Path(__file__).parents[1] / 'configs' / 'config.mixed-candidate.example.json'
        settings = Settings.model_validate_json(path.read_text())
        service = ServiceConfiguration(Path('/tmp/unused-state'), Path('/tmp/unused-sources'))
        with patch.object(service, 'configured_settings', return_value=settings):
            selected = service.settings({'id': 'job', 'owner': 'alice', 'payload': {}})
        self.assertEqual(selected.office_execution_mode, 'specialist')
        self.assertEqual(selected.role_models['data'].model, 'gemini-4-argon')
        self.assertTrue(selected.require_artifact_review)
        self.assertTrue(selected.require_user_billing)


class OfficeClaimTests(Fixture):
    def test_concurrent_same_file_different_tasks_have_one_producer(self):
        def claim(i):
            try:
                self.store.claim_assignment('alice', self.job, f'call{i}', f'work{i}',
                                            payload(task=f'報告{i}', outputs=['Report.docx']))
                return True
            except RecoverableToolError:
                return False
        with ThreadPoolExecutor(4) as pool:
            self.assertEqual(sum(pool.map(claim, range(8))), 1)

    def test_identical_native_resume_allowed_but_new_call_and_renamed_key_blocked(self):
        value = payload()
        self.store.claim_assignment('alice', self.job, 'first', 'read_source', value)
        self.store.claim_assignment('alice', self.job, 'first', 'read_source', value)
        for call_id, key, item in [('second', 'read_source', payload('改寫任務')),
                                   ('second', 'renamed_key', value)]:
            with self.assertRaisesRegex(RecoverableToolError, 'duplicate_assignment'):
                self.store.claim_assignment('alice', self.job, call_id, key, item)
        with self.assertRaisesRegex(ValueError, '恢復參數'):
            self.store.claim_assignment('alice', self.job, 'first', 'read_source', payload('已改變'))
        with self.assertRaises(ValueError):
            self.store.claim_assignment('bob', self.job, 'other', 'read_source', value)
        self.store.claim_assignment('alice', self.store.job('alice'), 'other', 'read_source', value)

    def test_output_owner_changes_only_on_explicit_same_role_revision(self):
        self.store.claim_assignment('alice', self.job, 'first', 'draft', payload(outputs=['Report.docx']))
        with self.assertRaisesRegex(RecoverableToolError, 'output_already_assigned'):
            self.store.claim_assignment('alice', self.job, 'bad', 'other', payload('換句話重做', ['report.DOCX']))
        self.store.claim_assignment('alice', self.job, 'second', 'revision', payload('修正', ['Report.docx']), ['first'])
        # An obsolete revision reference cannot steal the latest ownership.
        with self.assertRaisesRegex(RecoverableToolError, 'output_already_assigned'):
            self.store.claim_assignment('alice', self.job, 'third', 'revision_again', payload('再修正', ['Report.docx']), ['first'])

    def test_output_boundary_blocks_coordinator_wrong_role_and_read_only_child(self):
        self.config.office_execution_mode = 'specialist'
        ctx = Context('alice', self.job, self.config, self.store)
        for role, names, target, code in [
            ('coordinator', ('a.xlsx',), 'a.xlsx', 'wrong_producer'),
            ('data', ('a.docx',), 'a.docx', 'wrong_producer'),
            ('document', (), 'a.docx', 'output_not_assigned'),
            ('reviewer', ('a.docx',), 'a.docx', 'wrong_producer'),
        ]:
            with self.subTest(role=role), self.assertRaisesRegex(RecoverableToolError, code):
                prepare_output(replace(ctx, role=role, assignment_outputs=names), target)
        prepare_output(replace(ctx, role='data', assignment_outputs=('a.xlsx',)), 'a.xlsx')


class OfficeGraphTests(unittest.IsolatedAsyncioTestCase, Fixture):
    def setUp(self):
        super().setUp()
        self.config.structured_completion = True
        self.config.orchestration_mode = 'hybrid'
        self.config.office_execution_mode = 'specialist'

    async def test_missing_work_key_and_wrong_producer_never_start_child(self):
        model = RawRoleModel(coordinator=[
            call('delegate_data', {'task': '製作 Excel'}, 'no_key'),
            call('delegate_document', {'task': '製作 Excel', 'work_key': 'wrong', 'outputs': ['a.xlsx']}, 'wrong'),
            finish(outcome='blocked'),
        ])
        result = await DocumentAgent(self.config, model).run('alice', '製作 Excel')
        self.assertFalse(result['delegations'])
        self.assertFalse(result['files'])
        self.assertEqual(len(result['tool_feedback']), 2)
        self.assertEqual(set(model.invocations), {'coordinator'})

    async def test_read_only_document_cannot_create_undeclared_file(self):
        model = RawRoleModel(coordinator=[
            call('delegate_document', {'task': '只閱讀', 'work_key': 'read_only', 'outputs': []}, 'read'),
            finish(outcome='blocked'),
        ], document=[call('create_text', {'name': 'extra.txt', 'content': 'extra'}, 'extra'),
                     finish('complete_assignment', outcome='blocked')])
        result = await DocumentAgent(self.config, model).run('alice', '只閱讀，不產檔。')
        self.assertFalse(result['files'])
        self.assertIn('output_not_assigned', result['tool_feedback'][0]['message'])

    async def test_missing_assigned_output_cannot_be_reported_completed(self):
        model = RawRoleModel(coordinator=[
            call('delegate_document', {'task': '製作報告', 'work_key': 'report', 'outputs': ['report.txt']}, 'write'),
            finish(outcome='blocked'),
        ], document=[finish('complete_assignment'), finish('complete_assignment', outcome='blocked')])
        result = await DocumentAgent(self.config, model).run('alice', '製作報告。')
        self.assertEqual(result['status'], 'blocked', result)
        self.assertFalse(result['files'])
        self.assertIn('outputs 尚未全部產製', result['tool_feedback'][0]['message'])

    async def test_failed_assignment_can_be_explicitly_resolved_without_duplicate_dispatch(self):
        ids, previous = [], []

        def retry(messages):
            previous.append(messages[-1].tool_call_id)
            return call('delegate_document', {'task': '資料已補足，補做報告', 'work_key': 'report_retry',
                                             'outputs': ['report.txt'], 'resolves_assignments': previous}, 'retry')

        def child_finish(messages):
            ids.append(json.loads(messages[-1].content)['id'])
            return finish('complete_assignment', ids=ids)

        model = RawRoleModel(coordinator=[
            call('delegate_document', {'task': '製作報告', 'work_key': 'report', 'outputs': ['report.txt']}, 'first'),
            retry, lambda _: finish(ids=ids),
        ], document=[finish('complete_assignment', outcome='blocked'),
                     call('create_text', {'name': 'report.txt', 'content': '實際成果'}, 'write'), child_finish])
        result = await DocumentAgent(self.config, model).run('alice', '製作報告。')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(len(result['files']), 1)
        self.assertEqual(result['delegations'][1]['resolves_assignments'], previous)

    async def test_revision_reference_cannot_point_to_read_only_work(self):
        previous = []

        def invalid_revision(messages):
            previous.append(messages[-1].tool_call_id)
            return call('delegate_document', {'task': '假裝修訂', 'work_key': 'revision',
                                             'outputs': ['report.txt'], 'revises_assignments': previous}, 'invalid')

        model = RawRoleModel(coordinator=[
            call('delegate_document', {'task': '只讀摘要', 'work_key': 'read_only', 'outputs': []}, 'read'),
            invalid_revision, finish(outcome='blocked'),
        ], document=[finish('complete_assignment')])
        result = await DocumentAgent(self.config, model).run('alice', '閱讀摘要。')
        self.assertEqual(len(result['delegations']), 1)
        self.assertIn('invalid_assignment_revision', result['tool_feedback'][0]['message'])
        self.assertFalse(result['files'])

    async def test_duplicate_dispatch_does_not_invoke_child_again(self):
        ids = []

        def child_finish(messages):
            ids.append(json.loads(messages[-1].content)['id'])
            return finish('complete_assignment', ids=ids)

        model = RawRoleModel(coordinator=[
            call('delegate_document', {'task': '製作報告', 'work_key': 'report', 'outputs': ['report.txt']}, 'first'),
            call('delegate_document', {'task': '換句話重做報告', 'work_key': 'report_again', 'outputs': ['report.txt']}, 'second'),
            lambda _: finish(ids=ids),
        ], document=[call('create_text', {'name': 'report.txt', 'content': '實際內容'}, 'write'), child_finish])
        result = await DocumentAgent(self.config, model).run('alice', '製作報告。')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(len(result['delegations']), 1)
        self.assertEqual(len(result['files']), 1)
        self.assertEqual(model.invocations.count('document'), 2)
        self.assertIn('output_already_assigned', result['tool_feedback'][0]['message'])

    async def test_statistics_handoff_produces_each_file_once_and_independent_review(self):
        self.config.require_artifact_review = True
        self.config.require_delivery_plan = True
        self.file('source.xlsx', create_spreadsheet([{'name': '工程', 'rows': [[10], [20], ['排除']]}]))
        ids = {}

        def finish_data(messages):
            ids['xlsx'] = json.loads(messages[-1].content)['id']
            return finish('complete_assignment', ids=[ids['xlsx']])

        def delegate_document(messages):
            result = json.loads(messages[-1].content)
            return call('delegate_document', {
                'task': '用統計憑據製作報告，不重新計算', 'work_key': 'word_report',
                'outputs': ['summary.docx'], 'requires_evidence': [result['statistics_evidence'][0]['evidence_id']],
            }, 'document')

        def create_document(messages):
            request = json.loads(messages[1].content)
            self.assertEqual(request['statistics_evidence'][0]['result']['sum'], 30)
            self.assertEqual(request['outputs'], ['summary.docx'])
            return call('create_word', {'name': 'summary.docx', 'title': '統計',
                                       'paragraphs': [{'text': '合計：30；筆數：2', 'heading': False}]}, 'word')

        def finish_document(messages):
            ids['docx'] = json.loads(messages[-1].content)['id']
            return finish('complete_assignment', ids=[ids['docx']])

        def inspect(_):
            return AIMessage(content='', tool_calls=[
                {'name': 'inspect_artifact', 'args': {'artifact_id': key}, 'id': label} for label, key in ids.items()
            ])

        model = RawRoleModel(coordinator=[
            call('delegate_data', {'task': '統計並製作 Excel', 'work_key': 'excel_report', 'outputs': ['checked.xlsx']}, 'data'),
            delegate_document,
            call('delegate_reviewer', {'task': '獨立核對兩份成果', 'work_key': 'review_delivery', 'outputs': []}, 'review'),
            lambda _: finish(ids=list(ids.values())),
        ], data=[
            call('analyze_spreadsheet', {k: v for k, v in delivery_plan()['statistics'][0].items() if k != 'key'}, 'stats'),
            call('create_spreadsheet', {'name': 'checked.xlsx', 'sheets': [{'name': '結果', 'rows': [['合計', 30], ['筆數', 2]]}]}, 'xlsx'),
            finish_data,
        ], document=[create_document, finish_document], reviewer=[inspect, lambda _: finish('complete_assignment', ids=list(ids.values()))])
        result = await DocumentAgent(self.config, model).run('alice', '統計 Excel 並製作 Excel 與 Word 報告。', delivery_plan=delivery_plan())
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(result['office_execution_mode'], 'specialist')
        self.assertEqual([d['agent'] for d in result['delegations']], ['data', 'document', 'reviewer'])
        self.assertEqual(len(result['delivery_validation']), 2)
        self.assertEqual(len(self.store.evidence('alice', result['job_id'])), 1)
        self.assertEqual({f['name'] for f in result['files']}, {'checked.xlsx', 'summary.docx'})
        self.assertFalse(result['artifact_review']['unreviewed_ids'])

    async def test_explicit_revision_reuses_producer_and_requires_supersede(self):
        ids, calls = [], []

        def child_finish(messages):
            ids.append(json.loads(messages[-1].content)['id'])
            return finish('complete_assignment', ids=[ids[-1]])

        def revision(messages):
            calls.append(messages[-1].tool_call_id)
            return call('delegate_document', {'task': '修正錯字', 'work_key': 'report_revision',
                                             'outputs': ['report.txt'], 'revises_assignments': calls}, 'revise')

        def supersede(messages):
            files = {f['id']: f for f in json.loads(messages[-1].content)['artifacts']}
            return call('supersede_artifact', {'old_id': ids[0], 'old_sha256': files[ids[0]]['sha256'],
                                              'new_id': ids[1], 'new_sha256': files[ids[1]]['sha256']}, 'supersede')

        model = RawRoleModel(coordinator=[
            call('delegate_document', {'task': '製作報告', 'work_key': 'report', 'outputs': ['report.txt']}, 'first'),
            revision, supersede, lambda _: finish(ids=[ids[-1]]),
        ], document=[call('create_text', {'name': 'report.txt', 'content': '錯字'}, 'draft'), child_finish,
                     call('create_text', {'name': 'report.txt', 'content': '正確'}, 'correct'), child_finish])
        result = await DocumentAgent(self.config, model).run('alice', '製作並修正報告。')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(len(result['files']), 1)
        self.assertEqual(len(result['artifact_history']), 1)
        self.assertEqual(result['delegations'][1]['revises_assignments'], calls)

    async def test_approval_resume_keeps_claim_and_rejects_policy_change(self):
        ids = []

        def propose(messages):
            ids.append(json.loads(messages[-1].content)['id'])
            return call('propose_publish', {'artifact_id': ids[0], 'root': 'project', 'path': 'report.txt'}, 'publish')

        model = RawRoleModel(coordinator=[
            call('delegate_document', {'task': '製作並申請存檔', 'work_key': 'report', 'outputs': ['report.txt']}, 'write'),
        ], document=[call('create_text', {'name': 'report.txt', 'content': 'actual'}, 'create'), propose])
        agent = DocumentAgent(self.config, model)
        pending = await agent.run('alice', '製作報告並申請存檔。')
        self.assertEqual(pending['status'], 'awaiting_approval', pending)
        decision = {'proposal_id': pending['proposals'][0]['proposal_id'], 'decision': 'approve'}
        changed = self.config.model_copy(update={'office_execution_mode': 'flexible'})
        resumed = RawRoleModel(document=[lambda _: finish('complete_assignment', ids=ids)], coordinator=[lambda _: finish(ids=ids)])
        with self.assertRaisesRegex(ValueError, 'Office 分工政策'):
            await DocumentAgent(changed, resumed).resume('alice', pending['job_id'], decision)
        self.assertFalse(resumed.invocations)
        result = await DocumentAgent(self.config, resumed).resume('alice', pending['job_id'], decision)
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual((self.root / 'report.txt').read_text(), 'actual')
        self.assertEqual(len(result['files']), 1)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM assignment_claims WHERE job=%s', (pending['job_id'],)).fetchone()[0], 1)
