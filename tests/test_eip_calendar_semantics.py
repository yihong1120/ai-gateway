from __future__ import annotations

import copy
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import examples.official_document_workflow.application.group_processing as _group_processing_imports
from . import test_eip_group_processing as group_tests
from .test_official_document_processor_pipe import PIPE_MODULE as M
from examples.official_document_workflow.application.group_processing import parse_group
from examples.official_document_workflow.application.multi_agent import DocumentAgent
from examples.official_document_workflow.domain.calendar.calendar_semantics import assignment_event
from examples.official_document_workflow.domain.calendar.calendar_semantics import literal_source_candidates
from examples.official_document_workflow.domain.calendar.calendar_semantics import resolve_spans
from examples.official_document_workflow.domain.calendar.calendar_semantics import temporal_source_candidates
from examples.official_document_workflow.domain.calendar.calendar_semantics import uncovered_time_candidates
from examples.official_document_workflow.domain.calendar.calendar_semantics import verify_date
from examples.official_document_workflow.domain.calendar.calendar_semantics import verify_event_date
from examples.official_document_workflow.domain.calendar.calendar_semantics import verify_next_day_end
from examples.official_document_workflow.domain.calendar.calendar_semantics import verify_time
from examples.official_document_workflow.domain.validation import ContentValidationError

payload = group_tests.payload


MAIN = """會勘通知單
會勘事由：古坑工程149甲線號誌、路燈遷移及防汛道路封閉事宜會勘
會勘時間：詳附件1本次會勘資訊
備註：附件2為111年9月7日歷史會勘紀錄，僅供本次參照。
補充通知：本次會勘時間以附件1為準。"""
ATTACHMENT = """本次古坑工程149甲線會勘資訊
會勘時間：115年9月8日（星期二）上午10時整
會勘地點：工程東側起點鄰149甲線路口
請攜帶設計圖至現場討論。"""


# Calendar fixtures include valid naming metadata after the referenced lines.
MAIN += '\n' + group_tests.SOURCE


def spans(sid, start, end=None):
    return [{'source_id': sid, 'start_line': start, 'end_line': end or start}]


def event():
    return {
        'status': 'current',
        'title': '古坑工程｜149甲線號誌及路燈遷移會勘',
        'project_name': '古坑工程',
        'action': '號誌、路燈遷移及防汛道路封閉事宜會勘',
        'start_date': '2026-09-08',
        'start_time': '10:00',
        'end_date': None,
        'end_time': None,
        'location': '工程東側起點鄰149甲線路口',
        'responsible_party': None,
        'evidence_spans': {
            'title': spans('S1', 2),
            'action': spans('S1', 2),
            'project_name': spans('S1', 2),
            'start_date': spans('S2', 2),
            'start_time': spans('S2', 2),
            'location': spans('S2', 3),
            'relevance': spans('S1', 3, 5),
        },
        'description_spans': [*spans('S1', 2, 5), *spans('S2', 1, 4)],
    }


