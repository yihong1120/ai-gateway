"""Real HTTP MCP and PostgreSQL boundaries; only synthetic OIDC/NAS/EIP data."""
from __future__ import annotations

from dataclasses import replace
from functools import partial
from urllib.parse import parse_qs
import asyncio
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import jwt

from company_agent.tests.test_nas import NASFixture
from company_mcp.authentication import AuthorizationUnavailable, Principal, Verifier
from company_mcp.config import Configuration, PATH, SCOPE
from company_mcp.adapters.company import Resources
from company_mcp.tools import TOOLS
from company_mcp.server import Boundary, build
from company_mcp.storage import Repository, ResourceError
from company_agent.api.queue import WorkQueue
from examples.official_document_workflow.runtime import current_resources
from examples.official_document_workflow.infrastructure.storage.state import Link
from examples.official_document_workflow.api.schemas import LoginRequest


def configuration(**updates):
    return Configuration(**{
        'origin': 'https://company.example', 'issuer': 'https://identity.example/realms/company',
        'clients': ('company-codex', 'company-claude'), 'cidrs': ('127.0.0.0/8',),
        'backend_key': 'synthetic-private-backend-service-key-1234567890',
        'introspection_client': 'resource-verifier', 'introspection_secret': 'synthetic-private-verifier',
        'eip_transport_acknowledged': True, **updates,
    })


def user_for_owner(owner):
    return {'id': owner, 'role': 'user', 'oauth': {'oidc': {'sub': owner + '-sub'}}}


def owner_for_subject(subject):
    return subject.removesuffix('-sub')


def introspection_response(case, request):
    case.calls.append(request)
    return httpx.Response(200, json=case.introspection)


def actor_introspection(request):
    raw = parse_qs(request.content.decode())['token'][0]
    claims = jwt.decode(raw, options={'verify_signature': False})
    return httpx.Response(200, json={'active': True, 'sub': claims['sub'], 'client_id': claims['azp']})


async def worker_result(case, ctx, args):
    case.assertEqual(ctx.job, case.worker_row['id'])
    case.assertNotEqual(ctx.job, case.worker_workspace)
    case.store.artifact('alice', ctx.job, 'owned-result.txt', b'private-result')
    return {'status': 'done', 'files': [], 'notice': '已分析，尚未寫入行事曆'}


async def record_capacity(case, current, row):
    case.active_slots += 1
    case.maximum_slots = max(case.maximum_slots, case.active_slots)
    case.started_owners.append(row['owner'])
    try:
        await asyncio.sleep(.05)
        current.queue.update(row['id'], status='done')
    finally:
        case.active_slots -= 1


async def revoked_result(case, *args):
    case.resources.repository.set_grant('alice', 'company-codex', False)
    return {'content': 'synthetic-private-result'}


async def concurrent_result(case, person, name, value):
    case.entered_owners.add(person.owner)
    if len(case.entered_owners) == 2:
        case.execution_gate.set()
    await asyncio.wait_for(case.execution_gate.wait(), 5)
    return {'owner': person.owner}


