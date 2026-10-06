"""Shared resource admission must preserve owner fairness and cancellation."""
from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import patch

from official_document_workflow.infrastructure import concurrency as limits


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_owner_does_not_block_other_owner_and_fifo_is_preserved(self):
        gate = limits.OwnerSlots(2)
        release = asyncio.Event()
        acquired = []

        async def run(owner, number):
            async with gate.slot(owner):
                acquired.append((owner, number))
                if number == 0:
                    await release.wait()
        a = asyncio.create_task(run('alice', 0))
        await asyncio.sleep(0)
        queued = [asyncio.create_task(run('alice', n)) for n in [1, 2]]
        b = asyncio.create_task(run('bob', 3))
        await asyncio.sleep(0)
        self.assertEqual(acquired, [('alice', 0), ('bob', 3)])
        release.set()
        await asyncio.gather(a, b, *queued)
        self.assertEqual(acquired[-2:], [('alice', 1), ('alice', 2)])
        self.assertFalse(gate.active)
        self.assertFalse(gate.waiting)

    async def test_cancelled_waiter_removed_by_dispatch_before_finally_does_not_leak(self):
        gate = limits.OwnerSlots(1)
        async with gate.slot('alice'):
            async def waiting():
                async with gate.slot('bob'):
                    self.fail('cancelled request admitted')
            task = asyncio.create_task(waiting())
            await asyncio.sleep(0)
            task.cancel()
        # Slot release dispatches before the cancelled coroutine's finally.
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(gate.active)
        self.assertFalse(gate.waiting)
        async with gate.slot('carol'):
            self.assertEqual(gate.active, {'carol'})

    async def test_cancel_after_grant_before_yield_releases_owner(self):
        gate = limits.OwnerSlots(1)

        async def waiting():
            async with gate.slot('bob'):
                self.fail('cancelled request executed')
        async with gate.slot('alice'):
            task = asyncio.create_task(waiting())
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(gate.active)
        self.assertFalse(gate.waiting)

    async def test_model_gate_limits_requests_and_releases_failed_and_cancelled_calls(self):
        active = peak = 0

        @limits.model_request
        async def invoke(number):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(.01)
                if number == 3:
                    raise RuntimeError('native failure')
                return number
            finally:
                active -= 1
        with patch.object(limits, 'MODELS', asyncio.Semaphore(2)):
            results = await asyncio.gather(*(invoke(n) for n in range(20)), return_exceptions=True)
            self.assertEqual(peak, 2)
            self.assertIsInstance(results[3], RuntimeError)
            self.assertEqual(active, 0)
            self.assertEqual(await invoke(21), 21)

    async def test_cancelled_ocr_keeps_slot_until_native_request_finishes(self):
        started, release = threading.Event(), threading.Event()
        second_started = threading.Event()

        def infer():
            started.set()
            release.wait(5)

        def second():
            second_started.set()
            return 'second'
        with patch.object(limits, 'OCR', asyncio.Semaphore(1)):
            first = asyncio.create_task(limits.run_ocr(infer))
            while not started.is_set():
                await asyncio.sleep(.001)
            first.cancel()
            waiting = asyncio.create_task(limits.run_ocr(second))
            try:
                await asyncio.sleep(.03)
                self.assertFalse(first.done())
                self.assertFalse(second_started.is_set())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                self.assertEqual(await waiting, 'second')
            finally:
                release.set()
                await asyncio.gather(first, waiting, return_exceptions=True)

    async def test_waiting_ocr_does_not_launch_native_request(self):
        def unexpected():
            self.fail('cancelled waiting OCR called backend')
        gate = asyncio.Semaphore(1)
        with patch.object(limits, 'OCR', gate):
            await gate.acquire()
            task = asyncio.create_task(limits.run_ocr(unexpected))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            gate.release()
            self.assertEqual(await limits.run_ocr(lambda: 1), 1)

    async def test_document_gate_covers_all_upload_types_and_skips_auxiliary_tasks(self):
        gate = limits.OwnerSlots(2)
        release = asyncio.Event()
        entered = []

        class Pipe:
            async def _emit_processing_status(self, *args):
                pass

            @limits.document_request
            async def pipe(self, body, __user__=None, __task__=None):
                if __task__:
                    return 'title'
                entered.append((__user__['id'], body['type']))
                await release.wait()
                return body['type']
        pipe = Pipe()
        with patch.object(limits, 'DOCUMENTS', gate):
            tasks = [
                asyncio.create_task(pipe.pipe({'type': kind}, {'id': owner}))
                for owner, kind in [('alice', 'pdf'), ('alice', 'image'), ('bob', 'docx'), ('carol', 'pdf')]
            ]
            try:
                await asyncio.sleep(0)
                self.assertEqual(entered, [('alice', 'pdf'), ('bob', 'docx')])
                self.assertEqual(await pipe.pipe({}, {'id': 'alice'}, 'title_generation'), 'title')
                release.set()
                self.assertEqual(await asyncio.gather(*tasks), ['pdf', 'image', 'docx', 'pdf'])
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
        self.assertFalse(gate.waiting)
        self.assertFalse(gate.active)

    async def test_waiting_capacity_rejects_overflow_and_cancel_returns_capacity(self):
        gate = limits.OwnerSlots(1, max_waiting=2, max_per_owner=1)

        async def waiting(owner):
            async with gate.slot(owner):
                pass
        async with gate.slot('busy'):
            a = asyncio.create_task(waiting('alice'))
            await asyncio.sleep(0)
            with self.assertRaisesRegex(ValueError, '本人的等待'):
                await waiting('alice')
            b = asyncio.create_task(waiting('bob'))
            await asyncio.sleep(0)
            with self.assertRaisesRegex(ValueError, '佇列已滿'):
                await waiting('carol')
            a.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await a
        await b
        self.assertFalse(gate.waiting)
        self.assertFalse(gate.active)

    async def test_company_and_document_models_share_capacity_keep_verified_billing(self):
        import httpx
        import json
        from company_agent.runtime.models import gateway_for_verified_user, close_model
        from company_agent.tests.test_user_billing import verified
        started = asyncio.Event()
        release = asyncio.Event()
        requests = []

        async def handler(request):
            requests.append(json.loads(request.content))
            started.set()
            await release.wait()
            return httpx.Response(
                200, json={
                    'id': 'test', 'object': 'chat.completion', 'created': 1,
                    'model': 'model', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
                },
            )
        model = gateway_for_verified_user(
            verified(), 'https://gateway.example/v1',
            'key', 'model', httpx.MockTransport(handler),
        )
        document_started = asyncio.Event()

        @limits.model_request
        async def document():
            document_started.set()
            return 'document'
        with patch.object(limits, 'MODELS', asyncio.Semaphore(1)):
            company = asyncio.create_task(model.summarize('owned work'))
            await asyncio.wait_for(started.wait(), 5)
            other = asyncio.create_task(document())
            try:
                await asyncio.sleep(.01)
                self.assertFalse(document_started.is_set())
                release.set()
                self.assertEqual((await company).content, 'ok')
                self.assertEqual(await other, 'document')
                self.assertEqual(requests[0]['user'], 'oidc-alice')
                self.assertEqual(len(requests), 1)
            finally:
                release.set()
                await asyncio.gather(company, other, return_exceptions=True)
                await close_model(model)

    async def test_cached_pipe_settings_are_frozen_for_each_concurrent_owner(self):
        from pydantic import BaseModel
        release = asyncio.Event()
        started = asyncio.Event()

        class Valves(BaseModel):
            model: str = 'first'

        class Pipe:
            def __init__(self):
                self.valves = Valves()

            async def _emit_processing_status(self, *args):
                pass

            @limits.document_request
            async def pipe(self, body, __user__=None):
                started.set()
                await release.wait()
                return self.valves.model
        pipe = Pipe()
        with patch.object(limits, 'DOCUMENTS', limits.OwnerSlots(2)):
            first = asyncio.create_task(pipe.pipe({}, {'id': 'alice'}))
            await started.wait()
            pipe.valves = Valves(model='second')
            second = asyncio.create_task(pipe.pipe({}, {'id': 'bob'}))
            try:
                await asyncio.sleep(0)
                release.set()
                self.assertEqual(await asyncio.gather(first, second), ['first', 'second'])
                self.assertEqual(pipe.valves.model, 'second')
            finally:
                release.set()
                await asyncio.gather(first, second, return_exceptions=True)
