"""Preparation suggestions need independent, source-backed approval."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from examples.official_document_workflow.application.preparation_review import reviewed_preparations


class PreparationReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = SimpleNamespace(text='稽核前請先提送簡報。\n一般通知供參考。')
        self.suggestion = {'days_before': 9, 'action': '準備稽核簡報', 'reason': '留出整理時間',
                           'evidence_spans': [{'source_id': 'S1', 'start_line': 1, 'end_line': 1}]}
        self.event = {'status': 'current', 'title': '工程稽核', 'preparation_reminders': [self.suggestion]}
        self.analysis = {'accepted_events': [self.event], 'semantic_review': {'approved': True},
                         'analysis_input': {'sources': []}, 'sources': {'S1': self.source}}
        self.module = SimpleNamespace(CloudSemanticSource=SimpleNamespace)

    async def test_only_source_backed_approved_suggestions_are_selected(self):
        original = copy.deepcopy(self.analysis)
        check = {'index': 0, 'decision': 'keep', 'reason': '原文要求稽核前提交簡報',
                 'evidence_spans': [{'source_id': 'S1', 'start_line': 1, 'end_line': 1}]}
        pipe = SimpleNamespace(analyze_text=AsyncMock(return_value={'checks': [check]}))
        self.assertEqual(await reviewed_preparations(pipe,  self.analysis), [0])
        self.assertEqual(self.analysis, original)
        check['decision'] = 'reject'
        self.assertEqual(await reviewed_preparations(pipe,  self.analysis), [])

    async def test_other_source_line_cannot_authorize_suggestion(self):
        check = {'index': 0, 'decision': 'keep', 'reason': '可追蹤',
                 'evidence_spans': [{'source_id': 'S1', 'start_line': 2, 'end_line': 2}]}
        pipe = SimpleNamespace(analyze_text=AsyncMock(return_value={'checks': [check]}))
        with self.assertRaises(ValueError):
            await reviewed_preparations(pipe,  self.analysis)

    async def test_unapproved_anchor_never_invokes_model(self):
        self.analysis['semantic_review']['approved'] = False
        pipe = SimpleNamespace(analyze_text=AsyncMock())
        self.assertEqual(await reviewed_preparations(pipe,  self.analysis), [])
        pipe.analyze_text.assert_not_awaited()
