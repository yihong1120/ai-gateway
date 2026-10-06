from __future__ import annotations

import io
import json
import unittest
import zipfile
from subprocess import CalledProcessError
from types import SimpleNamespace

from langchain_core.messages import ToolMessage
from langchain_core.tools import ToolException

from company_agent.agents.document.revision import artifact_edit_tool
from company_agent.agents.document.revision import revise
from company_agent.runtime.context import Context
from company_agent.runtime.execution import DocumentAgent
from company_agent.services.office.documents import create_slides
from company_agent.services.office.documents import create_spreadsheet
from company_agent.services.office.documents import create_word
from company_agent.services.office.documents import extract
from company_agent.tests.test_agent import call
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_enterprise_completion import finish
from company_agent.tests.test_multi_agent import RawRoleModel


class PrivateRevisionTests(unittest.IsolatedAsyncioTestCase, Fixture):
    def seed(self, suffix='.pptx'):
        data = (
            create_slides([{'title': '工程進度', 'bullets': ['進度：待確認', '保留此段內容']}]) if suffix == '.pptx' else
            create_word('工程進度', [{'text': '進度：待確認', 'heading': False}, {'text': '保留此段內容', 'heading': False}])
        )
        artifact = self.store.artifact('alice', self.job, 'draft'+suffix, data)
        blocks = extract(data, suffix)
        run = next(r for b in blocks for r in b['runs'] if r['text'] == '進度：待確認')
        receipt = ToolMessage(
            content=json.dumps({'artifact_id': artifact['id'], 'sha256': artifact['sha256'], 'blocks': blocks}),
            name='inspect_artifact', tool_call_id='inspect',
        )
        runtime = SimpleNamespace(
            context=Context('alice', self.job, self.config, self.store), state={'messages': [receipt]},
            tool_call_id='revision', stream_writer=lambda _: None,
        )
        args = {
            'artifact_id': artifact['id'], 'sha256': artifact['sha256'], 'location': run['location'],
            'expected': run['text'], 'replacement': '進度：已確認', 'name': 'revised'+suffix,
        }
        return data, artifact, runtime, args

    async def test_word_and_slides_local_revision_preserves_other_parts_and_replay(self):
        for suffix in ('.docx', '.pptx'):
            data, artifact, runtime, args = self.seed(suffix)
            runtime.tool_call_id = 'revision'+suffix
            command = await revise(runtime, **args)
            value = json.loads(command.update['messages'][0].content)
            changed = self.store.read_artifact('alice', value['id'], self.job)[2]
            self.assertEqual(self.store.read_artifact('alice', artifact['id'], self.job)[2], data)
            text = ''.join(b['text'] for b in extract(changed, suffix))
            self.assertIn('進度：已確認', text)
            self.assertIn('保留此段內容', text)
            part = args['location'].split('#')[0]
            with zipfile.ZipFile(io.BytesIO(data)) as a, zipfile.ZipFile(io.BytesIO(changed)) as b:
                self.assertEqual(set(a.namelist()), set(b.namelist()))
                self.assertTrue(all(a.read(n) == b.read(n) for n in a.namelist() if n != part))
            again = await revise(runtime, **args)
            self.assertEqual(json.loads(again.update['messages'][0].content)['id'], value['id'])
        self.assertEqual(len(self.store.files('alice', self.job)), 4)
        self.assertFalse(list(self.root.iterdir()))

    async def test_no_inspection_wrong_hash_location_text_and_cross_job_rejected(self):
        _, _artifact, runtime, args = self.seed()
        bad = [
            {**args, 'sha256': '0'*64}, {**args, 'expected': '猜測內容'},
            {**args, 'location': 'ppt/slides/slide1.xml#t:99'},
        ]
        for value in bad:
            with self.assertRaises(ValueError):
                await revise(runtime, **value)
        runtime.state = {'messages': []}
        with self.assertRaises(ValueError):
            await revise(runtime, **args)
        runtime.context = Context('alice', self.store.job('alice'), self.config, self.store)
        with self.assertRaises(ValueError):
            await revise(runtime, **args)
        runtime.context = Context('bob', self.job, self.config, self.store)
        with self.assertRaises(ValueError):
            await revise(runtime, **args)
        self.assertEqual(len(self.store.files('alice', self.job)), 1)

    async def test_different_filename_required_and_no_newlines(self):
        _, _artifact, runtime, args = self.seed()
        for index, extra in enumerate([{'name': 'draft.pptx'}, {'name': '../escape.pptx'}, {'replacement': '換\n行'}]):
            runtime.tool_call_id = str(index)
            with self.assertRaises((ValueError, CalledProcessError)):
                await revise(runtime, **{**args, **extra})
        self.assertEqual(len(self.store.files('alice', self.job)), 1)

    def test_runtime_never_in_model_schema(self):
        self.assertNotIn('runtime', artifact_edit_tool().tool_call_schema.model_json_schema()['properties'])


