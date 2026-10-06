from __future__ import annotations

import hashlib
import json
import os
import sys
import unittest
from subprocess import CalledProcessError
from unittest.mock import patch

from langchain_core.messages import AIMessage
from pydantic import ValidationError

from company_agent.api.service import WorkQueue
from company_agent.runtime.execution import DocumentAgent
from company_agent.services.cad.sandbox import cad_worker
from company_agent.services.cad.schemas import CreateCad
from company_agent.services.cad.schemas import EditCad
from company_agent.services.cad.schemas import ReadCad
from company_agent.services.files.operations import ToolBox
from company_agent.services.sandbox import run_bounded
from company_agent.tests.test_agent import call
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_multi_agent import RawRoleModel


def sample():
    return CreateCad(
        name='測試.dxf', units='m', entities=[
            {'kind': 'line', 'point': [0, 0], 'end': [10, 0]},
            {'kind': 'circle', 'point': [5, 5], 'radius': 2.0},
            {'kind': 'text', 'point': [0, 2], 'text': '古坑測試', 'height': 1.0},
        ],
    )


def create():
    arg = sample()
    return cad_worker('create', units=arg.units, entities=[x.model_dump() for x in arg.entities])[1]


class CadContracts(unittest.TestCase):
    def test_chat_retains_fixed_cad_notice(self):
        from company_agent.services.cad.sandbox import NOTICE
        from company_agent.api.pipe import markdown
        result = {'status': 'done', 'answer': '完成', 'id': 'job', 'files': [], 'notices': [NOTICE, '<script>']}
        self.assertIn(NOTICE, markdown(result))
        self.assertNotIn('<script>', markdown(result))

    def test_reject_invalid_coordinates_units_paths_and_commands(self):
        for change in [
            {'units': 'ft'}, {'name': '../a.dxf'}, {'name': 'a.dwg'},
            {'entities': []}, {'entities': [{'kind': 'line', 'point': [0, 0], 'end': [0, 0]}]},
            {'entities': [{'kind': 'text', 'point': [0, 0], 'text': 'x\ny'}]},
            {'entities': [{'kind': 'circle', 'point': [float('nan'), 0], 'radius': 1.0}]},
        ]:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                CreateCad.model_validate({**sample().model_dump(), **change})
        for args in [{'view': 'shell'}, {'limit': 100}, {'query': 'x'*101}, {'start': -1}]:
            with self.assertRaises(ValidationError):
                ReadCad(root='p', path='a.dwg', **args)

    def test_dwg_write_and_bad_signature_never_launch_parser(self):
        with patch('company_agent.services.sandbox.run_bounded') as runner:
            for operation, data in [('edit', b'AC1032fake'), ('read', b'<html>')]:
                with self.assertRaises(ValueError):
                    cad_worker(operation, data=data, suffix='.dwg')
            runner.assert_not_called()

    def test_sandbox_contract(self):
        with patch('company_agent.services.sandbox.run_bounded', return_value=b'{"rows":[],"full_drawing_verified":false}') as runner:
            cad_worker('read', data=b'AC1032fake', suffix='.dwg')
            command = runner.call_args.args[0]
            self.assertIn('--read-only', command)
            self.assertIn('none', command)
            self.assertIn('never', command)
            self.assertNotIn('shell', command)
            self.assertNotIn('/var/run/docker.sock', ' '.join(command))
            self.assertIn('dst=/input,readonly', ' '.join(command))

    def test_invalid_worker_json_raises_native_error(self):
        import json
        with patch('company_agent.services.sandbox.run_bounded', return_value=b'{bad'), self.assertRaises(json.JSONDecodeError):
            cad_worker('read', data=b'AC1032fake', suffix='.dwg')

    def test_worker_output_bound(self):
        with patch('company_agent.services.sandbox.subprocess.run'), self.assertRaisesRegex(ValueError, '超過上限'):
            run_bounded([sys.executable, '-c', 'print("x"*31000)'], 'company-cad-unit-test')

    def test_worker_timeout_removes_only_own_container(self):
        with patch('company_agent.services.sandbox.time.monotonic', side_effect=[0, 61]), patch('company_agent.services.sandbox.subprocess.run') as cleanup:
            with self.assertRaisesRegex(ValueError, '60 秒'):
                run_bounded([sys.executable, '-c', 'import time; time.sleep(10)'], 'company-cad-unit-test')
            self.assertEqual(cleanup.call_args.args[0], ['docker', 'rm', '-f', 'company-cad-unit-test'])


