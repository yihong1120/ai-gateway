"""Concurrent fixed-window limits against a private disposable Redis process."""

from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from fastapi import HTTPException, Request
from redis.asyncio import Redis
from examples.identity.settings.native import NativeSettings
from examples.identity.providers.native_transactions import NativeTransactions


class AtomicRateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        binary = shutil.which('redis-server')
        if not binary:
            self.skipTest('redis-server is needed for real atomic rate-limit tests')
        temporary = tempfile.TemporaryDirectory(prefix='gateway-redis-')
        self.addCleanup(temporary.cleanup)
        socket = str(Path(temporary.name) / 'redis.sock')
        self.server = subprocess.Popen(
            [
                binary,
                '--port',
                '0',
                '--unixsocket',
                socket,
                '--unixsocketperm',
                '700',
                '--save',
                '',
                '--appendonly',
                'no',
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self.stop_server)
        self.redis = Redis(unix_socket_path=socket)
        self.addAsyncCleanup(self.redis.aclose)
        for _ in range(200):
            try:
                await self.redis.ping()
                break
            except Exception:
                await asyncio.sleep(0.01)
        else:
            self.fail('disposable Redis did not start')
        self.service = NativeTransactions(NativeSettings())
        self.request = Request(
            {'type': 'http', 'client': ('192.0.2.1', 1), 'headers': []}
        )
        self.key = self.service._rate_limit_key(self.request)

    def stop_server(self):
        self.server.terminate()
        self.server.wait(timeout=5)

    async def test_concurrent_requests_keep_the_twenty_request_window(self):
        async def request():
            try:
                await self.service.enforce_begin_rate_limit(self.request, self.redis)
                return True
            except HTTPException as error:
                self.assertEqual(error.status_code, 429)
                self.assertGreater(int(error.headers['Retry-After']), 0)
                return False

        accepted = await asyncio.gather(*(request() for _ in range(50)))
        self.assertEqual(sum(accepted), 20)
        self.assertEqual(await self.redis.get(self.key), b'50')
        self.assertTrue(0 < await self.redis.ttl(self.key) <= 300)

    async def test_poisoned_nonexpiring_counter_is_repaired_without_resetting_count(
        self,
    ):
        await self.redis.set(self.key, 20)
        self.assertEqual(await self.redis.ttl(self.key), -1)
        with self.assertRaises(HTTPException):
            await self.service.enforce_begin_rate_limit(self.request, self.redis)
        self.assertEqual(await self.redis.get(self.key), b'21')
        self.assertGreater(await self.redis.ttl(self.key), 0)