class RevisionGraphTests(unittest.IsolatedAsyncioTestCase, Fixture):
    def setUp(self):
        super().setUp()
        self.config.structured_completion = True
        self.config.require_artifact_review = True

    async def run_model(self, **scripts):
        model = self.last_model = RawRoleModel(**scripts)
        result = await DocumentAgent(self.config, model).run('alice', '建立並修訂本工作簡報草稿，不發布。')
        self.assertTrue(result['answer'].strip())
        self.assertFalse(list(self.root.iterdir()))
        return result, model

    async def test_create_inspect_revise_review_both_and_complete(self):
        ids = []

        def inspect_original(messages):
            ids.append(json.loads(messages[-1].content)['id'])
            return call('inspect_artifact', {'artifact_id': ids[0]}, 'inspect-old')

        def change(messages):
            value = json.loads(messages[-1].content)
            run = next(r for b in value['blocks'] for r in b['runs'] if r['text'] == '進度：待確認')
            return call(
                'edit_artifact_text_run', {
                    'artifact_id': ids[0], 'sha256': value['sha256'],
                    'location': run['location'], 'expected': run['text'], 'replacement': '進度：已確認', 'name': 'revised.pptx',
                }, 'change',
            )

        def completed(messages):
            ids.append(json.loads(messages[-1].content)['id'])
            return finish('complete_assignment', ids=ids)
        self.config.max_steps = 20
        result, _ = await self.run_model(
            coordinator=[
                call('delegate_document', {'task': '建立再修訂草稿'}, 'd'),
                call('delegate_reviewer', {'task': '核對原版及修訂版'}, 'r'), lambda _:finish(ids=ids),
            ],
            document=[
                call(
                    'create_slides', {
                        'name': 'draft.pptx', 'slides': [
                            {'title': '工程進度', 'bullets': ['進度：待確認']},
                        ],
                    }, 'create',
                ),
                inspect_original, change, completed,
            ],
            reviewer=[
                lambda _:call('inspect_artifact', {'artifact_id': ids[0]}, 'old'),
                lambda _:call('inspect_artifact', {'artifact_id': ids[1]}, 'new'), lambda _:finish(
                    'complete_assignment', ids=ids,
                ),
            ],
        )
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(len(result['files']), 2)
        self.assertFalse(result['artifact_review']['unreviewed_ids'])

    async def test_new_revision_does_not_inherit_original_review(self):
        ids = []

        def original(messages):
            ids.append(json.loads(messages[-1].content)['id'])
            return finish('complete_assignment', ids=ids)

        def change(messages):
            value = json.loads(messages[-1].content)
            run = next(r for b in value['blocks'] for r in b['runs'] if r['text'] == '待確認')
            return call(
                'edit_artifact_text_run', {
                    'artifact_id': ids[0], 'sha256': value['sha256'], 'location': run['location'],
                    'expected': '待確認', 'replacement': '已確認', 'name': 'revised.docx',
                }, 'change',
            )

        def revised(messages):
            ids.append(json.loads(messages[-1].content)['id'])
            return finish('complete_assignment', ids=ids)
        self.config.max_steps = 20
        result = await self.failed_job(
            self.run_model(
                coordinator=[
                    call('delegate_document', {'task': '建立草稿'}, 'd1'), call('delegate_reviewer', {'task': '讀原草稿'}, 'r'),
                    call('delegate_document', {'task': '讀取再修訂草稿'}, 'd2'), lambda _:finish(ids=ids),
                    lambda _:finish(outcome='blocked', summary='新版仍待審核', ids=ids),
                ],
                document=[
                    call(
                        'create_word', {
                            'name': 'draft.docx', 'title': '工程', 'paragraphs': [
                                {'text': '待確認', 'heading': False},
                            ],
                        }, 'create',
                    ), original,
                    lambda _:call('inspect_artifact', {'artifact_id': ids[0]}, 'read'), change, revised,
                ],
                reviewer=[
                    lambda _:call(
                        'inspect_artifact', {'artifact_id': ids[0]},
                        'read-old',
                    ), lambda _:finish('complete_assignment', ids=ids),
                ],
            ), ToolException,
        )
        self.assertEqual(result['status'], 'running', result)
        self.assertIn('未經獨立審核', str(self.native_error))
        self.assertEqual({f['id'] for f in result['files']}, set(ids))

    async def test_statistics_handoff_to_word_uses_real_result_and_review(self):
        self.file('source.xlsx', create_spreadsheet([{'name': '工程', 'rows': [[10], [20], ['不計入']]}]))
        ids = []
        stats = {}

        def data_done(messages):
            stats.update(json.loads(messages[-1].content))
            self.assertEqual((stats['sum'], stats['count']), (30, 2))
            return finish('complete_assignment', summary='合計30；數值筆數2；排除文字1筆。')

        def document_task(messages):
            handoff = json.loads(messages[-1].content)
            self.assertIn('合計30', handoff['summary'])
            return call('delegate_document', {'task': '依統計製作報告', 'context': handoff['summary']}, 'doc')

        def document_create(messages):
            self.assertIn('合計30', messages[1].content)
            return call(
                'create_word', {
                    'name': 'summary.docx', 'title': '統計結果', 'paragraphs': [
                        {'text': f"合計 {stats['sum']}；數值筆數 {stats['count']}；排除非數值文字。", 'heading': False},
                    ],
                }, 'create',
            )

        def document_done(messages):
            ids.append(json.loads(messages[-1].content)['id'])
            return finish('complete_assignment', ids=ids)
        model = self.last_model = RawRoleModel(
            coordinator=[
                call('delegate_data', {'task': '統計 source.xlsx 工程 A1:A3'}, 'data'), document_task,
                call('delegate_reviewer', {'task': '核對統計報告'}, 'review'), lambda _:finish(ids=ids),
            ],
            data=[
                call(
                    'analyze_spreadsheet', {
                        'root': 'project', 'path': 'source.xlsx',
                        'sheet': '工程', 'column': 1, 'start_row': 1, 'end_row': 3,
                    }, 'stats',
                ), data_done,
            ],
            document=[document_create, document_done], reviewer=[
                lambda _:call(
                    'inspect_artifact', {'artifact_id': ids[0]}, 'inspect',
                ), lambda _:finish('complete_assignment', ids=ids),
            ],
        )
        self.config.max_steps = 20
        result = await DocumentAgent(self.config, model).run('alice', 'Excel 統計交給文件角色製作 Word，獨立審核後交付。')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(set(result['agent_visits']), {'coordinator', 'data', 'document', 'reviewer'})

    async def test_missing_sheet_propagates_before_further_model_calls(self):
        self.file('source.xlsx', create_spreadsheet([{'name': '工程', 'rows': [[10], [20]]}]))
        model = self.last_model = RawRoleModel(
            coordinator=[call('delegate_data', {'task': '讀工作表'}, 'd'), finish(summary='實際工作表為工程，已完成讀取。')],
            data=[
                call('read_spreadsheet', {'root': 'project', 'path': 'source.xlsx', 'sheet': '工程表'}, 'wrong'),
                call(
                    'read_spreadsheet', {
                        'root': 'project', 'path': 'source.xlsx',
                        'sheet': '工程', 'start_row': 1,
                    }, 'correct',
                ),
                finish('complete_assignment', summary='實際工作表為工程，已讀取。'),
            ],
        )
        result = await self.failed_job(DocumentAgent(self.config, model).run('alice', '讀取 Excel，回報實際內容。'), CalledProcessError)
        self.assertEqual(result['status'], 'running', result)
        self.assertNotIn('errors', result)
        self.assertIn(b'ToolException', self.native_error.stderr)

    async def test_document_role_can_verify_excel_without_generic_file_reader(self):
        self.file('source.xlsx', create_spreadsheet([{'name': '工程', 'rows': [[10], [20]]}]))
        model = self.last_model = RawRoleModel(
            coordinator=[call('delegate_document', {'task': '核對 Excel 原始值'}, 'd'), finish(summary='已核對來源值。')],
            document=[
                call('read_spreadsheet', {'root': 'project', 'path': 'source.xlsx'}, 'read'),
                finish('complete_assignment', summary='來源 A1=10，A2=20。'),
            ],
        )
        result = await DocumentAgent(self.config, model).run('alice', '核對來源 Excel，不計算、不修改。')
        self.assertEqual(result['status'], 'done', result)
        self.assertNotIn('errors', result)
        self.assertNotIn('analyze_spreadsheet', model.schemas['document'])