class CompanyMCPTests(unittest.IsolatedAsyncioTestCase, NASFixture):
    async def asyncSetUp(self):
        self.configuration = configuration()
        self.workflow = current_resources()
        self.workflow.store.bind('alice', '17', 'eip-alice', 'synthetic-eip-password', enable_auto=False)
        self.adapter = SimpleNamespace(
            user_id='17', close=AsyncMock(),
            documents=AsyncMock(return_value=[{
                'id': '10', 'folder': 'inbox', 'subject': '測試公文', 'date': '2026-11-13',
            }]),
            document_detail=AsyncMock(return_value={'fields': [{'label': '主旨', 'value': '測試'}], 'attachments': []}),
            attachments=AsyncMock(return_value=[{'name': 'a.pdf', 'url': '/fo/a.pdf'}]),
            calendar=AsyncMock(return_value=[]),
        )
        self.generation = self.workflow.store.account('alice')['generation']
        self.workflow.state.links['alice'] = Link(self.adapter, 'eip-alice', binding_generation=self.generation)
        self.queue = WorkQueue(self.private, self.base / 'uploads', store=self.store)
        self.resources = Resources(self.configuration, self.store, current_resources, queue=self.queue)
        self.alice = Principal('alice', 'alice-sub', 'company-codex')
        self.bob = Principal('bob', 'bob-sub', 'company-codex')
        user = AsyncMock(side_effect=user_for_owner)
        self.enterContext(patch('examples.identity.adapters.webui.verified_user', user))

    async def invoke(self, name, person=None, **arguments):
        value = TOOLS[name][0].model_validate(arguments)
        return await self.resources.execute(person or self.alice, name, value)

    async def workspace(self, request_id='request-one', person=None):
        return (await self.invoke('workspace_prepare', person=person, request_id=request_id))['workspace']

    async def test_default_all_bindings_and_new_accounts_only_in_new_workspace(self):
        self.bind()
        old = await self.workspace()
        with self.store.db() as db:
            db.execute('DELETE FROM nas_login_limits')
        self.bind('alice', 'team')
        self.assertEqual((await self.invoke('workspace_prepare', request_id='request-two'))['nas_accounts'], 2)
        self.assertEqual(len(self.resources.repository.workspace(self.alice, old)['nas_revisions']), 1)
        self.assertEqual(await self.workspace(), old)
        self.assertFalse(self.workflow.store.account('alice')['enabled'])

    async def test_foreign_owner_client_and_revoked_grant_cannot_reuse_workspace(self):
        key = await self.workspace()
        for other in (self.bob, replace(self.alice, client='company-claude')):
            with self.assertRaises(ResourceError):
                self.resources.repository.workspace(other, key)
        self.resources.repository.set_grant('alice', self.alice.client, False)
        with self.assertRaises(ResourceError):
            self.resources.repository.workspace(self.alice, key)
        self.resources.repository.set_grant('alice', self.alice.client, True)
        with self.assertRaises(ResourceError):
            self.resources.repository.workspace(self.alice, key)

    async def test_restart_uses_durable_workspace_and_revocation(self):
        key = await self.workspace()
        restored = Repository(self.store)
        self.assertEqual(restored.workspace(self.alice, key)['eip_generation'], self.generation)
        restored.set_grant('alice', self.alice.client, False)
        with self.assertRaises(ResourceError):
            self.resources.repository.workspace(self.alice, key)

    async def test_expired_workspace_is_not_silently_recreated(self):
        key = await self.workspace()
        with self.store.db() as db:
            db.execute('UPDATE mcp_workspaces SET expires=0 WHERE id=%s', (key,))
        with self.assertRaises(ResourceError):
            await self.workspace()

    async def test_connections_never_expose_password_or_session(self):
        self.bind()
        output = await self.invoke('connections_list')
        self.assertEqual(output['resource_mode'], 'all_owned_bindings')
        serialized = json.dumps(output)
        for secret in ('alice-password', 'synthetic-eip-password', 'ciphertext', 'password'):
            self.assertNotIn(secret, serialized)
        self.assertNotIn('account', output['nas'][0])

    async def test_real_nas_roots_read_and_path_escape(self):
        self.bind()
        workspace = await self.workspace()
        roots = await self.invoke('nas_roots', workspace=workspace)
        root = next(row['name'] for row in roots['roots'] if row['label'] == 'home')
        output = await self.invoke('nas_read', workspace=workspace, root=root, path='a.txt')
        self.assertIn('alice secret', str(output))
        with self.assertRaises(ValueError):
            await self.invoke('nas_read', workspace=workspace, root=root, path='../bob/a.txt')
        self.assertFalse(self.dsm.sessions)

    async def test_binding_replacement_invalidates_old_nas_read(self):
        state = self.bind()
        workspace = await self.workspace()
        root = (await self.invoke('nas_roots', workspace=workspace))['roots'][0]['name']
        self.vault.unlink('alice', state['binding_id'])
        with self.assertRaises(ValueError):
            await self.invoke('nas_read', workspace=workspace, root=root, path='a.txt')

    async def test_eip_document_handles_and_foreign_replay(self):
        key = await self.workspace()
        response = await self.invoke('eip_query', workspace=key, folder='inbox', start='2026-11-01', end='2026-11-30')
        handle = response['documents'][0]['handle']
        detail = await self.invoke('eip_document_read', workspace=key, handle=handle)
        self.assertEqual(detail['fields'][0]['value'], '測試')
        other_key = await self.workspace('bob-workspace', self.bob)
        with self.assertRaises((ResourceError, ValueError)):
            await self.invoke('eip_document_read', self.bob, workspace=other_key, handle=handle)

    async def test_changed_eip_generation_stops_before_remote_call(self):
        key = await self.workspace()
        self.workflow.store.pause('alice')
        with self.assertRaises(ResourceError):
            await self.invoke('eip_calendar_query', workspace=key, start='2026-11-01', end='2026-11-14')
        self.adapter.calendar.assert_not_awaited()

    async def test_unapproved_eip_transport_blocks_only_eip(self):
        self.resources.configuration = configuration(eip_transport_acknowledged=False)
        key = await self.workspace()
        with self.assertRaises(ResourceError):
            await self.invoke('eip_calendar_query', workspace=key, start='2026-11-01', end='2026-11-14')
        self.assertTrue((await self.invoke('connections_list'))['eip']['saved'])

    async def test_paid_analysis_is_prepared_once_and_queued_only_after_browser_confirmation(self):
        key = await self.workspace()
        docs = await self.invoke('eip_query', workspace=key, folder='inbox', start='2026-11-01', end='2026-11-30')
        handles = await self.invoke('eip_attachments', workspace=key, handle=docs['documents'][0]['handle'])
        attachment = handles['attachments'][0]['handle']
        result = await self.invoke('document_analysis_prepare', workspace=key, request_id='paid-one', handles=[attachment])
        repeat = await self.invoke('document_analysis_prepare', workspace=key, request_id='paid-one', handles=[attachment])
        self.assertEqual(result, repeat)
        self.assertFalse(result['model_called'])
        self.assertEqual(self.queue.list('alice'), [])
        job = self.resources.repository.submit_analysis(self.alice, result['operation'], self.queue)
        self.assertEqual(self.resources.repository.submit_analysis(self.alice, result['operation'], self.queue), job)
        self.queue.update(job, status='interrupted')
        self.assertEqual(self.resources.repository.submit_analysis(self.alice, result['operation'], self.queue), job)
        self.assertEqual(len(self.queue.list('alice')), 1)
        self.assertEqual(self.queue.chat_pending(), [])

    async def test_calendar_plan_creates_only_private_draft(self):
        key = await self.workspace()
        output = await self.invoke('calendar_plan_prepare', workspace=key, events=[{
            'subject': '工程會議', 'start_date': '2026-11-18', 'end_date': '2026-11-18',
            'all_day': False, 'start_time': '09:00', 'end_time': '10:00',
        }])
        self.assertEqual(output['status'], 'prepared_not_written')
        self.assertIn('/agents/mcp?calendar=', output['confirmation_url'])
        self.assertEqual(len(self.store.files('alice', key)), 1)
        self.assertNotIn('commit', self.adapter.__dict__)

    async def test_confirmed_worker_isolates_result_and_does_not_replay(self):
        from company_agent.config import Settings
        from company_mcp.adapters.analysis import execute
        from unittest.mock import Mock
        key = await self.workspace()
        docs = await self.invoke('eip_query', workspace=key, folder='inbox', start='2026-11-01', end='2026-11-30')
        attachments = await self.invoke('eip_attachments', workspace=key, handle=docs['documents'][0]['handle'])
        ready = await self.invoke('document_analysis_prepare', workspace=key, request_id='worker-one',
                                  handles=[attachments['attachments'][0]['handle']])
        self.resources.repository.submit_analysis(self.alice, ready['operation'], self.queue)
        with self.store.db() as db:
            db.execute('UPDATE mcp_workspaces SET expires=0 WHERE id=%s', (key,))
        row = self.queue.claim()
        service = SimpleNamespace(queue=self.queue,
            identity=AsyncMock(return_value={'id': 'alice', 'role': 'user', 'oauth': {'oidc': {'sub': 'alice-sub'}}}),
            settings_factory=Mock(return_value=Settings(state_dir=str(self.private))),
            model_factory=Mock(return_value=SimpleNamespace(subject='alice-sub')))

        self.worker_row, self.worker_workspace = row, key
        with patch(
            'company_mcp.config.Configuration.from_environment', return_value=self.configuration,
        ), patch('company_mcp.adapters.analysis.process', side_effect=partial(worker_result, self)) as run, patch(
            'company_mcp.adapters.analysis.close_model', new=AsyncMock(),
        ):
            await execute(service, row)
            self.assertEqual(run.await_count, 1)
            result = self.queue.public('alice', row['id'])
            self.assertEqual(result['status'], 'done')
            self.assertFalse(result['resumable'])
            self.assertEqual(len(result['files']), 1)
            self.assertEqual(self.store.files('alice', key), [])
            self.assertEqual(self.queue.chat_pending(), [])
            from psycopg.errors import UniqueViolation
            with self.assertRaises(UniqueViolation):
                await execute(service, row)
            self.assertEqual(run.await_count, 1)

    async def test_external_work_types_share_company_concurrency_capacity(self):
        from company_agent.api.service import Service
        service = Service(self.queue, None, None, None, max_concurrent=1)
        self.active_slots, self.maximum_slots, self.started_owners = 0, 0, []
        jobs = []
        for owner, kind in (('alice', 'external_one'), ('bob', 'external_two')):
            service.register_job_handler(kind, partial(record_capacity, self))
            key = self.queue.submit(owner, owner + '-sub', 'test-chat', 'test-request', 'synthetic task', [])
            with self.store.db() as db:
                db.execute('UPDATE web_jobs SET kind=%s WHERE id=%s', (kind, key))
            jobs.append((owner, key))
        worker = asyncio.create_task(service.work())
        try:
            async with asyncio.timeout(10):
                while any(self.queue.get(owner, key)['status'] != 'done' for owner, key in jobs):
                    if worker.done():
                        worker.result()
                    await asyncio.sleep(.01)
            self.assertEqual(self.maximum_slots, 1)
            self.assertEqual(set(self.started_owners), {'alice', 'bob'})
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


class OIDCTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.config = configuration()
        self.resolve = AsyncMock(return_value='alice')
        self.introspection = {'active': True, 'sub': 'alice-sub', 'client_id': 'company-codex'}
        self.calls = []

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(partial(introspection_response, self)))
        self.verifier = Verifier(self.config, self.http, resolve_owner=self.resolve,
            key_client=SimpleNamespace(get_signing_key_from_jwt=Mock(return_value=SimpleNamespace(key=self.key.public_key()))))

    async def asyncTearDown(self):
        await self.http.aclose()

    def token(self, **changes):
        return jwt.encode({
            'iss': self.config.issuer, 'aud': self.config.resource, 'sub': 'alice-sub',
            'azp': 'company-codex', 'iat': int(time.time()), 'exp': int(time.time()) + 60,
            'scope': SCOPE, **changes,
        }, self.key, algorithm='RS256')

    async def test_verified_signature_resource_and_live_session(self):
        verified = await self.verifier.verify_token(self.token())
        self.assertEqual(verified.claims['owner'], 'alice')
        self.assertEqual(verified.resource, self.config.resource)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('synthetic-private-verifier', str(verified))

    async def test_wrong_audience_issuer_client_scope_expiry_and_signature_are_rejected_before_introspection(self):
        for changes in ({'aud': 'models-only'}, {'iss': 'https://other.example'}, {'azp': 'unapproved-client'},
                        {'scope': 'openid'}, {'exp': 1}, {'sub': ''}):
            self.assertIsNone(await self.verifier.verify_token(self.token(**changes)))
        self.assertEqual(self.calls, [])

    async def test_revoked_identity_missing_mapping_and_offline_idp_are_denied(self):
        self.introspection['active'] = False
        self.assertIsNone(await self.verifier.verify_token(self.token()))
        self.introspection['active'] = True
        self.resolve.return_value = None
        self.assertIsNone(await self.verifier.verify_token(self.token()))
        self.resolve.side_effect = ValueError('synthetic-secret-must-not-be-rendered')
        with self.assertRaises(AuthorizationUnavailable) as caught:
            await self.verifier.verify_token(self.token())
        self.assertNotIn('synthetic-secret', str(caught.exception))


