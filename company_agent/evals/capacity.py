"""Isolated capacity experiments; provider latency is simulated, never billed.

Run with COMPANY_AGENT_TEST_DATABASE_URL and EIP_TEST_DATABASE_URL. Random
schemas and temporary keys are destroyed after each profile. This never alters
production limits, bindings, queues, models, or calendar entries.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import resource
import tempfile
import time
import uuid
from collections import Counter
from functools import partial
from types import SimpleNamespace
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import psycopg
from psycopg import sql

from company_agent.api.queue import WorkQueue
from company_agent.api.service import Service
from company_agent.evals.sandbox import make_state_directory
from company_agent.evals.runner import evaluation
from examples.shared.runtime import concurrency
from examples.shared.runtime.workers import WorkerHealth
from examples.official_document_workflow.infrastructure.storage.automation_store import AutomationStore
from examples.official_document_workflow.infrastructure.storage.state import State, Journal


def percentile(values, fraction):
    ordered = sorted(values)
    return round(ordered[min(len(ordered)-1, int((len(ordered)-1)*fraction))], 3) if ordered else 0


def render_document():
    """Real local PDF/Word creation and PDF rendering, using synthetic text."""
    import pymupdf
    from docx import Document
    with pymupdf.open() as pdf:
        for number in range(10):
            page = pdf.new_page()
            page.insert_textbox(
                (50, 50, 500, 780),
                '\n'.join(f'Capacity test page {number+1}: record {row}, due 2026-10-04.' for row in range(35)),
            )
            page.get_pixmap(dpi=144).samples
        pdf_bytes = pdf.tobytes()
    word = Document()
    for number in range(50):
        word.add_paragraph(f'Synthetic record {number}; no external actions.')
    output = BytesIO()
    word.save(output)
    assert pdf_bytes.startswith(b'%PDF') and output.getvalue().startswith(b'PK')


async def profile(limit, model_limit, ocr_limit, users, hold):
    from examples.official_document_workflow.application import automation, background_upload
    company_dsn = os.environ['COMPANY_AGENT_TEST_DATABASE_URL']
    eip_dsn = os.environ['EIP_TEST_DATABASE_URL']
    company_schema = 'company_agent_test_' + uuid.uuid4().hex
    eip_schema = 'eip_test_' + uuid.uuid4().hex
    durations, waiting, acknowledgements, lag, started, failures = [], [], [], [], [], []
    active, peak, owner_active = Counter(), Counter(), set()
    with tempfile.TemporaryDirectory(prefix='capacity-profile-') as directory, patch.dict(
        os.environ, {
            'COMPANY_AGENT_DATABASE_URL': company_dsn, 'COMPANY_AGENT_DB_SCHEMA': company_schema,
            'EIP_DATABASE_URL': eip_dsn, 'EIP_DB_SCHEMA': eip_schema,
        },
    ):
        try:
            root = Path(directory)
            state_path = root/'state'
            make_state_directory(state_path)
            queue = WorkQueue(state_path, root/'uploads')
            db = AutomationStore(root/'eip.key')
            journal = Journal(dsn=eip_dsn, schema=eip_schema)
            state = State()
            state.processing = concurrency.OwnerSlots(limit)
            jobs = []
            for number in range(users):
                owner = f'load-{number}'
                for turn in range(2):
                    before = time.perf_counter()
                    if (number+turn) % 2:
                        key = queue.submit_document(owner, 'chat', str(turn), [{'id': 'synthetic'}], {}, 'synthetic')
                    else:
                        key = queue.submit(owner, 'subject-'+owner, 'chat', str(turn), 'synthetic task', [])
                    acknowledgements.append((time.perf_counter()-before)*1000)
                    jobs.append((owner, key))
            account_count = min(users, 64)  # Existing opt-in auto-account bound.
            for number in range(account_count):
                owner = f'auto-{number}'
                db.bind(owner, owner, owner, 'synthetic-password')
            with db.db() as connection:
                connection.execute('UPDATE accounts SET next_run=0')
            service = Service(queue, None, None, None, max_concurrent=limit)
            model_gate, ocr_gate = asyncio.Semaphore(model_limit), asyncio.Semaphore(ocr_limit)
            begin = time.perf_counter()

            @concurrency.model_request
            async def model():
                active['model'] += 1
                peak['model'] = max(peak['model'], active['model'])
                try:
                    await asyncio.sleep(.15)
                finally:
                    active['model'] -= 1

            async def ocr():
                # Use the production async/thread gate, but synthetic native
                # latency here. Live GPU results are a separate experiment.
                await concurrency.run_ocr(native_ocr)

            def native_ocr():
                time.sleep(.15)

            async def work(owner, kind, key):
                before = time.perf_counter()
                if (owner, 'queue') in owner_active:
                    raise AssertionError('same-owner concurrent execution')
                owner_active.add((owner, 'queue'))
                active[kind] += 1
                peak[kind] = max(peak[kind], active[kind])
                started.append((owner, kind, key))
                waiting.append(before-begin)
                try:
                    await asyncio.gather(model(), model(), model())
                    if kind != 'company':
                        await ocr()
                        await asyncio.to_thread(render_document)
                    await asyncio.sleep(hold)
                finally:
                    active[kind] -= 1
                    owner_active.remove((owner, 'queue'))
                    durations.append(time.perf_counter()-before)

            async def company(row):
                await work(row['owner'], 'company', row['id'])
                await service._io(queue.update, row['id'], status='done')

            async def document(current, row):
                async with concurrency.DOCUMENTS.slot(row['owner']):
                    await work(row['owner'], 'document', row['id'])
                await current._io(queue.update, row['id'], status='done')

            async def account(current, journal, owner, generation):
                async with state.processing.slot(owner):
                    await work(owner, 'eip', owner)
                await asyncio.to_thread(current.finish, owner, generation, 'synthetic completed')

            async def monitor():
                while True:
                    before = time.perf_counter()
                    await asyncio.sleep(.02)
                    lag.append(max(0, time.perf_counter()-before-.02)*1000)
                    peak['ocr'] = max(peak['ocr'], ocr_limit-ocr_gate._value)

            with patch.object(concurrency, 'DOCUMENTS', concurrency.OwnerSlots(limit)), \
                patch.object(concurrency, 'MODELS', model_gate), patch.object(concurrency, 'OCR', ocr_gate), \
                patch.object(service, 'execute', side_effect=company), \
                patch.object(background_upload, 'execute', side_effect=document):
                service.task = asyncio.create_task(service.loop())
                scheduler = automation.AutomaticScheduler(
                    db, journal, state, WorkerHealth(),
                    account=SimpleNamespace(run=partial(account, db, journal)),
                )
                eip_task = asyncio.create_task(scheduler.run())
                observer = asyncio.create_task(monitor())
                try:
                    async with asyncio.timeout(180):
                        while True:
                            await asyncio.sleep(.1)
                            if service.task.done():
                                service.task.result()
                            if eip_task.done():
                                eip_task.result()
                            if len(started) == len(jobs)+account_count and not owner_active:
                                if not service.executions and not state.automatic_tasks:
                                    break
                    elapsed = time.perf_counter()-begin
                    with queue.store.db() as connection:
                        failures = connection.execute(
                            "SELECT status,count(*) FROM web_jobs WHERE status<>'done' GROUP BY status",
                        ).fetchall()
                    assert not failures, failures
                    assert len({key for _, kind, key in started if kind != 'eip'}) == len(jobs)
                    # Mixed lanes can finish at different speeds. Preserve FIFO
                    # across each owner's two work types, even when one fills.
                    for owner in {owner for owner, _ in jobs}:
                        assert [key for current, kind, key in started if current == owner and kind != 'eip'] == [
                            key for current, key in jobs if current == owner
                        ], 'owner FIFO violated'
                    for name in ['company', 'document', 'eip']:
                        assert peak[name] <= limit
                    assert peak['model'] <= model_limit and peak['ocr'] <= ocr_limit
                finally:
                    for task in [service.task, eip_task, observer]:
                        task.cancel()
                    await asyncio.gather(service.task, eip_task, observer, return_exceptions=True)
                assert not service.executions and not state.automatic_tasks
                assert not concurrency.DOCUMENTS.active and not state.processing.active
                assert ocr_gate._value == ocr_limit and model_gate._value == model_limit
            return {
                'scope': 'real PostgreSQL dispatcher + EIP scheduler + local 10-page PDF/Word rendering; simulated provider/OCR latency',
                'limits': {'company': limit, 'documents': limit, 'eip': limit, 'models': model_limit, 'ocr': ocr_limit},
                'users': users, 'jobs': len(jobs)+account_count, 'errors': 0,
                'elapsed_seconds': round(elapsed, 3), 'jobs_per_minute': round((len(jobs)+account_count)*60/elapsed, 2),
                'submit_p95_ms': percentile(acknowledgements, .95), 'wait_p95_seconds': percentile(waiting, .95),
                'execution_p95_seconds': percentile(durations, .95), 'event_loop_lag_p95_ms': percentile(lag, .95),
                'event_loop_lag_max_ms': round(max(lag), 3), 'peaks': dict(peak),
                'max_rss_mib': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024, 1),
            }
        finally:
            for dsn, schema in [(company_dsn, company_schema), (eip_dsn, eip_schema)]:
                assert ((schema.startswith('company_agent_test_') and len(schema) == 51)
                        or (schema.startswith('eip_test_') and len(schema) == 41))
                with psycopg.connect(dsn, autocommit=True) as connection:
                    connection.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))


def backlog(total, per_user):
    """Measure real acceptance limits, duplicate requests and cancellation."""
    dsn = os.environ['COMPANY_AGENT_TEST_DATABASE_URL']
    schema = 'company_agent_test_'+uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='capacity-backlog-') as directory, patch.dict(
        os.environ, {
            'COMPANY_AGENT_DATABASE_URL': dsn, 'COMPANY_AGENT_DB_SCHEMA': schema,
        },
    ):
        try:
            root = Path(directory)
            make_state_directory(root/'state')
            queue = WorkQueue(root/'state', root/'uploads', max_pending=total, max_per_user=per_user)
            timings = []
            keys = []
            begin = time.perf_counter()
            for number in range(total):
                before = time.perf_counter()
                owner = 'owner-'+str(number//per_user)
                key = queue.submit(owner, 'sub-'+owner, 'chat', str(number), f'synthetic {number}', [])
                timings.append((time.perf_counter()-before)*1000)
                keys.append((owner, key))
            try:
                queue.submit('overflow', 'overflow', 'chat', 'overflow', 'synthetic', [])
            except ValueError as failure:
                assert '工作佇列已滿' in str(failure)
            else:
                raise AssertionError('global cap bypass')
            assert queue.submit('owner-0', 'sub-owner-0', 'chat', '0', 'synthetic 0', []) == keys[0][1]
            queue.request_cancel(*keys[-1])
            # Global slot is now available; first owner's cap must still hold.
            try:
                queue.submit('owner-0', 'sub-owner-0', 'chat', 'extra', 'synthetic', [])
            except ValueError as failure:
                assert '本人的待處理工作已達上限' in str(failure)
            else:
                raise AssertionError('owner cap bypass')
            queue.submit('overflow', 'overflow', 'chat', 'overflow', 'synthetic', [])
            with queue.store.db() as connection:
                assert connection.execute("SELECT count(*) FROM web_jobs WHERE status='queued'").fetchone()[0] == total
            return {
                'max_pending': total, 'max_pending_per_user': per_user, 'accepted': total,
                'submit_p95_ms': percentile(timings, .95), 'submit_max_ms': round(max(timings), 3),
                'seconds': round(time.perf_counter()-begin, 3), 'overflow_idempotency_cancellation': 'passed',
            }
        finally:
            with psycopg.connect(dsn, autocommit=True) as connection:
                connection.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))


@evaluation
async def main(args):
    results = {'warning': 'Not a live cloud-model or live EIP throughput measurement.', 'profiles': [], 'backlogs': []}
    for limit, model, ocr in [(4, 8, 2), (8, 16, 2), (12, 24, 3), (16, 32, 4)]:
        result = await profile(limit, model, ocr, args.users, args.hold)
        results['profiles'].append(result)
        Path(args.output).write_text(json.dumps(results, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False), flush=True)
    for total, per_user in [(500, 20), (1000, 40), (2000, 50)]:
        result = await asyncio.to_thread(backlog, total, per_user)
        results['backlogs'].append(result)
        Path(args.output).write_text(json.dumps(results, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--users', type=int, default=100)
    parser.add_argument('--hold', type=float, default=.8)
    parser.add_argument('--output', required=True)
    options = parser.parse_args()
    if not 16 <= options.users <= 200 or not .1 <= options.hold <= 10:
        parser.error('users must be 16–200; hold must be .1–10 seconds')
    asyncio.run(main(options))
