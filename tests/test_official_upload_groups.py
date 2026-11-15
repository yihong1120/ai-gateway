"""Chat entry, actual joint validation/CSV generation; model/OCR/storage are mocked."""
from __future__ import annotations

import asyncio
import copy
import csv
import io
import json
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

from pydantic import TypeAdapter
from pydantic import ValidationError

from .test_eip_group_processing import payload
from .test_official_document_processor_pipe import DEADLINE_EVIDENCE
from .test_official_document_processor_pipe import PIPE_MODULE as M
from .test_official_document_processor_pipe import SOURCE
from examples.official_document_workflow.application.group_processing import parse_group
from examples.official_document_workflow.application.long_source import group_text
from examples.official_document_workflow.application.multi_agent import GroupExporter
from examples.official_document_workflow.application.outcomes import DocumentResult
from examples.official_document_workflow.application.upload_batch import group_context
from examples.official_document_workflow.application.upload_batch import UploadAnalysis
from examples.official_document_workflow.application.upload_batch import validate_groups
from examples.official_document_workflow.domain.calendar.contracts import GROUP_ANALYSIS_SCHEMA
from examples.official_document_workflow.domain.calendar.contracts import GroupAnalysis
from examples.official_document_workflow.domain.documents import OutputUncertain
from examples.official_document_workflow.infrastructure.storage.processing_cache import upload_outcome
from examples.official_document_workflow.domain.workflow import JobPlan


class UploadGroupTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def unnamed_attachments(grouping):
        """Fixtures without title assertions explicitly declare unknown names."""
        if 'attachment_names' not in grouping:
            groups = {g['reference_number']: g for g in grouping['groups']}
            targets = {
                tuple(
                    sorted(
                        groups[link['reference_number']]['main_pages'] if link.get('reference_number')
                        else link['page_ids'],
                    ),
                )
                for g in grouping['groups'] for link in g['attachments']
            }
            grouping['attachment_names'] = [
                {'page_ids': list(pages), 'title': '', 'evidence_page_id': ''} for pages in sorted(targets)
            ]
        return grouping

    def linked_letters(self):
        first = payload(('S1',))
        first['read_source_ids'] = ['S1', 'S2', 'S3']
        own_ref = first['document']['reference_number']
        child_ref = '設計公司字第1150000012號'
        quote = f'附件：附件1-設計文（{child_ref}）及附件2-審查意見各1份。'
        self.add(SOURCE + '\n' + quote)
        self.add(SOURCE.replace(own_ref, child_ref).replace('115年8月13日', '115年8月12日') + '\n設計文')
        self.add('審查意見表')
        child = payload(('S1',))
        child['document']['reference_number'] = child['document']['evidence']['reference_number'] = child_ref
        child['document']['issued_date'] = '2026-08-12'
        child['document']['evidence']['issued_date'] = '115年8月12日'
        grouping = {
            'groups': [
                {
                    'reference_number': own_ref, 'main_pages': ['S1:P1'], 'related_reference_numbers': [],
                    'unread_source_ids': [], 'attachments': [
                        {
                            'page_ids': [], 'reference_number': child_ref, 'original_label': '附件1',
                            'evidence_page_id': 'S1:P1', 'evidence_text': quote,
                        },
                        {
                            'page_ids': ['S3:P1'], 'original_label': '附件2',
                            'evidence_page_id': 'S1:P1', 'evidence_text': quote,
                        },
                    ],
                },
                {
                    'reference_number': child_ref, 'main_pages': ['S2:P1'], 'related_reference_numbers': [],
                    'unread_source_ids': [], 'attachments': [],
                },
            ], 'attachment_names': [
                {'page_ids': ['S2:P1'], 'title': '設計文', 'evidence_page_id': 'S2:P1'},
                {'page_ids': ['S3:P1'], 'title': '審查意見表', 'evidence_page_id': 'S3:P1'},
            ], 'unassigned_source_ids': [],
        }
        return grouping, first, child

    async def test_parent_keeps_attachment_numbers_and_links_childs_own_files(self):
        import pymupdf
        grouping, first, child = self.linked_letters()
        own_ref, child_ref = (g['reference_number'] for g in grouping['groups'])
        answer = await self.run_upload(grouping, first, child)
        parent_section, child_section = answer.split('### 公文組 ')[1:]
        self.assertIn(f'附件1(設計文)：對應函件「{child_ref}」（公文組 2）', parent_section)
        self.assertIn('附件2(審查意見表) PDF', parent_section)
        self.assertNotIn('附件1(審查意見)', answer)
        self.assertLess(parent_section.index('附件1(設計文)'), parent_section.index('附件2(審查意見表)'))
        self.assertIn(f'本函為「{own_ref}」的附件1(設計文)', child_section)
        pdfs = [f for f in self.saved if f['kind'] == 'document_pdf']
        self.assertEqual(len(pdfs), 3)
        self.assertEqual(
            [f['extra_metadata']['official_document_references'] for f in pdfs],
            [[own_ref], [own_ref], [child_ref]],
        )
        self.assertTrue(pdfs[2]['display_name'].startswith('1150812_' + child_ref))
        for file, expected in zip(pdfs, ['Source file 1', 'Source file 3', 'Source file 2']):
            with pymupdf.open(stream=file['payload'], filetype='pdf') as pdf:
                self.assertEqual([p.get_text().strip() for p in pdf], [expected])
        for n in (5, 6):
            self.assertEqual(answer.count(f'/api/v1/files/saved-{n}/content'), 2)
        self.assertEqual(self.pipe.render_word.call_count, 3)
        self.assertEqual(self.pipe.ocr_context.await_count, 3)
        self.assertEqual(self.pipe.analyze_text.await_count, 1)
        scope = self.scopes[0]
        self.assertEqual(set(scope['read_source_ids']), {'S1', 'S2', 'S3'})
        self.assertEqual(scope['related_reference_numbers'], [child_ref])
        self.assertEqual(pdfs[0]['extra_metadata']['official_document_attachments'][0]['original_label'], '附件1')
        self.assertEqual(
            pdfs[2]['extra_metadata']['official_document_parent_references']
            [0]['reference_number'], own_ref,
        )

    async def test_multiple_parents_reuse_child_and_keep_each_original_label(self):
        grouping, first, child = self.linked_letters()
        third_ref = '另一母函字第1150000033號'
        child_ref = grouping['groups'][1]['reference_number']
        quote = f'附件四：設計文（{child_ref}）。'
        self.add(SOURCE.replace(first['document']['reference_number'], third_ref) + '\n' + quote)
        third = payload(('S1',))
        third['read_source_ids'] = ['S1', 'S2']
        third['document']['reference_number'] = third['document']['evidence']['reference_number'] = third_ref
        grouping['groups'].append({
            'reference_number': third_ref, 'main_pages': ['S4:P1'], 'related_reference_numbers': [],
            'unread_source_ids': [], 'attachments': [
                {
                    'page_ids': [], 'reference_number': child_ref, 'original_label': '附件四',
                    'evidence_page_id': 'S4:P1', 'evidence_text': quote,
                },
            ],
        })
        answer = await self.run_upload(grouping, first, child, third)
        self.assertEqual(sum(f['kind'] == 'document_pdf' for f in self.saved), 4)
        self.assertEqual(self.pipe.render_word.call_count, 4)
        self.assertEqual(answer.count('/api/v1/files/saved-5/content'), 3)
        self.assertIn(f'附件四(設計文)：對應函件「{child_ref}」（公文組 2）', answer.split('### 公文組 3')[1])
        self.assertEqual(self.pipe.analyze_text.await_count, 1)

    async def test_citation_alone_does_not_display_an_attachment_relationship(self):
        grouping, first, child = self.linked_letters()
        parent = grouping['groups'][0]
        parent['attachments'] = parent['attachments'][1:]
        grouping['attachment_names'] = grouping['attachment_names'][1:]
        parent['related_reference_numbers'] = [grouping['groups'][1]['reference_number']]
        answer = await self.run_upload(grouping, first, child)
        self.assertNotIn('對應函件', answer)
        self.assertNotIn('本函為', answer)
        self.assertEqual(answer.count('/api/v1/files/saved-5/content'), 1)

    async def test_unuploaded_citation_does_not_block_a_letter_without_attachments(self):
        cited_ref = '外部機關字第1150000042號'
        self.add(SOURCE + f'\n附件：無\n說明：依據{cited_ref}函辦理。')
        data = payload(('S1',))
        own_ref = data['document']['reference_number']
        grouping = {
            'groups': [{
                'reference_number': own_ref, 'main_pages': ['S1:P1'], 'attachments': [],
                'related_reference_numbers': [cited_ref, own_ref, cited_ref], 'unread_source_ids': [],
            }], 'attachment_names': [], 'unassigned_source_ids': [],
        }
        answer = await self.run_upload(grouping, data)
        self.assertIn('整理為 1 組', answer)
        self.assertNotIn('處理暫時失敗', answer)
        self.assertNotIn('對應函件', answer)
        scope = self.scopes[0]
        self.assertEqual(scope['read_source_ids'], ['S1'])
        self.assertEqual(scope['related_reference_numbers'], [])
        pdfs = [f for f in self.saved if f['kind'] == 'document_pdf']
        self.assertEqual(len(pdfs), 1)
        self.assertEqual(pdfs[0]['extra_metadata']['official_document_related_references'], [])
        self.assertEqual(self.pipe.analyze_text.await_count, 1)

    async def test_context_keeps_only_distinct_other_uploaded_letters(self):
        grouping, first, child = self.linked_letters()
        parent = grouping['groups'][0]
        child_ref = grouping['groups'][1]['reference_number']
        parent['attachments'] = parent['attachments'][1:]
        grouping['attachment_names'] = grouping['attachment_names'][1:]
        parent['related_reference_numbers'] = [
            child_ref, '外部機關字第1150000042號',
            parent['reference_number'], child_ref,
        ]
        answer = await self.run_upload(grouping, first, child)
        self.assertIn('整理為 2 組', answer)
        self.assertNotIn('對應函件', answer)
        scope = self.scopes[0]
        self.assertEqual(scope['related_reference_numbers'], [child_ref])
        self.assertEqual(set(scope['read_source_ids']), {'S1', 'S2', 'S3'})
        self.assertEqual(self.pipe.analyze_text.await_count, 1)

    async def test_link_target_and_original_number_require_original_evidence(self):
        grouping, _, _ = self.linked_letters()
        sources = {f'S{i}': self.source_by_path[p.pdf_path] for i, p in enumerate(self.prepared, 1)}
        for changes in (
            {'reference_number': grouping['groups'][0]['reference_number']},
            {'reference_number': '不存在字第123號'},
            {'reference_number': ''},
            {'page_ids': ['S2:P1']},
            {'original_label': '附件99'},
            {'evidence_page_id': 'S2:P1'},
            {'evidence_text': '不存在的附件引用'},
        ):
            with self.subTest(changes=changes):
                bad = copy.deepcopy(grouping)
                bad['groups'][0]['attachments'][0].update(changes)
                self.pipe.analyze_text = AsyncMock(return_value=bad)
                with self.assertRaises(ValueError):
                    await self.group_sources(sources)

    async def test_attachment_title_must_come_from_that_attachment_not_its_mother(self):
        grouping, _, _ = self.linked_letters()
        sources = {f'S{i}': self.source_by_path[p.pdf_path] for i, p in enumerate(self.prepared, 1)}
        for changes in ({'title': '全新摘要名稱'}, {'evidence_page_id': 'S1:P1'}, {'evidence_page_id': 'S9:P1'}):
            with self.subTest(changes=changes):
                bad = copy.deepcopy(grouping)
                bad['attachment_names'][1].update(changes)
                self.pipe.analyze_text = AsyncMock(return_value=bad)
                with self.assertRaisesRegex(ValueError, '附件正式名稱'):
                    await self.group_sources(sources)

    async def test_same_attachment_cannot_receive_multiple_names_or_lose_its_name_entry(self):
        grouping, _, _ = self.linked_letters()
        sources = {f'S{i}': self.source_by_path[p.pdf_path] for i, p in enumerate(self.prepared, 1)}
        names = grouping['attachment_names']
        for entries in (names[:1], [*names, {**names[1], 'title': '審查意見'}]):
            bad = copy.deepcopy(grouping)
            bad['attachment_names'] = entries
            self.pipe.analyze_text = AsyncMock(return_value=bad)
            with self.assertRaisesRegex(ValueError, '唯一對應'):
                await self.group_sources(sources)

    async def test_unknown_number_is_not_invented_and_same_names_remain_distinct(self):
        self.add(SOURCE + '\n附件：第一份圖面及第二份圖面。')
        self.add('第一份圖面')
        self.add('第二份圖面')
        data = payload(('S1',))
        data['read_source_ids'] = ['S1', 'S2', 'S3']
        grouping = {
            'groups': [{
                'reference_number': data['document']['reference_number'], 'main_pages': ['S1:P1'],
                'related_reference_numbers': [], 'unread_source_ids': [],
                'attachments': [{
                    'page_ids': [f'S{i}:P1'], 'original_label': '',
                    'evidence_page_id': 'S1:P1', 'evidence_text': '附件：第一份圖面及第二份圖面。',
                }
                    for i in (2, 3)],
            }], 'attachment_names': [
                {'page_ids': [f'S{i}:P1'], 'title': '圖面', 'evidence_page_id': f'S{i}:P1'} for i in (2, 3)
            ], 'unassigned_source_ids': [],
        }
        answer = await self.run_upload(grouping, data)
        self.assertNotIn('附件1', answer)
        self.assertNotIn('附件2', answer)
        pdfs = [f for f in self.saved if f['kind'] == 'document_pdf']
        self.assertEqual(len({f['display_name'] for f in pdfs}), 3)
        self.assertTrue(pdfs[1]['display_name'].endswith('_檔案01.pdf'))
        self.assertTrue(pdfs[2]['display_name'].endswith('_檔案02.pdf'))

    async def test_child_word_failure_keeps_its_single_pdf_available_under_parent(self):
        grouping, first, child = self.linked_letters()
        self.pipe.render_word.side_effect = [b'main', b'table', OSError('word unavailable')]
        answer = await self.run_upload(grouping, first, child)
        self.assertEqual(answer.count('/api/v1/files/saved-5/content'), 2)
        self.assertNotIn('/api/v1/files/saved-6/content', answer.split('### 公文組 2')[0])
        self.assertEqual(sum(f['kind'] == 'document_pdf' for f in self.saved), 3)
        self.assertEqual(self.pipe.render_word.call_count, 3)
        self.assertIn('word unavailable', answer)

    async def test_separate_main_and_named_multipage_attachments_match_screenshot(self):
        import pymupdf
        structural = set()
        text = self.pipe._page_tagged_cloud_text(
            SOURCE + '\n附件：附件1查核表、附件2路徑調整圖。\f查核表正文\f查核照片\f路徑調整圖',
            structural_lines=structural,
        )
        item = self.add(text)
        with pymupdf.open() as pdf:
            for label in ('LETTER', 'TABLE', 'PHOTO', 'MAP'):
                pdf.new_page().insert_text((72, 72), label)
            pdf[1].add_text_annot((120, 120), 'Original attachment note')
            item.pdf_path.write_bytes(pdf.tobytes())
        original = item.pdf_path.read_bytes()
        self.source_by_path[item.pdf_path] = M.CloudSemanticSource(text, 'native_pdf_text', frozenset(structural))
        data = payload(('S1',))
        deadline_line = next(n for n, line in enumerate(text.splitlines(), 1) if DEADLINE_EVIDENCE in line)
        for spans in [*data['events'][0]['evidence_spans'].values(), data['events'][0]['description_spans']]:
            for span in spans:
                span.update(start_line=deadline_line, end_line=deadline_line)
        grouping = {
            'groups': [{
                'reference_number': data['document']['reference_number'], 'main_pages': ['S1:P1'],
                'related_reference_numbers': [], 'unread_source_ids': [],
                'attachments': [
                    {
                        'page_ids': ['S1:P2', 'S1:P3'], 'original_label': '附件1',
                        'evidence_page_id': 'S1:P1', 'evidence_text': '附件：附件1查核表、附件2路徑調整圖。',
                    },
                    {
                        'page_ids': ['S1:P4'], 'original_label': '附件2',
                        'evidence_page_id': 'S1:P1', 'evidence_text': '附件：附件1查核表、附件2路徑調整圖。',
                    },
                ],
            }], 'attachment_names': [
                {'page_ids': ['S1:P2', 'S1:P3'], 'title': '查核表', 'evidence_page_id': 'S1:P2'},
                {'page_ids': ['S1:P4'], 'title': '路徑調整圖', 'evidence_page_id': 'S1:P4'},
            ], 'unassigned_source_ids': [],
        }
        answer = await self.run_upload(grouping, data)
        pdfs = [f for f in self.saved if f['kind'] == 'document_pdf']
        words = [f for f in self.saved if f['kind'] == 'document_docx']
        self.assertEqual(len(pdfs), 3)
        self.assertEqual(len(words), 3)
        for i, labels in enumerate((['LETTER'], ['TABLE', 'PHOTO'], ['MAP'])):
            with pymupdf.open(stream=pdfs[i]['payload'], filetype='pdf') as pdf:
                self.assertEqual([page.get_text().strip() for page in pdf], labels)
                if i == 1:
                    self.assertEqual(pdf[0].first_annot.info['content'], 'Original attachment note')
            self.assertEqual(Path(pdfs[i]['display_name']).stem, Path(words[i]['display_name']).stem)
            self.assertLessEqual(len(words[i]['display_name'].encode()), 200)
        self.assertIn('_附件1-查核表', pdfs[1]['display_name'])
        self.assertIn('_附件2-路徑調整圖', pdfs[2]['display_name'])
        self.assertIn('主文 PDF', answer)
        self.assertIn('附件1(查核表) PDF', answer)
        self.assertEqual(
            [
                len(c.kwargs['layout_pages'])
                for c in self.pipe.render_word.call_args_list
            ], [1, 2, 1],
        )
        self.assertEqual(self.pipe.analyze_text.await_count, 1)
        self.assertEqual(self.pipe.ocr_context.await_count, 1)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(item.pdf_path.read_bytes(), original)

    async def test_ai_preparation_uses_same_analysis_and_survives_csv_export(self):
        from datetime import datetime
        from .test_official_preparation import suggestion
        self.add()
        data = payload(('S1',))
        data['events'][0]['preparation_reminders'] = [suggestion(3), suggestion(3)]
        with patch('examples.official_document_workflow.application.upload_batch.datetime') as clock:
            clock.now.return_value = datetime(2026, 9, 1)
            answer = await self.run_upload([['S1']], data)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['Start Date'], '2026/09/10')
        self.assertEqual(rows[1]['Start Date'], '2026/09/07')
        self.assertEqual(rows[1]['Name'], 'AI填寫')
        self.assertEqual(rows[1]['Start Time(24hr)'], '09:30')
        self.assertEqual(rows[1]['End Time(24hr)'], '11:00')
        self.assertEqual(rows[1]['Work Hour'], '1.5')
        self.assertEqual(rows[1]['Report Status'], '待辦')
        self.assertIn('AI 建議準備提醒', rows[1]['Description'])
        self.assertIn('另加入 1 筆', answer)
        self.assertEqual(self.pipe.analyze_text.await_count, 1)
        self.assertEqual(self.pipe.ocr_context.await_count, 1)
        self.review.assert_awaited_once()
        self.assertEqual(len(self.saved), 3)
        self.assertIn('preparation_reminders', self.pipe.analyze_text.await_args_list[0].kwargs['prompt'])

    async def test_tentative_schedule_uses_joint_analysis_and_keeps_preparation_anchor(self):
        from datetime import datetime
        from .test_official_preparation import suggestion as preparation
        from .test_official_tentative_schedule import suggestion
        from examples.official_document_workflow.infrastructure.eip.calendar_csv import parse_csv
        from examples.official_document_workflow.domain.calendar.csv_schema import CALENDAR_CSV_FORMAT

        self.add()
        data = payload(('S1',))
        data['events'][0].update(tentative_schedule=suggestion(), preparation_reminders=[preparation(3)])
        with patch('examples.official_document_workflow.application.upload_batch.datetime') as clock:
            clock.now.return_value = datetime(2026, 9, 1)
            answer = await self.run_upload([['S1']], data)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['End Time(24hr)'], '11:30')
        self.assertIn('AI 暫排', rows[0]['Description'])
        self.assertEqual(rows[1]['Start Date'], '2026/09/07')
        self.assertIn('至 EIP 核對修改', answer)
        download = next(f for f in self.saved if f['kind'] == 'calendar_csv')
        imported, _ = parse_csv(download['payload'], csv_format=CALENDAR_CSV_FORMAT)
        self.assertEqual(len(imported), 2)
        self.pipe.analyze_text.assert_awaited_once()
        self.review.assert_awaited_once()

    async def test_tentative_schedule_preserves_main_attachment_and_csv_filenames(self):
        from .test_official_tentative_schedule import suggestion

        self.add()
        self.add(DEADLINE_EVIDENCE)
        await self.run_upload([['S1', 'S2']], payload())
        originals = [(f['kind'], f['display_name']) for f in self.saved]
        pdfs = [f['payload'] for f in self.saved if f['kind'] == 'document_pdf']
        self.saved.clear()
        estimated = payload()
        estimated['events'][0]['tentative_schedule'] = suggestion()
        await self.run_upload([['S1', 'S2']], estimated)
        self.assertEqual([(f['kind'], f['display_name']) for f in self.saved], originals)
        self.assertEqual([f['payload'] for f in self.saved if f['kind'] == 'document_pdf'], pdfs)
        document_names = [name for kind, name in originals if kind in ('document_pdf', 'document_docx')]
        self.assertEqual(len(document_names), 4)
        for name in document_names:
            self.assertTrue(name.startswith('1150813_南新工單一字第1153022027號_'))
            self.assertLessEqual(len(name.encode('utf-8')), 200)
            self.assertNotIn('AI 暫排', name)
        self.assertEqual(Path(document_names[0]).stem, Path(document_names[1]).stem)
        self.assertEqual(Path(document_names[2]).stem, Path(document_names[3]).stem)
        self.assertIn('AI 暫排', self.rows()[0]['Description'])

    async def test_midnight_download_explains_native_all_day_reminder(self):
        from .test_official_tentative_schedule import suggestion
        from examples.official_document_workflow.infrastructure.eip.calendar_csv import MIDNIGHT_IMPORT_NOTICE

        self.add()
        data = payload(('S1',))
        data['events'][0]['tentative_schedule'] = suggestion(
            start_time='23:30', end_date='2026-09-11', end_time='01:00',
        )
        answer = await self.run_upload([['S1']], data)
        self.assertEqual(len(self.rows()), 2)
        self.assertIn(MIDNIGHT_IMPORT_NOTICE, answer)

    async def test_preparation_switch_off_keeps_original_csv_only(self):
        from .test_official_preparation import suggestion
        self.add()
        self.pipe.valves.calendar_ai_preparation_reminders = False
        data = payload(('S1',))
        data['events'][0]['preparation_reminders'] = [suggestion()]
        await self.run_upload([['S1']], data)
        self.assertEqual(len(self.rows()), 1)
        self.assertNotIn('本次另需', self.pipe.analyze_text.await_args_list[0].kwargs['prompt'])

    async def test_rejected_event_cannot_export_its_preparation(self):
        from .test_official_preparation import suggestion
        self.add()
        data = payload(('S1',))
        data['events'][0]['preparation_reminders'] = [suggestion()]
        self.review.return_value = {'approved': False, 'issues': ['事件效力待確認'], 'checks': []}
        answer = await self.run_upload([['S1']], data)
        self.assertFalse([item for item in self.saved if item['kind'] == 'calendar_csv'])
        self.assertNotIn('另加入', answer)

    async def test_chat_and_auxiliary_tasks_export_only_one_batch(self):
        self.add()
        background = [
            self.pipe.pipe(
                {'messages': [{'role': 'user', 'content': '處理附件'}]},
                __user__={'id': 'owner'}, __files__=[{'id': 'file-1'}],
                __task__=task,
            )
            for task in ('query_generation', 'title_generation', 'tags_generation', 'follow_up_generation')
        ]
        answer, *tasks = await asyncio.gather(self.run_upload([['S1']], payload(('S1',))), *background)
        self.assertIn('saved-', answer)
        self.assertEqual(self.pipe.analyze_text.await_count, 1)
        self.pipe.owned_sources.assert_awaited_once()
        self.pipe.prepare_owned_source_documents.assert_awaited_once()
        self.assertEqual(len(self.saved), 3)
        self.review.assert_awaited_once()
        self.assertEqual(
            [json.loads(result) for result in tasks], [
                {'queries': []}, {'title': ''}, {'tags': []}, {'follow_ups': []},
            ],
        )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pipe = M.Pipe()
        self.pipe.valves.document_analysis_cache_enabled = False
        self.scopes = []

        def context(*args):
            result = group_context(*args)
            self.scopes.append({**result[2], 'read_source_ids': list(result[1])})
            return result
        p = patch('examples.official_document_workflow.application.upload_batch.group_context', side_effect=context)
        p.start()
        self.addCleanup(p.stop)
        self.prepared = []
        self.source_by_path = {}
        self.saved = []
        self.review = AsyncMock(return_value={'approved': True, 'checks': [], 'issues': []})
        p = patch('examples.official_document_workflow.application.multi_agent.review_calendar', self.review)
        p.start()
        self.addCleanup(p.stop)

        async def save(**values):
            self.saved.append(values)
            return types.SimpleNamespace(id=f"saved-{len(self.saved)}", filename=values['display_name'])

        p = self.pipe
        p.owned_sources = AsyncMock(
            side_effect=lambda *_: ('pdf', tuple(x.source_file for x in self.prepared)),
        )
        p.prepare_owned_source_documents = AsyncMock(side_effect=lambda *_: tuple(self.prepared))
        p.plan_current_user_request = AsyncMock(side_effect=AssertionError('chat text cannot authorize writes'))
        p.ocr_context = AsyncMock(
            side_effect=lambda path: (
                ((0,), {}) if self.source_by_path[path].kind == 'paddle_layout_ocr' else ((), None)
            ),
        )
        p.semantic_source = MagicMock(side_effect=lambda path, *_: self.source_by_path[path])
        p.paddleocr_vl_spotting_response = MagicMock(return_value={})
        p.paddleocr_vl_spotting_pages = MagicMock(return_value=(M.OcrLayoutPage(595, 842, ()),))
        p.render_word = MagicMock(return_value=b'editable-word')
        p.store_output = AsyncMock(side_effect=save)
        p.emit_status = AsyncMock()

    def add(self, text=SOURCE, *, kind='native_pdf_text'):
        i = len(self.prepared) + 1
        path = Path(self.tmp.name) / f'source-{i}.pdf'
        import pymupdf
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((72, 72), f'Source file {i}')
            path.write_bytes(pdf.tobytes())
        item = M.PreparedDocument(
            kind='pdf', source_files=(types.SimpleNamespace(id=f'file-{i}', filename=path.name),), pdf_path=path,
        )
        self.prepared.append(item)
        self.source_by_path[path] = M.CloudSemanticSource(text, kind)
        return item

    def grouping(self, groups, answers):
        """Make distinct formal letters for independent test groups."""
        result = []
        answer_index = 0
        for index, ids in enumerate(groups):
            answer = answers[min(answer_index, len(answers) - 1)] if answers else {}
            answer_index += 1
            primary_local = answer.get('primary_source_id', 'S1') if isinstance(answer, dict) else 'S1'
            primary = ids[int(primary_local[1:]) - 1]
            ref = payload()['document']['reference_number']
            new_ref = ref if index == 0 else ref.replace('1153022027', str(1153022027 + index))
            for sid in ids:
                item = self.prepared[int(sid[1:]) - 1]
                source = self.source_by_path[item.pdf_path]
                text = source.text.replace(ref, new_ref)
                if sid == primary and len(ids) > 1:
                    text += '\n附件：本函期程資料。'
                self.source_by_path[item.pdf_path] = M.CloudSemanticSource(text, source.kind, source.structural_lines)
            if isinstance(answer, dict):
                answer['document']['reference_number'] = new_ref
                answer['document']['evidence']['reference_number'] = new_ref
            result.append({
                'reference_number': new_ref, 'main_pages': [primary + ':P1'],
                'attachments': [{
                    'page_ids': [sid + ':P1' for sid in ids if sid != primary],
                    'evidence_page_id': primary + ':P1',
                    'evidence_text': '附件：本函期程資料。',
                }] if len(ids) > 1 else [],
                'related_reference_numbers': [], 'unread_source_ids': [],
            })
        return {'groups': result, 'unassigned_source_ids': []}

    async def group_sources(self, sources):
        self.unnamed_attachments(self.pipe.analyze_text.return_value)
        _text, _ = await group_text(self.pipe,  self.prepared, sources, allow_chunking=True)
        return validate_groups(self.pipe,  self.pipe.analyze_text.return_value, sources, {})

    def responses(self, groups, *answers):
        grouping = groups if isinstance(groups, dict) else self.grouping(groups, answers)
        self.unnamed_attachments(grouping)
        # Fixtures use role-local IDs; the batch wire contract uses stable IDs.
        initial, repairs = [], []
        remaining = iter(answers)
        for group in grouping['groups']:
            references = group['related_reference_numbers'] + \
                [link.get('reference_number') for link in group['attachments']]
            related = [g for g in grouping['groups'] if g['reference_number'] in references]

            def pages(g):
                return g['main_pages'] + [pid for link in g['attachments'] for pid in link['page_ids']]
            ids = list(dict.fromkeys(pid.split(':')[0] for g in [group, *related] for pid in pages(g)))
            mapping = {f'S{i}': sid for i, sid in enumerate(ids, 1)}

            def global_ids(value, key=None):
                if isinstance(value, dict):
                    return {k: global_ids(v, k) for k, v in value.items()}
                if isinstance(value, list):
                    return [global_ids(v, key) for v in value]
                return mapping.get(value, value) if key in ('source_id', 'primary_source_id', 'read_source_ids') else value

            def wire(value):
                if not isinstance(value, dict):
                    return value
                value = global_ids(copy.deepcopy(value))
                value['document'].pop('calendar_subject', None)
                value['document']['evidence'].pop('calendar_subject', None)
                for event in value['events']:
                    for field in (
                        'title', 'action', 'project_name', 'location', 'responsible_party', 'relevance',
                        'start_date', 'start_time', 'end_date', 'end_time', 'anchor_date',
                    ):
                        event['evidence_spans'].setdefault(field, [])
                return value
            value = next(remaining)
            initial.append({'reference_number': group['reference_number'], 'analysis': wire(value)})
            if isinstance(value, dict) and value.get('events') == []:
                repair = next(remaining, None)
                if repair is not None:
                    repairs.append(wire(repair))
        error = next((g['analysis'] for g in initial if isinstance(g['analysis'], Exception)), None)
        # Model output order must not decide which letter receives the analysis.
        batch = error if error is not None else {'groups': list(reversed(initial))}
        return [error if error is not None else {'grouping': grouping, 'analysis': batch}, *repairs]

    async def run_upload(self, groups, *answers):
        self.pipe.analyze_text = AsyncMock(side_effect=self.responses(groups, *answers))
        answer = await self.pipe.pipe(
            {'messages': [{'role': 'user', 'content': '不要Word，直接匯入EIP'}]},
            __user__={'id': 'owner'}, __files__=[{'id': x.source_file.id} for x in self.prepared],
        )
        self.pipe.plan_current_user_request.assert_not_awaited()
        return answer

    def rows(self):
        from examples.official_document_workflow.infrastructure.eip.calendar_csv import decode_csv

        files = [x for x in self.saved if x['kind'] == 'calendar_csv']
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]['extra_metadata'][M.CALENDAR_CSV_FORMAT_KEY], M.CALENDAR_CSV_FORMAT)
        payload = decode_csv(files[0]['payload'], M.CALENDAR_CSV_FORMAT)
        return list(csv.DictReader(io.StringIO(payload.decode('utf-8-sig'))))

    async def test_main_and_image_attachment_complement_each_other_through_chat_entry(self):
        main = SOURCE.replace(DEADLINE_EVIDENCE, '依附件所列日期提送評估報告予本段續辦。')
        self.add(main)
        self.add(DEADLINE_EVIDENCE, kind='paddle_layout_ocr')
        with patch('examples.official_document_workflow.application.upload_batch.group_text', wraps=group_text) as reading:
            answer = await self.run_upload([['S1', 'S2']], payload())
        reading.assert_awaited_once()
        self.assertIn('整理為 1 組', answer)
        self.assertNotIn('來源：', answer)
        self.assertNotIn('來源與下載', str(self.pipe.emit_status.await_args_list))
        for item in self.prepared:
            self.assertNotIn(self.pipe.download_link(None, item.source_file.id, item.source_file.filename), answer)
        self.assertEqual(self.rows()[0]['Start Date'], '2026/09/10')
        calls = self.pipe.analyze_text.await_args_list
        self.assertEqual(len(calls), 1)  # grouping and all analysis share one source submission
        self.assertEqual(calls[0].kwargs['schema'], UploadAnalysis.model_json_schema())
        analysis = json.loads(calls[0].args[0].text)
        self.assertNotIn('115年9月10', json.dumps(analysis['sources'][0], ensure_ascii=False))
        self.assertIn('115年9月10', json.dumps(analysis['sources'][1], ensure_ascii=False))
        self.assertEqual(self.pipe.semantic_source.call_count, 2)
        self.pipe.paddleocr_vl_spotting_response.assert_called_once()
        self.assertEqual(
            [
                len(c.kwargs['layout_pages'])
                for c in self.pipe.render_word.call_args_list
            ], [1, 1],
        )
        self.assertEqual(sum(x['kind'] == 'document_pdf' for x in self.saved), 2)
        self.assertEqual(sum(x['kind'] == 'document_docx' for x in self.saved), 2)
        self.review.assert_awaited_once()
        originals = {p.source_file.id: p.pdf_path.read_bytes() for p in self.prepared}
        for saved in self.saved:
            self.assertEqual(saved['user_id'], 'owner')
            if saved['kind'] == 'document_pdf':
                import pymupdf
                with pymupdf.open(stream=saved['payload'], filetype='pdf') as pdf:
                    expected = 'Source file 1' if saved['extra_metadata']['official_document_output_role'] == 'main' else 'Source file 2'
                    self.assertEqual([p.get_text().strip() for p in pdf], [expected])
                self.assertEqual(saved['extra_metadata']['eip_group_source_ids'], ['file-1', 'file-2'])
        self.assertTrue(all(p.pdf_path.read_bytes() == originals[p.source_file.id] for p in self.prepared))

    async def test_invalid_grouping_can_be_repaired_once_without_repeating_ocr(self):
        self.add()
        valid = self.responses([['S1']], payload(('S1',)))[0]
        invalid = copy.deepcopy(valid)
        invalid['grouping']['groups'][0]['main_pages'] = ['S1:P99']
        self.pipe.analyze_text = AsyncMock(side_effect=[invalid, valid])
        answer = await self.pipe.pipe(
            {'messages': [{'role': 'user', 'content': '處理公文'}]},
            __user__={'id': 'owner'}, __files__=[{'id': 'file-1'}],
        )
        self.assertIn('整理為 1 組', answer)
        self.assertEqual(self.rows()[0]['Start Date'], '2026/09/10')
        self.assertEqual(self.pipe.analyze_text.await_count, 2)
        self.pipe.ocr_context.assert_awaited_once()
        self.pipe.semantic_source.assert_called_once()
        initial, repair = self.pipe.analyze_text.await_args_list
        request = json.loads(repair.args[0].text)
        self.assertEqual(request['sources'], json.loads(initial.args[0].text)['sources'])
        self.assertEqual(request['correction']['previous_result'], invalid)
        self.assertTrue(request['correction']['errors'])

    async def test_invalid_grouping_after_one_repair_never_produces_downloads(self):
        self.add()
        invalid = self.responses([['S1']], payload(('S1',)))[0]
        invalid['grouping']['groups'][0]['main_pages'] = ['S1:P99']
        self.pipe.analyze_text = AsyncMock(return_value=invalid)
        await self.pipe.pipe(
            {'messages': [{'role': 'user', 'content': '處理公文'}]},
            __user__={'id': 'owner'}, __files__=[{'id': 'file-1'}],
        )
        self.assertEqual(self.pipe.analyze_text.await_count, 2)
        self.assertEqual(self.saved, [])
        self.review.assert_not_awaited()

    async def test_unrelated_group_cannot_borrow_another_groups_attachment_evidence(self):
        self.add()
        self.add(DEADLINE_EVIDENCE)
        self.add()
        # The second group's single source has no S2; it must not resolve S2
        # against the first group's supplement or leak its event into the CSV.
        invalid = payload()
        invalid['read_source_ids'] = ['S1']
        answer = await self.run_upload([['S1', 'S2'], ['S3']], payload(), invalid)
        self.assertEqual(len(self.rows()), 1)
        self.assertIn('待確認', answer)
        last = json.loads(self.pipe.analyze_text.await_args.args[0].text)
        self.assertEqual(self.scopes[1]['read_source_ids'], ['S3'])
        self.assertEqual(len(last['sources']), 3)  # all text appears only once

    async def test_joint_timeout_preserves_sources_without_inventing_groups_or_retrying(self):
        for i in range(6):
            self.add(SOURCE if i % 2 == 0 else DEADLINE_EVIDENCE)
        originals = [p.pdf_path.read_bytes() for p in self.prepared]
        answer = await self.run_upload(
            [['S1', 'S2'], ['S3', 'S4'], ['S5', 'S6']],
            payload(), M.CloudTemporaryFailure('雲端模型連線中斷或逾時'), payload(),
        )
        self.assertEqual(self.saved, [])
        self.assertEqual(self.pipe.analyze_text.await_count, 1)
        self.assertNotIn('公文組', answer)
        self.assertIn('逾時', answer)
        self.assertEqual([p.pdf_path.read_bytes() for p in self.prepared], originals)

    async def test_invalid_metadata_quote_keeps_exports_and_other_groups_calendar(self):
        self.add()
        self.add()
        invalid = payload(('S1',))
        invalid['document']['evidence']['subject'] = '原文不存在的主旨'
        answer = await self.run_upload([['S1'], ['S2']], invalid, payload(('S1',)))
        self.assertNotIn('公文處理暫時失敗', answer)
        self.assertIn('公文組 2', answer)
        self.assertIn('日期主旨待確認', answer)
        self.assertEqual(sum(f['kind'] == 'document_pdf' for f in self.saved), 2)
        self.assertEqual(sum(f['kind'] == 'document_docx' for f in self.saved), 2)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.pipe.analyze_text.await_count, 1)
        self.review.assert_awaited_once()

    async def test_numeric_and_already_named_uploads_use_verified_group_names(self):
        first = self.add()
        primary = self.add()
        last = self.add(DEADLINE_EVIDENCE)
        first.source_file.filename = '1150901_原文號_原主旨_附件1.pdf'
        primary.source_file.filename = last.source_file.filename = '20260921121550237.pdf'
        data = payload(('S1', 'S2', 'S3'))
        data['primary_source_id'] = 'S2'
        grouping = self.grouping([['S1', 'S2', 'S3']], [data])
        data['primary_source_id'] = 'S1'
        answer = await self.run_upload(grouping, data)
        pdfs = [f for f in self.saved if f['kind'] == 'document_pdf']
        base = Path(pdfs[0]['display_name']).stem
        self.assertEqual(len(pdfs), 2)
        self.assertEqual(pdfs[0]['display_name'], base + '.pdf')
        self.assertIn('_附件-', pdfs[1]['display_name'])
        self.assertTrue(base.startswith('1150813_'))  # The date comes from the PDF text.
        self.assertNotIn('原文號', answer)
        self.assertNotIn('20260921121550237', answer)
        words = [f['display_name'] for f in self.saved if f['kind'] == 'document_docx']
        self.assertEqual(words, [str(Path(f['display_name']).with_suffix('.docx')) for f in pdfs])
        for file in pdfs:
            self.assertEqual(file['extra_metadata']['eip_group_source_ids'], ['file-2', 'file-1', 'file-3'])
        self.assertEqual(self.pipe.analyze_text.await_count, 1)

    async def test_joint_rate_limit_does_not_guess_groups_or_retry(self):
        for _ in range(2):
            self.add().source_file.filename = '20260921121550237.pdf'
        answer = await self.run_upload(
            [['S1', 'S2']], M.CloudTemporaryFailure('雲端模型達到速率或額度限制（HTTP 429）'),
        )
        self.assertEqual(self.saved, [])
        self.assertIn('429', answer)
        self.assertEqual(self.pipe.analyze_text.await_count, 1)

    async def test_calendar_rate_limit_keeps_validated_document_names(self):
        self.add()
        self.add(DEADLINE_EVIDENCE)
        self.review.side_effect = M.CloudRateLimited('HTTP 429')
        answer = await self.run_upload([['S1', 'S2']], payload())
        self.assertIn('429', answer)
        self.assertNotIn('第 1 組判讀未完成', answer)
        self.assertIn('第 1 組行程資料待確認', answer)
        self.assertEqual(len(self.saved), 4)
        self.assertTrue(all(f['display_name'].startswith('1150813_') for f in self.saved))
        self.assertFalse(any(f['kind'] == 'calendar_csv' for f in self.saved))
        self.assertNotIn('公文欄位未確認', answer)

    async def test_calendar_repair_failure_keeps_validated_document_names(self):
        self.add()
        self.add(DEADLINE_EVIDENCE)
        data = payload()
        data['events'] = []
        answer = await self.run_upload([['S1', 'S2']], data, M.CloudRateLimited('HTTP 429'))
        self.assertIn('429', answer)
        self.assertNotIn('第 1 組判讀未完成', answer)
        self.assertIn('第 1 組行程資料待確認', answer)
        self.assertEqual(len(self.saved), 4)
        self.assertTrue(all(f['display_name'].startswith('1150813_') for f in self.saved))
        self.assertFalse(any(f['kind'] == 'calendar_csv' for f in self.saved))
        self.assertNotIn('公文欄位未確認', answer)

    async def test_long_subject_reserves_space_for_unique_group_suffixes(self):
        for _ in range(3):
            self.add()
        _, document, *_ = parse_group(
            self.pipe,  payload(), {
                'S1': M.CloudSemanticSource(SOURCE, 'native_pdf_text'),
                'S2': M.CloudSemanticSource(DEADLINE_EVIDENCE, 'native_pdf_text'),
            },
        )
        document = replace(document, subject='檢送相關資料，' * 100, filename_subject='')
        exporter = GroupExporter(
            self.pipe,  'owner', ['file-1', 'file-2', 'file-3'], DocumentResult.from_analysis({
                'members': self.prepared, 'primary': 'S1', 'document': document, 'group_index': 1,
            }), JobPlan(), None,
        )
        await exporter.run()
        names = [f['display_name'] for f in self.saved]
        self.assertEqual(len(set(names)), 6)
        self.assertTrue(all(len(name.encode('utf-8')) <= 200 for name in names))
        self.assertTrue(names[2].endswith('…_檔案02.pdf'))
        self.assertTrue(names[4].endswith('…_檔案03.pdf'))
        for pdf, word in zip(names[::2], names[1::2]):
            self.assertEqual(Path(pdf).stem, Path(word).stem)

    async def test_failed_word_export_does_not_discard_joint_evidence_or_other_downloads(self):
        self.add()
        self.add(DEADLINE_EVIDENCE)
        self.add()
        self.add(DEADLINE_EVIDENCE)
        self.pipe.render_word.side_effect = [
            M.ProcessingError('圖片附件沒有可定位文字'), b'word', b'word', b'word',
        ]
        answer = await self.run_upload([['S1', 'S2'], ['S3', 'S4']], payload(), payload())
        self.assertEqual(len(self.rows()), 2)
        self.assertIn('部分輸出未完成', answer)
        self.assertEqual(sum(x['kind'] == 'document_pdf' for x in self.saved), 4)
        self.assertEqual(sum(x['kind'] == 'document_docx' for x in self.saved), 3)

    async def test_uncertain_word_storage_keeps_pdf_and_reports_partial_without_retry(self):
        self.add()
        save = self.pipe.store_output.side_effect

        async def store(**values):
            if values['kind'] == 'document_docx':
                raise OutputUncertain('Word 寫入結果待核對')
            return await save(**values)

        self.pipe.store_output.side_effect = store
        report = {}
        token = upload_outcome.set(report)
        try:
            answer = await self.run_upload([['S1']], payload(('S1',)))
        finally:
            upload_outcome.reset(token)
        self.assertEqual(report['status'], 'partial')
        self.assertIn('部分輸出未完成', answer)
        self.assertIn('Word 寫入結果待核對', answer)
        self.assertIn('saved-1', answer)
        self.assertEqual([f['kind'] for f in self.saved], ['document_pdf', 'calendar_csv'])
        self.assertEqual(self.pipe.store_output.await_count, 3)

    async def test_csv_eip_link_reuses_output_and_keeps_deduplicated_original_source_ids(self):
        first, duplicate = self.add(), self.add()
        duplicate.pdf_path.write_bytes(first.pdf_path.read_bytes())
        answer = await self.run_upload([['S1']], payload(('S1',)))
        csv = next(item for item in self.saved if item['kind'] == 'calendar_csv')
        self.assertEqual(csv['extra_metadata']['eip_group_source_ids'], ['file-1', 'file-2'])
        self.assertIn('/eip?csv=saved-3', answer)
        self.assertIn('沿用已產生 CSV，預覽確認後才寫入 EIP', answer)
        self.assertEqual(len(self.saved), 3)

    async def test_failed_csv_storage_does_not_offer_an_eip_import_link(self):
        self.add()
        save = self.pipe.store_output.side_effect

        async def store(**values):
            if values['kind'] == 'calendar_csv':
                raise M.ProcessingError('CSV output unavailable')
            return await save(**values)

        self.pipe.store_output.side_effect = store
        answer = await self.run_upload([['S1']], payload(('S1',)))
        self.assertIn('CSV 未完成', answer)
        self.assertNotIn('/eip?csv=', answer)

    @staticmethod
    def fail_word():
        raise M.ProcessingError('圖片附件沒有可定位文字')

    async def test_unread_related_attachment_blocks_its_group_csv_but_not_other_groups(self):
        for _ in range(3):
            self.add()
        bad = self.prepared[1].pdf_path

        async def read(path):
            if path == bad:
                raise M.ProcessingError('圖片附件未能讀取')
            return (), None
        self.pipe.ocr_context.side_effect = read
        answers = [payload(('S1',)), payload(('S1',))]
        grouping = self.grouping([['S1', 'S2'], ['S3']], answers)
        grouping['groups'][0]['attachments'] = []
        grouping['groups'][0]['unread_source_ids'] = ['S2']
        answer = await self.run_upload(grouping, *answers)
        self.assertEqual(len(self.rows()), 1)
        self.assertIn('本組資訊待確認，未納入 CSV', answer)
        self.assertEqual(self.pipe.analyze_text.await_count, 1)

    async def test_unread_attachment_of_related_letter_blocks_incomplete_calendar(self):
        self.add()
        self.add()
        bad = self.add(DEADLINE_EVIDENCE)

        async def read(path):
            if path == bad.pdf_path:
                raise M.ProcessingError('附件未能讀取')
            return (), None
        self.pipe.ocr_context.side_effect = read
        answers = [payload(('S1', 'S2')), payload(('S1',))]
        grouping = self.grouping([['S1'], ['S2', 'S3']], answers)
        grouping['groups'][0]['related_reference_numbers'] = [grouping['groups'][1]['reference_number']]
        grouping['groups'][1]['attachments'] = []
        grouping['groups'][1]['unread_source_ids'] = ['S3']
        answer = await self.run_upload(grouping, *answers)
        self.assertFalse(any(f['kind'] == 'calendar_csv' for f in self.saved))
        self.assertEqual(answer.count('本組資訊待確認'), 2)
        self.assertTrue(all(not f['display_name'].startswith('待確認_') for f in self.saved))

    async def test_ten_uploads_form_five_groups_without_re_reading(self):
        for i in range(10):
            self.add(SOURCE if i % 2 == 0 else DEADLINE_EVIDENCE)
        groups = [[f'S{i}', f'S{i+1}'] for i in range(1, 11, 2)]
        answer = await self.run_upload(groups, *[payload() for _ in groups])
        self.assertEqual(len(self.rows()), 5)
        self.assertIn('整理為 5 組', answer)
        self.assertEqual(self.pipe.semantic_source.call_count, 10)
        self.assertEqual(self.pipe.analyze_text.await_count, 1)

        request = self.pipe.analyze_text.await_args
        joint = json.loads(request.args[0].text)
        self.assertEqual(len(joint['sources']), 10)
        self.assertEqual(len(self.scopes), 5)
        self.assertEqual(self.scopes[4]['primary_source_id'], 'S9')
        self.assertEqual(self.scopes[4]['read_source_ids'], ['S9', 'S10'])
        self.assertEqual(request.kwargs['max_output_tokens'], 30_000)

    async def test_grouping_cannot_drop_duplicate_or_invent_source_pages(self):
        self.add()
        self.add(DEADLINE_EVIDENCE)
        sources = {f'S{i}': self.source_by_path[p.pdf_path] for i, p in enumerate(self.prepared, 1)}
        valid = self.grouping([['S1', 'S2']], [payload()])
        for pages in ([], ['S2:P1', 'S2:P1'], ['S9:P1'], ['S2:P9']):
            bad = copy.deepcopy(valid)
            bad['groups'][0]['attachments'][0]['page_ids'] = pages
            with self.subTest(pages=pages):
                self.pipe.analyze_text = AsyncMock(return_value=bad)
                with self.assertRaises(ValueError):
                    await self.group_sources(sources)
        self.assertEqual(self.saved, [])

    async def test_no_verified_dates_does_not_create_empty_csv(self):
        self.add()
        self.add('附件為無日期的說明。')
        empty = payload()
        empty['events'] = []
        answer = await self.run_upload([['S1', 'S2']], empty, copy.deepcopy(empty))
        self.assertFalse(any(x['kind'] == 'calendar_csv' for x in self.saved))
        self.assertIn('未產生 CSV', answer)
        self.assertEqual(len(self.saved), 4)

    async def test_initial_calendar_error_keeps_own_validated_metadata(self):
        self.add()
        self.add(DEADLINE_EVIDENCE)
        data = payload()
        data['events'][0]['start_time'] = '23:59'
        answer = await self.run_upload([['S1', 'S2']], data)
        self.assertTrue(all(f['display_name'].startswith('1150813_') for f in self.saved))
        self.assertIn('未納入 CSV', answer)
        self.assertFalse(any(f['kind'] == 'calendar_csv' for f in self.saved))

    async def test_single_bundle_splits_original_pages_and_matching_word(self):
        first = payload(('S1',))
        first['events'] = []
        old_ref = first['document']['reference_number']
        new_ref = '測試乙字第1150000999號'
        second_source = SOURCE.replace(old_ref, new_ref).replace('115年8月13日', '115年8月14日')
        structural = set()
        text = self.pipe._page_tagged_cloud_text(SOURCE + '\f' + second_source, structural_lines=structural)
        item = self.add(text)
        import pymupdf
        with pymupdf.open() as pdf:
            for text_label in ('LETTER A', 'LETTER B'):
                page = pdf.new_page()
                page.insert_text((72, 72), text_label)
            item.pdf_path.write_bytes(pdf.tobytes())
        original = item.pdf_path.read_bytes()
        word_pages = []

        def word(path, _, **kwargs):
            with pymupdf.open(path) as pdf:
                word_pages.append([page.get_text().strip() for page in pdf])
            return b'editable-word'
        self.pipe.render_word.side_effect = word
        self.source_by_path[item.pdf_path] = M.CloudSemanticSource(text, 'native_pdf_text', frozenset(structural))
        second = copy.deepcopy(first)
        second['document']['reference_number'] = new_ref
        second['document']['evidence']['reference_number'] = new_ref
        second['document']['issued_date'] = '2026-08-14'
        second['document']['evidence']['issued_date'] = '115年8月14日'
        grouping = {
            'groups': [
                {
                    'reference_number': old_ref, 'main_pages': ['S1:P1'], 'attachments': [],
                    'related_reference_numbers': [new_ref], 'unread_source_ids': [],
                },
                {
                    'reference_number': new_ref, 'main_pages': ['S1:P2'], 'attachments': [],
                    'related_reference_numbers': [old_ref], 'unread_source_ids': [],
                },
            ], 'unassigned_source_ids': [],
        }
        answer = await self.run_upload(grouping, first, copy.deepcopy(first), second, copy.deepcopy(second))
        self.assertIn('整理為 2 組', answer)
        self.assertIn(old_ref, answer)
        self.assertIn(new_ref, answer)
        self.assertEqual(len(self.saved), 4)
        self.assertEqual(item.pdf_path.read_bytes(), original)
        self.assertEqual(word_pages, [['LETTER A'], ['LETTER B']])
        for i, expected in enumerate(('LETTER A', 'LETTER B')):
            file = self.saved[i * 2]
            with pymupdf.open(stream=file['payload'], filetype='pdf') as pdf:
                self.assertEqual(pdf.page_count, 1)
                self.assertEqual(pdf[0].get_text().strip(), expected)
            self.assertEqual(file['source_file_id'], item.source_file.id)
            self.assertEqual(file['extra_metadata']['official_document_source_pages'], [i + 1])
        self.assertTrue(self.saved[0]['display_name'].startswith('1150813_' + old_ref))
        self.assertTrue(self.saved[2]['display_name'].startswith('1150814_' + new_ref))
        self.assertNotIn('合併2件', answer)
        self.assertNotIn('待確認_', self.saved[0]['display_name'])
        self.assertEqual(answer.count('/api/v1/files/saved-1/content'), 1)
        self.assertEqual(self.saved[0]['extra_metadata']['official_document_references'], [old_ref])
        self.assertEqual(self.saved[2]['extra_metadata']['official_document_references'], [new_ref])
        for call in self.pipe.render_word.call_args_list:
            self.assertFalse(call.args[0].exists())
        self.assertEqual(self.pipe.semantic_source.call_count, 1)
        focus = json.loads(self.pipe.analyze_text.await_args.args[0].text)['document_focus']
        self.assertEqual(focus['reference_number'], new_ref)
        self.assertGreater(focus['main_spans'][0]['start_line'], 1)

    async def test_shared_table_preceding_letters_is_named_per_parent_and_rendered_once(self):
        first = payload(('S1',))
        first['events'] = []
        old_ref = first['document']['reference_number']
        new_ref = '測試乙字第1150000999號'
        second_source = SOURCE.replace(old_ref, new_ref).replace('115年8月13日', '115年8月14日')
        structural = set()
        text = self.pipe._page_tagged_cloud_text(
            '查核表。' + '\f' + SOURCE + '\n附件：共用查核表。' + '\f' + second_source + '\n附件：共用查核表。', structural_lines=structural,
        )
        item = self.add(text)
        import pymupdf
        with pymupdf.open() as pdf:
            for text_label in ('TABLE', 'LETTER A', 'LETTER B'):
                page = pdf.new_page()
                page.insert_text((72, 72), text_label)
            item.pdf_path.write_bytes(pdf.tobytes())
        original = item.pdf_path.read_bytes()
        word_pages = []
        self.pipe.native_pdf_layout_pages = MagicMock(wraps=self.pipe.native_pdf_layout_pages)

        def word(path, _, **kwargs):
            with pymupdf.open(path) as pdf:
                labels = [page.get_text().strip() for page in pdf]
                word_pages.append(labels)
                self.assertEqual([''.join(b.text for b in p.blocks).strip() for p in kwargs['layout_pages']], labels)
            return b'editable-word'
        self.pipe.render_word.side_effect = word
        self.source_by_path[item.pdf_path] = M.CloudSemanticSource(text, 'native_pdf_text', frozenset(structural))
        second = copy.deepcopy(first)
        second['document']['reference_number'] = new_ref
        second['document']['evidence']['reference_number'] = new_ref
        second['document']['issued_date'] = '2026-08-14'
        second['document']['evidence']['issued_date'] = '115年8月14日'
        grouping = {
            'groups': [
                {
                    'reference_number': old_ref, 'main_pages': ['S1:P2'], 'attachments': [{'page_ids': ['S1:P1'], 'evidence_page_id': 'S1:P2', 'evidence_text': '附件：共用查核表。'}],
                    'related_reference_numbers': [new_ref], 'unread_source_ids': [],
                },
                {
                    'reference_number': new_ref, 'main_pages': ['S1:P3'], 'attachments': [{'page_ids': ['S1:P1'], 'evidence_page_id': 'S1:P3', 'evidence_text': '附件：共用查核表。'}],
                    'related_reference_numbers': [old_ref], 'unread_source_ids': [],
                },
            ], 'unassigned_source_ids': [],
        }
        answer = await self.run_upload(grouping, first, copy.deepcopy(first), second, copy.deepcopy(second))
        self.assertIn('整理為 2 組', answer)
        self.assertIn(old_ref, answer)
        self.assertIn(new_ref, answer)
        self.assertEqual(len(self.saved), 8)
        self.assertEqual(item.pdf_path.read_bytes(), original)
        self.assertEqual(word_pages, [['LETTER A'], ['TABLE'], ['LETTER B']])
        for i, expected in enumerate(('LETTER A', 'LETTER B')):
            file = self.saved[i * 4]
            with pymupdf.open(stream=file['payload'], filetype='pdf') as pdf:
                self.assertEqual(pdf.page_count, 1)
                self.assertEqual(pdf[0].get_text().strip(), expected)
            self.assertEqual(file['source_file_id'], item.source_file.id)
            self.assertEqual(file['extra_metadata']['official_document_source_pages'], [i + 2])
        self.assertTrue(self.saved[0]['display_name'].startswith('1150813_' + old_ref))
        self.assertTrue(self.saved[4]['display_name'].startswith('1150814_' + new_ref))
        self.assertNotIn('合併2件', answer)
        self.assertNotIn('待確認_', self.saved[0]['display_name'])
        self.assertEqual(answer.count('/api/v1/files/saved-1/content'), 1)
        self.assertEqual(self.saved[0]['extra_metadata']['official_document_references'], [old_ref])
        self.assertEqual(self.saved[4]['extra_metadata']['official_document_references'], [new_ref])
        for call in self.pipe.render_word.call_args_list:
            self.assertFalse(call.args[0].exists())
        self.assertEqual(self.pipe.semantic_source.call_count, 1)
        focus = json.loads(self.pipe.analyze_text.await_args.args[0].text)['document_focus']
        self.assertEqual(focus['reference_number'], new_ref)
        self.assertGreater(focus['main_spans'][0]['start_line'], 1)

        self.pipe.native_pdf_layout_pages.assert_called_once()

    async def test_shared_attachment_has_parent_names_with_one_render(self):
        self.add()
        self.add()
        attachment = self.add(DEADLINE_EVIDENCE + '\n查核表', kind='paddle_layout_ocr')
        answers = [payload(), payload()]
        grouping = self.grouping([['S1', 'S3'], ['S2', 'S3']], answers)
        grouping['attachment_names'] = [{'page_ids': ['S3:P1'], 'title': '查核表', 'evidence_page_id': 'S3:P1'}]
        answer = await self.run_upload(grouping, *answers)
        answer = answer.replace('\\', '')  # Markdown escapes parentheses in link labels.
        shared = [
            f for f in self.saved if f['kind'] == 'document_pdf'
            and f['extra_metadata']['official_document_output_role'] == 'attachment'
        ]
        self.assertEqual(len(shared), 2)
        refs = [g['reference_number'] for g in grouping['groups']]
        self.assertNotIn('共用附件', answer)
        self.assertEqual(shared[0]['payload'], shared[1]['payload'])
        words = [
            f for f in self.saved if f['kind'] == 'document_docx'
            and f['extra_metadata']['official_document_output_role'] == 'attachment'
        ]
        self.assertEqual(len(words), 2)
        self.assertEqual(words[0]['payload'], words[1]['payload'])
        for i, file in enumerate(shared):
            self.assertEqual(answer.count(file['display_name']), 1)
            self.assertIn(refs[i], file['display_name'])
            self.assertIn('_附件-查核表', file['display_name'])
            self.assertEqual(file['extra_metadata']['official_document_groups'], [refs[i]])
            self.assertEqual(file['extra_metadata']['eip_group_source_ids'], [f'file-{i + 1}', 'file-3'])
            import pymupdf
            with pymupdf.open(stream=file['payload'], filetype='pdf') as pdf:
                self.assertEqual([p.get_text().strip() for p in pdf], ['Source file 3'])
            self.assertTrue(file['extra_metadata']['official_document_attachment_evidence'])
            mapping = file['extra_metadata']['official_document_page_origins']
            for link in file['extra_metadata']['official_document_attachment_evidence']:
                self.assertNotIn(link['evidence_page_id'], mapping)
                self.assertTrue(file['extra_metadata']['official_document_parent_main_pages'])
                self.assertTrue(all(pid in mapping for pid in link['page_ids']))
        self.assertNotIn(shared[0]['display_name'], answer.split('### 公文組 2')[1])
        self.assertIn(shared[1]['display_name'], answer.split('### 公文組 2')[1])
        self.assertEqual(self.pipe.semantic_source.call_count, 3)

        self.pipe.paddleocr_vl_spotting_response.assert_called_once_with(attachment.pdf_path)
        layouts = [c.kwargs['layout_pages'] for c in self.pipe.render_word.call_args_list]
        self.assertEqual(len(layouts), 3)  # Two letters and one shared attachment, each rendered once.
        self.assertEqual(self.pipe.analyze_text.await_count, 1)

    async def test_shared_attachment_title_stays_identical_with_different_parent_numbers(self):
        self.add()
        self.add()
        self.add(DEADLINE_EVIDENCE + '\n申請開挖河防建造物查核表')
        answers = [payload(), payload()]
        grouping = self.grouping([['S1', 'S3'], ['S2', 'S3']], answers)
        for number, group in zip(('附件2', '附件四'), grouping['groups']):
            item = self.prepared[int(group['main_pages'][0][1]) - 1]
            old = self.source_by_path[item.pdf_path]
            quote = number + '：申請開挖河防查核。'
            self.source_by_path[item.pdf_path] = M.CloudSemanticSource(old.text + '\n' + quote, old.kind)
            group['attachments'][0].update(original_label=number, evidence_text=quote)
        grouping['attachment_names'] = [{
            'page_ids': ['S3:P1'], 'title': '申請開挖河防建造物查核表',
            'evidence_page_id': 'S3:P1',
        }]
        answer = await self.run_upload(grouping, *answers)
        self.assertIn('附件2(申請開挖河防建造物查核表) PDF', answer)
        self.assertIn('附件四(申請開挖河防建造物查核表) PDF', answer)
        self.assertNotIn('(申請開挖河防查核)', answer)
        self.assertEqual(self.pipe.analyze_text.await_count, 1)
        self.assertEqual(self.pipe.render_word.call_count, 3)

    async def test_shared_attachment_word_failure_keeps_parent_pdfs_without_retry(self):
        self.add()
        self.add()
        self.add(DEADLINE_EVIDENCE, kind='paddle_layout_ocr')
        answers = [payload(), payload()]
        grouping = self.grouping([['S1', 'S3'], ['S2', 'S3']], answers)
        self.pipe.render_word.side_effect = [b'word-a', OSError('word unavailable'), b'word-b']
        answer = await self.run_upload(grouping, *answers)
        shared = [
            f for f in self.saved if f.get('extra_metadata', {}).get(
                'official_document_output_role',
            ) == 'attachment'
        ]
        self.assertEqual([f['kind'] for f in shared], ['document_pdf', 'document_pdf'])
        self.assertNotEqual(shared[0]['display_name'], shared[1]['display_name'])
        self.assertEqual(shared[0]['payload'], shared[1]['payload'])
        self.assertEqual(answer.count('/api/v1/files/saved-3/content'), 1)
        self.assertEqual(answer.count('/api/v1/files/saved-6/content'), 1)
        self.assertEqual(self.pipe.render_word.call_count, 3)
        self.pipe.paddleocr_vl_spotting_response.assert_called_once()
        self.assertEqual(answer.count('word unavailable'), 2)
        self.assertEqual(len(self.rows()), 2)

    async def test_attachment_evidence_cannot_borrow_another_letter_or_invent_a_quote(self):
        self.add()
        self.add()
        self.add(DEADLINE_EVIDENCE)
        grouping = self.grouping([['S1', 'S3'], ['S2']], [payload(), payload(('S1',))])
        sources = {f'S{i}': self.source_by_path[p.pdf_path] for i, p in enumerate(self.prepared, 1)}
        for page, quote in [('S2:P1', '主旨：'), ('S1:P1', '本函附有不存在的專用圖面'), ('S1:P1', '  ')]:
            bad = copy.deepcopy(grouping)
            bad['groups'][0]['attachments'][0].update(evidence_page_id=page, evidence_text=quote)
            self.pipe.analyze_text = AsyncMock(return_value=bad)
            with self.subTest(page=page), self.assertRaisesRegex(ValueError, '附件歸屬'):
                await self.group_sources(sources)
        self.assertEqual(self.saved, [])

    async def test_related_letter_cannot_replace_target_letter_metadata(self):
        self.add()
        self.add(DEADLINE_EVIDENCE)
        data = payload()
        grouping = self.grouping([['S1', 'S2']], [data])
        data['document']['reference_number'] = '被引用字第1150000001號'
        answer = await self.run_upload(grouping, data)
        self.assertIn('本組判讀文號或主文與分組不符', answer)
        self.assertTrue(all('_日期主旨待確認' in f['display_name'] for f in self.saved))

    async def test_formal_letter_cannot_be_claimed_as_unnumbered_attachment(self):
        self.add()
        self.add()
        answers = [payload(('S1',)), payload(('S1',))]
        grouping = self.grouping([['S1'], ['S2']], answers)
        grouping['groups'][0]['attachments'] = [{
            'page_ids': ['S2:P1'],
            'evidence_page_id': 'S1:P1', 'evidence_text': '主旨：',
        }]
        sources = {f'S{i}': self.source_by_path[p.pdf_path] for i, p in enumerate(self.prepared, 1)}
        self.pipe.analyze_text = AsyncMock(return_value=grouping)
        with self.assertRaisesRegex(ValueError, '有本件文號'):
            await self.group_sources(sources)

    def test_scoped_source_preserves_coordinates_and_excludes_unrelated_pages(self):
        from examples.official_document_workflow.application.upload_batch import source_pages, source_slice
        structural = set()
        text = self.pipe._page_tagged_cloud_text('第一份公文\f另一件不相關公文', structural_lines=structural)
        source = M.CloudSemanticSource(text, 'native_pdf_text', frozenset(structural))
        pages = source_pages({'S1': source})
        scoped = source_slice( source, [pages['S1:P2']])
        self.assertNotIn('第一份公文', scoped.text)
        self.assertIn('另一件不相關公文', scoped.text)
        self.assertEqual(scoped.text.splitlines().index('另一件不相關公文'), text.splitlines().index('另一件不相關公文'))

    async def test_standalone_and_bundle_exports_have_distinct_names_and_correct_parents(self):
        import pymupdf
        first = payload(('S1',))
        first['events'] = []
        old_ref = first['document']['reference_number']
        new_ref = '測試乙字第1150000999號'
        other_text = SOURCE.replace(old_ref, new_ref)
        standalone = self.add()
        structural = set()
        text = self.pipe._page_tagged_cloud_text(SOURCE + '\f' + other_text, structural_lines=structural)
        bundle = self.add(text)
        self.source_by_path[bundle.pdf_path] = M.CloudSemanticSource(text, 'native_pdf_text', frozenset(structural))
        for item, labels in ((standalone, ('LETTER A',)), (bundle, ('LETTER A', 'LETTER B'))):
            with pymupdf.open() as pdf:
                for label in labels:
                    page = pdf.new_page()
                    page.insert_text((72, 72), label)
                item.pdf_path.write_bytes(pdf.tobytes())
        originals = {item.source_file.id: item.pdf_path.read_bytes() for item in (standalone, bundle)}
        second = payload(('S1',))
        second['events'] = []
        second['document']['reference_number'] = new_ref
        second['document']['evidence']['reference_number'] = new_ref
        grouping = {
            'groups': [
                {
                    'reference_number': old_ref, 'main_pages': ['S1:P1'], 'attachments': [],
                    'related_reference_numbers': [], 'unread_source_ids': [],
                },
                {
                    'reference_number': new_ref, 'main_pages': ['S2:P1'], 'attachments': [],
                    'related_reference_numbers': [], 'unread_source_ids': [],
                },
            ], 'unassigned_source_ids': [],
        }
        self.pipe.ocr_context.side_effect = lambda path: ((), None)
        self.pipe.semantic_source.side_effect = lambda path, *_: (
            self.source_by_path[path] if path in self.source_by_path else M.CloudSemanticSource(
                other_text, 'native_pdf_text',
            )
        )
        await self.run_upload(grouping, first, copy.deepcopy(first), second, copy.deepcopy(second))
        self.assertEqual(len(self.saved), 4)
        self.assertEqual(len({f['display_name'] for f in self.saved}), 4)
        pdfs = [f for f in self.saved if f['kind'] == 'document_pdf']
        self.assertEqual([f['source_file_id'] for f in pdfs], ['file-1', 'file-2'])
        self.assertEqual(pdfs[0]['payload'], originals['file-1'])
        for file, label in zip(pdfs, ('LETTER A', 'LETTER B')):
            with pymupdf.open(stream=file['payload'], filetype='pdf') as pdf:
                self.assertEqual(pdf.page_count, 1)
                self.assertEqual(pdf[0].get_text().strip(), label)
        self.assertEqual(pdfs[0]['extra_metadata']['eip_group_source_ids'], ['file-1', 'file-2'])
        self.assertEqual(pdfs[1]['extra_metadata']['official_document_source_pages'], [2])
        for item in (standalone, bundle):
            self.assertEqual(item.pdf_path.read_bytes(), originals[item.source_file.id])

    async def test_split_keeps_non_contiguous_attachment_pages_and_original_annotations(self):
        import pymupdf
        item = self.add()
        with pymupdf.open() as pdf:
            for label in ('LETTER A', 'A TABLE', 'LETTER B', 'A FIGURE'):
                page = pdf.new_page()
                page.insert_text((72, 72), label)
            pdf[1].add_text_annot((120, 120), 'Original review note')
            item.pdf_path.write_bytes(pdf.tobytes())
        original = item.pdf_path.read_bytes()
        received = []

        def word(path, _, **kwargs):
            with pymupdf.open(path) as pdf:
                received.extend(page.get_text().strip() for page in pdf)
            return b'word'
        self.pipe.render_word.side_effect = word
        exporter = GroupExporter(
            self.pipe,  'owner', ['file-1'],
            DocumentResult.from_analysis({'document': None, 'primary': None, 'group_index': 1}), JobPlan(), None,
        )
        await exporter.export(item, 1, spotting_required=False, filename_stem='separated', page_numbers=(1, 2, 4))
        self.assertEqual(received, ['LETTER A', 'A TABLE', 'A FIGURE'])
        with pymupdf.open(stream=self.saved[0]['payload'], filetype='pdf') as pdf:
            self.assertEqual(pdf.page_count, 3)
            self.assertEqual(pdf[1].first_annot.info['content'], 'Original review note')
        self.assertEqual(self.saved[0]['extra_metadata']['official_document_source_pages'], [1, 2, 4])
        self.assertEqual(item.pdf_path.read_bytes(), original)

    def test_split_rejects_out_of_range_pages_without_overwriting_source(self):
        import pymupdf
        from examples.official_document_workflow.application.multi_agent import write_pdf_pages
        item = self.add()
        with pymupdf.open() as pdf:
            pdf.new_page()
            item.pdf_path.write_bytes(pdf.tobytes())
        original = item.pdf_path.read_bytes()
        dest = Path(self.tmp.name) / 'split.pdf'
        for pages in ((), (0,), (2,)):
            with self.subTest(pages=pages), self.assertRaises(ValueError):
                write_pdf_pages(item.pdf_path, dest, pages)
        self.assertFalse(dest.exists())
        self.assertEqual(item.pdf_path.read_bytes(), original)

    def test_source_pages_keep_original_page_numbers_even_when_text_is_missing(self):
        from examples.official_document_workflow.application.upload_batch import source_pages
        text = '<page index="1">\nfirst\n</page>\n<page index="3">\nthird\n</page>'
        source = M.CloudSemanticSource(text, 'native_pdf_text', frozenset({1, 3, 4, 6}))
        self.assertEqual(list(source_pages({'S1': source})), ['S1:P1', 'S1:P3'])

    async def test_different_letters_on_the_same_page_do_not_fake_a_page_split(self):
        self.add()
        self.add()
        answers = [payload(('S1',)), payload(('S1',))]
        grouping = self.grouping([['S1'], ['S2']], answers)
        grouping['groups'][1]['main_pages'] = ['S1:P1']
        grouping['unassigned_source_ids'] = ['S2']
        sources = {f'S{i}': self.source_by_path[p.pdf_path] for i, p in enumerate(self.prepared, 1)}
        self.pipe.analyze_text = AsyncMock(return_value=grouping)
        with self.assertRaisesRegex(ValueError, '正文頁面重疊'):
            await self.group_sources(sources)


class UniqueUploadPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.items = []

    def pdf(self, labels, *, annotation=None):
        import pymupdf
        n = len(self.items) + 1
        path = self.root / f'{n}.pdf'
        with pymupdf.open() as pdf:
            for label in labels:
                page = pdf.new_page()
                page.insert_text((72, 72), label)
            if annotation:
                pdf[0].add_text_annot((120, 120), annotation)
            pdf.save(path)
        item = M.PreparedDocument(
            kind='pdf', source_files=(
                types.SimpleNamespace(id=f'file-{n}', filename=path.name),
            ), pdf_path=path,
        )
        self.items.append(item)
        return item

    def test_standalone_and_merged_copies_read_once_with_all_original_origins(self):
        from examples.official_document_workflow.application.upload_batch import unique_uploads, group_pdf
        import pymupdf
        a = self.pdf(['MAIN A'])
        b = self.pdf(['TABLE', 'PHOTO'])
        c = self.pdf(['MAIN A', 'TABLE', 'PHOTO'])
        before = [item.pdf_path.read_bytes() for item in self.items]
        unique, origins = unique_uploads(self.items, self.root)
        self.assertEqual(unique, [a, b])
        self.assertEqual(len(origins), 3)
        self.assertEqual(
            origins['S2:P2'], [
                {
                    'source_file_id': 'file-2',
                    'page_number': 2,
                }, {'source_file_id': 'file-3', 'page_number': 3},
            ],
        )
        path = group_pdf({'S1': a, 'S2': b}, ['S1:P1', 'S2:P1', 'S2:P2'], self.root/'group.pdf')
        with pymupdf.open(path) as pdf:
            self.assertEqual([p.get_text().strip() for p in pdf], ['MAIN A', 'TABLE', 'PHOTO'])
        self.assertEqual([item.pdf_path.read_bytes() for item in self.items], before)
        # Upload order cannot make the individual copies reappear.
        unique, origins = unique_uploads([c, a, b], self.root)
        self.assertEqual(unique, [c])
        self.assertTrue(all(len(v) == 2 for v in origins.values()))

    def test_partial_overlap_keeps_new_pages_and_original_page_coordinates(self):
        from examples.official_document_workflow.application.upload_batch import unique_uploads
        import pymupdf
        self.pdf(['MAIN A'])
        self.pdf(['MAIN A', 'MAIN B'])
        unique, origins = unique_uploads(self.items, self.root)
        self.assertEqual(len(unique), 2)
        with pymupdf.open(unique[1].pdf_path) as pdf:
            self.assertEqual(pdf.page_count, 1)
            self.assertEqual(pdf[0].get_text().strip(), 'MAIN B')
        self.assertEqual(origins['S2:P1'], [{'source_file_id': 'file-2', 'page_number': 2}])

    def test_duplicate_pages_within_one_document_keep_their_occurrences(self):
        from examples.official_document_workflow.application.upload_batch import unique_uploads
        self.pdf(['REPEATED', 'REPEATED'])
        self.pdf(['REPEATED', 'REPEATED'])
        unique, origins = unique_uploads(self.items, self.root)
        self.assertEqual(len(unique), 1)
        self.assertEqual(len(origins), 2)
        self.assertEqual([x['page_number'] for x in origins['S1:P2']], [2, 2])

    def test_similar_or_annotated_pages_are_not_discarded(self):
        from examples.official_document_workflow.application.upload_batch import unique_uploads
        self.pdf(['REFERENCE 123'])
        self.pdf(['REFERENCE 124'])
        self.pdf(['REFERENCE 123'], annotation='Keep this review note')
        unique, origins = unique_uploads(self.items, self.root)
        self.assertEqual(len(unique), 3)
        self.assertEqual(len(origins), 3)