@unittest.skipUnless(os.getenv('COMPANY_AGENT_CAD_TEST') == '1', 'Set COMPANY_AGENT_CAD_TEST=1 after building CAD image')
class CadContainerTests(unittest.TestCase):
    def test_real_create_paginate_read_edit_and_source_unchanged(self):
        data = create()
        sha = hashlib.sha256(data).hexdigest()
        page = cad_worker('read', data=data, view='entities', limit=1)
        self.assertEqual(page['next_start'], 1)
        self.assertEqual(page['total_matches'], 3)
        row = cad_worker('read', data=data, view='texts')['rows'][0]
        self.assertEqual(row['text'], '古坑測試')
        result, changed = cad_worker('edit', data=data, handle=row['handle'], expected=row['text'], replacement='核對完成')
        self.assertEqual(cad_worker('read', data=changed, view='texts')['rows'][0]['text'], '核對完成')
        self.assertEqual(hashlib.sha256(data).hexdigest(), sha)
        self.assertFalse(result['full_drawing_verified'])

    def test_malformed_and_mismatched_text_do_not_publish(self):
        for data, suffix in [(b'AC1032fake', '.dwg'), (b'garbage', '.dxf')]:
            with self.assertRaises((ValueError, CalledProcessError)):
                cad_worker('read', data=data, suffix=suffix)
        data = create()
        row = cad_worker('read', data=data, view='texts')['rows'][0]
        with self.assertRaises((ValueError, CalledProcessError)):
            cad_worker('edit', data=data, handle=row['handle'], expected='wrong', replacement='x')

    def test_complex_entity_rejected(self):
        # Add a LINE->RAY type to a synthetic file: must not emit an edited file.
        data = create().replace(b'\nLINE\n', b'\nRAY\n')
        with self.assertRaises((ValueError, CalledProcessError)):
            cad_worker('edit', data=data, handle='31', expected='古坑測試', replacement='x')


class CadToolTests(Fixture):
    def test_disabled_and_cross_owner_access(self):
        box = ToolBox(self.config, self.store, 'alice', self.job)
        with self.assertRaisesRegex(ValueError, '未啟用'):
            box.read_cad(ReadCad(root='project', path='a.dwg'))
        self.config.enable_cad = True
        self.file('a.dwg', b'AC1032fake')
        with patch('company_agent.services.cad.sandbox.cad_worker') as parser:
            for root, path in [('project', '../a.dwg'), ('missing', 'a.dwg')]:
                with self.assertRaises(ValueError):
                    box.read_cad(ReadCad(root=root, path=path))
            with self.assertRaises(ValueError):
                ToolBox(self.config, self.store, 'bob', self.job).read_cad(ReadCad(root='project', path='a.dwg'))
            parser.assert_not_called()

    def test_source_hash_revalidated_and_private_artifact(self):
        self.config.enable_cad = True
        box = ToolBox(self.config, self.store, 'alice', self.job)
        self.file('a.dxf', b'old')
        sha = hashlib.sha256(b'old').hexdigest()
        args = EditCad(
            root='project', path='a.dxf', sha256=sha, handle='FF',
            expected='old', replacement='new', name='new.dxf',
        )
        with patch('company_agent.services.cad.sandbox.cad_worker', return_value={'rows': []}):
            with self.assertRaisesRegex(ValueError, '先在本工作'):
                box.edit_cad_text(args)
            box.read_cad(ReadCad(root='project', path='a.dxf'))
            self.file('a.dxf', b'changed')
            with self.assertRaisesRegex(ValueError, '來源已變更'):
                box.edit_cad_text(args)
        with patch('company_agent.services.cad.sandbox.cad_worker', return_value=({'created': True}, b'new-dxf')):
            artifact = box.create_cad(sample())
        self.assertEqual(self.store.read_artifact('alice', artifact['id'], self.job)[2], b'new-dxf')
        with self.assertRaises(ValueError):
            self.store.read_artifact('bob', artifact['id'], self.job)
        self.assertFalse((self.root/sample().name).exists())

    def test_web_queue_accepts_cad(self):
        queue = WorkQueue(self.private, self.base/'queue_sources')
        key = queue.submit(
            'alice', 'subject', 'chat', 'request', '讀圖', [
                ('sample.dwg', b'AC1032sample'), ('draft.dxf', b'dxf'),
            ],
        )
        self.assertEqual(
            [
                r['path'] for r in queue.get('alice', key)['payload']
                ['manifest']
            ], ['input_1.dwg', 'input_2.dxf'],
        )


