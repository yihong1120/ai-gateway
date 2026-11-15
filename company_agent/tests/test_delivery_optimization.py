"""Failure-injection and real PostgreSQL coverage of optimized job contracts."""
from __future__ import annotations

import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import httpx
from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from langchain_core.messages import ToolMessage
from langchain_core.tools import ToolException

from company_agent.config import ModelRoute
from company_agent.config import Settings
from company_agent.runtime.context import Context
from company_agent.runtime.execution import DocumentAgent
from company_agent.runtime.models import close_model, gateway_for_verified_user, model_policy, routed_model
from company_agent.services.delivery.schemas import DeliveryPlan
from company_agent.services.delivery.validation import prepare_output, validate_delivery, verify_evidence_sources
from company_agent.services.files.errors import RecoverableToolError
from company_agent.services.files.operations import ToolBox, worker
from company_agent.services.office.documents import create_spreadsheet, create_word
from company_agent.services.office.schemas import AnalyzeSpreadsheet
from company_agent.tests.test_agent import Fixture, call
from company_agent.tests.test_enterprise_completion import finish
from company_agent.tests.test_multi_agent import RawRoleModel
from company_agent.tests.test_user_billing import response, verified


def delivery_plan():
    return {
        'statistics': [{'key': 'values', 'root': 'project', 'path': 'source.xlsx', 'sheet': '工程',
                        'column': 1, 'start_row': 1, 'end_row': 3}],
        'files': [
            {'name': 'checked.xlsx', 'sheet_names': ['結果'], 'cells': [
                {'sheet': '結果', 'cell': 'A1', 'expected': '合計'},
                {'sheet': '結果', 'cell': 'B1', 'statistic': {'key': 'values', 'metric': 'sum'}},
                {'sheet': '結果', 'cell': 'A2', 'expected': '筆數'},
                {'sheet': '結果', 'cell': 'B2', 'statistic': {'key': 'values', 'metric': 'count'}},
            ]},
            {'name': 'summary.docx', 'texts': [
                {'text': '合計：{value}', 'statistic': {'key': 'values', 'metric': 'sum'}},
                {'text': '筆數：{value}', 'statistic': {'key': 'values', 'metric': 'count'}},
            ]},
        ],
    }


