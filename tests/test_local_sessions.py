"""Real Redis concurrency, single-use rotation, and independent account snapshots."""
from __future__ import annotations

import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

import examples.identity.sessions.authentication as _authentication_imports
from examples.identity.cache import rate_limiter_service
from examples.identity.config import Settings
from examples.identity.redis_counters import increment_window
from examples.identity.runtime import identity_scope
from examples.identity.runtime import IdentityResources
from examples.identity.schemas.auth import RefreshRequest
from examples.identity.sessions.revocation import is_access_token_revoked
from tests.redis_fixture import RedisFixture


class LocalSessionTests(RedisFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = await self.start_redis()
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    'PYTHON_DOTENV_DISABLED': '1',
                    'JWT_SECRET_KEY': 'synthetic-session-key-at-least-32-bytes',
                },
                clear=True,
            ),
        )
        self.resources = IdentityResources(
            Settings(authjwt_secret_key='synthetic-session-key-at-least-32-bytes'),
        )
        self.addAsyncCleanup(self.resources.http_clients.close)
        self.tokens = self.resources.local_tokens
        self.user = SimpleNamespace(
            id=1,
            username='alice',
            role='user',
            group_id=None,
            status='active',
            tenant_id=None,
        )

    def jti(self, pair):
        return self.resources.jwt_access.decode_token_for_lifecycle(
            pair['access_token'],
        )['jti']

    async def pair(self):
        return await self.tokens.issue(self.redis, self.user, ['read'])

    async def test_many_concurrent_logins_preserve_every_independent_session(self):
        pairs = await asyncio.gather(*(self.pair() for _ in range(20)))
        snapshot = await rate_limiter_service.get_user_data(self.redis, 'alice')
        self.assertEqual(set(snapshot['jti_list']), {self.jti(pair) for pair in pairs})
        for pair in pairs:
            await self.tokens.verify(self.redis, pair['refresh_token'])
            self.assertFalse(
                await is_access_token_revoked(self.redis, {'jti': self.jti(pair)}),
            )
        raw = await self.redis.get(rate_limiter_service._user_key('alice'))
        self.assertNotIn('jti_list', json.loads(raw))
        for pair in pairs:
            self.assertNotIn(pair['refresh_token'].encode(), raw)

    async def test_distinct_refresh_families_rotate_concurrently_without_lost_updates(
        self,
    ):
        pairs = await asyncio.gather(*(self.pair() for _ in range(10)))
        rotated = await asyncio.gather(
            *(self.tokens.rotate(self.redis, pair['refresh_token']) for pair in pairs),
        )
        for pair in rotated:
            await self.tokens.verify(self.redis, pair['refresh_token'])
        meta = await self.resources.local_sessions.snapshot(self.redis, 'alice')
        self.assertEqual(len(meta), 20)

    async def test_same_refresh_is_single_use_and_replay_revokes_its_family_and_access(
        self,
    ):
        pair = await self.pair()
        results = await asyncio.gather(
            *(self.tokens.rotate(self.redis, pair['refresh_token']) for _ in range(2)),
            return_exceptions=True,
        )
        successful = [item for item in results if isinstance(item, dict)]
        self.assertLessEqual(len(successful), 1)
        self.assertTrue(any(isinstance(item, HTTPException) for item in results))
        self.assertTrue(
            await is_access_token_revoked(self.redis, {'jti': self.jti(pair)}),
        )
        for item in successful:
            with self.assertRaises(HTTPException):
                await self.tokens.verify(self.redis, item['refresh_token'])
            self.assertTrue(
                await is_access_token_revoked(self.redis, {'jti': self.jti(item)}),
            )

    async def test_logout_does_not_clobber_another_login(self):
        first, second = await asyncio.gather(self.pair(), self.pair())
        await asyncio.gather(
            self.tokens.logout(
                self.redis, first['refresh_token'], 'Bearer ' + first['access_token'],
            ),
            self.pair(),
        )
        await self.tokens.verify(self.redis, second['refresh_token'])
        self.assertFalse(
            await is_access_token_revoked(self.redis, {'jti': self.jti(second)}),
        )
        self.assertTrue(
            await is_access_token_revoked(self.redis, {'jti': self.jti(first)}),
        )

    async def test_all_session_revocation_and_stale_issuer_cannot_resurrect_tokens(
        self,
    ):
        pair = await self.pair()
        epochs = await self.resources.local_sessions.epochs(self.redis, 'alice')
        await self.resources.local_sessions.revoke_all(self.redis, 'alice')
        with self.assertRaises(HTTPException):
            await self.tokens.issue(self.redis, self.user, ['read'], epochs=epochs)
        with self.assertRaises(HTTPException):
            await self.tokens.verify(self.redis, pair['refresh_token'])
        self.assertTrue(
            await is_access_token_revoked(self.redis, {'jti': self.jti(pair)}),
        )
        fresh = await self.pair()
        await self.tokens.verify(self.redis, fresh['refresh_token'])

    async def test_idempotent_account_revocation_does_not_kill_a_later_login(self):
        old = await self.pair()
        await self.resources.local_sessions.revoke_once(
            self.redis, 'alice', 'action-hash',
        )
        fresh = await self.pair()
        await self.resources.local_sessions.revoke_once(
            self.redis, 'alice', 'action-hash',
        )
        self.assertTrue(
            await is_access_token_revoked(self.redis, {'jti': self.jti(old)}),
        )
        self.assertFalse(
            await is_access_token_revoked(self.redis, {'jti': self.jti(fresh)}),
        )
        await self.tokens.verify(self.redis, fresh['refresh_token'])

    async def test_legacy_profile_is_sanitized_and_never_imports_old_bearers(self):
        key = rate_limiter_service._user_key('alice')
        await self.redis.set(
            key,
            json.dumps(
                {
                    'db_user': {'username': 'alice'},
                    'feature_names': [],
                    'jti_list': ['old'],
                    'jti_meta': {'old': 9999999999},
                    'refresh_tokens': ['old-secret'],
                },
            ),
        )
        snapshot = await rate_limiter_service.get_user_data(self.redis, 'alice')
        self.assertEqual(snapshot['jti_list'], [])
        self.assertNotIn(b'old-secret', await self.redis.get(key))
        self.assertGreater(await self.redis.ttl(key), 0)

    async def test_existing_native_refresh_adapter_keeps_its_response_contract(self):

        pair = await self.pair()
        with identity_scope(self.resources):
            result = await _authentication_imports.refresh_tokens(
                RefreshRequest(refresh_token=pair['refresh_token']), self.redis,
            )
        self.assertEqual(
            set(result), {'access_token', 'refresh_token', 'feature_names'},
        )
        self.assertEqual(result['feature_names'], ['read'])

    async def test_shared_counter_repairs_ttl_and_preserves_the_fixed_window(self):
        await self.redis.set('poison', 7)
        count, ttl = await increment_window(self.redis, 'poison', 30)
        self.assertEqual(count, 8)
        self.assertGreater(ttl, 0)
        await self.redis.expire('poison', 12)
        results = await asyncio.gather(
            *(increment_window(self.redis, 'poison', 30) for _ in range(30)),
        )
        self.assertEqual(sorted(count for count, _ in results), list(range(9, 39)))
        self.assertLessEqual(await self.redis.ttl('poison'), 12)

    async def test_successful_refresh_extends_profile_lifetime_with_the_new_session(
        self,
    ):
        pair = await self.pair()
        key = rate_limiter_service._user_key('alice')
        await self.redis.expire(key, 5)
        await self.tokens.rotate(self.redis, pair['refresh_token'])
        self.assertGreater(await self.redis.ttl(key), 2591900)
