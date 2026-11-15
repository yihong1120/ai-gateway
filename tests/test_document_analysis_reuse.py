"""Real PostgreSQL: one common analysis, independent recipient calendars."""
from __future__ import annotations

import asyncio
import multiprocessing
import tempfile
import time
import unittest
from concurrent.futures import ProcessPoolExecutor
from copy import deepcopy
from datetime import date
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

from examples.official_document_workflow.application.multi_agent import DocumentAgent
from examples.official_document_workflow.domain.calendar.relative_dates import (
    ReceiptContext,
)
from examples.official_document_workflow.domain.documents import ProcessingError
from examples.official_document_workflow.infrastructure.eip import calendar_sync
from examples.official_document_workflow.infrastructure.storage.analysis_results import (
    AnalysisResults,
)
from examples.official_document_workflow.infrastructure.storage.automation_store import (
    AutomationStore,
)
from examples.official_document_workflow.services.analysis_reuse import analyze_content
from examples.official_document_workflow.services.output_delivery import output_job
from examples.official_document_workflow.services.source_readings import read_source
from examples.shared.runtime.modules import ModuleReference
from tests.eip_postgres import EIPPostgresTestCase
from tests.test_eip_connector import remote
from tests.test_eip_group_processing import M
from tests.test_eip_group_processing import payload
from tests.test_eip_group_processing import SOURCE

_pymupdf = ModuleReference('pymupdf')
_fixture_test_eip_connector = ModuleReference('tests.test_eip_connector')


SCHEMA = {
    'type': 'object', 'properties': {'answer': {'type': 'string'}},
    'required': ['answer'], 'additionalProperties': False,
}


def independent_analysis(dsn, schema, key_file):
    store = AutomationStore(Path(key_file), dsn=dsn, schema=schema)
    registry = AnalysisResults(store, 'cross-process')

    def compute():
        with store.db() as db:
            db.execute('INSERT INTO analysis_producer_calls DEFAULT VALUES')
        time.sleep(0.1)
        return {'answer': 'one model invocation'}
    return registry.get_or_compute(registry.key('common-document'), compute, replay_safe=False)


class AnalysisReuseTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.registry = self._workflow.analysis_results
        self.registry.store

    def pipe(self, owner, analyze):
        pipe = M.Pipe()
        pipe._usage_owner = owner
        pipe.analysis_results = self.registry
        pipe.analyze_text = analyze
        pipe._analysis_model_identity = ('synthetic-route', 'model', 'low', 4096)
        return pipe

    async def call(self, pipe, *, prompt='Read the original facts.', schema=SCHEMA, revisions=('original-sha',)):
        return await analyze_content(
            pipe, M.CloudSemanticSource('original content', 'native_pdf_text'),
            stage='synthetic', prompt=prompt, schema=schema, revisions=revisions,
        )

    async def test_ten_recipients_share_analysis_and_review_with_independent_dates_and_idempotent_writes(self):
        common = payload()
        common['document'].pop('calendar_subject')
        common['document']['evidence'].pop('calendar_subject')
        event = common['events'][0]
        event.update(start_date=None, tentative_schedule=None)
        for name in ('title', 'action', 'project_name', 'location', 'responsible_party', 'relevance', 'start_date', 'start_time', 'end_date', 'end_time', 'anchor_date'):
            event['evidence_spans'].setdefault(name, [])
        event['assignment'] = {
            'executor': None, 'date_basis': 'relative', 'anchor_date': None,
            'relative_deadline': {'anchor': 'receipt', 'days': 7, 'day_type': 'calendar_days', 'include_anchor_day': False},
        }
        sources = {
            'S1': M.CloudSemanticSource(SOURCE, 'native_pdf_text'),
            'S2': M.CloudSemanticSource('請於文到後七日內提送評估報告。', 'native_pdf_text'),
        }
        calls = {'analysis': 0, 'review': 0}
        requests = []

        async def upstream(source, *, prompt, schema, **kwargs):
            requests.append(source.text)
            await asyncio.sleep(0.02)
            if 'issues' in schema.get('properties', {}):
                calls['review'] += 1
                return {
                    'issues': [], 'checks': [{
                        'index': 0, 'decision': 'keep', 'target': None,
                        'reason': '原文具有成立的工作與明示期間。',
                        'evidence_spans': [{'source_id': 'S2', 'start_line': 1, 'end_line': 1}],
                    }],
                }
            calls['analysis'] += 1
            return deepcopy(common)

        async def recipient(n):
            owner = f'owner-{n}'
            pipe = self.pipe(owner, upstream)
            pipe._estimate_relative_dates = True
            received = date(2026, 9, 1) + timedelta(days=n)
            pipe._eip_receipt_context = ReceiptContext(
                f'private-flow-{n}', common['document']['reference_number'], received.isoformat(),
            )
            members = []
            for i in range(2):
                path = self.root / f'{owner}-{i}.pdf'
                path.write_bytes(b'original-common-' + str(i).encode())
                members.append(
                    M.PreparedDocument(
                        kind='pdf', source_files=(
                            SimpleNamespace(
                                id=f'{owner}-upload-{i}', filename=f'private-{owner}-{i}.pdf',
                            ),
                        ), pdf_path=path,
                    ),
                )
            with output_job(owner, f'job-{n}'):
                result = await DocumentAgent(pipe, None).run(members, 1, 1, sources=sources)
            self.assertEqual(result['analysis_status'], 'validated')
            self.assertTrue(result['semantic_review']['approved'])
            self.assertEqual(result['rows'][0]['Start Date'], (received + timedelta(days=7)).strftime('%Y/%m/%d'))
            journal = self._workflow.journal
            rows = result['rows']
            events = calendar_sync.verified_events(rows, [remote(row) for row in rows], str(100 + n), journal)
            adapter = SimpleNamespace(
                calendar_import_matches=AsyncMock(return_value=[{'status': 'new'}]),
                commit=AsyncMock(return_value={'event_id': f'private-event-{n}', 'detail_verified': True}),
            )
            self.assertEqual(await calendar_sync.write_events(adapter, events, journal), {'created': 1, 'skipped': 0})
            self.assertEqual(await calendar_sync.write_events(adapter, events, journal), {'created': 0, 'skipped': 1})
            adapter.commit.assert_awaited_once()
            return result['rows'][0]['Start Date']

        dates = await asyncio.gather(*(recipient(i) for i in range(10)))
        self.assertEqual(len(set(dates)), 10)
        self.assertEqual(calls, {'analysis': 1, 'review': 1})
        self.assertNotIn('private-flow', '\n'.join(requests))
        self.assertNotIn('private-owner', '\n'.join(requests))
        self.assertNotIn('received_date', '\n'.join(requests))
        with self.registry.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM document_analysis_results').fetchone()['n'], 2)
            self.assertEqual(db.execute('SELECT count(*) AS n FROM document_analysis_uses').fetchone()['n'], 20)

    async def test_success_survives_restart_and_cache_age_and_returns_independent_objects(self):
        model = AsyncMock(return_value={'answer': 'original'})
        alice = self.pipe('alice', model)
        first = await self.call(alice)
        first['answer'] = 'private edit'
        with self.registry.store.db() as db:
            db.execute("UPDATE document_analysis_results SET expires_at=CURRENT_TIMESTAMP-INTERVAL '90 days'")
        bob = self.pipe('bob', model)
        bob.analysis_results = AnalysisResults(self.registry.store, self.registry.scope)
        self.assertEqual(await self.call(bob), {'answer': 'original'})
        model.assert_awaited_once()
        with self.registry.store.db() as db:
            blob = bytes(db.execute('SELECT payload FROM document_analysis_results').fetchone()['payload'])
        self.assertNotIn(b'original', blob)

    async def test_changed_source_contract_prompt_and_model_have_separate_records(self):
        model = AsyncMock(return_value={'answer': 'ok'})
        pipe = self.pipe('alice', model)
        await self.call(pipe)
        await self.call(pipe, revisions=('changed-original-sha',))
        await self.call(pipe, prompt='A corrected source policy.')
        await self.call(pipe, schema={**SCHEMA, 'description': 'new contract'})
        pipe._analysis_model_identity = ('synthetic-route', 'different-model')
        await self.call(pipe)
        self.assertEqual(model.await_count, 5)

    async def test_ambiguous_paid_failure_is_not_repeated_by_other_recipients_or_after_restart(self):
        async def fail(*args, **kwargs):
            await asyncio.sleep(0.01)
            raise TimeoutError('synthetic paid outcome unknown')
        model = AsyncMock(side_effect=fail)
        errors = await asyncio.gather(*(self.call(self.pipe(str(i), model)) for i in range(10)), return_exceptions=True)
        self.assertTrue(all(isinstance(error, (TimeoutError, ProcessingError)) for error in errors))
        model.assert_awaited_once()
        pipe = self.pipe('next-owner', model)
        pipe.analysis_results = AnalysisResults(self.registry.store, self.registry.scope)
        with self.assertRaises(ProcessingError):
            await self.call(pipe)
        model.assert_awaited_once()

    def test_registry_defaults_to_protecting_uncertain_paid_calls_without_expiry(self):
        model = Mock(side_effect=TimeoutError('unknown provider outcome'))
        key = self.registry.key('direct-paid-call')
        with self.assertRaises(TimeoutError):
            self.registry.get_or_compute(key, model)
        with self.registry.store.db() as db:
            db.execute("UPDATE document_analysis_results SET expires_at=CURRENT_TIMESTAMP-INTERVAL '90 days'")
        with self.assertRaises(ProcessingError):
            self.registry.get_or_compute(key, model)
        model.assert_called_once()

    async def test_corrupt_completed_record_does_not_start_another_paid_call(self):
        model = AsyncMock(return_value={'answer': 'ok'})
        pipe = self.pipe('alice', model)
        await self.call(pipe)
        with self.registry.store.db() as db:
            db.execute('UPDATE document_analysis_results SET payload=%s', (b'corrupt',))
        with self.assertRaises(ProcessingError):
            await self.call(self.pipe('bob', model))
        model.assert_awaited_once()

    async def test_missing_key_cannot_be_regenerated_when_only_paid_results_exist(self):
        await self.call(self.pipe('alice', AsyncMock(return_value={'answer': 'kept'})))
        self._workflow.settings.key.unlink()
        with self.assertRaisesRegex(ValueError, '金鑰遺失'):
            AutomationStore(self._workflow.settings.key, dsn=self.registry.store.dsn, schema=self.registry.store.schema)
        self.assertFalse(self._workflow.settings.key.exists())

    async def test_invalid_model_shape_never_becomes_a_reusable_success(self):
        model = AsyncMock(return_value={'unexpected': 'missing answer'})
        pipe = self.pipe('alice', model)
        with self.assertRaises(Exception):
            await self.call(pipe)
        with self.registry.store.db() as db:
            self.assertEqual(db.execute('SELECT state FROM document_analysis_results').fetchone()['state'], 'unknown')
        with self.assertRaises(ProcessingError):
            await self.call(self.pipe('bob', model))
        model.assert_awaited_once()

    async def test_manual_not_started_resolution_is_audited_and_never_overrides_an_active_or_completed_call(self):
        pipe = self.pipe('alice', AsyncMock(side_effect=TimeoutError('not started')))
        with self.assertRaises(TimeoutError):
            await self.call(pipe)
        with self.registry.store.db() as db:
            key = db.execute('SELECT key FROM document_analysis_results').fetchone()['key']
        with self.registry._connect() as locked:
            locked.execute('SELECT pg_advisory_lock(hashtextextended(%s,0))', (self.registry._lock_name(key),))
            with self.assertRaises(ValueError):
                await asyncio.to_thread(self.registry.confirm_not_started, key, actor='operator', reason='Provider transport confirmed no request was submitted.')
        self.registry.confirm_not_started(
            key, actor='operator', reason='Provider transport confirmed no request was submitted.',
        )
        pipe.analyze_text = AsyncMock(return_value={'answer': 'recovered'})
        self.assertEqual(await self.call(pipe), {'answer': 'recovered'})
        with self.assertRaises(ValueError):
            self.registry.confirm_not_started(key, actor='operator', reason='Must not reset a completed result.')
        with self.registry.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM document_analysis_resolutions').fetchone()['n'], 1)

    async def test_independent_processes_share_the_same_paid_producer(self):
        with self.registry.store.db() as db:
            db.execute('CREATE TABLE analysis_producer_calls(id BIGSERIAL PRIMARY KEY)')
        with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context('spawn')) as pool:
            futures = [
                pool.submit(
                    independent_analysis, self.registry.store.dsn, self.registry.store.schema,
                    str(self._workflow.settings.key),
                ) for _ in range(2)
            ]
            values = await asyncio.gather(*(asyncio.to_thread(future.result, 15) for future in futures))
        self.assertEqual(values, [{'answer': 'one model invocation'}] * 2)
        with self.registry.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM analysis_producer_calls').fetchone()['n'], 1)

    async def test_cancelled_recipient_does_not_cancel_another_recipients_analysis(self):
        entered, finish = asyncio.Event(), asyncio.Event()

        async def compute(*args, **kwargs):
            entered.set()
            await finish.wait()
            return {'answer': 'completed'}
        model = AsyncMock(side_effect=compute)
        alice = asyncio.create_task(self.call(self.pipe('alice', model)))
        await asyncio.wait_for(entered.wait(), 3)
        bob = asyncio.create_task(self.call(self.pipe('bob', model)))
        alice.cancel()
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await alice
        self.assertEqual(await asyncio.wait_for(bob, 3), {'answer': 'completed'})
        model.assert_awaited_once()

    async def test_cache_hit_still_requires_each_recipients_current_source_authorization(self):
        model = AsyncMock(return_value={'answer': 'authorized facts'})
        await self.call(self.pipe('alice', model))
        bob = self.pipe('bob', model)
        bob._analysis_authorize = AsyncMock(side_effect=PermissionError('Source access was revoked.'))
        with self.assertRaises(PermissionError):
            await self.call(bob)
        model.assert_awaited_once()

    def source_document(self, owner):
        pymupdf = _pymupdf.load()
        first = self.root / 'original.pdf'
        if not first.exists():
            with pymupdf.open() as document:
                document.new_page().insert_text((40, 50), 'Original shared document')
                document.save(first)
        path = self.root / (owner + '.pdf')
        path.write_bytes(first.read_bytes())
        return M.PreparedDocument(kind='pdf', source_files=(SimpleNamespace(id=owner, filename=owner+'.pdf'),), pdf_path=path)

    async def test_durable_source_provenance_prevents_reocr_from_repeating_the_same_analysis(self):
        model = AsyncMock(return_value={'answer': 'verified facts'})
        alice, bob = self.pipe('alice', model), self.pipe('bob', model)
        alice.ocr_context = AsyncMock(return_value=((), None))
        alice.semantic_source = Mock(
            return_value=M.CloudSemanticSource(
                'Original line one\nOriginal line two', 'native_pdf_text',
            ),
        )
        bob.ocr_context = AsyncMock(side_effect=AssertionError('Do not rerun OCR after the rebuildable cache expires.'))
        bob.semantic_source = Mock(side_effect=AssertionError('Do not replace the source line coordinates.'))
        one, _ = await read_source(alice, self.source_document('alice'))
        await analyze_content(alice, one, stage='document', prompt='facts', schema=SCHEMA)
        with self.registry.store.db() as db:
            db.execute("UPDATE document_analysis_results SET expires_at=CURRENT_TIMESTAMP-INTERVAL '90 days'")
        bob.analysis_results = AnalysisResults(self.registry.store, self.registry.scope)
        two, _ = await read_source(bob, self.source_document('bob'))
        self.assertEqual(one, two)
        self.assertEqual(await analyze_content(bob, two, stage='document', prompt='facts', schema=SCHEMA), {'answer': 'verified facts'})
        model.assert_awaited_once()
        bob.ocr_context.assert_not_awaited()
        bob.semantic_source.assert_not_called()

    async def test_source_archive_hits_keep_recipient_limits_without_poisoning_the_complete_reading(self):
        alice, bob = self.pipe('alice', AsyncMock()), self.pipe('bob', AsyncMock())
        alice.valves.cloud_ocr_text_max_input_characters = 1000
        alice.ocr_context = AsyncMock(return_value=((), None))
        alice.semantic_source = Mock(return_value=M.CloudSemanticSource('Original text ' * 150, 'native_pdf_text'))
        with self.assertRaises(ProcessingError):
            await read_source(alice, self.source_document('alice'))
        bob.valves.eip_large_sources = True
        bob.ocr_context = AsyncMock(side_effect=AssertionError('Complete reading must already exist.'))
        result, _ = await read_source(bob, self.source_document('bob'))
        self.assertGreater(len(result.text), 1000)
        bob.ocr_context.assert_not_awaited()

    async def test_one_uncertain_calendar_write_does_not_repeat_analysis_or_block_other_recipients(self):
        row = _fixture_test_eip_connector.attribute('row')
        model = AsyncMock(return_value={'answer': 'shared facts'})
        await self.call(self.pipe('alice', model))
        await self.call(self.pipe('bob', model))
        model.assert_awaited_once()
        rows = [row()]
        journal = self._workflow.journal
        alice = calendar_sync.verified_events(rows, [remote(rows[0])], '17', journal)
        bob = calendar_sync.verified_events(rows, [remote(rows[0])], '18', journal)
        first = SimpleNamespace(
            calendar_import_matches=AsyncMock(return_value=[{'status': 'new'}]),
            commit=AsyncMock(side_effect=TimeoutError('Ambiguous remote write.')),
        )
        second = SimpleNamespace(
            calendar_import_matches=AsyncMock(return_value=[{'status': 'new'}]),
            commit=AsyncMock(return_value={'event_id': 'bob-event', 'detail_verified': True}),
        )
        with self.assertRaises(TimeoutError):
            await calendar_sync.write_events(first, alice, journal)
        with self.assertRaises(ValueError):
            await calendar_sync.write_events(first, alice, journal)
        self.assertEqual(await calendar_sync.write_events(second, bob, journal), {'created': 1, 'skipped': 0})
        first.commit.assert_awaited_once()
        second.commit.assert_awaited_once()
