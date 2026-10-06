from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from eip_real_live import run_batch


class RealRunnerTests(IsolatedAsyncioTestCase):
    async def test_temporary_failure_stops_other_cases_and_is_not_a_pass(self):
        evaluate = AsyncMock(side_effect=[{'document_id': '1', 'deferred': True, 'calls': 1}])
        with TemporaryDirectory() as root:
            folder = Path(root)/'run'
            results = await run_batch([{'document_id': '1'}, {'document_id': '2'}], folder, evaluate)
            self.assertEqual(len(results), 1)
            evaluate.assert_awaited_once()
            report = json.loads((folder/'execution.json').read_text())
            self.assertTrue(report['provider_deferred'])
            self.assertEqual(report['unexecuted'], ['2'])
            self.assertNotIn('passed', report)

    async def test_existing_artifacts_cannot_be_overwritten_or_reused_as_new_run(self):
        evaluate = AsyncMock()
        with TemporaryDirectory() as root:
            with self.assertRaises(FileExistsError):
                await run_batch([{'document_id': '1'}], Path(root), evaluate)
        evaluate.assert_not_awaited()

    async def test_completed_cases_continue_without_repeating_prior_case(self):
        evaluate = AsyncMock(side_effect=[{'document_id': '1'}, {'document_id': '2'}])
        with TemporaryDirectory() as root:
            folder = Path(root)/'run'
            await run_batch([{'document_id': '1'}, {'document_id': '2'}], folder, evaluate)
            self.assertEqual(evaluate.await_count, 2)
            self.assertEqual(json.loads((folder/'execution.json').read_text())['unexecuted'], [])
