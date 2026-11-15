"""Security and lifecycle regressions without production services or credentials."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from fastapi import FastAPI, Request, HTTPException
import httpx

from examples.company_ai_access.schemas import Principal
from examples.company_ai_access.routers.control import create_router
from examples.company_ai_access.routers.inference import inference_router
from examples.company_ai_access.services.inference import InferenceProxy
from examples.company_ai_access.storage.repository import Repository
from examples.company_ai_access.config import Settings
from examples.company_ai_access.services.devices import Service, challenge
from examples.company_ai_access.storage.registry import Store, digest
from examples.company_ai_access.adapters.upstream import Directory, LiteLLM
from tests.ai_access_postgres import access_database


class Clock:
    value = 1800000000

    def __call__(self):
        return self.value


class FakeDirectory:
    def __init__(self):
        self.disabled = set()
        self.down = False

    async def allowed(self, subject, fresh=False):
        if self.down:
            raise HTTPException(503)
        return subject not in self.disabled

    async def is_admin(self, subject):
        return await self.allowed(subject, fresh=True) and subject == 'admin'


class FakeLiteLLM:
    def __init__(self):
        self.created, self.renewed, self.blocked = [], [], []
        self.down = False

    async def ensure_key(self, device, raw):
        if self.down:
            raise HTTPException(503)
        self.created.append(raw)

    async def renew(self, raw, seconds):
        if self.down:
            raise HTTPException(503)
        self.renewed.append((raw, seconds))

    async def block(self, raw):
        if self.down:
            raise HTTPException(503)
        self.blocked.append(raw)


class Fixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        access_database(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = Clock()
        self.settings = Settings(
            origin='https://ai-dev.changdar-server.mooo.com',
            state_dir=Path(self.temp.name),
            litellm_url='http://127.0.0.1:14002',
            master_key='test-master',
            models=('claude-test',),
            inference_cidrs=('127.0.0.0/8',),
            environment='test',
        )
        registry = Store(self.settings.state_dir)
        self.store = await Repository(registry).__aenter__()
        self.addAsyncCleanup(self.store.__aexit__, None, None, None)
        self.llm, self.directory = FakeLiteLLM(), FakeDirectory()
        self.service = Service(self.settings, self.store, self.llm, self.directory, clock=self.clock)
        self.verifier = 'a' * 43

    async def approve(self, subject='alice', replace_id=None):
        row = await self.service.start('WIN-TEST', challenge(self.verifier), replace_id, 'test-' + subject)
        await self.service.approve(subject, row['request_id'], row['user_code'])
        return row

    async def enroll(self, subject='alice', replace_id=None):
        row = await self.approve(subject, replace_id)
        delivered = await self.service.poll(row['request_id'], self.verifier)
        await self.service.ack(row['request_id'], self.verifier)
        return delivered

    async def assert_error(self, status, function, *args, **kwargs):
        with self.assertRaises(HTTPException) as caught:
            await function(*args, **kwargs)
        self.assertEqual(caught.exception.status_code, status)


class Lifecycle(Fixture):
    async def test_no_key_without_browser_approval(self):
        row = await self.service.start('WIN-TEST', challenge(self.verifier), None, 'test')
        result = await self.service.poll(row['request_id'], self.verifier)
        self.assertEqual(result, {'status': 'pending'})
        self.assertEqual(self.llm.created, [])

    async def test_pkce_and_approval_code_are_both_required(self):
        row = await self.service.start('WIN-TEST', challenge(self.verifier), None, 'test')
        await self.assert_error(400, self.service.approve, 'alice', row['request_id'], '0000-0000')
        await self.service.approve('alice', row['request_id'], row['user_code'])
        await self.assert_error(401, self.service.poll, row['request_id'], 'b' * 43)
        self.assertFalse(self.llm.created)

    async def test_repeat_delivery_recovers_lost_response_and_ack_consumes(self):
        row = await self.approve()
        first, second = await asyncio.gather(
            self.service.poll(row['request_id'], self.verifier),
            self.service.poll(row['request_id'], self.verifier),
        )
        self.assertEqual(first, second)
        self.assertEqual(len(self.llm.created), 1)
        await self.service.ack(row['request_id'], self.verifier)
        await self.service.ack(row['request_id'], self.verifier)
        await self.assert_error(410, self.service.poll, row['request_id'], self.verifier)

    async def test_last_second_approval_has_bounded_independent_activation_window(self):
        row = await self.service.start('WIN-LATE', challenge(self.verifier), None, 'test')
        self.clock.value = row['expires_at'] - 1
        await self.service.approve('alice', row['request_id'], row['user_code'])
        self.clock.value += 3  # Next native poll lands after browser-code expiry.
        await self.service.maintain()
        ready = await self.service.poll(row['request_id'], self.verifier)
        await self.assert_error(410, self.service.preview, 'alice', row['request_id'])
        await self.assert_error(410, self.service.approve, 'alice', row['request_id'], row['user_code'])
        self.clock.value += 30
        await self.service.maintain()
        await self.service.ack(row['request_id'], self.verifier)
        await self.service.ack(row['request_id'], self.verifier)
        self.assertEqual((await self.service.authorize(ready['key']))['status'], 'active')

    async def test_late_approval_cannot_extend_activation_indefinitely(self):
        row = await self.service.start('WIN-LATE', challenge(self.verifier), None, 'test')
        self.clock.value = row['expires_at'] - 1
        await self.service.approve('alice', row['request_id'], row['user_code'])
        self.clock.value += 3
        ready = await self.service.poll(row['request_id'], self.verifier)
        await self.assert_error(401, self.service.ack, row['request_id'], 'b' * 43)
        self.clock.value = row['expires_at'] - 1 + self.settings.delivery_seconds
        await self.assert_error(410, self.service.ack, row['request_id'], self.verifier)
        await self.service.maintain()
        await self.assert_error(401, self.service.authorize, ready['key'])

    async def test_expired_pending_request_still_cannot_be_approved_or_polled(self):
        row = await self.service.start('WIN-EXPIRED', challenge(self.verifier), None, 'test')
        self.clock.value = row['expires_at']
        await self.assert_error(410, self.service.approve, 'alice', row['request_id'], row['user_code'])
        await self.assert_error(410, self.service.poll, row['request_id'], self.verifier)
        self.assertFalse(self.llm.created)

    async def test_provisioning_failure_reuses_persisted_key(self):
        row = await self.approve()
        self.llm.down = True
        await self.assert_error(503, self.service.poll, row['request_id'], self.verifier)
        device = (await self.store.list('device'))[0]
        saved = self.store.open(device['key_box'])
        self.llm.down = False
        ready = await self.service.poll(row['request_id'], self.verifier)
        self.assertEqual(saved, ready['key'])

    async def test_secrets_encrypted_and_not_in_device_list_or_audit(self):
        ready = await self.enroll()
        listing = json.dumps((await self.service.devices('alice')))
        self.assertNotIn(ready['key'], listing)
        self.assertNotIn('key_box', listing)
        self.assertNotIn(ready['key'], json.dumps((await self.store.list('device'))))
        async with self.store.db() as db:
            self.assertNotIn(
                ready['key'],
                str(await (await db.execute('SELECT * FROM audit')).fetchall()),
            )

    async def test_owner_boundary(self):
        ready = await self.enroll()
        await self.assert_error(404, self.service.revoke, 'bob', ready['device_id'])
        self.assertEqual((await self.service.devices('bob')), [])
        row = await self.service.start('ATTACKER', challenge(self.verifier), ready['device_id'], 'bob')
        await self.assert_error(404, self.service.approve, 'bob', row['request_id'], row['user_code'])

    async def test_device_limit_and_atomic_replacement_after_ack(self):
        first = await self.enroll()
        await self.enroll()
        await self.enroll()
        row = await self.service.start('FOURTH', challenge(self.verifier), None, 'alice')
        await self.assert_error(409, self.service.approve, 'alice', row['request_id'], row['user_code'])
        replacement = await self.approve(replace_id=first['device_id'])
        new = await self.service.poll(replacement['request_id'], self.verifier)
        self.assertNotEqual(new['key'], first['key'])
        self.assertEqual((await self.service.authorize(first['key']))['subject'], 'alice')
        await self.service.ack(replacement['request_id'], self.verifier)
        await self.assert_error(401, self.service.authorize, first['key'])
        self.assertEqual((await self.service.authorize(new['key']))['subject'], 'alice')

    async def test_revoke_denies_even_when_upstream_unavailable(self):
        ready = await self.enroll()
        self.llm.down = True
        result = await self.service.revoke('alice', ready['device_id'])
        self.assertTrue(result['upstream_block_pending'])
        await self.assert_error(401, self.service.authorize, ready['key'])
        self.llm.down = False
        await self.service.maintain()
        self.assertIn(ready['key'], self.llm.blocked)
        self.assertFalse(self.llm.renewed)

    async def test_replacement_retry_retires_all_same_device_ancestors(self):
        original = await self.enroll()
        request = await self.approve(replace_id=original['device_id'])
        unacked = await self.service.poll(request['request_id'], self.verifier)
        latest = await self.enroll(replace_id=unacked['device_id'])
        await self.assert_error(401, self.service.authorize, original['key'])
        await self.assert_error(401, self.service.authorize, unacked['key'])
        self.assertEqual((await self.service.authorize(latest['key']))['subject'], 'alice')

    async def test_membership_removal_denies_all_devices(self):
        ready = await self.enroll()
        other = await self.enroll()
        self.directory.disabled.add('alice')
        await self.assert_error(403, self.service.authorize, ready['key'])
        await self.assert_error(401, self.service.authorize, other['key'])
        self.assertTrue(all(row['status'] == 'revoked' for row in (await self.store.list('device'))))

    async def test_directory_unavailable_fails_closed_and_does_not_renew(self):
        ready = await self.enroll()
        self.clock.value += 24 * 86400
        self.directory.down = True
        await self.assert_error(503, self.service.authorize, ready['key'])
        result = await self.service.maintain()
        self.assertEqual(result['unavailable'], 1)
        self.assertFalse(self.llm.renewed)

    async def test_renew_same_key_without_extending_confirmation_deadline(self):
        ready = await self.enroll()
        initial = await self.store.get('device', ready['device_id'])
        for _ in range(3):
            self.clock.value += 24 * 86400
            result = await self.service.maintain()
            self.assertEqual(result['renewed'], 1)
        device = await self.store.get('device', ready['device_id'])
        self.assertEqual(device['hard_expires_at'], initial['hard_expires_at'])
        self.assertLessEqual(device['expires_at'], device['hard_expires_at'])
        self.assertTrue(all(raw == ready['key'] for raw, _ in self.llm.renewed))
        self.clock.value = device['hard_expires_at']
        await self.assert_error(401, self.service.authorize, ready['key'])
        await self.service.maintain()
        self.assertEqual((await self.store.get('device', ready['device_id']))['status'], 'revoked')

    async def test_lease_expired_is_never_silently_resurrected(self):
        ready = await self.enroll()
        self.clock.value = ready['expires_at']
        await self.service.maintain()
        self.assertEqual(self.llm.renewed, [])
        await self.assert_error(401, self.service.authorize, ready['key'])

    async def test_revocation_wins_over_renewal_already_in_flight(self):
        ready = await self.enroll()
        self.clock.value += 24 * 86400
        entered, resume = asyncio.Event(), asyncio.Event()
        original = self.llm.renew

        async def delayed(raw, seconds):
            entered.set()
            await resume.wait()
            await original(raw, seconds)

        self.llm.renew = delayed
        pending = asyncio.create_task(self.service.maintain())
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(self.service.revoke('alice', ready['device_id']), 1)
        resume.set()
        result = await pending
        self.assertEqual(result['renewed'], 0)
        self.assertEqual((await self.store.get('device', ready['device_id']))['status'], 'revoked')
        await self.assert_error(401, self.service.authorize, ready['key'])

    async def test_unacknowledged_device_expires(self):
        row = await self.approve()
        ready = await self.service.poll(row['request_id'], self.verifier)
        self.clock.value += self.settings.delivery_seconds
        # Deadline is enforced even if the periodic maintenance task is down.
        await self.assert_error(401, self.service.authorize, ready['key'])
        await self.service.maintain()
        await self.assert_error(401, self.service.authorize, ready['key'])

    async def test_late_activation_ack_does_not_revive_expired_delivery(self):
        row = await self.approve()
        ready = await self.service.poll(row['request_id'], self.verifier)
        self.clock.value += self.settings.delivery_seconds
        await self.assert_error(409, self.service.ack, row['request_id'], self.verifier)
        await self.service.maintain()
        device = await self.store.get('device', ready['device_id'])
        self.assertEqual(device['status'], 'revoked')
        self.assertFalse(device['acked'])
        await self.assert_error(409, self.service.ack, row['request_id'], self.verifier)
        await self.assert_error(401, self.service.authorize, ready['key'])

    async def test_duplicate_browser_approval_rejected(self):
        row = await self.approve()
        await self.assert_error(409, self.service.approve, 'alice', row['request_id'], row['user_code'])

    async def test_setup_requests_expire(self):
        row = await self.approve()
        self.clock.value += self.settings.enrollment_seconds
        await self.assert_error(410, self.service.poll, row['request_id'], self.verifier)

    async def test_local_administrator_can_revoke_owned_registry_entry(self):
        ready = await self.enroll()
        await self.service.revoke('admin', ready['device_id'], admin=True)
        await self.assert_error(401, self.service.authorize, ready['key'])


class API(Fixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.forwarded = []

        def upstream(request):
            self.forwarded.append(request)
            if b'"stream":true' in request.content:
                return httpx.Response(
                    200,
                    headers={'content-type': 'text/event-stream'},
                    content=b'event: message_stop\ndata: {}\n\n',
                )
            return httpx.Response(
                200,
                json={'id': 'msg_test', 'content': [{'type': 'text', 'text': 'ok'}]},
            )

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.addAsyncCleanup(self.http.aclose)
        self.app = FastAPI()

        async def principal(request: Request):
            if not request.headers.get('x-test-subject'):
                raise HTTPException(401)
            return Principal(
                request.headers['x-test-subject'],
                request.headers.get('x-test-admin') == '1',
            )

        self.app.state.company_ai_access = self.service
        self.app.state.company_ai_inference = InferenceProxy(self.settings, self.http)
        self.app.include_router(create_router(principal))
        self.app.include_router(inference_router())
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=('127.0.0.1', 42)),
            base_url=self.settings.origin,
        )
        self.addAsyncCleanup(self.client.aclose)
        self.browser = {
            'X-Test-Subject': 'alice',
            'Origin': self.settings.origin,
            'X-Company-AI-Action': '1',
        }

    async def test_csrf_and_login_required(self):
        ready = await self.enroll()
        path = '/api/company-ai/devices/' + ready['device_id'] + '/revoke'
        self.assertEqual(
            (await self.client.post(path, headers={'X-Test-Subject': 'alice'})).status_code,
            403,
        )
        self.assertEqual(
            (await self.client.post(path, headers={**self.browser, 'Origin': 'https://evil.example'})).status_code,
            403,
        )
        self.assertEqual((await self.client.get('/api/company-ai/devices')).status_code, 401)
        self.assertEqual((await self.client.post(path, headers=self.browser)).status_code, 200)

    async def test_no_admin_escalation(self):
        self.assertEqual(
            (await self.client.get('/api/company-ai/admin/devices', headers=self.browser)).status_code,
            403,
        )

    async def test_bff_control_origin_requires_explicit_configuration_and_action(self):
        ready = await self.enroll()
        path = '/api/company-ai/devices/' + ready['device_id'] + '/revoke'
        headers = {**self.browser, 'Origin': 'https://dev.changdar-server.mooo.com'}
        self.assertEqual((await self.client.post(path, headers=headers)).status_code, 403)
        self.service.settings = replace(self.settings, control_origins=(headers['Origin'],))
        self.assertEqual(
            (await self.client.post(path, headers={**headers, 'X-Company-AI-Action': ''})).status_code,
            403,
        )
        self.assertEqual(
            (await self.client.post(path, headers={**headers, 'Sec-Fetch-Site': 'cross-site'})).status_code,
            403,
        )
        self.assertEqual(
            (
                await self.client.post(
                    path,
                    headers={**headers, 'Origin': headers['Origin'] + '.evil.example'},
                )
            ).status_code,
            403,
        )
        self.assertEqual((await self.client.post(path, headers=headers)).status_code, 200)
        self.assertEqual(self.service.policy()['portal_origin'], self.settings.origin)

    async def test_browser_cannot_start_native_device_flow(self):
        response = await self.client.post(
            '/api/company-ai/device/start',
            headers=self.browser,
            json={'device_name': 'WIN', 'code_challenge': challenge(self.verifier)},
        )
        self.assertEqual(response.status_code, 403)

    async def test_untrusted_client_cannot_choose_subject_models_or_key(self):
        ready = await self.enroll()
        raw = {
            'model': 'claude-test',
            'messages': [],
            'max_tokens': 1,
            'user': 'bob',
            'api_key': 'attacker',
            'api_base': 'https://evil.example',
            'metadata': {'user_id': 'bob'},
            'litellm_metadata': {'user': 'bob'},
        }
        response = await self.client.post(
            '/company-ai/v1/messages',
            json=raw,
            headers={
                'Authorization': 'Bearer ' + ready['key'],
                'x-litellm-customer-id': 'bob',
                'x-litellm-end-user-id': 'bob',
                'x-forwarded-for': '10.0.0.1',
                'anthropic-version': '2023-06-01',
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        forwarded = self.forwarded[-1]
        body = json.loads(forwarded.content)
        self.assertEqual(forwarded.headers['x-litellm-customer-id'], 'alice')
        self.assertNotIn('x-litellm-end-user-id', forwarded.headers)
        self.assertEqual(body['metadata'], {'user_id': 'alice'})
        self.assertEqual(body['litellm_metadata'], {'user': 'alice'})
        self.assertNotIn('user', body)
        self.assertNotIn('api_base', body)
        self.assertNotIn('api_key', body)

    async def test_sse_and_count_tokens(self):
        ready = await self.enroll()
        headers = {'Authorization': 'Bearer ' + ready['key']}
        response = await self.client.post(
            '/company-ai/v1/messages',
            headers=headers,
            json={'model': 'claude-test', 'messages': [], 'stream': True},
        )
        self.assertEqual(response.headers['x-accel-buffering'], 'no')
        self.assertIn('event: message_stop', response.text)
        response = await self.client.post(
            '/company-ai/v1/messages/count_tokens',
            headers=headers,
            json={'model': 'claude-test', 'messages': []},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.forwarded[-1].url.path, '/v1/messages/count_tokens')

    async def test_model_allowlist_and_query_denial(self):
        ready = await self.enroll()
        headers = {'Authorization': 'Bearer ' + ready['key']}
        response = await self.client.post('/company-ai/v1/messages', headers=headers, json={'model': 'forbidden'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            (await self.client.get('/company-ai/v1/models?api_key=anything', headers=headers)).status_code,
            400,
        )
        self.assertFalse(self.forwarded)

    async def test_sdk_beta_query_is_accepted_only_on_message_endpoints_and_stripped(
        self,
    ):
        ready = await self.enroll()
        headers = {
            'Authorization': 'Bearer ' + ready['key'],
            'anthropic-beta': 'synthetic-beta',
        }
        for path in ('/company-ai/v1/messages', '/company-ai/v1/messages/count_tokens'):
            response = await self.client.post(
                path + '?beta=true',
                headers=headers,
                json={
                    'model': 'claude-test',
                    'messages': [],
                    'max_tokens': 1,
                    'stream': True,
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.forwarded[-1].url.query, b'')
            self.assertEqual(self.forwarded[-1].headers['anthropic-beta'], 'synthetic-beta')
            self.assertIn('event: message_stop', response.text)
        self.assertEqual(
            (await self.client.get('/company-ai/v1/models?beta=true', headers=headers)).status_code,
            400,
        )

    async def test_sdk_beta_query_does_not_allow_extra_duplicate_encoded_or_credential_params(
        self,
    ):
        ready = await self.enroll()
        headers = {'Authorization': 'Bearer ' + ready['key']}
        for query in (
            'beta=false',
            'beta=TRUE',
            'beta=',
            'beta=true&api_key=synthetic',
            'api_key=synthetic',
            'beta=true&beta=true',
            'beta=true&provider=other',
            'beta=true&api_base=https://invalid.test',
            'beta=true&',
            '%62eta=true',
            'beta=%74rue',
            'beta=true%26api_key%3Dsynthetic',
        ):
            response = await self.client.post(
                '/company-ai/v1/messages?' + query,
                headers=headers,
                json={'model': 'claude-test', 'messages': []},
            )
            self.assertEqual(response.status_code, 400, query)
            self.assertEqual(response.json()['detail']['code'], 'ai_access_query_not_allowed')
        self.assertFalse(self.forwarded)

    async def test_source_header_spoofing_does_not_bypass_network(self):
        ready = await self.enroll()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=('203.0.113.99', 4)),
            base_url=self.settings.origin,
        ) as client:
            response = await client.get(
                '/company-ai/v1/models',
                headers={
                    'Authorization': 'Bearer ' + ready['key'],
                    'x-forwarded-for': '127.0.0.1',
                    'x-real-ip': '127.0.0.1',
                },
            )
        self.assertEqual(response.status_code, 403)

    async def test_empty_network_allowlist_denies_everyone(self):
        self.service.settings = replace(self.settings, inference_cidrs=())
        self.assertEqual((await self.client.get('/company-ai/v1/models')).status_code, 403)

    async def test_denylist_takes_priority_for_router_nat_source(self):
        self.service.settings = replace(self.settings, inference_deny_cidrs=('127.0.0.1/32',))
        self.assertEqual((await self.client.get('/company-ai/v1/models')).status_code, 403)

    async def test_budget_errors_keep_status_without_provider_message(self):
        ready = await self.enroll()

        async def upstream(request):
            return httpx.Response(
                422,
                json={
                    'error': {
                        'type': 'budget_exceeded',
                        'message': 'sensitive-provider-message',
                    }
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            self.app.state.company_ai_inference = InferenceProxy(self.settings, client)
            response = await self.client.post(
                '/company-ai/v1/messages',
                headers={'Authorization': 'Bearer ' + ready['key']},
                json={'model': 'claude-test', 'messages': []},
            )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['detail']['code'], 'ai_access_budget_exceeded')
        self.assertNotIn('sensitive-provider-message', response.text)

    async def test_management_routes_not_forwarded(self):
        ready = await self.enroll()
        response = await self.client.post(
            '/company-ai/key/generate',
            headers={'Authorization': 'Bearer ' + ready['key']},
            json={},
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(self.forwarded)


class Configuration(unittest.TestCase):
    def test_control_origins_must_be_exact_https_origins(self):
        base = dict(
            origin='https://example.com',
            state_dir=Path('/tmp/test'),
            litellm_url='http://127.0.0.1:4000',
            master_key='test',
        )
        for value in (
            'http://dev.example.com',
            'https://*.example.com',
            'https://dev.example.com/path',
            'https://user@dev.example.com',
        ):
            with self.subTest(origin=value), self.assertRaises(ValueError):
                Settings(**base, control_origins=(value,))

    def test_restore_required_if_encryption_key_lost(self):
        access_database(self)
        with tempfile.TemporaryDirectory() as path:
            Store(Path(path))
            (Path(path) / 'access.key').unlink()
            with self.assertRaises(ValueError):
                Store(Path(path))

    def test_private_files_and_no_symlinks(self):
        with tempfile.TemporaryDirectory() as path:
            folder = Path(path) / 'state'
            folder.mkdir(mode=0o755)
            with self.assertRaises(ValueError):
                Store(folder)
            folder.chmod(0o700)
            (folder / 'access.key').symlink_to(Path(path) / 'other')
            with self.assertRaises(ValueError):
                Store(folder)

    def test_no_remote_management_or_unbounded_confirmation(self):
        base = dict(
            origin='https://example.com',
            state_dir=Path('/tmp/test'),
            litellm_url='http://127.0.0.1:4000',
            master_key='test',
        )
        with self.assertRaises(ValueError):
            Settings(**{**base, 'litellm_url': 'https://remote.example'})
        with self.assertRaises(ValueError):
            Settings(**{**base, 'confirmation_seconds': 91 * 86400})


class Upstreams(unittest.IsolatedAsyncioTestCase):
    async def test_directory_checks_current_enabled_state_and_client_roles(self):
        clock = Clock()
        disabled = False
        calls = []

        def upstream(request):
            calls.append(request)
            if request.url.path.endswith('/token'):
                return httpx.Response(200, json={'access_token': 'test', 'expires_in': 300})
            if request.url.path.endswith('/composite'):
                return httpx.Response(200, json=[{'name': 'dev-user'}])
            return httpx.Response(200, json={'id': 'alice', 'enabled': not disabled})

        settings = Settings(
            origin='https://example.com',
            state_dir=Path('/tmp/test'),
            litellm_url='http://127.0.0.1:4000',
            master_key='test',
            issuer='https://idp.example/realms/company',
            directory_client='readonly',
            directory_secret='secret',
            role_client_uuid='uuid',
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            directory = Directory(settings, client, clock=clock)
            self.assertTrue(await directory.allowed('alice'))
            disabled = True
            self.assertTrue(await directory.allowed('alice'))
            clock.value += settings.identity_cache_seconds
            self.assertFalse(await directory.allowed('alice'))
        self.assertTrue(all(request.method == 'GET' or request.url.path.endswith('/token') for request in calls))

    async def test_native_key_reconciliation_uses_hash_in_query(self):
        calls = []
        settings = Settings(
            origin='https://example.com',
            state_dir=Path('/tmp/test'),
            litellm_url='http://127.0.0.1:4000',
            master_key='master',
        )

        def upstream(request):
            calls.append(request)
            return httpx.Response(404, json={}) if request.method == 'GET' else httpx.Response(200, json={})

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            llm = LiteLLM(settings, client)
            await llm.ensure_key(
                {'id': 'test', 'subject': 'alice', 'expires_at': 2000000000},
                'sk-private-test',
            )
        self.assertEqual(calls[0].url.params['key'], digest('sk-private-test'))
        self.assertNotIn('sk-private-test', str(calls[0].url))
        self.assertNotIn('master', calls[1].content.decode())


class WebUIBridge(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from examples.company_ai_access.adapters.webui import install_webui

        self.forwarded = []

        async def verified(request):
            if request.headers.get('x-test-subject'):
                return types.SimpleNamespace(
                    role='user',
                    oauth={'oidc': {'sub': request.headers['x-test-subject']}},
                )
            return None

        module = types.ModuleType('open_webui.utils.auth')
        module.get_optional_verified_user_from_request = verified
        self.app = FastAPI()
        with patch.dict('sys.modules', {'open_webui.utils.auth': module}):
            install_webui(self.app)

        def upstream(request):
            if request.url.path == '/company-ai/network-access':
                return httpx.Response(200, json={'allowed': True})
            self.forwarded.append(request)
            return httpx.Response(200, json={'status': 'ok'})

        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.addAsyncCleanup(self.upstream.aclose)
        from examples.company_ai_access.adapters.bridge import BridgeClient, BridgeSettings
        self.app.state.company_ai_bridge = BridgeClient(
            BridgeSettings('http://127.0.0.1:18083', 'private-bridge-key-for-tests-only-1234'), self.upstream,
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=('192.0.2.3', 8)),
            base_url='https://ai-dev.changdar-server.mooo.com',
        )
        self.addAsyncCleanup(self.client.aclose)
        self.env = patch.dict(
            'os.environ',
            {
                'AI_ACCESS_BRIDGE_KEY': 'private-bridge-key-for-tests-only-1234',
                'AI_ACCESS_BRIDGE_URL': 'http://127.0.0.1:18083',
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    async def test_bridge_uses_validated_startup_snapshot_after_environment_changes(self):
        with patch.dict('os.environ', {'AI_ACCESS_BRIDGE_URL': 'http://untrusted.invalid', 'AI_ACCESS_BRIDGE_KEY': 'changed'}):
            response = await self.client.get('/api/company-ai/devices', headers={'x-test-subject': 'alice'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.forwarded[-1].url.host, '127.0.0.1')
        self.assertEqual(self.forwarded[-1].url.port, 18083)
        self.assertEqual(self.forwarded[-1].headers['x-company-ai-bridge'], 'private-bridge-key-for-tests-only-1234')

    async def test_control_requires_browser_session_and_overwrites_assertions(self):
        response = await self.client.get('/api/company-ai/devices', headers={'x-company-ai-subject': 'attacker'})
        self.assertEqual(response.status_code, 401)
        response = await self.client.get(
            '/api/company-ai/devices',
            headers={
                'x-test-subject': 'alice',
                'x-company-ai-subject': 'bob',
                'x-company-ai-admin': '1',
            },
        )
        self.assertEqual(response.status_code, 200)
        headers = self.forwarded[-1].headers
        self.assertEqual(headers['x-company-ai-subject'], 'alice')
        self.assertEqual(headers['x-company-ai-admin'], '0')
        self.assertEqual(headers['x-company-ai-client-ip'], '192.0.2.3')
        self.assertNotIn('authorization', headers)
        self.assertEqual(response.headers['cache-control'], 'no-store')

    async def test_empty_body_revoke_is_supported(self):
        response = await self.client.post(
            '/api/company-ai/devices/device-id/revoke',
            headers={'x-test-subject': 'alice'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(self.forwarded[-1].content), {})

    async def test_bridge_accepts_and_strips_only_sdk_beta_query(self):
        for path in ('/company-ai/v1/messages', '/company-ai/v1/messages/count_tokens'):
            response = await self.client.post(
                path + '?beta=true',
                headers={
                    'authorization': 'Bearer sk-synthetic',
                    'anthropic-beta': 'synthetic-beta',
                },
                json={'model': 'test', 'messages': []},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.forwarded[-1].url.query, b'')
            self.assertEqual(self.forwarded[-1].headers['anthropic-beta'], 'synthetic-beta')
        accepted = len(self.forwarded)
        for path in (
            '/company-ai/v1/messages?beta=true&api_key=synthetic',
            '/company-ai/v1/messages?beta=true&beta=true',
            '/company-ai/v1/models?beta=true',
            '/api/company-ai/device/start?beta=true',
        ):
            response = await self.client.post(path, json={'model': 'test'})
            self.assertEqual(response.status_code, 400, path)
        self.assertEqual(len(self.forwarded), accepted)

    async def test_native_and_inference_preserve_no_caller_identity(self):
        response = await self.client.post(
            '/api/company-ai/device/start',
            json={'test': 'payload'},
            headers={'x-company-ai-subject': 'attacker'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('x-company-ai-subject', self.forwarded[-1].headers)
        response = await self.client.post(
            '/company-ai/v1/messages',
            json={'model': 'test'},
            headers={
                'authorization': 'Bearer sk-client',
                'x-company-ai-bridge': 'attacker',
                'x-litellm-customer-id': 'bob',
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.forwarded[-1].headers['authorization'], 'Bearer sk-client')
        self.assertEqual(self.forwarded[-1].headers['x-company-ai-bridge'], 'private-bridge-key-for-tests-only-1234')
        self.assertNotIn('x-litellm-customer-id', self.forwarded[-1].headers)


class RequestPolicySnapshot(unittest.TestCase):
    def test_proxy_proof_and_network_policy_do_not_follow_environment_changes(self):
        from examples.company_ai_access.routers.dependencies import client_ip, require_inference_network

        settings = Settings(
            origin='https://example.com', state_dir=Path('/tmp/unused-policy-test'),
            litellm_url='http://127.0.0.1:4000', master_key='test',
            bridge_key='original-bridge', network_proxy_key='original-proxy',
            inference_cidrs=('203.0.113.0/24',), inference_deny_cidrs=('203.0.113.9/32',),
        )
        app = FastAPI()
        app.state.company_ai_access = types.SimpleNamespace(settings=settings)

        def request(proof, address):
            return Request({
                'type': 'http', 'app': app, 'method': 'GET', 'scheme': 'https',
                'path': '/api/company-ai/devices', 'query_string': b'',
                'client': ('127.0.0.1', 1), 'server': ('example.com', 443),
                'headers': [(b'x-company-ai-network-proxy', proof.encode()),
                            (b'x-company-ai-client-ip', address.encode())],
            })

        with patch.dict('os.environ', {'AI_ACCESS_NETWORK_PROXY_KEY': 'changed-proxy',
                                     'AI_ACCESS_BRIDGE_KEY': 'changed-bridge'}):
            self.assertEqual(client_ip(request('original-proxy', '203.0.113.8')), '203.0.113.8')
            require_inference_network(request('original-proxy', '203.0.113.8'))
            for proof, address in [('changed-proxy', '203.0.113.8'),
                                   ('original-proxy', '203.0.113.9'),
                                   ('original-proxy', '192.0.2.1')]:
                with self.subTest(proof=proof, address=address), self.assertRaises(HTTPException) as failure:
                    require_inference_network(request(proof, address))
                self.assertEqual(failure.exception.status_code, 403)
        self.assertNotIn('original-bridge', repr(settings))
        self.assertNotIn('original-proxy', repr(settings))


class WorkerLease(unittest.IsolatedAsyncioTestCase):
    async def test_second_worker_cannot_race_revocation(self):
        access_database(self)
        from examples.company_ai_access.runtime import runtime

        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(
                origin='https://example.com',
                state_dir=Path(folder),
                litellm_url='http://127.0.0.1:4000',
                master_key='test',
            )
            async with runtime(FastAPI(), settings):
                with self.assertRaises(RuntimeError):
                    async with runtime(FastAPI(), settings):
                        self.fail('A second process acquired the same registry.')


class StandaloneSecurity(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from examples.company_ai_access.app import create_app

        settings = Settings(
            origin='https://example.com',
            state_dir=Path('/tmp/unused-test-registry'),
            litellm_url='http://127.0.0.1:4000',
            master_key='test',
            inference_cidrs=('203.0.113.0/24',),
        )
        self.bridge_key = 'private-bridge-key-for-loopback-only'
        settings = replace(settings, bridge_key=self.bridge_key)
        with (
            patch('examples.company_ai_access.app.Settings.from_env', return_value=settings),
            patch.dict('os.environ', {'AI_ACCESS_BRIDGE_KEY': self.bridge_key}),
        ):
            self.app = create_app()
        self.app.state.company_ai_access = types.SimpleNamespace(settings=settings)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=('203.0.113.1', 3)),
            base_url='https://example.com',
        )
        self.addAsyncCleanup(self.client.aclose)

    async def test_external_caller_cannot_use_internal_bridge_assertion(self):
        response = await self.client.get(
            '/api/company-ai/devices',
            headers={
                'x-company-ai-bridge': self.bridge_key,
                'x-company-ai-subject': 'alice',
                'x-company-ai-admin': '1',
            },
        )
        self.assertEqual(response.status_code, 401)

    async def test_validation_never_echoes_submitted_proof(self):
        proof = 'sensitive-verifier-that-must-not-appear-in-validation-response'
        response = await self.client.post(
            '/api/company-ai/device/poll',
            json={
                'request_id': 'invalid',
                'code_verifier': proof,
                'extra_secret': proof,
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(proof, response.text)
        self.assertEqual(response.json()['detail']['code'], 'ai_access_invalid_request')

    async def test_control_body_limit_before_json_parsing(self):
        response = await self.client.post(
            '/api/company-ai/device/start',
            content=b'x' * 16001,
            headers={'content-type': 'application/json'},
        )
        self.assertEqual(response.status_code, 413)

    async def test_checked_in_flutter_contract_is_current(self):
        from examples.company_ai_access.cli.export_contract import contract

        path = Path(__file__).resolve().parents[1] / 'docs/company-ai-access-openapi.json'
        self.assertEqual(json.loads(path.read_text()), contract())


if __name__ == '__main__':
    unittest.main()