class HTTPTests(unittest.IsolatedAsyncioTestCase, NASFixture):
    token = OIDCTests.token
    async def asyncSetUp(self):
        await CompanyMCPTests.asyncSetUp(self)
        OIDCTests.setUp(self)

    async def asyncTearDown(self):
        await OIDCTests.asyncTearDown(self)

    async def http_request(self, method, path, **kwargs):
        managed = build(self.configuration, self.verifier, Mock(return_value=self.resources), tools=TOOLS)
        async with managed.router.lifespan_context(managed):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=Boundary(managed, self.configuration)), base_url=self.configuration.origin,
            ) as client:
                return await client.request(method, path, **kwargs)

    async def rpc(self, method, params, *, token=True):
        headers = {'accept': 'application/json, text/event-stream'}
        if token:
            headers['Authorization'] = 'Bearer ' + self.token()
        return await self.http_request('POST', PATH, headers=headers, json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})

    async def test_http_catalog_real_tool_and_secret_safe_invalid_arguments(self):
        reply = await self.rpc('tools/list', {})
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(len(reply.json()['result']['tools']), len(TOOLS))
        tool = await self.rpc('tools/call', {'name': 'connections_list', 'arguments': {}})
        self.assertEqual(tool.json()['result']['structuredContent']['resource_mode'], 'all_owned_bindings')
        bad = await self.rpc('tools/call', {'name': 'nas_read', 'arguments': {'password': 'do-not-echo-this-secret'}})
        self.assertTrue(bad.json()['result']['isError'])
        self.assertNotIn('do-not-echo-this-secret', bad.text)

    async def test_http_bearer_challenge_and_network_query_rejection(self):
        denied = await self.rpc('tools/list', {}, token=False)
        self.assertEqual(denied.status_code, 401)
        self.assertIn('resource_metadata', denied.headers['www-authenticate'])
        self.assertEqual((await self.http_request('GET', PATH + '?token=forbidden')).status_code, 400)
        self.assertEqual((await self.http_request('GET', PATH, headers={'Origin': 'https://untrusted.example'})).status_code, 403)
        metadata = await self.http_request('GET', '/.well-known/oauth-protected-resource/company-mcp')
        self.assertEqual(metadata.json()['resource'], self.configuration.resource)
        self.assertEqual(metadata.headers['cache-control'], 'no-store')

    async def test_high_level_validation_never_renders_malformed_argument_values(self):
        secret = 'synthetic-secret-not-for-protocol-errors'
        for arguments in (secret, [secret], {'password': secret}):
            reply = await self.rpc('tools/call', {'name': 'connections_list', 'arguments': arguments})
            self.assertNotIn(secret, reply.text)

    async def test_revocation_during_execution_suppresses_result_and_catalog(self):
        with patch.object(self.resources, 'execute', side_effect=partial(revoked_result, self)):
            reply = await self.rpc('tools/call', {'name': 'connections_list', 'arguments': {}})
        self.assertTrue(reply.json()['result']['isError'])
        self.assertNotIn('synthetic-private-result', reply.text)
        self.assertEqual((await self.rpc('tools/list', {})).json()['result']['tools'], [])

    async def test_high_level_server_keeps_concurrent_owners_separate(self):
        from company_mcp.server import ResourceServer
        self.assertIsInstance(ResourceServer(self.configuration, self.verifier, Mock(return_value=self.resources), TOOLS),
                              __import__('mcp.server.mcpserver', fromlist=['MCPServer']).MCPServer)
        self.resolve.side_effect = owner_for_subject
        async with httpx.AsyncClient(transport=httpx.MockTransport(actor_introspection)) as upstream:
            self.verifier.client = upstream
            managed = build(self.configuration, self.verifier, Mock(return_value=self.resources), tools=TOOLS)
            self.entered_owners = set()
            self.execution_gate = asyncio.Event()
            async with managed.router.lifespan_context(managed):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=managed),
                                             base_url=self.configuration.origin) as client:
                    with patch.object(self.resources, 'execute', side_effect=partial(concurrent_result, self)):
                        replies = await asyncio.gather(*[client.post(PATH,
                            headers={'Authorization': 'Bearer ' + self.token(sub=owner + '-sub'),
                                     'accept': 'application/json, text/event-stream'},
                            json={'jsonrpc': '2.0', 'id': index, 'method': 'tools/call',
                                  'params': {'name': 'connections_list', 'arguments': {}}})
                            for index, owner in enumerate(('alice', 'bob'))])
            self.assertEqual([reply.json()['result']['structuredContent']['owner'] for reply in replies],
                             ['alice', 'bob'])


