"""Real PostgreSQL rollback, concurrent one-use actions, and mail ambiguity."""
from __future__ import annotations

import asyncio
import os
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch
from urllib.parse import parse_qs
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
from fastapi import HTTPException
from psycopg import sql
from sqlalchemy import text
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from examples.identity.accounts.mail import MailResult
from examples.identity.accounts.passwords import verify_password
from examples.identity.accounts.recovery import now
from examples.identity.accounts.recovery import token_hash
from examples.identity.config import Settings
from examples.identity.migrations import require_schema
from examples.identity.migrations import upgrade
from examples.identity.models import AccountActionToken
from examples.identity.models import Base
from examples.identity.models import Tenant
from examples.identity.models import User
from examples.identity.models import UserProfile
from examples.identity.runtime import identity_scope
from examples.identity.runtime import IdentityResources
from tests.redis_fixture import RedisFixture


class AccountRecoveryTests(RedisFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        dsn = os.getenv('EIP_TEST_DATABASE_URL')
        if not dsn:
            self.skipTest(
                'EIP_TEST_DATABASE_URL is required for PostgreSQL account tests',
            )
        self.redis = await self.start_redis()
        schema = 'identity_test_' + uuid4().hex
        with psycopg.connect(dsn, autocommit=True) as db:
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))

        def drop():
            with psycopg.connect(dsn, autocommit=True) as db:
                db.execute(
                    sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)),
                )

        self.addCleanup(drop)
        self.engine = create_async_engine(
            dsn.replace('postgresql://', 'postgresql+asyncpg://', 1),
            connect_args={'server_settings': {'search_path': schema}},
            pool_size=8,
            max_overflow=0,
        )
        self.addAsyncCleanup(self.engine.dispose)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as db:
            await db.run_sync(Base.metadata.create_all)
        await upgrade(self.engine)
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    'PYTHON_DOTENV_DISABLED': '1',
                    'JWT_SECRET_KEY': 'synthetic-recovery-key-at-least-32-bytes',
                },
                clear=True,
            ),
        )
        self.resources = IdentityResources(
            Settings(
                authjwt_secret_key='synthetic-recovery-key-at-least-32-bytes',
                brevo_api_key='test-mail-secret',
                mail_from='test@example.test',
                app_public_url='https://app.test',
            ),
        )
        self.addAsyncCleanup(self.resources.http_clients.close)
        self.enterContext(identity_scope(self.resources))
        self.recovery = self.resources.recovery
        self.recovery.mailer.send = AsyncMock(return_value=MailResult('sent'))
        async with self.sessions() as db:
            tenant = Tenant(name='test tenant')
            db.add(tenant)
            await db.flush()
            user = User(
                username='alice',
                password_hash='old-hash',
                role='user',
                status='email_unverified',
                tenant_id=tenant.id,
            )
            db.add(user)
            await db.flush()
            db.add(
                UserProfile(
                    user_id=user.id,
                    family_name='Test',
                    given_name='Alice',
                    email='alice@example.test',
                ),
            )
            await db.commit()
            self.user_id = user.id

    async def issue(self, purpose='reset_password'):
        async with self.sessions() as db:
            user = await self.recovery.user_by_email(db, 'alice@example.test')
            return await self.recovery.issue(db, user, purpose, 'alice@example.test')

    async def record(self, raw):
        async with self.sessions() as db:
            return await db.get(AccountActionToken, token_hash(raw))

    async def test_password_commit_failure_rolls_back_account_and_token_and_allows_retry(
        self,
    ):
        raw = await self.issue()
        async with self.sessions() as db:
            with patch.object(
                db,
                'commit',
                AsyncMock(side_effect=OSError('private connection information')),
            ):
                with self.assertRaises(HTTPException) as caught:
                    await self.recovery.reset(raw, 'new-password', db)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertNotIn('private connection', str(caught.exception.detail))
        self.assertIsNone((await self.record(raw)).consumed_at)
        async with self.sessions() as db:
            self.assertEqual(
                (await db.get(User, self.user_id)).password_hash, 'old-hash',
            )
            await self.recovery.reset(raw, 'new-password', db)
        async with self.sessions() as db:
            self.assertTrue(
                await verify_password(
                    'new-password', (await db.get(User, self.user_id)).password_hash,
                ),
            )
        self.assertTrue((await self.record(raw)).revocation_pending)

    async def test_email_commit_failure_keeps_link_usable(self):
        raw = await self.issue('verify_email')
        async with self.sessions() as db:
            with patch.object(
                db, 'commit', AsyncMock(side_effect=OSError('database down')),
            ):
                with self.assertRaises(HTTPException):
                    await self.recovery.verify(raw, db)
        self.assertIsNone((await self.record(raw)).consumed_at)
        async with self.sessions() as db:
            result = await self.recovery.verify(raw, db)
        self.assertEqual(result['status'], 'pending_admin_approval')

    async def test_concurrent_consumers_can_update_the_account_only_once(self):
        raw = await self.issue()

        async def reset(password):
            async with self.sessions() as db:
                try:
                    await self.recovery.reset(raw, password, db)
                    return password
                except HTTPException as error:
                    return error.status_code

        results = await asyncio.gather(
            reset('first-password'), reset('second-password'),
        )
        accepted = [result for result in results if isinstance(result, str)]
        self.assertEqual(len(accepted), 1)
        self.assertIn(400, results)
        async with self.sessions() as db:
            self.assertTrue(
                await verify_password(
                    accepted[0], (await db.get(User, self.user_id)).password_hash,
                ),
            )

    async def test_expired_superseded_wrong_purpose_and_changed_email_cannot_mutate(
        self,
    ):
        old = await self.issue()
        latest = await self.issue()
        with self.assertRaises(HTTPException):
            async with self.sessions() as db:
                await self.recovery.reset(old, 'new-password', db)
        with self.assertRaises(HTTPException):
            async with self.sessions() as db:
                await self.recovery.verify(latest, db)
        async with self.sessions() as db:
            await db.execute(
                update(AccountActionToken)
                .where(AccountActionToken.token_hash == token_hash(latest))
                .values(expires_at=now() - timedelta(seconds=1)),
            )
            await db.commit()
        with self.assertRaises(HTTPException):
            async with self.sessions() as db:
                await self.recovery.reset(latest, 'new-password', db)
        fresh = await self.issue()
        async with self.sessions() as db:
            await db.execute(
                update(UserProfile)
                .where(UserProfile.user_id == self.user_id)
                .values(email='changed@example.test'),
            )
            await db.commit()
        with self.assertRaises(HTTPException):
            async with self.sessions() as db:
                await self.recovery.reset(fresh, 'new-password', db)

    async def test_concurrent_issuers_leave_exactly_one_live_action(self):
        tokens = await asyncio.gather(*(self.issue() for _ in range(5)))
        rows = [await self.record(raw) for raw in tokens]
        self.assertEqual(sum(row.consumed_at is None for row in rows), 1)
        self.assertTrue(all(row.token_hash not in tokens for row in rows))

    async def test_unknown_mail_keeps_a_valid_link_and_no_automatic_resend(self):
        self.recovery.mailer.send.return_value = MailResult(
            'unknown', 'transport_unavailable',
        )
        async with self.sessions() as db:
            result = await self.recovery.request_reset('alice@example.test', db)
        self.assertIn('If the email exists', result['message'])
        payload = self.recovery.mailer.send.call_args.args[0]
        link = payload['textContent'].splitlines()[1]
        raw = parse_qs(urlsplit(link).query)['token'][0]
        record = await self.record(raw)
        self.assertEqual(record.delivery, 'unknown')
        self.assertIsNone(record.consumed_at)
        async with self.sessions() as db:
            await self.recovery.reset(raw, 'new-password', db)
        self.recovery.mailer.send.assert_awaited_once()

    async def test_known_rejection_disables_only_that_link(self):
        self.recovery.mailer.send.return_value = MailResult('rejected')
        async with self.sessions() as db:
            with self.assertRaises(HTTPException):
                await self.recovery.request_reset('alice@example.test', db)
        payload = self.recovery.mailer.send.call_args.args[0]
        raw = parse_qs(urlsplit(payload['textContent'].splitlines()[1]).query)['token'][
            0
        ]
        record = await self.record(raw)
        self.assertEqual(record.delivery, 'rejected')
        self.assertIsNotNone(record.consumed_at)

    async def test_mail_success_receipt_failure_does_not_discard_link_or_resend(self):
        raw = await self.issue()
        async with self.sessions() as db:
            with patch.object(
                db, 'commit', AsyncMock(side_effect=OSError('receipt unavailable')),
            ):
                result = await self.recovery.deliver(db, raw, {'template': 'test'})
        self.assertEqual(result.state, 'sent')
        self.assertIsNone((await self.record(raw)).consumed_at)
        self.assertEqual((await self.record(raw)).delivery, 'prepared')
        self.recovery.mailer.send.assert_awaited_once()

    async def test_post_commit_revocation_is_durable_and_idempotent_when_ack_fails(
        self,
    ):
        user = SimpleNamespace(
            id=self.user_id,
            username='alice',
            role='user',
            group_id=None,
            status='active',
            tenant_id=None,
        )
        pair = await self.resources.local_tokens.issue(self.redis, user, [])
        raw = await self.issue()
        async with self.sessions() as db:
            await self.recovery.reset(raw, 'new-password', db)
        async with self.sessions() as db:
            with patch.object(
                db, 'commit', AsyncMock(side_effect=OSError('acknowledgement lost')),
            ):
                with self.assertRaises(OSError):
                    await self.recovery.drain_revocations(db, self.redis)
        self.assertTrue((await self.record(raw)).revocation_pending)
        fresh = await self.resources.local_tokens.issue(self.redis, user, [])
        async with self.sessions() as db:
            self.assertEqual(await self.recovery.drain_revocations(db, self.redis), 1)
        self.assertFalse((await self.record(raw)).revocation_pending)
        await self.resources.local_tokens.verify(self.redis, fresh['refresh_token'])
        with self.assertRaises(HTTPException):
            await self.resources.local_tokens.verify(self.redis, pair['refresh_token'])

    async def test_migration_is_idempotent_and_required_for_startup(self):
        await require_schema(self.engine)
        await upgrade(self.engine)
        async with self.engine.begin() as db:
            self.assertEqual(
                await db.scalar(
                    text('SELECT count(*) FROM identity_schema_migrations'),
                ),
                1,
            )
            await db.execute(text('DELETE FROM identity_schema_migrations'))
        with self.assertRaises(RuntimeError):
            await require_schema(self.engine)

    async def test_cancellation_drains_mail_and_records_its_result_once(self):
        raw = await self.issue()
        started, release = asyncio.Event(), asyncio.Event()

        async def send(payload):
            started.set()
            await release.wait()
            return MailResult('sent')

        self.recovery.mailer.send.side_effect = send
        async with self.sessions() as db:
            task = asyncio.create_task(
                self.recovery.deliver(db, raw, {'template': 'test'}),
            )
            await started.wait()
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual((await self.record(raw)).delivery, 'sent')
        self.recovery.mailer.send.assert_awaited_once()

    async def test_migration_widens_the_existing_approval_status_column(self):
        async with self.engine.begin() as db:
            await db.execute(text('DELETE FROM identity_schema_migrations'))
            await db.execute(
                text('ALTER TABLE users ALTER COLUMN status TYPE VARCHAR(20)'),
            )
        await upgrade(self.engine)
        raw = await self.issue('verify_email')
        async with self.sessions() as db:
            result = await self.recovery.verify(raw, db)
        self.assertEqual(result['status'], 'pending_admin_approval')
