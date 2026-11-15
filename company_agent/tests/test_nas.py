from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import threading
import time
import unittest
from http.cookies import SimpleCookie
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
from cryptography.exceptions import InvalidTag

import company_agent.runtime.models as _models_imports
from company_agent.api.service import WorkQueue
from company_agent.runtime.execution import DocumentAgent
from company_agent.services.files.nas import file_access
from company_agent.services.files.nas import NASAccess
from company_agent.services.files.nas import NASClient
from company_agent.services.files.nas import NASVault
from company_agent.services.files.operations import ToolBox
from company_agent.services.files.schemas import Read
from company_agent.tests.test_agent import Fixture
from examples.shared.runtime.modules import ModuleReference

_fixture_test_user_billing = ModuleReference('company_agent.tests.test_user_billing')


class FakeDSM:
    def __init__(self):
        self.sessions = {}
        self.calls = []
        self.counter = 0
        self.deny = False
        self.changed = False
        self.escape = False
        self.bad_password = False
        self.on_info = None
        self.no_shared = False
        self.allow_homes = False
        self.hide_homes = False
        self.shared_in_homes = False
        self.cookie_sids = []
        self.session_error = None

    def real_path(self, path, name):
        if path == '/home' or path.startswith('/home/'):
            return '/volume1/homes/'+name+path[len('/home'):]
        if self.shared_in_homes and (path == '/shared' or path.startswith('/shared/')):
            return '/volume1/homes/team'+path[len('/shared'):]
        return '/volume1'+path

    def handler(self, request):
        assert request.method == 'POST' and not request.url.query
        p = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.calls.append(p)
        api, method = p['api'], p['method']
        assert '_sid' not in p
        if api == 'SYNO.API.Auth' and method == 'login':
            assert p['format'] == 'cookie'
            if self.bad_password or p['passwd'] != p['account']+'-password':
                return httpx.Response(200, json={'success': False, 'error': {'code': 400}})
            self.counter += 1
            sid = 'session-'+str(self.counter)
            self.sessions[sid] = p['account']
            return httpx.Response(200, json={'success': True, 'data': {'sid': sid}}, headers={'set-cookie': f'id={sid}; Path=/; Secure; HttpOnly'})
        cookie = SimpleCookie(request.headers.get('cookie', ''))
        sid = cookie['id'].value if 'id' in cookie else None
        self.cookie_sids.append(sid)
        name = self.sessions.get(sid)
        if not name:
            return httpx.Response(200, json={'success': False, 'error': {'code': 106}})
        if api == 'SYNO.API.Auth' and method == 'logout':
            self.sessions.pop(sid)
            data = {}
        elif api == 'SYNO.FileStation.Info':
            if self.session_error:
                return httpx.Response(200, json={'success': False, 'error': {'code': self.session_error}})
            if self.on_info:
                self.on_info()
            data = {'is_manager': name == 'admin'}
        elif method == 'list_share':
            paths = ['/home']+(['/homes'] if (name == 'admin' or self.allow_homes)
                               and not self.hide_homes else [])+([] if self.no_shared else ['/shared'])
            data = {'shares': [{'path': x, 'name': x[1:], 'isdir': True} for x in paths], 'total': len(paths)}
        elif api == 'SYNO.FileStation.Download':
            if self.deny:
                return httpx.Response(404)
            return httpx.Response(200, content=(name+' secret').encode())
        elif method == 'getinfo':
            path = json.loads(p['path'])[0]
            isdir = path in ('/home', '/homes', '/shared')
            real = self.real_path(path, name)
            if self.escape and not isdir:
                real = '/volume1/homes/other/private.txt'
            size = 0 if isdir else len((name+' secret').encode())
            data = {
                'files': [{
                    'path': path, 'isdir': isdir, 'additional': {
                        'real_path': real, 'size': size, 'mount_point_type': '',
                        'time': {'mtime': 2 if self.changed else 1, 'ctime': 1, 'atime': self.counter},
                    },
                }],
            }
        elif method == 'list':
            path = json.loads(p['folder_path'])
            real = self.real_path(path, name)
            data = {
                'total': 1, 'files': [{
                    'path': path+'/a.txt', 'name': 'a.txt', 'isdir': False,
                    'additional': {'real_path': real+'/a.txt', 'size': len((name+' secret').encode()), 'time': {'mtime': 1}, 'mount_point_type': ''},
                }],
            }
        else:
            raise AssertionError((api, method))
        return httpx.Response(200, json={'success': True, 'data': data})


class NASFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.dsm = FakeDSM()
        env = patch.dict(os.environ, {'COMPANY_AGENT_NAS_URL': 'https://nas.example:5001'})
        env.start()
        self.addCleanup(env.stop)

        def factory(*a):
            return NASClient(*a, transport=httpx.MockTransport(self.dsm.handler))
        self.factory = factory
        patched = patch('company_agent.services.files.nas.NASClient', factory)
        patched.start()
        self.addCleanup(patched.stop)
        self.vault = NASVault(self.store)

    def bind(self, owner='alice', account=None):
        account = account or owner
        return self.vault.bind(owner, account, account+'-password')

    def remote(self, owner='alice'):
        return NASAccess(self.vault, owner, self.vault.status(owner)['revision'], 20000)

    def nas_root(self, remote, label): return next(x['name'] for x in remote.visible_roots() if x['label'] == label)


class NASTests(NASFixture):
    def test_cookie_session_covers_listing_download_and_logout(self):
        client = self.factory('https://nas.example', 'alice', 'alice-password')
        with client:
            self.assertEqual(len(client.shares()), 2)
            self.assertEqual(client.download('/home/a.txt', 20000), b'alice secret')
        self.assertEqual(self.dsm.cookie_sids, ['session-1']*4)
        self.assertFalse(self.dsm.sessions)
        self.assertFalse(client.http.cookies)
        self.assertIsNone(client.sid)

    def test_invalid_session_propagates_without_relogin(self):
        self.dsm.session_error = 119
        client = self.factory('https://nas.example', 'alice', 'alice-password')
        with self.assertRaisesRegex(ValueError, '119'):
            with client:
                pass
        self.assertEqual([c['method'] for c in self.dsm.calls], ['login', 'get', 'logout'])
        self.assertFalse(client.http.cookies)
        self.assertFalse(self.dsm.sessions)

    def test_parallel_same_account_sessions_are_serialized(self):
        revision = self.bind()['revision']
        gate = threading.Barrier(4)

        def operation(_):
            gate.wait(timeout=5)
            # Distinct vault instances must share the database lock.
            with NASVault(self.store).session('alice', revision) as client:
                self.assertEqual(len(self.dsm.sessions), 1)
                time.sleep(.025)
                return client.shares()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            self.assertTrue(all(pool.map(operation, range(4))))
        self.assertFalse(self.dsm.sessions)

    def test_account_lock_is_independent_and_released_on_error(self):
        gate = threading.Barrier(2)

        def operation(account):
            with self.vault.account_operation('https://nas.example:5001', account):
                gate.wait(timeout=5)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(operation, ['alice', 'bob']))
        with self.assertRaisesRegex(RuntimeError, 'test'):
            with self.vault.account_operation('https://nas.example:5001', 'alice'):
                raise RuntimeError('test')
        with self.vault.account_operation('https://nas.example:5001', 'alice'):
            pass

    def test_separate_users_home_and_shares(self):
        for user in ('alice', 'bob'):
            self.bind(user)
        a, b = self.remote(), self.remote('bob')
        ar = self.nas_root(a, 'home')
        br = self.nas_root(b, 'home')
        self.assertNotEqual(ar, br)
        self.assertEqual(a.read(ar, 'a.txt')[0], b'alice secret')
        self.assertEqual(b.read(br, 'a.txt')[0], b'bob secret')
        self.assertEqual({r['label'] for r in a.visible_roots()}, {'home', 'shared'})
        with self.assertRaises(ValueError):
            a.read(br, 'a.txt')
        self.assertFalse(self.dsm.sessions)

    def test_admin_login_allowed_and_session_logged_out(self):
        state = self.bind('alice', 'admin')
        self.assertTrue(state['bound'])
        self.assertTrue(state['usable'])
        self.assertEqual({s['name'] for s in state['shares']}, {'home', 'homes', 'shared'})
        self.assertFalse(self.dsm.sessions)

    def test_admin_binding_keeps_home_boundary_and_read_only(self):
        self.bind('alice', 'admin')
        remote = self.remote()
        root = self.nas_root(remote, 'home')
        self.assertEqual(remote.read(root, 'a.txt')[0], b'admin secret')
        with self.assertRaises(ValueError):
            remote.read(root, '../homes/bob/a.txt')
        with self.assertRaises(ValueError):
            remote.allowed(root, write=True)
        self.assertFalse(self.dsm.sessions)

    def test_homes_readable_when_nas_grants_it_and_revocation_still_denied(self):
        # Permission comes from DSM's share list, not the account's role.
        self.dsm.allow_homes = True
        self.bind()
        remote = self.remote()
        root = self.nas_root(remote, 'homes')
        self.assertEqual(remote.read(root, 'bob/a.txt')[0], b'alice secret')
        downloads = [c for c in self.dsm.calls if c['api'] == 'SYNO.FileStation.Download']
        self.assertEqual(json.loads(downloads[-1]['path']), ['/homes/bob/a.txt'])
        self.assertEqual(remote.scan(root)[0][0]['path'], 'a.txt')
        with self.assertRaises(ValueError):
            remote.allowed(root, write=True)
        self.dsm.hide_homes = True
        with self.assertRaises(ValueError):
            remote.read(root, 'bob/a.txt')

    def test_authorized_share_physically_inside_homes_is_allowed(self):
        self.dsm.shared_in_homes = True
        self.bind()
        remote = self.remote()
        root = self.nas_root(remote, 'shared')
        self.assertEqual(remote.read(root, 'a.txt')[0], b'alice secret')

    def test_same_nas_account_not_shared_between_owners(self):
        self.bind()
        with self.assertRaisesRegex(ValueError, '另一位'):
            self.bind('bob', 'alice')
        self.assertFalse(self.vault.status('bob')['bound'])

    def test_credential_ciphertext_owner_binding_and_key_permissions(self):
        state = self.bind()
        with self.store.db() as db:
            row = db.execute('SELECT ciphertext FROM nas_bindings WHERE owner=%s', ('alice',)).fetchone()
            self.assertNotIn(b'alice-password', row[0])
            db.execute(
                'INSERT INTO nas_bindings(owner,account_hash,ciphertext,revision,updated,usable,binding_id) VALUES (%s,%s,%s,%s,%s,TRUE,%s)',
                ('bob', 'other', row[0], state['revision'], 1, state['binding_id']),
            )
        with self.assertRaises(InvalidTag):
            self.vault.credentials('bob', state['revision'])
        self.assertEqual((self.private/'nas.key').stat().st_mode & 0o777, 0o600)
        self.assertNotIn('password', str(self.vault.status('alice')))

    def test_missing_key_fails_closed(self):
        state = self.bind()
        (self.private/'nas.key').unlink()
        with self.assertRaises(FileNotFoundError):
            self.vault.credentials('alice', state['revision'])
        self.assertFalse((self.private/'nas.key').exists())

    def test_unlink_invalidates_old_revision(self):
        self.bind()
        remote = self.remote()
        root = self.nas_root(remote, 'home')
        self.vault.unlink('alice')
        with self.assertRaises(ValueError):
            remote.read(root, 'a.txt')
        self.assertFalse(self.vault.status('alice')['bound'])

    def test_unlink_while_login_pending_does_not_rebind(self):
        self.dsm.on_info = lambda: self.vault.unlink('alice')
        with self.assertRaisesRegex(ValueError, '取消'):
            self.bind()
        self.assertFalse(self.vault.status('alice')['bound'])

    def test_traversal_homes_write_and_real_path_escape_denied(self):
        self.bind()
        remote = self.remote()
        root = self.nas_root(remote, 'home')
        for path in ('../bob/a.txt', '/homes/bob/a.txt', 'x/../../a.txt', 'x\\a.txt', '@eaDir/a.txt'):
            with self.assertRaises(ValueError):
                remote.read(root, path)
        with self.assertRaises(ValueError):
            remote.allowed(root, write=True)
        self.dsm.escape = True
        with self.assertRaisesRegex(ValueError, '一般檔案'):
            remote.read(root, 'a.txt')
        self.assertFalse(any(c['api'] == 'SYNO.FileStation.Download' for c in self.dsm.calls))

    def test_revoked_share_and_file_download_permission(self):
        self.bind()
        remote = self.remote()
        root = self.nas_root(remote, 'shared')
        self.dsm.no_shared = True
        with self.assertRaises(ValueError):
            remote.read(root, 'a.txt')
        self.dsm.no_shared = False
        self.dsm.deny = True
        with self.assertRaisesRegex(ValueError, '拒絕'):
            remote.read(root, 'a.txt')

    def test_password_failure_propagates_without_changing_binding(self):
        self.bind()
        remote = self.remote()
        self.dsm.bad_password = True
        with self.assertRaises(PermissionError):
            remote.visible_roots()
        self.assertTrue(self.vault.status('alice')['usable'])
        count = len(self.dsm.calls)
        with self.assertRaises(PermissionError):
            remote.visible_roots()
        self.assertGreater(len(self.dsm.calls), count)

    def test_rate_limit_precedes_second_login(self):
        self.bind()
        count = len(self.dsm.calls)
        with self.assertRaisesRegex(ValueError, '一分鐘'):
            self.bind()
        self.assertEqual(count, len(self.dsm.calls))

    def test_https_no_redirect_and_no_mutating_api(self):
        with self.assertRaises(ValueError):
            NASClient('http://nas.example', 'alice', 'pw')
        redirect = NASClient(
            'https://nas.example', 'alice', 'pw', transport=httpx.MockTransport(
                lambda r: httpx.Response(302, headers={'location': 'https://evil.example'}),
            ),
        )
        with self.assertRaises(httpx.HTTPStatusError):
            with redirect:
                pass
        client = self.factory('https://nas.example', 'alice', 'alice-password')
        with client:
            with self.assertRaises(ValueError):
                client._json('SYNO.FileStation.Delete', 'start', path='/home')
        self.assertTrue(all('passwd' not in c or c['api'] == 'SYNO.API.Auth' for c in self.dsm.calls))

    def test_remote_read_uses_existing_document_parser_and_source_hash(self):
        state = self.bind()
        self.config.nas_binding_revision = state['revision']
        remote = self.remote()
        root = self.nas_root(remote, 'home')
        box = ToolBox(self.config, self.store, 'alice', self.job)
        result = box.read_file(Read(root=root, path='a.txt'))
        self.assertEqual(result['sha256'], hashlib.sha256(b'alice secret').hexdigest())
        self.assertIn('alice secret', str(result['blocks']))
        listing, _ = box.access.scan(root)
        self.assertEqual(listing[0]['path'], 'a.txt')
        self.assertNotIn('password', str(result))

    def test_jobs_capture_nas_revision_and_reject_rebound_followup(self):
        state = self.bind()
        queue = WorkQueue(self.private, self.base/'uploads')
        key = queue.submit('alice', 'oidc-alice', 'chat', 'm', 'read NAS', [])
        self.assertEqual(queue.get('alice', key)['payload']['nas_binding_revision'], state['revision'])
        queue.update(key, status='done')
        self.vault.unlink('alice')
        with self.assertRaisesRegex(ValueError, 'NAS'):
            queue.submit('alice', 'oidc-alice', 'chat', 'm2', 'continue', [], [key])

    def test_no_binding_keeps_local_files_available(self):
        self.file()
        access = file_access(self.config, self.store, 'alice')
        self.assertEqual(access.read('project', 'a.txt')[0], b'hello')
        self.assertFalse(self.dsm.calls)