class PersistenceSchemaTests(unittest.TestCase):
    def test_keycloak_metadata_requires_issuer_bound_callbacks_and_fixed_endpoints(self):
        from company_mcp.doctor import validate_metadata
        config = configuration()
        metadata = {'issuer': config.issuer, 'code_challenge_methods_supported': ['S256'],
                    'authorization_response_iss_parameter_supported': True}
        metadata.update({key: config.issuer + '/protocol/openid-connect' + suffix for key, suffix in (
            ('authorization_endpoint', '/auth'), ('token_endpoint', '/token'),
            ('jwks_uri', '/certs'), ('introspection_endpoint', '/token/introspect'))})
        self.assertTrue(validate_metadata(config, metadata)['issuer_metadata_valid'])
        for changes in ({'authorization_response_iss_parameter_supported': False},
                        {'token_endpoint': 'https://untrusted.example/token'},
                        {'code_challenge_methods_supported': ['plain']}):
            with self.assertRaises(ValueError):
                validate_metadata(config, {**metadata, **changes})

    def test_old_login_semantics_and_separate_saved_read_only_choice(self):
        old = LoginRequest(accept_http=True, account='alice', password='synthetic')
        self.assertIsNone(old.save_credentials)
        self.assertFalse(old.enable_auto)
        new = LoginRequest(accept_http=True, account='alice', password='synthetic', save_credentials=True)
        self.assertTrue(new.save_credentials)
        self.assertFalse(new.enable_auto)
        with self.assertRaises(ValueError):
            LoginRequest(accept_http=True, account='alice', password='synthetic', save_credentials=False, enable_auto=True)
