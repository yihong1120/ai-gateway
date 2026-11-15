"""Regressions for failures measured in the 104-job replacement campaign."""
from __future__ import annotations

import io
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import psycopg
from langchain_core.messages import ToolMessage
from langchain_core.tools import ToolException
from openpyxl import Workbook

from company_agent.agents.registry import ROLE_TOOLS
from company_agent.evals.data_replacement_cases import CASES
from company_agent.runtime.context import Context
from company_agent.runtime.execution import DocumentAgent
from company_agent.services.delivery.explicit import check_explicit_cells, explicit_outputs
from company_agent.services.delivery.schemas import DeliveryPlan
from company_agent.services.delivery.tools import plan
from company_agent.services.delivery.validation import statistic_text_matches
from company_agent.services.files.errors import RecoverableToolError
from company_agent.services.files.operations import worker
from company_agent.services.files.tools import file_tool
from company_agent.services.office.documents import create_word, extract
from company_agent.services.office.schemas import AnalyzeSpreadsheet
from company_agent.tests.test_agent import Fixture, call
from company_agent.tests import test_artifact_revision
from company_agent.tests.test_enterprise_completion import finish
from company_agent.tests.test_multi_agent import RawRoleModel


class RequirementTests(unittest.TestCase):
    def test_all_real_workflow_requests_require_the_entire_delivery_list(self):
        for case in CASES:
            if case.lane != 'workflow':
                continue
            with self.subTest(case=case.id):
                self.assertEqual(explicit_outputs(case.prompt), set(case.outputs))
                for omitted in case.outputs:
                    incomplete = DeliveryPlan(files=[{'name': n} for n in case.outputs if n != omitted])
                    with self.assertRaises(ToolException):
                        check_explicit_cells(case.prompt, incomplete)

    def test_source_mentions_negations_and_quoted_labels_do_not_change_output_scope(self):
        self.assertEqual(explicit_outputs('交付 checked.xlsx（「來源」與「讀取」為欄名）與 summary.docx，參考 source.docx。'),
                         {'checked.xlsx', 'summary.docx'})
        self.assertEqual(explicit_outputs('不要另交 extra.docx。讀 source.xlsx。另製 `結果.docx`。'), {'結果.docx'})

    def test_complete_numeric_tokens_not_prefixes(self):
        for value in ('合計：356.0', '合計：3.56e2', '合計：356。', '合計：356；筆數：10'):
            self.assertTrue(statistic_text_matches('合計：{value}', 356, value), value)
        for value in ('合計：356.1', '合計：3560', '合計：356,000', '合計：356e2', '合計：356,01'):
            self.assertFalse(statistic_text_matches('合計：{value}', 356, value), value)
        self.assertTrue(statistic_text_matches('合計：{value}元', 1000, '合計：1,000.0元'))
        self.assertFalse(statistic_text_matches('筆數：{value}', 10, '筆數：10.5'))

    def test_only_coordinator_exposes_global_planning(self):
        self.assertIn('plan_delivery', ROLE_TOOLS['coordinator'])
        for role in ('data', 'document', 'reviewer'):
            self.assertNotIn('plan_delivery', ROLE_TOOLS[role])


