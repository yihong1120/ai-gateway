"""Real PostgreSQL bindings and snapshots; synthetic DSM accounts, no live NAS."""
from __future__ import annotations

import unittest

from company_agent.api.queue import WorkQueue
from company_agent.runtime.storage import Store
from company_agent.services.files.nas import file_access
from company_agent.tests.test_nas import NASFixture, NASAccess


class MultipleNASTests(NASFixture):
    def reset_limit(self):
        with self.store.db() as db:
            db.execute('UPDATE nas_login_limits SET next_at=0')

    def bind_second(self, account='team', label='工程部'):
        self.reset_limit()
        return self.vault.bind('alice', account, account+'-password', label=label)

    def multi_access(self, revisions):
        settings = self.config.model_copy(update={'nas_binding_revisions': revisions})
        return file_access(settings, self.store, 'alice')

    def test_two_accounts_have_distinct_roots_and_read_their_own_home(self):
        one, two = self.bind(), self.bind_second()
        self.assertEqual(len(self.vault.bindings('alice')), 2)
        access = self.multi_access([one['revision'], two['revision']])
        roots = [r for r in access.visible_roots() if r.get('source') == 'NAS']
        homes = [r for r in roots if r['label'].endswith(' / home')]
        self.assertEqual(len(homes), 2)
        self.assertEqual(len({r['name'] for r in homes}), 2)
        contents = {r['connection']: access.read(r['name'], 'a.txt')[0] for r in homes}
        self.assertEqual(contents, {'alice': b'alice secret', '工程部': b'team secret'})
        for root in homes:
            with self.assertRaises(ValueError):
                access.allowed(root['name'], write=True)
        self.assertFalse(self.dsm.sessions)
        self.assertNotIn('password', str(self.vault.bindings('alice')))

    def test_update_one_binding_preserves_identity_and_other_account(self):
        one, two = self.bind(), self.bind_second()
        self.reset_limit()
        changed = self.vault.bind('alice', 'other', 'other-password', binding_id=one['binding_id'], label='新帳號')
        self.assertEqual(changed['binding_id'], one['binding_id'])
        self.assertNotEqual(changed['revision'], one['revision'])
        self.assertEqual(len(self.vault.bindings('alice')), 2)
        with self.assertRaises(ValueError):
            self.vault.credentials('alice', one['revision'])
        self.assertEqual(self.vault.credentials('alice', two['revision'])['account'], 'team')
        self.assertEqual(self.vault.status('alice', one['binding_id'])['revision'], changed['revision'])

    def test_selected_unlink_keeps_other_and_ambiguous_legacy_unlink_is_rejected(self):
        one, two = self.bind(), self.bind_second()
        with self.assertRaisesRegex(ValueError, '選擇'):
            self.vault.unlink('alice')
        self.vault.unlink('alice', one['binding_id'])
        with self.assertRaises(ValueError):
            self.vault.credentials('alice', one['revision'])
        remote = NASAccess(self.vault, 'alice', two['revision'], 20000)
        self.assertEqual(remote.read(self.nas_root(remote, 'home'), 'a.txt')[0], b'team secret')
        self.assertEqual(len(self.vault.bindings('alice')), 1)

    def test_foreign_binding_ids_do_not_authenticate_or_unlink(self):
        one = self.bind()
        count = len(self.dsm.calls)
        with self.assertRaises(ValueError):
            self.vault.bind('bob', 'bob', 'bob-password', binding_id=one['binding_id'])
        with self.assertRaises(ValueError):
            self.vault.unlink('bob', one['binding_id'])
        with self.assertRaises(ValueError):
            self.vault.status('bob', one['binding_id'])
        with self.assertRaises(ValueError):
            self.vault.credentials('bob', one['revision'])
        self.assertEqual(len(self.dsm.calls), count)
        self.assertTrue(self.vault.status('alice')['bound'])

    def test_rebinding_same_account_does_not_create_duplicate(self):
        one = self.bind()
        self.reset_limit()
        two = self.vault.bind('alice', 'alice', 'alice-password', label='個人')
        self.assertEqual(one['binding_id'], two['binding_id'])
        self.assertEqual(len(self.vault.bindings('alice')), 1)
        self.assertEqual(two['bindings'][0]['label'], '個人')

    def test_new_job_snapshots_all_accounts_and_followup_does_not_expand_scope(self):
        one, two = self.bind(), self.bind_second()
        queue = WorkQueue(self.private, self.base/'uploads', store=self.store)
        first = queue.submit('alice', 'oidc-alice', 'chat', 'one', 'read both', [])
        revisions = sorted([one['revision'], two['revision']])
        self.assertEqual(queue.get('alice', first)['payload']['nas_binding_revisions'], revisions)
        queue.update(first, status='done')
        three = self.bind_second('third', '第三組')
        follow = queue.submit('alice', 'oidc-alice', 'chat', 'two', 'continue', [], [first])
        self.assertEqual(queue.get('alice', follow)['payload']['nas_binding_revisions'], revisions)
        new = queue.submit('alice', 'oidc-alice', 'other-chat', 'new', 'read all', [])
        self.assertEqual(set(queue.get('alice', new)['payload']['nas_binding_revisions']), {*revisions, three['revision']})
        self.vault.unlink('alice', one['binding_id'])
        with self.assertRaisesRegex(ValueError, 'NAS'):
            queue.submit('alice', 'oidc-alice', 'chat', 'three', 'continue after unlink', [], [first])
        self.assertEqual(queue.submit('alice', 'oidc-alice', 'chat', 'one', 'read both', []), first)

    def test_job_cannot_use_new_account_root_added_after_snapshot(self):
        one = self.bind()
        access = self.multi_access([one['revision']])
        two = self.bind_second()
        other = NASAccess(self.vault, 'alice', two['revision'], 20000)
        with self.assertRaises(ValueError):
            access.read(self.nas_root(other, 'home'), 'a.txt')

    def test_local_only_followup_does_not_gain_newly_bound_accounts(self):
        queue = WorkQueue(self.private, self.base/'uploads', store=self.store)
        old = queue.submit('alice', 'oidc-alice', 'chat', 'one', 'read uploads', [])
        queue.update(old, status='done')
        self.bind()
        follow = queue.submit('alice', 'oidc-alice', 'chat', 'two', 'continue', [], [old])
        self.assertEqual(queue.get('alice', follow)['payload']['nas_binding_revisions'], [])

    def test_regeneration_does_not_merge_different_multi_account_scopes(self):
        self.bind()
        self.bind_second()
        queue = WorkQueue(self.private, self.base/'uploads', store=self.store)
        first = queue.submit('alice', 'oidc-alice', 'chat', 'one', 'read NAS', [])
        self.bind_second('third')
        second = queue.submit('alice', 'oidc-alice', 'chat', 'two', 'read NAS', [])
        self.assertNotEqual(first, second)
        self.assertEqual(len(queue.get('alice', second)['payload']['nas_binding_revisions']), 3)

    def test_version_four_migration_preserves_legacy_ciphertext_and_revision(self):
        one = self.bind()
        with self.store.db() as db:
            before = db.execute('SELECT ciphertext,revision FROM nas_bindings').fetchone()
            db.execute('DELETE FROM schema_migrations WHERE version>=5')
            db.execute('DROP TABLE mcp_grants,mcp_workspaces,mcp_operations,mcp_request_limits')
            db.execute('DROP INDEX nas_bindings_owner_revision')
            db.execute('ALTER TABLE nas_bindings DROP CONSTRAINT nas_bindings_pkey')
            db.execute('ALTER TABLE nas_bindings DROP COLUMN binding_id')
            db.execute('ALTER TABLE nas_bindings ADD PRIMARY KEY(owner)')
        restored = Store(self.private)
        with restored.db() as db:
            after = db.execute('SELECT ciphertext,revision,binding_id FROM nas_bindings').fetchone()
        self.assertEqual(after[:2], before)
        self.assertEqual(after[2], one['revision'])
        remote = NASAccess(self.vault, 'alice', one['revision'], 20000)
        self.assertEqual(remote.read(self.nas_root(remote, 'home'), 'a.txt')[0], b'alice secret')
        self.assertEqual(len(self.bind_second()['bindings']), 2)

    def test_revoke_one_does_not_invalidate_the_other_root(self):
        one, two = self.bind(), self.bind_second()
        access = self.multi_access([one['revision'], two['revision']])
        roots = [r for r in access.visible_roots() if r.get('source') == 'NAS' and r['label'].endswith(' / home')]
        self.vault.unlink('alice', one['binding_id'])
        for root in roots:
            if root['connection'] == 'alice':
                with self.assertRaises(ValueError):
                    access.read(root['name'], 'a.txt')
            else:
                self.assertEqual(access.read(root['name'], 'a.txt')[0], b'team secret')

    def test_revision_prefix_collision_fails_closed(self):
        one, two = self.bind(), self.bind_second()
        access = self.multi_access([one['revision'], two['revision']])
        access.remotes[1].revision = one['revision'][:8] + two['revision'][8:]
        with self.assertRaisesRegex(ValueError, '不唯一'):
            access.backend('nas_' + one['revision'][:8] + '_1234')