class DeliveryStoreTests(Fixture):
    def create_outputs(self, label='筆數', word_sum=30):
        sheet = self.store.artifact('alice', self.job, 'checked.xlsx', create_spreadsheet([
            {'name': '結果', 'rows': [['合計', 30], [label, 2]]},
        ]))
        doc = self.store.artifact('alice', self.job, 'summary.docx', create_word('統計', [
            {'text': f'合計：{word_sum}；筆數：2', 'heading': False},
        ]))
        return sheet, doc

    def evidence(self):
        self.file('source.xlsx', create_spreadsheet([{'name': '工程', 'rows': [[10], [20], ['排除']]}]))
        args = AnalyzeSpreadsheet(root='project', path='source.xlsx', sheet='工程', column=1, start_row=1, end_row=3)
        result = ToolBox(self.config, self.store, 'alice', self.job).analyze_spreadsheet(args)
        return self.store.save_evidence('alice', self.job, 'real-call', {
            'source': {k: result[k] for k in ('root', 'path', 'sha256')},
            'parameters': args.model_dump(), 'result': result,
        })

    def test_supersede_keeps_bytes_and_requires_same_owner_job_hash(self):
        old = self.store.artifact('alice', self.job, 'same.txt', b'wrong')
        new = self.store.artifact('alice', self.job, 'same.txt', b'right')
        args = (old['id'], old['sha256'], new['id'], new['sha256'])
        for owner, job, changed in [('bob', self.job, args), ('alice', self.store.job('alice'), args),
                                    ('alice', self.job, (old['id'], '0'*64, new['id'], new['sha256']))]:
            with self.assertRaises(ValueError):
                self.store.supersede_artifact(owner, job, *changed)
        result = self.store.supersede_artifact('alice', self.job, *args)
        self.assertEqual(self.store.supersede_artifact('alice', self.job, *args), result)
        self.assertEqual([f['id'] for f in self.store.deliverable_files('alice', self.job)], [new['id']])
        self.assertEqual(self.store.read_artifact('alice', old['id'], self.job)[2], b'wrong')
        self.assertEqual(self.store.result('alice', self.job)['artifact_history'][0]['id'], old['id'])
        with self.assertRaises(ValueError):
            self.store.supersede_artifact('alice', self.job, new['id'], new['sha256'], old['id'], old['sha256'])

    def test_concurrent_replacements_choose_one_and_final_cannot_be_replaced(self):
        old = self.store.artifact('alice', self.job, 'old.txt', b'old')
        candidates = [self.store.artifact('alice', self.job, f'new{i}.txt', str(i).encode()) for i in range(2)]

        def replace(new):
            try:
                self.store.supersede_artifact('alice', self.job, old['id'], old['sha256'], new['id'], new['sha256'])
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(sum(pool.map(replace, candidates)), 1)
        files = self.store.deliverable_files('alice', self.job)
        self.store.finalize_artifacts('alice', self.job, [f['id'] for f in files])
        with self.assertRaises(ValueError):
            self.store.supersede_artifact('alice', self.job, candidates[0]['id'], candidates[0]['sha256'],
                                          candidates[1]['id'], candidates[1]['sha256'])

    def test_wrong_unicode_label_rejected_correct_revision_passes_and_history_survives(self):
        self.store.delivery_plan('alice', self.job, DeliveryPlan.model_validate(delivery_plan()).model_dump())
        self.evidence()
        old, _ = self.create_outputs(label='筆數\u200d')
        with self.assertRaisesRegex(RecoverableToolError, 'A2'):
            validate_delivery(self.store, 'alice', self.job)
        new = self.store.artifact('alice', self.job, 'checked.xlsx', create_spreadsheet([
            {'name': '結果', 'rows': [['合計', 30], ['筆數', 2]]},
        ]))
        with self.assertRaisesRegex(ToolException, '同名'):
            validate_delivery(self.store, 'alice', self.job)
        self.store.supersede_artifact('alice', self.job, old['id'], old['sha256'], new['id'], new['sha256'])
        receipts = validate_delivery(self.store, 'alice', self.job)
        self.assertEqual(len(receipts), 2)
        self.assertTrue(all(r['status'] == 'passed' and r['evidence_ids'] == ['real-call'] for r in receipts))

    def test_missing_statistics_and_wrong_cross_file_number_cannot_pass(self):
        self.store.delivery_plan('alice', self.job, DeliveryPlan.model_validate(delivery_plan()).model_dump())
        self.create_outputs(word_sum=31)
        with self.assertRaisesRegex(ToolException, '必要統計'):
            validate_delivery(self.store, 'alice', self.job)
        self.evidence()
        with self.assertRaisesRegex(RecoverableToolError, '合計'):
            validate_delivery(self.store, 'alice', self.job)
        with self.assertRaisesRegex(ValueError, '不可修改'):
            self.store.delivery_plan('alice', self.job, {'statistics': [], 'files': []})

    def test_evidence_immutable_and_source_change_or_revocation_stops_delivery(self):
        evidence = self.evidence()
        with self.assertRaisesRegex(ValueError, '不可改寫'):
            self.store.save_evidence('alice', self.job, 'real-call', {'result': 'fake'})
        ctx = Context('alice', self.job, self.config, self.store)
        verify_evidence_sources(ctx, [evidence])
        self.file('source.xlsx', create_spreadsheet([{'name': '工程', 'rows': [[999]]}]))
        with self.assertRaisesRegex(ValueError, '來源已變更'):
            verify_evidence_sources(ctx, [evidence])
        self.config.roots[0].readers = []
        with self.assertRaises(ValueError):
            verify_evidence_sources(ctx, [evidence])

    def test_required_plan_blocks_unplanned_output_and_protects_explicit_original(self):
        self.config.require_delivery_plan = True
        ctx = Context('alice', self.job, self.config, self.store)
        with self.assertRaisesRegex(RecoverableToolError, 'delivery_plan_required'):
            prepare_output(ctx, 'draft.txt')
        self.store.delivery_plan('alice', self.job, DeliveryPlan(files=[{'name': 'draft.txt'}, {'name': 'revised.txt'}]).model_dump())
        with self.assertRaisesRegex(RecoverableToolError, 'file_not_planned'):
            prepare_output(ctx, 'unrequested.txt')
        prepare_output(ctx, 'draft.txt')
        old = self.store.artifact('alice', self.job, 'draft.txt', b'original')
        new = self.store.artifact('alice', self.job, 'revised.txt', b'revised')
        with self.assertRaisesRegex(ToolException, '保留舊檔名'):
            self.store.supersede_artifact('alice', self.job, old['id'], old['sha256'], new['id'], new['sha256'])
        self.assertEqual(len(self.store.deliverable_files('alice', self.job)), 2)

    def test_preserved_formula_requires_formula_type_and_does_not_trust_cache(self):
        import io
        from openpyxl import Workbook
        self.store.delivery_plan('alice', self.job, DeliveryPlan(files=[{
            'name': 'formula.xlsx', 'cells': [{'sheet': '工程', 'cell': 'A1',
                                            'expected': '=SUM(B1:B2)', 'formula': True}],
        }]).model_dump())
        book = Workbook(); book.active.title = '工程'; book.active['A1'] = '=SUM(B1:B2)'
        data = io.BytesIO(); book.save(data); book.close()
        original = self.store.artifact('alice', self.job, 'formula.xlsx', data.getvalue())
        self.assertEqual(validate_delivery(self.store, 'alice', self.job)[0]['status'], 'passed')
        text = self.store.artifact('alice', self.job, 'formula.xlsx', create_spreadsheet([
            {'name': '工程', 'rows': [['=SUM(B1:B2)']]},
        ]))
        self.store.supersede_artifact('alice', self.job, original['id'], original['sha256'], text['id'], text['sha256'])
        with self.assertRaisesRegex(RecoverableToolError, 'A1'):
            validate_delivery(self.store, 'alice', self.job)

    def test_parser_error_is_safe_structured_feedback_but_corrupt_input_still_fails(self):
        data = create_spreadsheet([{'name': '工程', 'rows': [[10]]}])
        with self.assertRaises(RecoverableToolError) as caught:
            worker('read_spreadsheet', data=data, sheet='工程表')
        error = json.loads(str(caught.exception))
        self.assertEqual(error['available_sheets'], ['工程'])
        self.assertEqual(error['code'], 'sheet_not_found')
        self.assertNotIn('Traceback', str(caught.exception))
        from subprocess import CalledProcessError
        with self.assertRaises(CalledProcessError):
            worker('read_spreadsheet', data=b'not an office file')


