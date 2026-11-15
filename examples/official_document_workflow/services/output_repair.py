"""Just-in-time leased verification; only transient failures remain automatic."""

from __future__ import annotations

import asyncio
import logging

from examples.shared.runtime.io import run_sync
from examples.official_document_workflow.application.output_receipts import (
    WRITE_PENDING,
    DELETE_PENDING,
)
from examples.official_document_workflow.application.output_verification import FileVerification


class OutputRepair:
    concurrency = 4
    batch_size = 32
    timeout_seconds = 45
    heartbeat_seconds = 15

    def __init__(self, receipts, files, publish=None):
        self.receipts, self.files, self.publish = receipts, files, publish

    async def run_once(self):
        budget = iter(range(self.batch_size))

        async def worker():
            count = 0
            for _ in budget:
                claims = await run_sync(self.receipts.claim_reviews, 1)
                if not claims:
                    break
                await self._leased(claims[0])
                count += 1
            return count

        results = await asyncio.gather(
            *(worker() for _ in range(self.concurrency)), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return sum(results)

    async def _leased(self, claim):
        # Renewal also covers draining a blocking storage read on cancellation.
        # Never abandon a thread and start unlimited replacement reads.
        current = [claim]
        task = asyncio.create_task(self._review(current))
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=self.heartbeat_seconds)
                if done:
                    break
                if not await run_sync(self.receipts.renew_review, current[0]):
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    return
            await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _verification(self, record):
        try:
            async with asyncio.timeout(self.timeout_seconds):
                if record.state in WRITE_PENDING:
                    return await self.files.inspect(record)
                return await self.files.inspect_deletion(record)
        except TimeoutError:
            return FileVerification('unavailable', 'verification_timeout')
        except Exception:
            return FileVerification('unavailable', 'storage_read_failed')

    async def _review(self, current):
        claim = current[0]
        record = claim.record
        if (
            not record.review_required
            and record.state in WRITE_PENDING | DELETE_PENDING
        ):
            result = await self._verification(record)
            state = (
                ('stored' if record.state in WRITE_PENDING else 'deleted')
                if result.outcome in {'verified','deleted'}
                else ('unknown' if record.state in WRITE_PENDING else 'delete_unknown')
            )
            claim = await run_sync(
                self.receipts.resolve_review, claim, state, verification=result
            )
            if claim is None:
                return
            current[0] = claim
        published = not claim.record.projection_pending
        if claim.record.projection_pending:
            try:
                if claim.record.job and self.publish is None:
                    published = False
                elif claim.record.job:
                    async with asyncio.timeout(self.timeout_seconds):
                        published = (
                            await self.publish(claim.record.owner, claim.record.job)
                            is not False
                        )
                else:
                    published = True
            except Exception as error:
                logging.getLogger(__name__).warning(
                    'Output notification deferred (%s)', type(error).__name__
                )
        await run_sync(self.receipts.finish_review, claim, published=published)