class NativeOutputContractTests(unittest.TestCase):
    def test_document_evidence_requires_strings_and_cannot_be_omitted(self):
        adapter = TypeAdapter(GroupAnalysis)
        fields = {name: None for name in GROUP_ANALYSIS_SCHEMA['$defs']['DocumentEvidence']['properties']}
        value = {
            'calendar_contract_version': 2, 'events': [], 'primary_source_id': 'S1',
            'group_status': 'related', 'read_source_ids': ['S1'],
            'document': {**fields, 'evidence': fields.copy()},
        }
        adapter.validate_python(value)
        missing = copy.deepcopy(value)
        del missing['document']['evidence']
        bad_type = copy.deepcopy(value)
        bad_type['document']['evidence']['issued_date'] = {'source_id': 'S1', 'start_line': 1, 'end_line': 1}
        for bad in (missing, bad_type):
            with self.assertRaises(ValidationError):
                adapter.validate_python(bad)

    def test_schema_does_not_replace_literal_date_evidence_validation(self):
        data = payload()
        data['document']['evidence']['issued_date'] = data['document']['issued_date']
        with self.assertRaisesRegex(M.CloudExtractionError, 'lacks source evidence'):
            parse_group(
                M.Pipe(),  data, {
                    'S1': M.CloudSemanticSource(SOURCE, 'native_pdf_text'),
                    'S2': M.CloudSemanticSource(DEADLINE_EVIDENCE, 'native_pdf_text'),
                },
            )


if __name__ == '__main__':
    unittest.main()