class OptimizedGraphTests(unittest.IsolatedAsyncioTestCase, Fixture):
    def setUp(self):
        super().setUp()
        self.config.structured_completion = True
        self.config.max_steps = 20

    async def test_hybrid_statistics_then_two_files_then_independent_review(self):
        self.config.orchestration_mode = 'hybrid'
        self.config.require_delivery_plan = True
        self.config.require_artifact_review = True
        self.file('source.xlsx', create_spreadsheet([{'name': '工程', 'rows': [[10], [20], ['排除']]}]))
        ids = []

        def create(messages):
            result = json.loads(messages[-1].content)
            self.assertEqual(result['statistics_evidence']['result']['sum'], 30)
            return AIMessage(content='', tool_calls=[
                {'id': 'xlsx', 'name': 'create_spreadsheet', 'args': {
                    'name': 'checked.xlsx', 'sheets': [{'name': '結果', 'rows': [['合計', 30], ['筆數', 2]]}],
                }},
                {'id': 'docx', 'name': 'create_word', 'args': {
                    'name': 'summary.docx', 'title': '統計',
                    'paragraphs': [{'text': '合計：30；筆數：2', 'heading': False}],
                }},
            ])

        def review(messages):
            ids.extend(json.loads(m.content)['id'] for m in messages if isinstance(m, ToolMessage)
                       and m.name in {'create_word', 'create_spreadsheet'})
            return call('delegate_reviewer', {'task': '核對兩份統計成果'}, 'review')

        def inspect(messages):
            return AIMessage(content='', tool_calls=[
                {'name': 'inspect_artifact', 'args': {'artifact_id': key}, 'id': str(i)} for i, key in enumerate(ids)
            ])
        model = RawRoleModel(coordinator=[
            call('analyze_spreadsheet', delivery_plan()['statistics'][0] | {'key': 'values'}, 'invalid'),
            call('analyze_spreadsheet', {k: v for k, v in delivery_plan()['statistics'][0].items() if k != 'key'}, 'stats'),
            create, review, lambda _: finish(ids=ids),
        ], reviewer=[inspect, lambda _: finish('complete_assignment', ids=ids)])
        result = await DocumentAgent(self.config, model).run('alice', '統計並製作 Excel 及 Word，交付前核對。', delivery_plan=delivery_plan())
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(len(result['files']), 2)
        self.assertTrue(all(f['lifecycle'] == 'final' for f in result['files']))
        self.assertEqual(len(result['delivery_validation']), 2)
        self.assertEqual({d['agent'] for d in result['delegations']}, {'reviewer'})
        self.assertEqual(len(result['tool_feedback']), 1)
        self.assertEqual(len(self.store.evidence('alice', result['job_id'])), 1)

    async def test_early_creation_and_invented_evidence_return_feedback_without_child_or_output(self):
        model = RawRoleModel(coordinator=[
            call('plan_delivery', delivery_plan(), 'plan'),
            call('delegate_document', {'task': '製作文件', 'requires_evidence': ['invented']}, 'bad'),
            finish(outcome='blocked', summary='尚未取得統計結果。'),
        ])
        result = await DocumentAgent(self.config, model).run('alice', '依真正統計結果製作文件。')
        self.assertEqual(result['status'], 'blocked', result)
        self.assertFalse(result['files'])
        self.assertFalse(result['delegations'])
        self.assertEqual(len(result['tool_feedback']), 1)
        self.assertEqual(set(model.invocations), {'coordinator'})

    async def test_completed_followup_explicitly_resolves_only_prior_same_role_blocker(self):
        self.file('source.txt', b'actual source')
        previous = []

        def redo(messages):
            previous.append(messages[-1].tool_call_id)
            return call('delegate_knowledge', {'task': '來源已提供，補做原閱讀任務',
                                             'resolves_assignments': previous}, 'retry')
        model = RawRoleModel(coordinator=[
            call('delegate_knowledge', {'task': '讀必要來源'}, 'initial'), redo, finish(summary='已讀到 actual source。'),
        ], knowledge=[
            finish('complete_assignment', outcome='blocked', summary='尚需來源位置。'),
            call('read_file', {'root': 'project', 'path': 'source.txt'}, 'read'),
            finish('complete_assignment', summary='來源實際內容為 actual source。'),
        ])
        result = await DocumentAgent(self.config, model).run('alice', '讀取必要來源。')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(result['delegations'][0]['status'], 'blocked')
        self.assertEqual(result['delegations'][1]['resolves_assignments'], previous)
        self.assertEqual(len(result['sources']), 1)

    async def test_chat_has_only_completion_and_short_prompt_and_no_source_discovery(self):
        self.config.orchestration_mode = 'chat'

        def direct(messages):
            self.assertLess(len(messages[0].content), 650)
            self.assertNotIn('可用根目錄', messages[0].content)
            return finish(summary='200 × 89% = 178。')
        model = RawRoleModel(coordinator=[direct])
        with patch('company_agent.runtime.middleware.visible_sources', side_effect=AssertionError('chat must not discover sources')):
            result = await DocumentAgent(self.config, model).run('alice', '200 的 89% 是多少？')
        self.assertEqual(set(model.schemas['coordinator']), {'complete_job'})
        self.assertEqual(result['model_steps'], 1)
        self.assertEqual(result['answer'], '200 × 89% = 178。')

    async def test_mode_cannot_change_on_resume(self):
        def interrupted(messages):
            raise RuntimeError('interrupted')
        model = RawRoleModel(coordinator=[interrupted])
        result = await self.failed_job(DocumentAgent(self.config, model).run('alice', '查詢'), RuntimeError)
        self.config.orchestration_mode = 'hybrid'
        with self.assertRaisesRegex(ValueError, '執行模式已變更'):
            await DocumentAgent(self.config, RawRoleModel()).resume('alice', result['job_id'])


