"""Registry API/signature compatibility and real one-use redemption transactions."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import unittest
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch
from uuid import uuid4

import psycopg
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.hazmat.primitives.serialization import NoEncryption
from cryptography.hazmat.primitives.serialization import PrivateFormat
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import sql
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from examples.deployment.registry import enrollments
from examples.deployment.registry import service
from examples.deployment.registry.router import router
from examples.identity.database import Base
from examples.identity.database import get_db
from examples.identity.models import Deployment
from examples.identity.models import DeploymentEnrollmentCode
from examples.identity.models import Tenant
from examples.identity.models import utc_now
from examples.identity.redis_pool import get_redis_pool
from tests.redis_fixture import RedisFixture


class RegistryContracts(unittest.TestCase):
    def test_signature_preserves_canonical_payload_and_pinned_key_id(self):
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption(),
        ).decode()
        deployment = SimpleNamespace(
            id=uuid4(),
            tenant_id=uuid4(),
            api_base_url='https://api.test/hazard/api',
            config_revision=2,
        )
        document = service.build_registry_document(
            deployment, pem, 'pinned-key', 60, issued_at=10,
        )
        self.assertEqual(document['expires_at'], 70)
        self.assertEqual(document['key_id'], 'pinned-key')
        self.assertEqual(len(document), 9)
        signature = base64.urlsafe_b64decode(document['signature'] + '==')
        payload = {
            k: v for k, v in document.items() if k not in {'signature', 'key_id'}
        }
        key.public_key().verify(
            signature,
            json.dumps(
                payload, ensure_ascii=False, separators=(',', ':'), sort_keys=True,
            ).encode(),
        )
        invalid = generate_private_key(public_exponent=65537, key_size=2048)
        pem = invalid.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption(),
        ).decode()
        with self.assertRaisesRegex(ValueError, 'Ed25519'):
            service.build_registry_document(
                deployment, pem, 'pinned-key', 60, issued_at=10,
            )

    def test_exchange_http_codes_and_minimal_response_are_unchanged(self):
        app = FastAPI()
        app.include_router(router)
        db, redis = SimpleNamespace(), SimpleNamespace()
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_redis_pool] = lambda: redis
        settings = SimpleNamespace(
            deployment_enrollment_code_pepper='p' * 32,
            deployment_enrollment_rate_limit_max=5,
            deployment_enrollment_rate_limit_window_seconds=300,
        )
        key = uuid4()
        with (
            TestClient(app) as client,
            patch.object(service, 'get_settings', return_value=settings),
            patch.object(
                service,
                'enforce_enrollment_exchange_rate_limit',
                AsyncMock(return_value=None),
            ) as limit,
            patch.object(service, 'redeem_enrollment_code', AsyncMock()) as redeem,
        ):
            for status, expected in [
                ('redeemed', 200),
                ('invalid', 403),
                ('terminal', 410),
            ]:
                redeem.return_value = enrollments.EnrollmentExchangeResult(
                    status, key if status == 'redeemed' else None,
                )
                response = client.post(
                    '/deployment-registry/v1/enrollments/exchange',
                    json={'enrollment_code': 'a' * 32},
                )
                self.assertEqual(response.status_code, expected)
                if expected == 200:
                    self.assertEqual(response.json(), {'deployment_id': str(key)})
            redeem.reset_mock()
            limit.return_value = 17
            response = client.post(
                '/deployment-registry/v1/enrollments/exchange',
                json={'enrollment_code': 'a' * 32},
            )
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers['Retry-After'], '17')
            redeem.assert_not_awaited()
            limit.reset_mock()
            response = client.post(
                '/deployment-registry/v1/enrollments/exchange',
                json={'enrollment_code': 'short'},
            )
            self.assertEqual(response.status_code, 422)
            limit.assert_not_awaited()


class EnrollmentCounters(RedisFixture, unittest.IsolatedAsyncioTestCase):
    async def test_shared_atomic_counter_retains_window_and_repairs_missing_ttl(self):
        redis = await self.start_redis()
        self.assertIsNone(
            await enrollments.enforce_enrollment_exchange_rate_limit(
                redis, 'test-ip', 'verifier', 1, 60,
            ),
        )
        keys = await redis.keys('*')
        self.assertEqual(len(keys), 2)
        for key in keys:
            await redis.persist(key)
        retry = await enrollments.enforce_enrollment_exchange_rate_limit(
            redis, 'test-ip', 'verifier', 1, 60,
        )
        self.assertTrue(0 < retry <= 60)
        for key in keys:
            self.assertTrue(0 < await redis.ttl(key) <= 60)


class EnrollmentTransactions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        dsn = os.getenv('EIP_TEST_DATABASE_URL')
        if not dsn:
            self.skipTest(
                'EIP_TEST_DATABASE_URL is required for PostgreSQL registry tests',
            )
        schema = 'registry_test_' + uuid4().hex
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
        )
        self.addAsyncCleanup(self.engine.dispose)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as db:
            await db.run_sync(
                lambda connection: Base.metadata.create_all(
                    connection,
                    tables=[
                        Tenant.__table__,
                        Deployment.__table__,
                        DeploymentEnrollmentCode.__table__,
                    ],
                ),
            )
        async with self.sessions() as db:
            deployment = Deployment(
                tenant=Tenant(name='test', status='active'),
                api_base_url='https://registry.test/hazard/api',
                config_revision=1,
                status='active',
            )
            db.add(deployment)
            await db.commit()
            self.deployment_id = deployment.id
            result = await enrollments.provision_enrollment_code(
                db, deployment.id, utc_now() + timedelta(minutes=5), 'tester', 'p' * 32,
            )
            await db.commit()
            self.verifier = result.enrollment.code_verifier_hash

    async def test_concurrent_redemption_has_exactly_one_winner(self):
        async def redeem():
            async with self.sessions() as db:
                return await enrollments.redeem_enrollment_code(db, self.verifier)

        results = await asyncio.gather(redeem(), redeem())
        self.assertEqual(sorted(r.status for r in results), ['redeemed', 'terminal'])
        self.assertEqual(
            next(r.deployment_id for r in results if r.status == 'redeemed'),
            self.deployment_id,
        )

    async def test_transaction_failure_does_not_consume_code(self):
        async with self.sessions() as db:
            begin = db.begin

            @asynccontextmanager
            async def failed_commit():
                async with begin():
                    yield
                    raise RuntimeError('commit failed')

            with (
                patch.object(db, 'begin', failed_commit),
                self.assertRaisesRegex(RuntimeError, 'commit failed'),
            ):
                await enrollments.redeem_enrollment_code(db, self.verifier)
        async with self.sessions() as db:
            row = await db.scalar(select(DeploymentEnrollmentCode))
            self.assertIsNone(row.redeemed_at)
        async with self.sessions() as db:
            self.assertEqual(
                (await enrollments.redeem_enrollment_code(db, self.verifier)).status,
                'redeemed',
            )
