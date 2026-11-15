from __future__ import annotations

import copy
import tempfile
import unittest
from unittest.mock import AsyncMock

from .test_eip_calendar_verification import EVENT
from .test_eip_calendar_verification import fixture
from examples.official_document_workflow.infrastructure.eip.calendar_projects import assign_project
from examples.official_document_workflow.infrastructure.eip.calendar_projects import match_project
from examples.official_document_workflow.infrastructure.eip.calendar_projects import project_assignment_parameters
from examples.official_document_workflow.infrastructure.eip.calendar_projects import project_name_from_description
from examples.official_document_workflow.infrastructure.eip.calendar_projects import verify_project_assignment_detail
from examples.official_document_workflow.infrastructure.eip.calendar_sync import write_project_assignments
from examples.official_document_workflow.infrastructure.eip.netask import JSArrayResponseParser
from examples.official_document_workflow.infrastructure.storage.state import Journal
from tests.eip_postgres import EIPPostgresTestCase


class ProjectMatchingTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    def test_midnight_end_survives_project_and_attachment_parameters(self):
        from examples.official_document_workflow.infrastructure.eip.calendar_attachments import attachment_parameters
        line, detail, _ = fixture()
        fields = line.split(',')
        fields[2] = '1260'
        fields[4] = '1440'
        line = ','.join(fields)
        detail[0][11] = '1260'
        detail[0][12] = '1440'
        params = project_assignment_parameters(detail, line, '1709', EVENT, '77')
        self.assertEqual((params['start_time'], params['end_time']), ('1260', '1440'))
        detail[0][4] = '77'
        params = attachment_parameters(
            detail, line, '1709', EVENT, '765',
            [{'id': '123', 'filename': '改名後.pdf', 'url': '/fo/v/123', 'size': 3}], project_id='77',
        )
        self.assertEqual((params['start_time'], params['end_time']), ('1260', '1440'))
        self.assertEqual(params['proj_id'], '77')
        self.assertEqual(params['isMailtoShared'], '')

    def test_sparse_pm_list_is_parsed_without_javascript_execution(self):
        raw = 'var data=[[0,""],["",,,,"77","","01-%u4EE3%u8FA6%u570B%u90533%u865F"]]; legacy();'
        data = JSArrayResponseParser(raw).parse()
        self.assertEqual(data[1][4], '77')
        with self.assertRaises((ValueError, ValueError)):
            JSArrayResponseParser('var data=[evil()];').parse()

    def test_only_unique_current_project_can_match(self):
        current = [
            {'id': '77', 'name': '01-代辦國道3號古坑交流道平面側車道工程'},
            {'id': '79', 'name': '01-台3線楠西橋改建工程'},
        ]
        match = match_project('代辦國道3號古坑交流道平面側車道工程', current)
        self.assertEqual(match['project'], current[0])
        self.assertEqual(match_project('古坑工程', current)['status'], 'unmatched')
        self.assertEqual(match_project('台3線楠西橋改建工程', current * 2)['status'], 'ambiguous')
        description = '辦理事項：會勘\n工程名稱：代辦國道3號古坑交流道平面側車道工程\n日期：2026/09/08'
        self.assertEqual(project_name_from_description(description), '代辦國道3號古坑交流道平面側車道工程')
        self.assertIsNone(project_name_from_description(description + '\n工程名稱：另一工程'))

    async def test_project_update_changes_only_project_and_reads_back(self):
        line, detail, _ = fixture()
        after = copy.deepcopy(detail)
        after[0][4] = '77'
        adapter = type('Adapter', (), {})()
        adapter.user_id = '1709'
        adapter.calendar_detail = AsyncMock(side_effect=[detail, after])
        adapter.rpc = AsyncMock(return_value=[[0, '']])
        receipt = await assign_project(adapter, EVENT, line, '77')
        self.assertTrue(receipt['detail_verified'])
        self.assertEqual(adapter.rpc.call_args.args, ('Update',))
        self.assertEqual(adapter.rpc.call_args.kwargs['proj_id'], '77')
        self.assertEqual(adapter.rpc.call_args.kwargs['task_id'], '')
        self.assertEqual(project_assignment_parameters(detail, line, '1709', EVENT, '77')['public'], '0')

    async def test_only_new_receipts_are_changed_and_unmatched_stays_private(self):
        line, _, _ = fixture()
        adapter = type('Adapter', (), {})()
        adapter.user_id = '1709'
        adapter.assign_calendar_project = AsyncMock(return_value={'detail_verified': True})
        matched = {
            'line': line, 'receipt': {'event_id': EVENT}, 'project_match': {
                'status': 'matched', 'document_project': '工程甲', 'project': {'id': '77', 'name': '01-工程甲', 'members': ['Ru1709', 'Ru1721']},
            },
        }
        unmatched = {
            'line': line, 'receipt': {'event_id': EVENT}, 'project_match': {
                'status': 'unmatched', 'document_project': '工程乙',
            },
        }
        old = {'line': line, 'project_match': matched['project_match']}
        with tempfile.TemporaryDirectory() as _:
            result = await write_project_assignments(adapter, [matched, unmatched, old], Journal())
        self.assertEqual((result['assigned_events'], result['private_events'], result['skipped_events']), (1, 1, 1))
        adapter.assign_calendar_project.assert_awaited_once_with(EVENT, line, '77', sharing=['Ru1709', 'Ru1721'])

    def test_live_editor_normalization_allowed_but_wrong_sharing_rejected(self):
        line, before, _ = fixture()
        after = copy.deepcopy(before)
        after[0][4] = '77'
        after[0][15] = 'Ru1721,Ru1709'
        after[0][31:34] = ['0', '0000', '0']
        members = ['Ru1709', 'Ru1721']
        self.assertTrue(
            verify_project_assignment_detail(
                before, after, line,
                '1709', EVENT, '77', sharing=members,
            )['detail_verified'],
        )
        for index, value in ((4, '79'), (15, 'Ru1709,Ru999'), (12, '780'), (14, 'changed'), (33, '1'), (39, 'changed')):
            bad = copy.deepcopy(after)
            bad[0][index] = value
            with self.assertRaises(ValueError):
                verify_project_assignment_detail(before, bad, line, '1709', EVENT, '77', sharing=members)
        fields = line.split(',')
        fields[5] = '1'
        fields[2] = fields[4] = ''
        before[0][10] = '1'
        before[0][11] = before[0][12] = ''
        after[0][10] = '1'
        after[0][11] = after[0][12] = '480'
        self.assertTrue(
            verify_project_assignment_detail(
                before, after, ','.join(
                    fields,
                ), '1709', EVENT, '77', sharing=members,
            )['detail_verified'],
        )

    async def test_human_changed_content_is_rejected_before_project_write(self):
        line, detail, _ = fixture()
        detail[0][14] = 'human edited'
        adapter = type('Adapter', (), {'user_id': '1709'})()
        adapter.calendar_detail = AsyncMock(return_value=detail)
        adapter.rpc = AsyncMock()
        with self.assertRaises(ValueError):
            await assign_project(adapter, EVENT, line, '77', sharing=['Ru1709'])
        adapter.rpc.assert_not_awaited()

    async def test_unknown_project_update_never_replayed(self):
        line, _, _ = fixture()
        adapter = type('Adapter', (), {'user_id': '1709'})()
        adapter.assign_calendar_project = AsyncMock(side_effect=ValueError('unknown'))
        event = {
            'receipt': {'event_id': EVENT}, 'line': line, 'project_match': {
                'status': 'matched', 'project': {'id': '77', 'name': '工程', 'members': ['Ru1709']},
            },
        }
        with tempfile.TemporaryDirectory() as _:
            journal = Journal()
            for _ in range(2):
                with self.assertRaises(ValueError):
                    await write_project_assignments(adapter, [event], journal)
        self.assertEqual(adapter.assign_calendar_project.await_count, 1)