class ExplicitRequirementTests(unittest.TestCase):
    def test_all_explicit_outputs_required_without_treating_references_as_outputs(self):
        from company_agent.services.delivery.explicit import check_explicit_cells, explicit_outputs
        text = '讀來源.xlsx，交付`統計.xlsx`（「結果」表A1=合計）與`說明.docx`，並參考`對照.docx`。不要建立`多餘.txt`。'
        self.assertEqual(explicit_outputs(text), {'統計.xlsx', '說明.docx'})
        self.assertEqual(explicit_outputs('交付memo.docx週報與恰好三頁meeting.pptx，參考source.docx。'), {'memo.docx', 'meeting.pptx'})
        incomplete = DeliveryPlan(files=[{'name': '統計.xlsx', 'cells': [{'sheet': '結果', 'cell': 'A1', 'expected': '合計'}]}])
        with self.assertRaisesRegex(ToolException, '說明.docx'):
            check_explicit_cells(text, incomplete)
        complete = incomplete.model_copy(deep=True)
        from company_agent.services.delivery.schemas import ExpectedFile
        complete.files.append(ExpectedFile(name='說明.docx'))
        check_explicit_cells(text, complete)

    def test_ambiguous_blank_is_not_inferred_as_literal_text(self):
        from company_agent.services.delivery.explicit import check_explicit_cells
        plan = DeliveryPlan(files=[{'name': 'safe.xlsx', 'cells': [{'sheet': '結果', 'cell': 'A1', 'expected': None}]}])
        check_explicit_cells('交付safe.xlsx（「結果」表A1=空白）。', plan)

    def test_wrong_format_rules_rejected_before_plan_can_be_committed(self):
        from pydantic import ValidationError
        with self.assertRaisesRegex(ValidationError, 'cells'):
            DeliveryPlan(files=[{'name': 'stats.xlsx', 'texts': [{'text': '合計15'}]}])
        with self.assertRaisesRegex(ValidationError, 'XLSX'):
            DeliveryPlan(files=[{'name': 'report.docx', 'cells': [{'sheet': '結果', 'cell': 'A1', 'expected': '合計'}]}])

    def test_named_output_cells_cannot_swap_text_labels_and_statistic_values(self):
        from company_agent.services.delivery.explicit import check_explicit_cells
        instruction = '請交付 checked.xlsx（「結果」工作表A1=合計/B1=合計值/A2=筆數/B2=筆數值）與 summary.docx。'
        correct = DeliveryPlan.model_validate(delivery_plan())
        check_explicit_cells(instruction, correct)
        wrong = correct.model_copy(deep=True)
        wrong.files[0].cells[0].expected = None
        wrong.files[0].cells[0].statistic = wrong.files[0].cells[1].statistic
        with self.assertRaisesRegex(ToolException, 'A1.*指定文字'):
            check_explicit_cells(instruction, wrong)
        wrong = correct.model_copy(deep=True)
        wrong.files[0].cells.clear()
        with self.assertRaisesRegex(ToolException, '漏列'):
            check_explicit_cells(instruction, wrong)
        # No general intent inference from ambiguous source descriptions.
        check_explicit_cells('來源工作表 A1=文字，請摘要。', wrong)


class TimeoutRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_role_timeout_reaches_http_request_and_is_checkpointed_without_retries(self):
        requests = []

        def handler(request):
            requests.append(request)
            return response('ok')
        model = gateway_for_verified_user(verified(), 'https://gateway.example/v1', 'key', 'lite', httpx.MockTransport(handler))
        self.addAsyncCleanup(close_model, model)
        settings = Settings(state_dir='/tmp/unused', model_timeout_seconds=45,
                            role_models={'data': ModelRoute(model='argon', request_timeout_seconds=150)})
        configured = routed_model(model, settings)
        await configured.for_task('data').bind_tools([]).ainvoke([HumanMessage(content='測試')])
        self.assertEqual(requests[0].extensions['timeout']['read'], 150)
        self.assertEqual(model.client.request_timeout, 60)
        self.assertEqual(model_policy(configured)['request_timeout_seconds'], 45)
        self.assertEqual(model_policy(configured)['routes']['data']['request_timeout_seconds'], 150)
        body = json.loads(requests[0].content)
        self.assertEqual(body['num_retries'], 0)
        self.assertEqual(body['fallbacks'], [])