class VersionTests(unittest.IsolatedAsyncioTestCase, Fixture):
    seed = test_artifact_revision.PrivateRevisionTests.seed
    async def test_same_delivery_name_is_immutable_new_version_and_replay_is_idempotent(self):
        self.config.require_delivery_plan = True
        self.store.delivery_plan('alice', self.job, DeliveryPlan(files=[{'name': 'draft.docx'}]).model_dump())
        data, old, runtime, args = self.seed('.docx')
        args['name'] = old['name']
        from company_agent.agents.document.revision import revise
        result = json.loads((await revise(runtime, **args)).update['messages'][0].content)
        again = json.loads((await revise(runtime, **args)).update['messages'][0].content)
        self.assertEqual(result, again)
        self.assertNotEqual(result['id'], old['id'])
        self.assertEqual(result['supersedes'], old['id'])
        self.assertEqual([f['id'] for f in self.store.deliverable_files('alice', self.job)], [result['id']])
        self.assertEqual(self.store.read_artifact('alice', old['id'], self.job)[2], data)
        self.assertEqual(len(self.store.files('alice', self.job)), 2)
        self.assertFalse(self.store.validations('alice', self.job))

    def test_concurrent_revisions_have_one_winner_and_no_orphan_drafts(self):
        old = self.store.artifact('alice', self.job, 'same.docx', create_word('old', []))
        for i in range(2):
            self.store.begin_tool('alice', self.job, str(i), 'edit_artifact_text_run', {'i': i})
        def change(i):
            try:
                return self.store.revise_artifact('alice', self.job, old['id'], old['sha256'], create_word(str(i), []), str(i))
            except RecoverableToolError:
                return None
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(change, range(2)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(len(self.store.files('alice', self.job)), 2)
        self.assertEqual(len(self.store.deliverable_files('alice', self.job)), 1)

    def test_receipt_commit_failure_rolls_back_new_version_and_supersession(self):
        old = self.store.artifact('alice', self.job, 'same.docx', create_word('old', []))
        self.store.begin_tool('alice', self.job, 'revision', 'edit_artifact_text_run', {})
        with self.store.db() as db:
            db.execute("CREATE FUNCTION fail_receipt_update() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected'; END $$")
            db.execute('CREATE TRIGGER fail_receipt BEFORE UPDATE ON tool_receipts FOR EACH ROW EXECUTE FUNCTION fail_receipt_update()')
        with self.assertRaises(psycopg.errors.RaiseException):
            self.store.revise_artifact('alice', self.job, old['id'], old['sha256'], create_word('new', []), 'revision')
        self.assertEqual(len(self.store.files('alice', self.job)), 1)
        self.assertEqual(self.store.deliverable_files('alice', self.job)[0]['lifecycle'], 'draft')


class StructuredFailureTests(unittest.IsolatedAsyncioTestCase, Fixture):
    async def test_incomplete_model_plan_returns_feedback_before_lock_and_can_be_corrected(self):
        self.config.structured_completion = True
        self.config.require_delivery_plan = True
        model = RawRoleModel(coordinator=[
            call('plan_delivery', {'files': [{'name': 'checked.xlsx'}]}, 'incomplete'),
            call('plan_delivery', {'files': [{'name': 'checked.xlsx'}, {'name': 'summary.docx'}]}, 'correct'),
            finish(outcome='blocked', summary='本測試只驗證計畫，不產檔。')])
        result = await DocumentAgent(self.config, model).run('alice', '交付 checked.xlsx，另交 summary.docx。')
        self.assertEqual(len(result['tool_feedback']), 1)
        self.assertIn('summary.docx', result['tool_feedback'][0]['message'])
        committed = self.store.delivery_plan('alice', result['job_id'])
        self.assertEqual({f['name'] for f in committed['files']}, {'checked.xlsx', 'summary.docx'})
        self.assertFalse(result['files'])

    async def test_producer_cannot_start_before_coordinator_plan(self):
        self.config.structured_completion = True
        self.config.require_delivery_plan = True
        self.config.office_execution_mode = 'specialist'
        model = RawRoleModel(coordinator=[
            call('delegate_document', {'task': '製作摘要', 'work_key': 'document', 'outputs': ['summary.docx']}, 'too-early'),
            finish(outcome='blocked', summary='交付計畫尚未建立。')])
        result = await DocumentAgent(self.config, model).run('alice', '交付 summary.docx。')
        self.assertFalse(result['delegations'])
        self.assertFalse(result['files'])
        self.assertEqual(set(model.invocations), {'coordinator'})
        self.assertIn('delivery_plan_required', result['tool_feedback'][0]['message'])

    async def test_subagent_cannot_lock_global_plan(self):
        runtime = SimpleNamespace(context=Context('alice', self.job, self.config, self.store, role='document', delegation_id='d'))
        with self.assertRaisesRegex(RecoverableToolError, 'coordinator_plan_required'):
            await plan(runtime, files=[{'name': 'unexpected.docx'}])
        self.assertIsNone(self.store.delivery_plan('alice', self.job))

    def test_large_source_limit_is_structured_even_for_one_row_read(self):
        book = Workbook(); book.active.title = '工程'; book.active['A12000'] = 5
        data = io.BytesIO(); book.save(data); book.close()
        with self.assertRaises(RecoverableToolError) as raised:
            worker('read_spreadsheet', data=data.getvalue(), row_limit=1)
        value = json.loads(str(raised.exception))
        self.assertEqual((value['code'], value['actual'], value['maximum']), ('spreadsheet_resource_limit', 12000, 10000))
        self.assertFalse(value['executed'])

    async def test_sheet_repair_stops_parser_after_two_failed_attempts(self):
        args = {'root': 'project', 'path': 'source.xlsx', 'sheet': '工程', 'column': 1, 'start_row': 1, 'end_row': 3}
        messages = [ToolMessage(name='analyze_spreadsheet', tool_call_id=str(i), status='error',
                               content=json.dumps({'code': 'sheet_not_found', 'root': 'project', 'path': 'source.xlsx'})) for i in range(2)]
        runtime = SimpleNamespace(context=Context('alice', self.job, self.config, self.store), state={'messages': messages})
        with patch('company_agent.services.files.tools.ToolBox.analyze_spreadsheet') as parser:
            with self.assertRaisesRegex(RecoverableToolError, 'sheet_recovery_exhausted'):
                await file_tool(runtime, tool_name='analyze_spreadsheet', input_schema=AnalyzeSpreadsheet, **args)
        parser.assert_not_called()

    async def test_incomplete_injected_plan_is_rejected_before_job_or_model(self):
        model = RawRoleModel()
        with self.assertRaisesRegex(ToolException, 'summary.docx'):
            await DocumentAgent(self.config, model).run('alice', '交付 checked.xlsx，另交 summary.docx。',
                                                       delivery_plan={'files': [{'name': 'checked.xlsx'}]})
        self.assertFalse(model.invocations)

    async def test_same_name_revision_reviews_only_new_version_and_completes(self):
        self.config.structured_completion = True
        self.config.require_delivery_plan = True
        self.config.require_artifact_review = True
        self.config.office_execution_mode = 'specialist'
        self.config.max_steps = 20
        ids = []
        def inspect(messages):
            ids.append(json.loads(messages[-1].content)['id'])
            return call('inspect_artifact', {'artifact_id': ids[0]}, 'inspect')
        def change(messages):
            receipt = json.loads(messages[-1].content)
            run = next(r for b in receipt['blocks'] for r in b['runs'] if r['text'] == '待確認')
            return call('edit_artifact_text_run', {'artifact_id': ids[0], 'sha256': receipt['sha256'],
                'name': 'summary.docx', 'location': run['location'], 'expected': '待確認', 'replacement': '已確認'}, 'revise')
        def child_done(messages):
            ids.append(json.loads(messages[-1].content)['id'])
            return finish('complete_assignment', ids=[ids[-1]])
        model = RawRoleModel(coordinator=[
            call('plan_delivery', {'files': [{'name': 'summary.docx', 'texts': [{'text': '已確認'}]}]}, 'plan'),
            call('delegate_document', {'task': '建立並修訂摘要', 'work_key': 'document', 'outputs': ['summary.docx']}, 'document'),
            call('delegate_reviewer', {'task': '讀取最新版', 'work_key': 'review', 'outputs': []}, 'review'),
            lambda _: finish(ids=[ids[-1]])], document=[
            call('create_word', {'name': 'summary.docx', 'title': '摘要', 'paragraphs': [{'text': '待確認', 'heading': False}]}, 'create'),
            inspect, change, child_done], reviewer=[
            lambda _: call('inspect_artifact', {'artifact_id': ids[-1]}, 'inspect-new'),
            lambda _: finish('complete_assignment', ids=[ids[-1]])])
        result = await DocumentAgent(self.config, model).run('alice', '交付 summary.docx，草稿中的待確認改為已確認，獨立審核最新版。')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual([f['id'] for f in result['files']], [ids[-1]])
        self.assertEqual([f['id'] for f in result['artifact_history']], [ids[0]])
        self.assertEqual([x['id'] for x in result['artifact_review']['read_by_reviewer']], [ids[-1]])
        self.assertFalse(result['artifact_review']['unreviewed_ids'])
        self.assertIn('待確認', ''.join(b['text'] for b in extract(self.store.read_artifact('alice', ids[0])[2], '.docx')))

    async def test_unexpected_child_failure_saves_error_state_without_leaking_body(self):
        self.config.structured_completion = True
        def fail(_):
            raise RuntimeError('PRIVATE FAILURE BODY')
        failed = await self.failed_job(DocumentAgent(self.config, RawRoleModel(coordinator=[fail])).run('alice', '回答'), RuntimeError)
        self.assertEqual(failed['status'], 'error')
        self.assertTrue(failed['resumable'])
        self.assertNotIn('PRIVATE', failed['answer'])
