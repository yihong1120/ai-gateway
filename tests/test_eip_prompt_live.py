"""Opt-in live LLM eval; fixture text replaces OCR, not model workflow steps.

EIP_PROMPT_LIVE=1 enables provider calls using the deployed Pipe configuration.
No EIP login, exports, calendar writes, or uploads are performed.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest import skipUnless
from unittest.mock import AsyncMock
from unittest.mock import Mock

from langsmith import tracing_context

from examples.official_document_workflow.application.multi_agent import DocumentAgent
from examples.official_document_workflow.domain import documents as module
from examples.shared.runtime.modules import ModuleReference
from tests.eip_prompt_cases import BLIND_CASES
from tests.eip_prompt_cases import CASES
from tests.eip_prompt_cases import HOLDOUT_CASES
from tests.eip_prompt_cases import ROLE_DATE_CASES

_workflow_integration = ModuleReference('examples.official_document_workflow.application.integration')


@skipUnless(os.environ.get('EIP_PROMPT_LIVE') == '1', 'Live provider calls are opt-in')
class PromptLiveTests(IsolatedAsyncioTestCase):
    async def test_contrast_cases_through_document_workflow(self):

        configured_pipe = _workflow_integration.attribute('configured_pipe')
        provider_failure = False
        cases = ROLE_DATE_CASES + HOLDOUT_CASES + BLIND_CASES + CASES
        selected = set(filter(None, os.environ.get('EIP_EVAL_CASES', '').split(',')))
        if selected:
            self.assertFalse(selected - {c['name'] for c in cases}, 'Unknown evaluation case')
            cases = [c for c in cases if c['name'] in selected]
        for case in cases:
            with self.subTest(case=case['name']):
                pipe = await configured_pipe()
                pipe.emit_status = AsyncMock()
                pipe.ocr_context = AsyncMock(return_value=((), None))
                pipe.semantic_source = Mock(
                    side_effect=[
                        module.CloudSemanticSource(text=text, kind='native_pdf_text') for text in case['sources']
                    ],
                )
                members = [
                    SimpleNamespace(
                        pdf_path=Path(f"fixture-{i}.pdf"),
                        source_file=SimpleNamespace(filename=f"fixture-{i}.pdf"),
                    )
                    for i in range(len(case['sources']))
                ]
                with tracing_context(enabled=False):
                    state = await DocumentAgent(pipe,  None).run(members, 1, 1)
                rows = sorted(
                    state.get('rows', []),
                    key=lambda r: (
                        r['Start Date'],
                        r['Start Time(24hr)'],
                        r['Subject'],
                    ),
                )
                events = (state.get('payload') or {}).get('events', [])
                accepted = state.get('accepted_events', [])
                pending = state.get('pending_events', [])
                record = {
                    'case': case['name'],
                    'model': pipe.valves.cloud_ocr_text_model,
                    'status': state.get('analysis_status'),
                    'rows': len(rows),
                    'attempts': state.get('analysis_attempts'),
                    'events': [
                        {
                            key: e.get(key)
                            for key in (
                                'status',
                                'action',
                                'start_date',
                                'end_date',
                                'evidence_spans',
                            )
                        }
                        for e in events
                    ],
                    'repairs': state.get('repair_history'),
                    'errors': state.get('errors'),
                    'csv_errors': state.get('csv_errors'),
                    'review': state.get('semantic_review'),
                    'deferred': bool(state.get('deferred')),
                }
                print(json.dumps(record, ensure_ascii=False), flush=True)
                if os.environ.get('EIP_EVAL_ARTIFACT_DIR'):
                    folder = Path(os.environ['EIP_EVAL_ARTIFACT_DIR'])
                    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
                    (folder / (case['name'] + '.json')).write_text(
                        json.dumps(
                            {
                                **record,
                                'calendar_rows': rows,
                                'payload': state.get('payload'),
                                'accepted_events': accepted,
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
                if state.get('deferred'):
                    # Do not turn a provider outage into a retry storm or a pass.
                    provider_failure = True
                    raise RuntimeError(
                        'Live evaluation stopped after temporary provider failure; remaining cases untested.',
                    )
                self.assertFalse(state.get('errors'))
                if case['mode'] in ('dated', 'mixed'):
                    if case['mode'] == 'dated':
                        self.assertFalse(state.get('csv_errors'))
                    else:
                        self.assertTrue(any(e.get('status') == 'needs_review' for e in [*events, *pending]))
                        self.assertTrue(state.get('csv_errors'))
                    self.assertEqual([row['Start Date'] for row in rows], case['dates'])
                    for field, expected in (
                        ('Start Time(24hr)', 'times'),
                        ('End Date', 'end_dates'),
                        ('End Time(24hr)', 'end_times'),
                    ):
                        if expected in case:
                            self.assertEqual([row[field] for row in rows], case[expected])
                    self.assertTrue(state.get('semantic_review', {}).get('approved'))
                    self.assertTrue(all(row['Private'] == '真' for row in rows))
                    self.assertTrue(all(re.fullmatch(r'.+\[AI產生\](?:\(.+\))?', row['Subject']) for row in rows))
                    if 'responsible_party' in case:
                        self.assertEqual(
                            [e.get('responsible_party') for e in accepted],
                            [case['responsible_party']],
                        )
                elif case['mode'] == 'review':
                    self.assertFalse(rows)
                    self.assertTrue(state.get('csv_errors'))
                    self.assertTrue(
                        all('日期待補：' in error or '日期或附件關係待確認' in error for error in state['csv_errors']),
                        state['csv_errors'],
                    )
                else:
                    self.assertFalse(rows)
                    self.assertFalse(state.get('csv_errors'))
                    self.assertTrue(all(e['status'] in ('historical', 'cancelled', 'out_of_scope') for e in events))
            if provider_failure:
                break