class MultipleNASGraphTests(unittest.IsolatedAsyncioTestCase, NASFixture):
    async def test_one_billed_graph_reads_two_accounts_without_credentials_in_model(self):
        import json
        import httpx
        from company_agent.runtime.execution import DocumentAgent
        from company_agent.runtime.models import gateway_for_verified_user, close_model
        from company_agent.tests.test_user_billing import verified, response
        one = self.bind()
        with self.store.db() as db:
            db.execute('UPDATE nas_login_limits SET next_at=0')
        two = self.vault.bind('alice', 'team', 'team-password', label='工程部')
        remotes = [NASAccess(self.vault, 'alice', state['revision'], 20000) for state in (one, two)]
        roots = [self.nas_root(remote, 'home') for remote in remotes]
        self.config.nas_binding_revisions = [one['revision'], two['revision']]
        self.config.structured_completion = True
        self.config.require_user_billing = True
        script = [
            ('delegate_knowledge', {'task': 'Read home a.txt in both NAS connections'}),
            *[('read_file', {'root': root, 'path': 'a.txt'}) for root in roots],
            ('complete_assignment', {'outcome': 'completed', 'summary': '已讀取兩組 NAS 文件', 'artifact_ids': []}),
            ('complete_job', {'outcome': 'completed', 'summary': '已讀取兩組 NAS 文件', 'artifact_ids': []}),
        ]
        requests = []
        def model_api(request):
            requests.append(json.loads(request.content))
            name, args = script.pop(0)
            return response(name, args)
        model = gateway_for_verified_user(verified(), 'https://gateway.example/v1', 'key', 'model', httpx.MockTransport(model_api))
        try:
            result = await DocumentAgent(self.config, model).run('alice', '閱讀兩組 NAS 的 home/a.txt')
        finally:
            await close_model(model)
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual({s['root'] for s in result['sources']}, set(roots))
        self.assertEqual(len(script), 0)
        self.assertTrue(all(r['user'] == 'oidc-alice' for r in requests))
        serialized = json.dumps(requests)
        for secret in ('alice-password', 'team-password', 'session-'):
            self.assertNotIn(secret, serialized)
        self.assertIn('alice secret', serialized)
        self.assertIn('team secret', serialized)
        self.assertFalse(self.dsm.sessions)
