"""Both entry points retain successful documents when calendar output is unavailable."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from examples.official_document_workflow.application.calendar_stage import CalendarStage
from examples.official_document_workflow.application.outcomes import CalendarResult
from examples.official_document_workflow.domain.documents import OutputUncertain
from examples.official_document_workflow.domain.workflow import JobPlan


class CalendarStageTests(unittest.IsolatedAsyncioTestCase):
    async def test_unreviewed_rows_never_reach_output_writer(self):
        stage = CalendarStage(None, 'owner')
        stage.exporter.save = AsyncMock()
        prepared = stage.prepare(CalendarResult(({'Subject': 'unreviewed'},), (), False), JobPlan())
        result = await stage.save(prepared, 'calendar.csv', 'source', {})
        self.assertEqual(result.state, 'empty')
        stage.exporter.save.assert_not_awaited()

    async def test_output_failure_is_a_partial_result_with_confirmed_rows_retained(self):
        stage = CalendarStage(None, 'owner')
        stage.exporter.save = AsyncMock(side_effect=OutputUncertain('output needs review'))
        prepared = stage.prepare(
            CalendarResult(
                ({'Subject': 'verified event'},),
                ('another group pending',), True,
            ), JobPlan(),
        )
        result = await stage.save(prepared, 'calendar.csv', 'source', {})
        self.assertEqual(result.state, 'failed')
        self.assertEqual(result.prepared.calendar.export_rows(), [{'Subject': 'verified event'}])
        self.assertEqual(result.prepared.calendar.issues, ('another group pending',))
        self.assertIn('needs review', result.error)

    async def test_success_preserves_import_notice_and_file_identity(self):
        stage = CalendarStage(None, 'owner')
        item = SimpleNamespace(id='file')
        stage.exporter.save = AsyncMock(return_value=(item, 'import notice'))
        prepared = stage.prepare(CalendarResult(({'Subject': 'verified event'},), (), True), JobPlan())
        result = await stage.save(prepared, 'calendar.csv', 'source', {})
        self.assertEqual(result.state, 'stored')
        self.assertIs(result.file, item)
        self.assertEqual(result.notice, 'import notice')