class CadGraphTests(unittest.IsolatedAsyncioTestCase, Fixture):
    @unittest.skipUnless(os.getenv('COMPANY_AGENT_CAD_TEST') == '1', 'Needs CAD image')
    async def test_real_cad_tools_typed_completion_and_reviewer(self):
        from company_agent.tests.test_enterprise_completion import finish
        self.config.enable_cad = True
        self.config.require_artifact_review = True
        self.config.structured_completion = True
        ids = []

        def done(messages):
            ids.append(json.loads(messages[-1].content)['id'])
            return finish('complete_assignment', ids=ids)
        model = RawRoleModel(
            coordinator=[
                call('delegate_engineering', {'task': '依明確座標建立 DXF 草稿'}, 'eng'),
                call('delegate_reviewer', {'task': '實際讀取 DXF 圖元並核對'}, 'review'), lambda _:finish(ids=ids),
            ],
            engineering=[call('create_cad', sample().model_dump(), 'create'), done],
            reviewer=[
                lambda _:call(
                    'inspect_artifact', {'artifact_id': ids[0]},
                    'inspect',
                ), lambda _:finish('complete_assignment', ids=ids),
            ],
        )
        result = await DocumentAgent(self.config, model).run('alice', '以公尺建立線 (0,0) 到 (10,0)、圓心 (5,5) 半徑 2、文字古坑測試在 (0,2) 高 1。另存 DXF。')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(result['completion_protocol'], 'typed_tool')
        self.assertFalse(result['artifact_review']['unreviewed_ids'])
        self.assertTrue(result['notices'])
        self.assertEqual(len(result['files']), 1)

    async def test_engineering_handoff_sources_review_and_completion(self):
        self.config.enable_cad = True
        self.config.require_artifact_review = True
        self.file('a.dwg', b'AC1032fake')

        def inspect(messages):
            # Artifact is only passed through coordinator handoff context.
            return call('inspect_artifact', {'artifact_id': self.store.files('alice', self.active_job)[0]['id']}, 'inspect')
        model = RawRoleModel(
            coordinator=[
                call('delegate_engineering', {'task': '讀來源並新建示意 DXF'}, 'eng'),
                call('delegate_reviewer', {'task': '校核 DXF'}, 'review'), AIMessage(content='完成示意圖，特殊物件未完整解析。'),
            ],
            engineering=[
                call('read_cad', {'root': 'project', 'path': 'a.dwg'}, 'read'), call(
                    'create_cad', sample().model_dump(), 'make',
                ), AIMessage(content='已建立草稿。'),
            ],
            reviewer=[inspect, AIMessage(content='已讀取结构，非視覺驗證。')],
        )
        from company_agent.runtime.storage import Store
        method = Store.artifact

        def remember(store, owner, job, name, data):
            self.active_job = job
            return method(store, owner, job, name, data)

        def fake(operation, **kwargs): return ({'created': True}, b'dxf') if operation == 'create' else {
            'rows': [], 'unknown_entity_count': 11, 'full_drawing_verified': False,
        }
        with patch('company_agent.services.cad.sandbox.cad_worker', side_effect=fake), patch.object(Store, 'artifact', remember):
            result = await DocumentAgent(self.config, model).run('alice', '讀 a.dwg，另外新建簡單示意圖')
        self.assertEqual(result['status'], 'done', result)
        self.assertIn('engineering', result['agent_visits'])
        self.assertTrue(result['sources'])
        self.assertTrue(result['notices'])
        self.assertFalse(result['artifact_review']['unreviewed_ids'])