class NASGraphTests(unittest.IsolatedAsyncioTestCase, NASFixture):
    async def test_official_graph_reads_own_nas_without_credentials_in_model(self):
        verified, response = _fixture_test_user_billing.attributes('verified', 'response')
        state = self.bind()
        root = self.nas_root(self.remote(), 'home')
        self.config.nas_binding_revision = state['revision']
        self.config.structured_completion = True
        self.config.require_user_billing = True
        script = [
            ('delegate_knowledge', {'task': 'Read own NAS home a.txt'}),
            ('read_file', {'root': root, 'path': 'a.txt'}),
            ('complete_assignment', {'outcome': 'completed', 'summary': '已讀取本人文件', 'artifact_ids': []}),
            ('complete_job', {'outcome': 'completed', 'summary': '已讀取 NAS 文件', 'artifact_ids': []}),
        ]
        requests = []

        def model_api(request):
            value = json.loads(request.content)
            requests.append(value)
            name, args = script.pop(0)
            return response(name, args)
        model = _models_imports.gateway_for_verified_user(
            verified(), 'https://gateway.example/v1',
            'key', 'model', httpx.MockTransport(model_api),
        )
        try:
            result = await DocumentAgent(self.config, model).run('alice', '閱讀 NAS 本人文件 a.txt')
        finally:
            await _models_imports.close_model(model)
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(result['sources'][0]['root'], root)
        self.assertTrue(all(r['user'] == 'oidc-alice' for r in requests))
        serialized = json.dumps(requests)
        self.assertNotIn('alice-password', serialized)
        self.assertNotIn('session-', serialized)
        self.assertIn('alice secret', serialized)
        self.assertFalse(self.dsm.sessions)