class CalendarSemanticsTests(unittest.TestCase):
    def test_unrelated_clock_does_not_force_a_calendar_error(self):

        agent = DocumentAgent(None, None)
        candidate = {'status': 'current', 'start_time': '09:00'}
        state = {
            'payload': {'events': [candidate]}, 'sources': {
                'S1': M.CloudSemanticSource(
                    '會議於上午9時召開。\n前次工作於下午3時完成。', 'native_pdf_text',
                ),
            }, 'analysis_attempts': 2,
        }
        with patch.object(agent, 'document_source', return_value=None), patch(
            'examples.official_document_workflow.application.multi_agent.parse_group',
            return_value=('source', 'document', [], [{'Subject': '會議'}], []),
        ):
            result = agent.validate(state)
        self.assertEqual(result['csv_errors'], [])
        self.assertEqual(result['analysis_status'], 'validated')

    def test_explicit_event_without_date_evidence_stays_pending(self):
        candidate = event()
        candidate['assignment'] = {
            'executor': None,
            'date_basis': 'explicit',
            'anchor_date': None,
        }
        candidate['start_date'] = None
        candidate['evidence_spans']['start_date'] = []
        sources = {
            'S1': M.CloudSemanticSource(MAIN, 'native_pdf_text'),
            'S2': M.CloudSemanticSource(ATTACHMENT, 'native_pdf_text'),
        }
        result = assignment_event(candidate, sources)
        self.assertEqual(result['status'], 'needs_review')
        self.assertIsNone(result['start_date'])

    def test_historical_milestone_is_not_promoted_by_document_reference(self):
        candidate = event()
        candidate.update(status='historical', kind='project_milestone')
        candidate['assignment'] = {
            'executor': None,
            'date_basis': 'explicit',
            'anchor_date': None,
        }
        candidate['evidence_spans']['relevance'] = spans('S1', 2)
        sources = {
            'S1': M.CloudSemanticSource(MAIN, 'native_pdf_text'),
            'S2': M.CloudSemanticSource(ATTACHMENT, 'native_pdf_text'),
        }
        result = assignment_event(candidate, sources, 'S1')
        self.assertEqual(result['status'], 'historical')

    def test_temporal_index_is_literal_bounded_and_includes_cross_day_context(self):
        sources = {
            'S1': M.CloudSemanticSource(
                '發文日期：115年8月1日\n預定於115年9月2日晚間9時至翌日6時辦理\n無時間資料',
                'native_pdf_text',
            ),
        }
        indexed = temporal_source_candidates(sources, limit=1)
        self.assertEqual(indexed, [{'source_id': 'S1', 'line': 1, 'text': '發文日期：115年8月1日'}])
        indexed = temporal_source_candidates(sources)
        self.assertEqual(indexed[1]['line'], 2)
        self.assertIn('翌日6時', indexed[1]['text'])

    def test_nearby_uncovered_times_request_one_completeness_correction(self):
        sources = {
            'S1': M.CloudSemanticSource(
                '工程名稱：示例工程\n115年9月18日上午9時50分辦理簡報，預定10時30分現場查核，預定11時30分文件檢討。',
                'native_pdf_text',
            ),
        }
        selected = {
            'status': 'current',
            'start_time': '09:50',
            'end_time': None,
            'evidence_spans': {
                'start_date': spans('S1', 2),
                'start_time': spans('S1', 2),
                'relevance': spans('S1', 2),
            },
        }
        missing = uncovered_time_candidates([selected], sources)
        self.assertEqual([item['time'] for item in missing], ['10時30分', '11時30分'])
        self.assertEqual(
            uncovered_time_candidates([{**selected, 'end_time': '10:30'}], sources)[0]['time'],
            '11時30分',
        )

    def test_untimed_candidate_does_not_hide_nearby_explicit_clock(self):
        sources = {
            'S1': M.CloudSemanticSource('會議結論\n散會（上午11時00分）', 'native_pdf_text'),
        }
        selected = {
            'status': 'current',
            'start_time': None,
            'end_time': None,
            'evidence_spans': {'relevance': spans('S1', 1)},
        }
        self.assertEqual(uncovered_time_candidates([selected], sources)[0]['time'], '上午11時00分')

    def test_inherited_afternoon_end_time_is_already_covered(self):
        sources = {
            'S1': M.CloudSemanticSource(
                '執行時段：116年5月6日下午2時20分至3時40分', 'native_pdf_text',
            ),
        }
        selected = {
            'status': 'current',
            'start_time': '14:20',
            'end_time': '15:40',
            'evidence_spans': {
                'start_date': spans('S1', 1),
                'start_time': spans('S1', 1),
                'end_time': spans('S1', 1),
                'relevance': spans('S1', 1),
            },
        }
        self.assertEqual(uncovered_time_candidates([selected], sources), [])

    def test_literal_correction_candidates_include_wrapped_name_not_ocr_guesses(self):
        sources = {
            'S1': M.CloudSemanticSource('其他資料\n\n工程名稱：測試甲\n線改善工程\n結尾', 'native_pdf_text'),
            'S2': M.CloudSemanticSource('工程名稱：測式甲線改善工程', 'native_pdf_text'),
        }
        original = {
            'project_name': '測試甲線改善工程',
            'evidence_spans': {'project_name': spans('S2', 1)},
        }
        before = copy.deepcopy(original)
        matches = literal_source_candidates([original], sources)
        self.assertEqual(matches[0]['exact_source_candidates'], spans('S1', 3, 4))
        self.assertEqual(original, before)
        self.assertEqual(
            literal_source_candidates([{'project_name': '測試乙線改善工程'}], sources)[0]['exact_source_candidates'],
            [],
        )

    def setUp(self):
        self.pipe = M.Pipe()
        self.sources = {
            'S1': M.CloudSemanticSource(MAIN, 'native_pdf_text'),
            'S2': M.CloudSemanticSource(ATTACHMENT, 'native_pdf_text'),
        }

    def parse(self, events=None):
        return self.pipe.calendar.events(

            {'events': events if events is not None else [event()]},
            self.sources,
            None,
        )

    def test_null_title_or_action_is_a_validation_error_not_a_batch_crash(self):
        for field in ('title', 'action'):
            candidate = event()
            candidate[field] = None
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.parse([candidate])

    def test_cross_attachment_fields_and_generated_title_without_metadata(self):
        deadlines, rows = self.parse()
        self.assertEqual(rows[0]['Subject'], '149甲線號誌及路燈遷移會勘[AI產生](古坑工程)')
        self.assertEqual(rows[0]['Start Date'], '2026/09/08')
        self.assertEqual(rows[0]['Start Time(24hr)'], '10:00')
        self.assertEqual(rows[0]['End Time(24hr)'], '')
        self.assertEqual(rows[0]['Custom Field1'], '')
        self.assertIn('請攜帶設計圖', rows[0]['Description'])
        self.assertIn('相關附件 1 補充內容', rows[0]['Description'])
        self.assertNotIn('【來源 S', rows[0]['Description'])
        self.assertEqual(len(deadlines), 1)
        self.assertEqual(rows[0]['Private'], '真')

    def test_description_always_includes_header_facts_even_if_model_selects_only_remarks(
        self,
    ):
        e = event()
        e['description_spans'] = spans('S2', 4)
        row = self.parse([e])[1][0]
        for text in ('115年9月8日', '上午10時整', '路口', '請攜帶設計圖'):
            self.assertIn(text, row['Description'])

    def test_historical_cancelled_not_new_events(self):
        historical = {
            'status': 'historical',
            'evidence_spans': {'relevance': spans('S1', 4)},
        }
        cancelled = {
            'status': 'cancelled',
            'evidence_spans': {'relevance': spans('S1', 4)},
        }
        self.assertEqual(len(self.parse([event(), historical, cancelled])[1]), 1)
        self.assertEqual(self.parse([historical])[1], [])
        self.assertEqual(self.parse([]), ([], []))

    def test_conflicting_or_missing_date_fails_closed(self):
        for mutation in (
            {'status': 'needs_review'},
            {'start_date': None},
            {'start_date': '2026-09-09'},
            {'start_time': '14:00'},
            {'end_time': '09:00'},
            {'title': '很長' * 100},
        ):
            with self.subTest(mutation=mutation):
                e = event()
                e.update(mutation)
                with self.assertRaises(ValueError):
                    self.parse([e])

    def test_invalid_source_location_or_missing_provenance(self):
        for field, value in [('location', '公司總部'), ('responsible_party', '某某人')]:
            e = event()
            e[field] = value
            with self.assertRaises(ValueError):
                self.parse([e])
        e = event()
        e['evidence_spans']['start_date'] = spans('S999', 2)
        with self.assertRaises(ValueError):
            self.parse([e])
        e = event()
        e['evidence_spans']['start_date'] = spans('S1', 4)
        with self.assertRaises(ValueError):
            self.parse([e])

    def test_metadata_failure_stops_even_with_valid_event(self):
        data = payload()
        data['calendar_contract_version'] = 2
        data['document']['subject'] = '來源沒有的主旨'
        data['document']['evidence']['subject'] = '來源沒有的主旨'
        data['events'] = [event()]
        _ = []
        with self.assertRaises(M.CloudExtractionError):
            parse_group(self.pipe,  data, self.sources)

    def test_metadata_known_ocr_wrappers_do_not_cause_false_subject_failure(self):
        data = payload()
        data['calendar_contract_version'] = 2
        data['events'] = []
        original = group_tests.SOURCE
        subject = data['document']['subject']
        wrapped = original.replace(subject, '$ \\underline{\\text{' + subject + '}} $')
        sources = {
            'S1': M.CloudSemanticSource(wrapped, 'paddle_layout_ocr'),
            'S2': M.CloudSemanticSource(group_tests.DEADLINE_EVIDENCE, 'native_pdf_text'),
        }
        diagnostics = []
        _, doc, _, _, errors = parse_group(self.pipe,  data, sources, diagnostics=diagnostics)
        self.assertIsNotNone(doc)
        self.assertEqual(doc.subject, subject)
        self.assertFalse(diagnostics)
        self.assertFalse(errors)
        self.assertEqual(sources['S1'].text, wrapped)
        data['document']['subject'] = '沒有來源的另一個工程主旨'
        _, doc, _, _, _ = parse_group(self.pipe,  data, sources)
        self.assertEqual(doc.subject, subject)
        data['document']['evidence']['subject'] = '沒有來源的另一個工程主旨'
        with self.assertRaises(M.CloudExtractionError):
            parse_group(self.pipe,  data, sources)

    def test_one_bad_event_aborts_before_returning_partial_rows(self):
        bad = event()
        bad['start_date'] = '2026-09-09'
        with self.assertRaises(ValueError):
            self.pipe.calendar.events({'events': [event(), bad]}, self.sources, None)

    def test_metadata_native_pdf_wrap_between_chinese_and_digits(self):
        data = payload()
        data.update(calendar_contract_version=2, events=[])
        original = data['document']['subject']
        subject = '有關「111年0918地震-寶華大橋改建工程」勞動檢查缺失，請儘速辦理並函復。'
        data['document'].update(subject=subject, filename_subject=None)
        data['document']['evidence'].update(subject=subject, filename_subject=None)
        source = group_tests.SOURCE.replace(original, subject.replace('111年0918', '111年\n0918'))
        sources = {
            'S1': M.CloudSemanticSource(source, 'native_pdf_text'),
            'S2': M.CloudSemanticSource(group_tests.DEADLINE_EVIDENCE, 'native_pdf_text'),
        }
        before = copy.deepcopy(data)
        diagnostics = []
        _, doc, _, _, errors = parse_group(self.pipe,  data, sources, diagnostics=diagnostics)
        self.assertIsNotNone(doc)
        self.assertEqual(doc.subject, subject)
        self.assertEqual(diagnostics, [])
        self.assertEqual(errors, [])
        self.assertEqual(sources['S1'].text, source)
        self.assertEqual(data, before)
        for claimed in (
            subject.replace('0918', '0919'),
            subject.replace('寶華大橋', '古坑'),
            subject.replace('勞動檢查缺失，', ''),
        ):
            with self.subTest(claimed=claimed):
                data['document']['subject'] = claimed
                data['document']['evidence']['subject'] = claimed
                with self.assertRaises(M.CloudExtractionError):
                    parse_group(self.pipe,  data, sources)

    def test_metadata_wrap_does_not_join_separate_numbers_or_latin_words(self):

        for source, expected in (
            ('111\n0918', '111 0918'),
            ('ABC\nDEF', 'ABC DEF'),
            ('111年\n\n0918', '111年\n\n0918'),
            ('主旨\n<page>\n文字', '主旨<page>文字'),
        ):
            with self.subTest(source=source):
                self.assertEqual(_group_processing_imports.metadata_text(self.pipe, source), expected)

    def test_metadata_canonical_cjk_glyphs_in_native_pdf(self):
        data = payload()
        data.update(calendar_contract_version=2, events=[])
        source = group_tests.SOURCE.replace('年', '年').replace('路', '路')
        sources = {
            'S1': M.CloudSemanticSource(source, 'native_pdf_text'),
            'S2': M.CloudSemanticSource(group_tests.DEADLINE_EVIDENCE, 'native_pdf_text'),
        }
        diagnostics = []
        _, doc, _, _, errors = parse_group(self.pipe,  data, sources, diagnostics=diagnostics)
        self.assertIsNotNone(doc)
        self.assertEqual(diagnostics, [])
        self.assertEqual(errors, [])
        self.assertEqual(doc.subject, data['document']['subject'])
        self.assertEqual(sources['S1'].text, source)
        # A different number is not a typography variant.
        data['document']['evidence']['issued_date'] = '中華民國999年1月1日'
        with self.assertRaises(M.CloudExtractionError):
            parse_group(self.pipe,  data, sources)

    def test_metadata_normalization_preserves_compatibility_distinctions(self):

        self.assertEqual(
            _group_processing_imports.metadata_text(self.pipe, '第①期／Ａ版／２０２６／年路'),
            '第①期／Ａ版／２０２６／年路',
        )

    def test_metadata_ocr_wrapper_gap_after_project_quote(self):
        data = payload()
        data.update(calendar_contract_version=2, events=[])
        subject = '檢送「古坑工程」橋墩處防震拉條詳圖（A版）審查意見1份，請查照。'
        original = data['document']['subject']
        data['document'].update(subject=subject, filename_subject=None)
        data['document']['evidence'].update(subject=subject, filename_subject=None)
        wrapped = subject.replace('橋墩處', r' $ \underline{\text{橋墩處}} $')
        source = group_tests.SOURCE.replace(original, wrapped)
        sources = {
            'S1': M.CloudSemanticSource(source, 'paddle_layout_ocr'),
            'S2': M.CloudSemanticSource(group_tests.DEADLINE_EVIDENCE, 'native_pdf_text'),
        }
        diagnostics = []
        _, doc, _, _, errors = parse_group(self.pipe,  data, sources, diagnostics=diagnostics)
        self.assertIsNotNone(doc)
        self.assertEqual(doc.subject, subject)
        self.assertEqual(diagnostics, [])
        self.assertEqual(errors, [])
        self.assertEqual(sources['S1'].text, source)

        for text in ('工程」\n\n橋墩', '111年 0918', 'ABC DEF', '1 2', '第①期／Ａ版'):
            self.assertEqual(_group_processing_imports.metadata_text(self.pipe, text), text)

    def test_undated_task_requests_date_instead_of_inventing_one(self):
        for status in ('current', 'needs_review'):
            e = event()
            e.update(status=status, start_date=None, start_time=None)
            with (
                self.subTest(status=status),
                self.assertRaisesRegex(ValueError, '日期待補.*實際送達日'),
            ):
                self.parse([e])

    def test_title_variation_does_not_duplicate_same_event(self):
        second = event()
        second['title'] = '古坑工程｜號誌及路燈遷移現場會勘'
        self.assertEqual(len(self.parse([event(), second])[1]), 1)

    def test_distinct_events_have_distinct_generated_titles(self):
        second = event()
        second['title'] = '古坑工程｜攜帶設計圖參加現場討論'
        second['action'] = '攜帶設計圖參加現場討論'
        second['evidence_spans']['action'] = spans('S2', 4)
        second['evidence_spans']['title'] = spans('S2', 4)
        rows = self.parse([event(), second])[1]
        self.assertEqual(
            [r['Subject'] for r in rows],
            [e['title'].split('｜', 1)[1] + '[AI產生](古坑工程)' for e in (event(), second)],
        )

    def test_same_day_distinct_agenda_stages_remain_separate(self):
        self.sources = {
            'S1': M.CloudSemanticSource(
                '工程名稱：示例工程\n'
                '115年9月18日上午9時50分於會議室辦理聯防簡報及注意事項告知。\n'
                '同日預定10時30分於工地現場辦理稽核。\n'
                '同日預定11時30分於會議室辦理內業稽核及檢討。',
                'native_pdf_text',
            ),
        }

        def agenda(title, action, clock, line):
            return {
                'status': 'current',
                'title': title,
                'project_name': '示例工程',
                'action': action,
                'start_date': '2026-09-18',
                'start_time': clock,
                'end_date': None,
                'end_time': None,
                'location': None,
                'responsible_party': None,
                'evidence_spans': {
                    'title': spans('S1', line),
                    'project_name': spans('S1', 1),
                    'action': spans('S1', line),
                    'start_date': spans('S1', 2, line),
                    'start_time': spans('S1', line),
                    'relevance': spans('S1', line),
                },
                'description_spans': spans('S1', line),
            }

        _, rows = self.parse(
            [
                agenda(
                    '辦理聯防簡報及注意事項告知',
                    '辦理聯防簡報及注意事項告知',
                    '09:50',
                    2,
                ),
                agenda('辦理工地現場稽核', '辦理工地現場稽核', '10:30', 3),
                agenda('辦理內業稽核及檢討', '辦理內業稽核及檢討', '11:30', 4),
            ],
        )
        self.assertEqual(
            [(r['Start Date'], r['Start Time(24hr)'], r['Subject']) for r in rows],
            [
                ('2026/09/18', '09:50', '辦理聯防簡報及注意事項告知[AI產生](示例工程)'),
                ('2026/09/18', '10:30', '辦理工地現場稽核[AI產生](示例工程)'),
                ('2026/09/18', '11:30', '辦理內業稽核及檢討[AI產生](示例工程)'),
            ],
        )

    def test_unknown_project_is_explicit_and_not_invented(self):
        e = event()
        e.pop('project_name')
        e['evidence_spans'].pop('project_name')
        e['title'] = '149甲線號誌及路燈遷移會勘'
        row = self.parse([e])[1][0]
        self.assertTrue(row['Subject'].endswith('[AI產生]'))
        self.assertIn('未猜測工地', row['Description'])

    def test_project_requires_its_own_source_and_can_come_from_attachment(self):
        e = event()
        e['project_name'] = '台3線吳鳳橋改建工程'
        with self.assertRaises(ValueError):
            self.parse([e])
        e['project_name'] = '古坑工程'
        e['evidence_spans']['project_name'] = spans('S2', 1)
        self.assertTrue(self.parse([e])[1][0]['Subject'].endswith('[AI產生](古坑工程)'))

    def test_precise_date_time_normalization(self):
        for value, source in [
            ('2026-09-08', '１１５年９月８日'),
            ('2026-09-08', '2026/09/08'),
        ]:
            self.assertTrue(verify_date(value, source))
        self.assertFalse(verify_date('2026-09-08', '111年9月8日'))
        self.assertFalse(verify_date('2026-02-30', '115年2月30日'))
        for value, source in [
            ('10:00', '上午10時整'),
            ('09:50', '上午9時50分'),
            ('14:00', '下午2時'),
            ('00:00', '凌晨12時'),
            ('12:00', '中午12時'),
        ]:
            self.assertTrue(verify_time(value, source), (value, source))
        self.assertFalse(verify_time('14:00', '上午2時'))

    def test_issue_date_is_not_evidence_for_relative_deadline(self):
        for text in (
            '發文日期：中華民國115年9月2日',
            '發文日期：\n中華民國115年9月2日',
            '發文日期：115年9月2日\n貴公司若有異議請於一週內回覆。',
        ):
            self.assertFalse(verify_event_date('2026-09-02', text))
        self.assertTrue(verify_event_date('2026-09-08', '發文日期：115年9月2日\n會勘時間：115年9月8日上午10時'))
        self.assertTrue(verify_event_date('2026-09-10', '並於115年9月10日前提報複核表及改善前、後照片各一式2份。'))

    def test_time_period_shared_only_within_adjacent_forward_interval(self):
        for text, valid, invalid in (
            ('下午2時20分至3時40分', '15:40', '03:40'),
            ('下午1:15～2:30', '14:30', '02:30'),
            ('上午9時到11時', '11:00', '23:00'),
            ('上午11時至下午2時', '14:00', '02:00'),
            ('下午2時20分。另一事項3時40分', '03:40', '15:40'),
        ):
            with self.subTest(text=text):
                self.assertTrue(verify_time(valid, text))
                self.assertFalse(verify_time(invalid, text))
        self.assertFalse(verify_time('13:00', '下午2時至1時'))
        self.assertFalse(verify_time('15:40', '下午2時\n隔日3時40分'))

    def test_schedule_row_can_use_its_own_month_day_and_same_source_schedule_year(self):
        source = """日期 工務段
新工南分局 9月11日 第一段 9月16日 第六段
公路局115年9月份工安督導預定排程(無外聘委員)"""
        self.assertTrue(verify_event_date('2026-09-16', '新工南分局 9月11日 第一段 9月16日 第六段', [source]))
        self.assertFalse(verify_event_date('2026-09-17', '新工南分局 9月11日 第一段 9月16日 第六段', [source]))
        self.assertFalse(verify_event_date('2026-09-16', '新工南分局 9月11日 第一段 9月16日 第六段', []))
        self.assertFalse(verify_event_date('2026-09-16', '發文日期：115年9月4日', [source]))

    def test_explicit_overnight_end_cannot_be_omitted_for_default_duration(self):
        e = event()
        self.sources['S2'] = M.CloudSemanticSource(
            '本次施工\n115年9月8日晚間9時至翌日6時封閉道路。\n工程東側起點鄰149甲線路口\n施工',
            'native_pdf_text',
        )
        e['start_time'] = '21:00'
        e['evidence_spans']['action'] = spans('S2', 2)
        with self.assertRaisesRegex(ValueError, '原文明載跨日結束時間'):
            self.parse([e])

    def test_next_day_end_verifies_date_rollover_and_clocks(self):
        for start, end in [
            ('2027-04-30', '2027-05-01'),
            ('2027-12-31', '2028-01-01'),
            ('2028-02-28', '2028-02-29'),
            ('2028-02-29', '2028-03-01'),
        ]:
            e = {'start_date': start, 'start_time': '21:00', 'end_time': '06:00'}
            with self.subTest(start=start):
                self.assertTrue(verify_next_day_end(end, start + ' 晚間9時至翌日6時', e))
                self.assertFalse(verify_next_day_end(start, start + ' 晚間9時至翌日6時', e))
                self.assertFalse(verify_next_day_end(end, start + ' 晚間8時至翌日6時', e))
                self.assertFalse(verify_next_day_end(end, start + ' 晚間9時至翌日7時', e))

    def test_next_day_end_does_not_guess_missing_anchor_or_unrelated_interval(self):
        e = {'start_date': '2027-04-30', 'start_time': '21:00', 'end_time': '06:00'}
        for text in (
            '晚間9時至翌日6時',
            '2027-04-30 晚間9時，翌日6時另一件事',
            '2027-04-30 晚間9時至後日6時',
            '發文日期：2027-04-30\n晚間9時至翌日6時',
        ):
            with self.subTest(text=text):
                self.assertFalse(verify_next_day_end('2027-05-01', text, e))
        self.assertFalse(verify_next_day_end('2027-05-01', '2027-04-30 晚間9時至翌日6時', {**e, 'end_time': None}))

    def test_complete_overnight_event_retains_source_end_not_default_duration(self):
        e = event()
        e.update(start_time='21:00', end_date='2026-09-09', end_time='06:00')
        self.sources['S2'] = M.CloudSemanticSource(
            '本次施工\n115年9月8日晚間9時至翌日6時封閉道路。\n工程東側起點鄰149甲線路口\n施工',
            'native_pdf_text',
        )
        for field in ('action', 'end_date', 'end_time'):
            e['evidence_spans'][field] = spans('S2', 2)
        rows = self.parse([e])[1]
        self.assertEqual(
            (rows[0]['Start Date'], rows[0]['End Date'], rows[0]['End Time(24hr)']),
            ('2026/09/08', '2026/09/09', '06:00'),
        )
        self.assertNotIn('預留', rows[0]['Description'])

    def test_overnight_end_in_time_reference_not_action_still_required(self):
        e = event()
        e['start_time'] = '21:00'
        self.sources['S2'] = M.CloudSemanticSource(
            '本次施工\n115年9月8日晚間9時至次日上午6時封閉道路。\n工程東側起點鄰149甲線路口\n施工',
            'native_pdf_text',
        )
        with self.assertRaisesRegex(ValueError, '原文明載跨日結束時間'):
            self.parse([e])

    def test_september10_case_keeps_deadline_not_audit_or_issue_date(self):
        text = '主旨：本分局115年8月28日實地辦理古坑工程稽核缺失改善。\n發文日期：115年9月1日\n二、並於115年9月10日前提報複核表及改善前、後照片各一式2份。'
        self.sources = {'S1': M.CloudSemanticSource(text, 'native_pdf_text')}
        e = event()
        e.update(
            title='提報稽核缺失改善複核表及改善前後照片',
            start_date='2026-09-10',
            start_time=None,
            location=None,
            evidence_spans={
                k: spans('S1', 1 if k in ('title', 'project_name') else 3)
                for k in ('title', 'project_name', 'action', 'start_date', 'relevance')
            },
            description_spans=spans('S1', 3),
        )
        e['action'] = '提報複核表及改善前後照片'
        row = self.parse([e])[1][0]
        self.assertEqual(
            (
                row['Start Date'],
                row['End Date'],
                row['Start Time(24hr)'],
                row['End Time(24hr)'],
                row['All day event'],
            ),
            ('2026/09/10', '2026/09/10', '', '', '真'),
        )
        self.assertIn('各一式2份', row['Description'])
        e['start_date'] = '2026-09-01'
        e['evidence_spans']['start_date'] = spans('S1', 2)
        with self.assertRaises(ValueError):
            self.parse([e])

    def test_unknown_project_prefix_is_not_guessed_or_persisted(self):
        e = event()
        e['title'] = '另一工地｜號誌會勘'
        with self.assertRaises(ValueError):
            self.parse([e])

    def test_real_bridge_case_preserves_requirements_without_ocr_markup(self):
        project = '代辦國道3號古坑交流道平面側車道工程'
        raw = (
            '主旨：檢送「'
            + project
            + '」'
            + r'$ \underline{\text{橋墩處}} $防震拉條詳圖（A版）審查意見1份，請查照。'
            + '\n'
            + r'二、請貴所依旨揭審查意見修正後於 $ \underline{\text{115年9月9日前提送本段續辦}} $。'
        )
        self.sources = {'S1': M.CloudSemanticSource(raw, 'paddle_layout_ocr')}
        e = {
            'status': 'current',
            'title': '提報橋墩防震拉條詳圖審查修正結果',
            'project_name': project,
            'action': '依審查意見修正後提送本段續辦',
            'start_date': '2026-09-09',
            'start_time': None,
            'evidence_spans': {
                k: spans('S1', 1 if k in ('title', 'project_name') else 2)
                for k in ('title', 'project_name', 'action', 'start_date', 'relevance')
            },
            'description_spans': spans('S1', 1, 2),
        }
        row = self.parse([e])[1][0]
        self.assertEqual(row['Subject'], e['title'] + '[AI產生](' + project + ')')
        self.assertEqual(row['All day event'], '真')
        self.assertIn('115年9月9日前提送本段續辦', row['Description'])
        self.assertIn('公文內容：', row['Description'])
        self.assertNotIn('\\underline', row['Description'])
        self.assertNotIn('【來源 S', row['Description'])
        self.assertEqual(self.sources['S1'].text, raw)

    def test_only_known_layout_markers_removed(self):
        source = M.CloudSemanticSource('首段\n<page 2>\n完整末段', 'native_pdf_text', frozenset({2}))
        result = resolve_spans(spans('S1', 1, 3), {'S1': source})
        self.assertEqual(result[0][3], '首段\n完整末段')
        with self.assertRaises(ValueError):
            resolve_spans(
                spans('S1', 1, 3),
                {'S1': M.CloudSemanticSource(source.text, source.kind)},
            )

    def test_overlapping_cross_field_ranges_are_unioned_without_losing_text(self):
        source = M.CloudSemanticSource('甲\n乙\n丙\n丁', 'native_pdf_text')
        selected = [
            {'source_id': 'S1', 'start_line': 3, 'end_line': 4},
            {'source_id': 'S1', 'start_line': 1, 'end_line': 3},
        ]
        self.assertEqual(resolve_spans(selected, {'S1': source})[0][3], '甲\n乙\n丙\n丁')

    def test_one_past_last_line_is_canonicalized_but_larger_overshoot_fails(self):
        source = M.CloudSemanticSource('甲\n乙', 'native_pdf_text')
        self.assertEqual(resolve_spans(spans('S1', 1, 3), {'S1': source})[0][3], '甲\n乙')
        with self.assertRaises(ValueError):
            resolve_spans(spans('S1', 1, 4), {'S1': source})


class CalendarGraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_generated_title_can_use_validated_action_provenance(self):
        data = payload()
        e = event()
        e['evidence_spans']['title'] = []
        data.update(calendar_contract_version=2, events=[e])
        self.install_payloads(data)
        result = await self.run_group()
        self.assertTrue(result['calendar_ready'], result['errors'])
        self.review.assert_awaited_once()
        candidate = self.review.await_args.args[2][0]
        self.assertEqual(candidate['evidence_spans']['title'], e['evidence_spans']['action'])
        self.assertEqual(e['evidence_spans']['title'], [])

    async def test_rejected_independent_candidate_is_quarantined_not_exported(self):

        first = event()
        second = event()
        second['title'] = '獨立核准會勘'
        data = payload()
        data.update(calendar_contract_version=2, events=[first, second])
        original = copy.deepcopy(data)
        self.install_payloads(data)
        self.review.return_value = {
            'approved': True,
            'issues': ['第一筆責任需核對'],
            'global_issues': [],
            'checks': [
                {
                    'index': 0,
                    'decision': 'reject',
                    'target': None,
                    'reason': '第一筆責任需核對',
                },
                {
                    'index': 1,
                    'decision': 'keep',
                    'target': None,
                    'reason': '獨立日期已確認',
                },
            ],
        }
        state = await DocumentAgent(self.pipe,  None).run(self.items, 1, 1)
        self.assertEqual(state['payload'], original)
        self.assertEqual(len(state['rows']), 1)
        self.assertEqual(state['accepted_events'][0]['title'], '獨立核准會勘')
        self.assertEqual(state['pending_events'][0]['status'], 'needs_review')
        self.assertIsNone(state['pending_events'][0]['start_date'])
        self.assertIn('第一筆責任需核對', state['csv_errors'][0])
        self.pipe.analyze_text.assert_awaited_once()
        self.review.assert_awaited_once()

    async def test_long_action_is_corrected_without_losing_source(self):
        data = payload()
        e = event()
        e['action'] = '具體工作內容' * 30
        data.update(calendar_contract_version=2, events=[e])
        corrected = payload()
        corrected.update(calendar_contract_version=2, events=[event()])
        self.install_payloads(data, corrected)
        result = await self.run_group()
        self.assertTrue(result['calendar_ready'], result['errors'])
        self.assertEqual(self.pipe.analyze_text.await_count, 2)

    async def test_pending_item_keeps_independently_verified_calendar_row(self):
        data = payload()
        pending = event()
        pending.update(status='needs_review', start_date=None)
        data.update(calendar_contract_version=2, events=[pending, event()])
        self.install_payloads(data)
        result = await self.run_group()
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(result['files']), 5)
        self.assertTrue(result['csv_ids'])
        self.assertTrue(result['calendar_ready'])
        self.assertIn('日期待補', result['errors'][0])
        self.pipe.analyze_text.assert_awaited_once()
        self.review.assert_awaited_once()

    async def test_mixed_missing_date_keeps_corrected_independent_calendar_row(self):
        pending = event()
        pending.update(status='needs_review', start_date=None)
        bad = event()
        bad['start_date'] = '2026-09-09'
        first = payload()
        first.update(calendar_contract_version=2, events=[pending, bad])
        second = payload()
        second.update(calendar_contract_version=2, events=[pending, event()])
        self.install_payloads(first, second)
        result = await self.run_group()
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['calendar_ready'])
        self.assertTrue(result['csv_ids'])
        self.assertIn('日期待補', result['errors'][0])
        self.assertEqual(self.pipe.analyze_text.await_count, 2)

    async def test_unverified_optional_label_not_fabricated_or_calendar_blocker(self):
        data = payload()
        e = event()
        e['responsible_party'] = '來源未載明的完整稱呼'
        e['evidence_spans']['responsible_party'] = spans('S1', 2)
        data.update(calendar_contract_version=2, events=[e])
        self.install_payloads(data)
        result = await self.run_group()
        self.assertTrue(result['calendar_ready'])
        candidate = self.review.await_args.args[2][0]
        self.assertIsNone(candidate['responsible_party'])
        self.assertTrue(candidate['_field_notices'])
        self.assertEqual(data['events'][0]['responsible_party'], '來源未載明的完整稱呼')

    async def test_dependent_candidates_keep_exports_without_calendar(self):
        data = payload()
        pending = event()
        pending.update(status='needs_review', start_date=None)
        data.update(calendar_contract_version=2, events=[pending, event()])
        self.install_payloads(data, data)
        self.review.return_value = {
            'approved': False,
            'issues': ['同一活動的改期尚未確定。'],
        }
        result = await self.run_group()
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(result['files']), 4)
        self.assertFalse(result['csv_ids'])
        self.pipe.analyze_text.assert_awaited_once()
        self.review.assert_awaited_once()

    def setUp(self):

        group_tests.GroupTests.setUp(self)
        review = patch(
            'examples.official_document_workflow.application.multi_agent.review_calendar',
            new_callable=AsyncMock,
            return_value={'approved': True, 'issues': []},
        )
        self.review = review.start()
        self.addCleanup(review.stop)

    run_group = group_tests.GroupTests.run_group

    def install_payloads(self, *values):
        self.sources = {
            'S1': M.CloudSemanticSource(MAIN, 'native_pdf_text'),
            'S2': M.CloudSemanticSource(ATTACHMENT, 'native_pdf_text'),
        }
        self.pipe.semantic_source.side_effect = list(self.sources.values())
        self.pipe.analyze_text.side_effect = list(values)

    async def test_one_bounded_format_correction_can_recover(self):
        good = payload()
        good.update(calendar_contract_version=2, events=[event()])
        bad = copy.deepcopy(good)
        bad['events'] = None
        self.install_payloads(bad, good)
        result = await self.run_group()
        self.assertTrue(result['calendar_ready'], result['errors'])
        self.assertEqual(self.pipe.analyze_text.await_count, 2)

    async def test_repeated_format_error_keeps_documents_without_calendar(self):
        bad = payload()
        bad.update(calendar_contract_version=2, events=None)
        self.install_payloads(bad, bad)
        result = await self.run_group()
        self.assertFalse(result['calendar_ready'])
        self.assertFalse(result['csv_ids'])
        self.assertEqual(len(result['files']), 4)
        self.assertEqual(self.pipe.analyze_text.await_count, 2)

    async def test_current_without_date_exports_without_correction_or_calendar(self):
        good = payload()
        good.update(calendar_contract_version=2, events=[event()])
        bad = copy.deepcopy(good)
        bad['events'][0]['start_date'] = None
        self.install_payloads(bad, good)
        result = await self.run_group()
        self.assertEqual(len(result['files']), 4)
        self.assertFalse(result['csv_ids'])
        self.assertIn('日期待補', result['errors'][0])
        self.pipe.analyze_text.assert_awaited_once()

    async def test_current_missing_date_keeps_documents_without_invention(self):
        bad = payload()
        bad.update(calendar_contract_version=2, events=[event()])
        bad['events'][0]['start_date'] = None
        pending = copy.deepcopy(bad)
        pending['events'][0]['status'] = 'needs_review'
        self.install_payloads(bad, pending)
        result = await self.run_group()
        self.assertEqual({f['kind'] for f in result['files']}, {'document_pdf', 'document_docx'})
        self.assertFalse(result['calendar_ready'])
        self.assertEqual(result['calendar_status'], 'needs_review')
        self.pipe.analyze_text.assert_awaited_once()

    async def test_classified_merge_is_revalidated_before_export(self):

        self.pipe.calendar_csv = Mock(wraps=self.pipe.calendar_csv)
        one = event()
        two = copy.deepcopy(one)
        one['action'] = '準備會勘'
        two['action'] = '辦理會勘'
        data = payload()
        data.update(calendar_contract_version=2, events=[one, two])
        self.install_payloads(data)
        self.review.return_value = {
            'approved': True,
            'issues': [],
            'checks': [
                {'index': 0, 'decision': 'merge', 'target': 1},
                {'index': 1, 'decision': 'keep'},
            ],
        }
        result = await self.run_group()
        self.assertTrue(result['calendar_ready'], result['errors'])
        self.assertEqual(len(result['csv_ids']), 1)
        rows = self.pipe.calendar_csv.call_args.args[0]
        self.assertEqual(len(rows), 1)
        self.assertIn('請攜帶設計圖至現場討論。', rows[0]['Description'])

    async def test_unverified_project_is_corrected_from_source(self):
        good = payload()
        good.update(calendar_contract_version=2, events=[event()])
        bad = copy.deepcopy(good)
        bad['events'][0]['project_name'] = '不存在的工程'
        self.install_payloads(bad, good)
        result = await self.run_group()
        self.assertTrue(result['calendar_ready'], result['errors'])
        self.assertEqual(self.review.await_args.args[2][0]['project_name'], '古坑工程')
        self.assertEqual(self.pipe.analyze_text.await_count, 2)

    async def test_no_retry_for_semantic_uncertainty_or_missing_date(self):
        for change in (
            {'status': 'needs_review'},
            {'status': 'needs_review', 'start_date': None},
        ):
            with self.subTest(change=change):
                data = payload()
                e = event()
                e.update(change)
                data['events'] = [e]
                self.install_payloads(data)
                self.pipe.analyze_text.reset_mock()
                if e['start_date'] is None:
                    result = await self.run_group()
                    self.assertFalse(result['csv_ids'])
                    self.assertEqual(len(result['files']), 4)
                else:
                    result = await self.run_group()
                    self.assertFalse(result['csv_ids'])
                    self.assertEqual(len(result['files']), 4)
                self.pipe.analyze_text.assert_awaited_once()

    async def test_date_provenance_error_can_be_corrected_once(self):
        bad = payload()
        e = event()
        e['start_date'] = '2026-09-09'
        bad.update(calendar_contract_version=2, events=[e])
        good = payload()
        good.update(calendar_contract_version=2, events=[event()])
        self.install_payloads(bad, good)
        result = await self.run_group()
        self.assertTrue(result['calendar_ready'], result['errors'])
        self.assertEqual(self.pipe.analyze_text.await_count, 2)

    async def test_date_correction_can_leave_the_item_pending(self):

        bad = payload()
        e = event()
        e['start_date'] = '2026-09-09'
        bad.update(calendar_contract_version=2, events=[e])
        pending = copy.deepcopy(bad)
        pending['events'][0].update(status='needs_review', start_date=None, start_time=None)
        self.install_payloads(bad, pending)
        result = await self.run_group()
        self.assertFalse(result['calendar_ready'])
        self.assertFalse(result['csv_ids'])
        self.assertIn('日期待補', result['errors'][0])
        self.assertEqual(self.pipe.analyze_text.await_count, 2)

    async def test_missing_overnight_end_can_be_corrected_from_source(self):
        good = payload()
        e = event()
        e.update(start_time='21:00', end_date='2026-09-09', end_time='06:00')
        for field in ('action', 'end_date', 'end_time'):
            e['evidence_spans'][field] = spans('S2', 2)
        good.update(calendar_contract_version=2, events=[e])
        bad = copy.deepcopy(good)
        bad['events'][0].update(end_date=None, end_time=None)
        self.install_payloads(bad, good)
        self.pipe.semantic_source.side_effect = [
            self.sources['S1'],
            M.CloudSemanticSource(
                '本次施工\n115年9月8日晚間9時至翌日6時封閉道路。\n工程東側起點鄰149甲線路口\n施工',
                'native_pdf_text',
            ),
        ]
        result = await self.run_group()
        self.assertTrue(result['calendar_ready'], result['errors'])
        self.assertEqual(self.pipe.analyze_text.await_count, 2)

    async def test_repeated_date_provenance_error_keeps_documents(self):
        bad = payload()
        e = event()
        e['start_date'] = '2026-09-09'
        bad.update(calendar_contract_version=2, events=[e])
        self.install_payloads(bad, bad)
        result = await self.run_group()
        self.assertFalse(result['calendar_ready'])
        self.assertFalse(result['csv_ids'])
        self.assertEqual(len(result['files']), 4)
        self.assertEqual(self.pipe.analyze_text.await_count, 2)

    async def test_failure_does_not_drop_previously_detected_event(self):
        bad = payload()
        e = event()
        e['evidence_spans']['start_date'] = spans('S2', 999)
        bad.update(calendar_contract_version=2, events=[e])
        empty = copy.deepcopy(bad)
        empty['events'] = []
        self.install_payloads(bad, empty)
        result = await self.run_group()
        self.assertFalse(result['calendar_ready'])
        self.assertFalse(result['csv_ids'])
        self.assertEqual(result['validation_issues'][0]['code'], 'event_repair_omitted_items')
        self.assertEqual(self.pipe.analyze_text.await_count, 2)

    async def test_failure_does_not_silently_reduce_two_events_to_one(self):
        good = payload()
        good.update(calendar_contract_version=2, events=[event()])
        bad = copy.deepcopy(good)
        bad['events'].append(event())
        bad['events'][1]['evidence_spans']['start_date'] = spans('S2', 999)
        self.install_payloads(bad, good)
        result = await self.run_group()
        self.assertFalse(result['calendar_ready'])
        self.assertFalse(result['csv_ids'])
        self.assertEqual(result['validation_issues'][0]['code'], 'event_repair_omitted_items')
        self.assertEqual(self.pipe.analyze_text.await_count, 2)

    async def test_event_capacity_exceeded_is_not_retried_or_truncated(self):
        bad = payload()
        bad.update(
            calendar_contract_version=2,
            events=[event()] * (self.pipe.valves.cloud_max_events + 1),
        )
        self.install_payloads(bad)
        result = await self.run_group()
        self.assertFalse(result['calendar_ready'])
        self.assertFalse(result['csv_ids'])
        self.pipe.analyze_text.assert_awaited_once()

    async def test_metadata_warning_raises_before_exports(self):
        data = payload()
        data['calendar_contract_version'] = 2
        data['events'] = [event()]
        data['document']['subject'] = '來源沒有的主旨'
        data['document']['evidence']['subject'] = '來源沒有的主旨'
        self.sources = {
            'S1': M.CloudSemanticSource(MAIN, 'native_pdf_text'),
            'S2': M.CloudSemanticSource(ATTACHMENT, 'native_pdf_text'),
        }
        self.pipe.semantic_source.side_effect = list(self.sources.values())
        self.pipe.analyze_text.return_value = data
        with self.assertRaises(ContentValidationError):
            await self.run_group()
        self.assertEqual(self.pipe.analyze_text.await_count, 2)
        self.pipe.store_output.assert_not_awaited()

    async def test_semantic_reviewer_rejection_does_not_rewrite_candidates(self):
        good = payload()
        good.update(calendar_contract_version=2, events=[event()])
        self.install_payloads(good, good)
        self.review.return_value = {
            'approved': False,
            'issues': ['日期屬待核准提議，未有同意來源。'],
        }
        result = await self.run_group()
        self.assertFalse(result['calendar_ready'])
        self.assertFalse(result['csv_ids'])
        self.assertEqual(len(result['files']), 4)
        self.assertIn('語意核對', ' '.join(result['calendar_errors']))
        self.review.assert_awaited_once()
        self.pipe.analyze_text.assert_awaited_once()
        self.assertFalse(result['orchestration']['agents'][1]['semantic_review']['approved'])

    async def test_incorrect_review_cannot_replace_a_correct_candidate_and_self_approve(
        self,
    ):

        good = payload()
        good.update(calendar_contract_version=2, events=[event()])
        original = deepcopy(good)
        wrong = deepcopy(good)
        wrong['events'][0]['responsible_party'] = '無來源的新責任人'
        self.install_payloads(good, wrong)
        self.review.side_effect = [
            {'approved': False, 'issues': ['改成無來源的新責任人。']},
            {'approved': True, 'issues': []},
        ]
        state = await DocumentAgent(self.pipe,  None).run(self.items, 1, 1)
        self.assertEqual(state['payload'], original)
        self.assertEqual(state['analysis_attempts'], 1)
        self.assertEqual(state['repair_history'], [])
        self.assertFalse(state['rows'])
        self.assertTrue(state['accepted_events'])
        self.review.assert_awaited_once()
        self.pipe.analyze_text.assert_awaited_once()

    async def test_semantic_reviewer_failure_never_exports_calendar(self):
        good = payload()
        good.update(calendar_contract_version=2, events=[event()])
        self.install_payloads(good)
        self.review.side_effect = RuntimeError('private response')
        with self.assertRaises(RuntimeError):
            await self.run_group()
        self.pipe.analyze_text.assert_awaited_once()
        self.pipe.store_output.assert_not_awaited()

    async def test_semantic_reviewer_rate_limit_raises_without_deferral(self):
        good = payload()
        good.update(calendar_contract_version=2, events=[event()])
        self.install_payloads(good)
        self.review.side_effect = M.CloudRateLimited('HTTP 429')
        with self.assertRaises(M.CloudRateLimited):
            await self.run_group()
        self.pipe.analyze_text.assert_awaited_once()
        self.pipe.store_output.assert_not_awaited()
