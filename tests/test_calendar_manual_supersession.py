"""Managed AI provenance, manual clock priority, encrypted backup and no replay."""
from __future__ import annotations

import asyncio
import base64
import copy
import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from examples.official_document_workflow.application.automation import AutomaticAccount
from examples.official_document_workflow.infrastructure.eip.calendar_supersession import (
    reconcile,
)
from examples.official_document_workflow.infrastructure.eip.calendar_sync import (
    verified_events,
    write_events,
)
from examples.official_document_workflow.infrastructure.eip.netask import (
    Netask,
    import_line,
    js_escape,
)
from examples.official_document_workflow.infrastructure.storage.calendar_managed import (
    ManagedCalendar,
    digest,
)
from examples.official_document_workflow.infrastructure.storage.state import Journal
from tests.eip_postgres import EIPPostgresTestCase
from tests.test_calendar_deduplication_mechanism import manual_pair
from tests.test_eip_calendar_reconciliation import EVENT, USER, fixture
from tests.test_eip_connector import remote

KEEP = '2026-09-10 09:00:00.001;2002'


class ManualSupersessionTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.source, _, _, ai, _ = fixture()
        incoming, human = manual_pair()
        self.source['Description'] = incoming['description']
        self.line = import_line(remote(self.source), USER)
        self.fields = self.line.split(',')
        ai[8], ai[14] = self.fields[0], self.fields[19]
        manual = copy.deepcopy(ai)
        manual[0], manual[2] = KEEP, '18'
        manual[8], manual[14] = js_escape(human['title']), js_escape(human['description'])
        manual[11], manual[12] = '540', '600'
        self.ai, self.manual = [ai, [0]], [manual, [0]]
        self.details = {EVENT: self.ai}
        self.journal = Journal()
        self.history = ManagedCalendar(self._workflow.store)
        self.key = self.journal.key(USER, {'import': self.line})
        self.client = Netask.__new__(Netask)
        self.client.user_id = USER
        self.client.calendar_detail = AsyncMock(side_effect=lambda event_id: copy.deepcopy(self.details[event_id]))
        self.deletes = []
        self.client.rpc = AsyncMock(side_effect=self.rpc)
        self.client.download = AsyncMock(side_effect=lambda *a: io.BytesIO(b'%PDF-test-backup'))
        self.client.commit = AsyncMock(return_value={'event_id': EVENT, 'detail_verified': True})

    async def rpc(self, method, **kwargs):
        if method == 'Delete':
            self.deletes.append(kwargs['event_id'])
            del self.details[kwargs['event_id']]
            return [[0, '']]
        self.assertEqual(method, 'List')
        rows = [[0, '']]
        for event_id, detail in self.details.items():
            r = detail[0]; row = ['']*21
            for k, value in {0:r[2], 2:event_id, 6:r[8], 7:r[9], 8:r[10], 9:r[11], 10:r[12]}.items(): row[k]=value
            rows.append(row)
        return rows

    async def registered(self, *, attachments=False):
        if attachments:
            self.ai[1] = [1]
            self.ai.append(['17', '765', '會勘.pdf', '/fo/v/100', '100', '', ''])
        self.journal.reserve_event(self.key)
        await self.client.begin_calendar_creation(self.line, self.key)
        await self.client.record_calendar_creation(self.line, self.key, {'event_id':EVENT,'detail_verified':True})
        self.journal.done(self.key, receipt={'event_id':EVENT,'detail_verified':True})
        self.details[KEEP] = self.manual

    def row(self):
        with self._workflow.store.db() as db:
            return db.execute('SELECT * FROM calendar_managed_events WHERE identity=%s AND event_id=%s', (USER, EVENT)).fetchone()

    async def run_cleanup(self, **kwargs):
        return await reconcile(self.client, self.history, self.journal, **kwargs)

    async def test_manual_explicit_different_clock_wins_and_only_ai_is_deleted(self):
        await self.registered()
        original = copy.deepcopy(self.manual)
        result = await self.run_cleanup()
        self.assertEqual(result, {'checked':1,'removed':1,'needs_review':0})
        self.assertEqual(self.deletes, [EVENT])
        self.assertEqual(self.details[KEEP], original)
        self.assertEqual(self.row()['state'], 'deleted')
        self.assertEqual(self.journal.status(self.key), 'done')

    async def test_completed_manual_event_is_retained_with_its_progress(self):
        await self.registered()
        self.manual[0][17]='100'
        self.assertEqual((await self.run_cleanup())['removed'],1)
        self.assertEqual(self.details[KEEP][0][17],'100')

    async def test_later_native_upload_retaining_ai_title_overrides_managed_ai(self):
        await self.registered()
        later = '2026-09-10 12:00:00.001;2002'
        self.manual[0][0] = later
        self.manual[0][8], self.manual[0][14] = self.ai[0][8], self.ai[0][14]
        del self.details[KEEP]; self.details[later] = self.manual
        self.assertEqual((await self.run_cleanup())['removed'], 1)
        self.assertIn(later,self.details)
        self.assertEqual(self.details[later][0][11], '540')

    async def test_ai_marked_legacy_upload_with_unproven_origin_is_not_guessed_manual(self):
        await self.registered()
        self.manual[0][8], self.manual[0][14] = self.ai[0][8], self.ai[0][14]
        self.assertEqual((await self.run_cleanup())['needs_review'],1)
        self.assertFalse(self.deletes)

    async def test_unknown_creation_intent_cannot_masquerade_as_later_manual_upload(self):
        await self.registered()
        later = '2026-09-10 12:00:00.001;2002'
        self.manual[0][0]=later
        self.manual[0][8], self.manual[0][14]=self.ai[0][8],self.ai[0][14]
        del self.details[KEEP]; self.details[later]=self.manual
        self.history.begin('18','unconfirmed-write')
        self.assertEqual((await self.run_cleanup())['needs_review'],1)
        self.assertFalse(self.deletes)

    async def test_actual_creation_is_tracked_before_replay_can_clean_it(self):
        self.client.calendar_import_matches=AsyncMock(return_value=[{'status':'new'}])
        events=verified_events([self.source],[remote(self.source)],USER,self.journal)
        self.assertEqual(await write_events(self.client,events,self.journal),{'created':1,'skipped':0})
        self.assertEqual(self.row()['state'],'ready')
        with self._workflow.store.db() as db:
            state=db.execute('SELECT state FROM calendar_creation_intents WHERE identity=%s AND source_key=%s',(USER,events[0]['key'])).fetchone()['state']
        self.assertEqual(state,'created')

    async def test_creation_failure_keeps_unknown_intent_and_never_registers_success(self):
        self.client.calendar_import_matches=AsyncMock(return_value=[{'status':'new'}])
        self.client.commit.side_effect=TimeoutError('lost create')
        events=verified_events([self.source],[remote(self.source)],USER,self.journal)
        with self.assertRaises(TimeoutError): await write_events(self.client,events,self.journal)
        self.assertTrue(self.history.unresolved_creation(USER))
        self.assertIsNone(self.row())
        self.assertEqual(self.journal.status(events[0]['key']),'unknown')

    async def test_adoption_never_registers_an_external_event_as_our_creation(self):
        self.details={KEEP:self.manual}
        events=verified_events([self.source],[remote(self.source)],USER,self.journal)
        self.assertEqual(await write_events(self.client,events,self.journal),{'created':0,'skipped':1})
        self.assertIsNone(self.row())
        self.client.commit.assert_not_awaited()

    async def test_unexpected_human_change_is_not_promoted_as_an_automatic_edit(self):
        await self.registered()
        before=copy.deepcopy(self.ai);before[0][15]='Ru17,Ru99'
        self.history.advance(USER,before,self.ai)
        self.assertEqual(self.row()['state'],'review')
        self.assertFalse(self.deletes)

    async def test_unknown_import_alias_blocks_removal_even_when_primary_receipt_done(self):
        await self.registered()
        with self._workflow.store.db() as db:
            row=self.row()
            value=self._workflow.store.decode(self.history.scope(USER,EVENT),row['payload'])
            value['aliases']=['peer-unknown']
            db.execute('UPDATE calendar_managed_events SET payload=%s WHERE identity=%s AND event_id=%s',
                       (self._workflow.store.encode(self.history.scope(USER,EVENT),value),USER,EVENT))
        self.journal.reserve_event('peer-unknown')
        self.assertEqual((await self.run_cleanup())['needs_review'],1)
        self.assertFalse(self.deletes)

    async def test_scope_cannot_be_moved_to_another_account_by_ciphertext_swap(self):
        await self.registered()
        row=self.row()
        with self.assertRaises(ValueError): self._workflow.store.decode(self.history.scope('19',EVENT),row['payload'])

    async def test_files_and_both_events_backed_up_encrypted_before_delete(self):
        await self.registered(attachments=True)
        async def delete(method, **kwargs):
            if method == 'Delete':
                row = self.row()
                self.assertEqual(row['state'], 'deleting')
                self.assertNotIn('測試工程'.encode(), bytes(row['backup']))
                backup = self._workflow.store.decode(self.history.scope(USER, EVENT), row['backup'])
                self.assertEqual(base64.b64decode(backup['files'][0]['data_base64']), b'%PDF-test-backup')
                self.assertEqual(backup['retained'][0][0], KEEP)
                self.assertEqual(backup['removed'][0][0], EVENT)
            return await self.rpc(method, **kwargs)
        self.client.rpc.side_effect = delete
        self.assertEqual((await self.run_cleanup())['removed'], 1)

    async def test_same_title_without_creation_provenance_is_never_removed(self):
        self.details[KEEP] = self.manual
        self.journal.reserve_event(self.key)
        self.journal.done(self.key, receipt={'event_id':EVENT,'detail_verified':True})
        self.assertEqual((await self.run_cleanup())['checked'], 0)
        self.assertFalse(self.deletes)

    async def test_different_work_source_assignment_or_occurrence_does_not_delete(self):
        for change in ('work','source','assignment','date'):
            with self.subTest(change=change):
                self.details = {EVENT:self.ai, KEEP:copy.deepcopy(self.manual)}
                self.history = ManagedCalendar(self._workflow.store)
                if self.row() is None: await self.registered()
                else: self.history.state(USER, EVENT, 'ready')
                item = self.details[KEEP][0]
                if change=='work': item[8]=js_escape('「測試工程」材料品質檢驗')
                elif change=='source': item[14]=js_escape('主旨：另一份公文。\n說明：不同來源。')
                elif change=='assignment':
                    self.details[EVENT][0][40] = '甲單位'; item[40]='乙單位'
                else: item[9]='new Date(2026, 8, 11, 0, 0, 0)'
                await self.run_cleanup(keys=[self.key])
                self.assertFalse(self.deletes)

    async def test_manual_or_ai_recurring_series_are_not_deleted(self):
        await self.registered()
        self.details[KEEP][0][22] = '1'
        self.assertEqual((await self.run_cleanup())['needs_review'],1)
        self.assertFalse(self.deletes)

    async def test_human_edit_to_ai_after_creation_stops_cleanup(self):
        await self.registered()
        self.ai[0][14] += js_escape('\n人工加註，請保留')
        self.assertEqual((await self.run_cleanup())['needs_review'], 1)
        self.assertEqual(self.row()['state'], 'review')
        self.assertFalse(self.deletes)

    async def test_multiple_manual_clocks_are_not_arbitrarily_chosen(self):
        await self.registered()
        other = copy.deepcopy(self.manual); other[0][0]='2026-09-10 08:00:00.001;3003';other[0][11]='480'
        self.details[other[0][0]]=other
        self.assertEqual((await self.run_cleanup())['needs_review'], 1)
        self.assertFalse(self.deletes)

    async def test_shared_ai_owned_by_another_account_is_not_deleted(self):
        await self.registered()
        self.ai[0][2] = '18'
        self.assertEqual((await self.run_cleanup())['needs_review'], 1)
        self.assertFalse(self.deletes)

    async def test_unrelated_account_cannot_claim_another_accounts_records(self):
        await self.registered()
        self.client.user_id='19'
        self.assertEqual((await self.run_cleanup())['checked'],0)
        self.client.rpc.assert_not_awaited()

    async def test_binding_revoked_during_backup_prevents_delete(self):
        await self.registered(attachments=True)
        active = True
        async def download(*args):
            nonlocal active
            active = False
            return io.BytesIO(b'%PDF-test-backup')
        self.client.download.side_effect=download
        with self.assertRaises(ValueError): await self.run_cleanup(allowed=lambda:active)
        self.assertFalse(self.deletes)

    async def test_failed_backup_never_reserves_or_deletes(self):
        await self.registered(attachments=True)
        self.client.download.side_effect=OSError('unavailable')
        with self.assertRaises(OSError): await self.run_cleanup()
        self.assertFalse(self.deletes)
        self.assertEqual(self.row()['state'],'ready')
        self.assertIsNone(self.row()['backup'])

    async def test_human_edit_after_backup_prevents_delete(self):
        await self.registered()
        save = self.history.backup
        def backup(*args):
            save(*args)
            self.details[KEEP][0][11] = '600'
        self.history.backup=backup
        self.assertEqual((await self.run_cleanup())['needs_review'],1)
        self.assertFalse(self.deletes)

    async def test_delete_timeout_never_replays_even_after_restart(self):
        await self.registered()
        async def rpc(method, **kwargs):
            if method=='Delete': raise TimeoutError('lost response')
            return await self.rpc(method, **kwargs)
        self.client.rpc.side_effect=rpc
        with self.assertRaises(TimeoutError): await self.run_cleanup()
        self.assertEqual(self.row()['state'],'deleting')
        self.client.rpc.reset_mock()
        await reconcile(self.client,ManagedCalendar(self._workflow.store),Journal())
        self.client.rpc.assert_not_awaited()
        self.assertEqual(self.journal.status(self.key),'done')

    async def test_delete_success_without_verified_absence_stays_unknown(self):
        await self.registered()
        async def rpc(method, **kwargs):
            if method=='Delete': return [[0,'']]
            return await self.rpc(method, **kwargs)
        self.client.rpc.side_effect=rpc
        with self.assertRaises(ValueError): await self.run_cleanup()
        self.assertEqual(self.row()['state'],'deleting')
        self.assertIsNotNone(self.row()['backup'])

    async def test_concurrent_workers_delete_once(self):
        await self.registered()
        results = await asyncio.gather(self.run_cleanup(),reconcile(self.client,ManagedCalendar(self._workflow.store),Journal()))
        self.assertEqual(sum(r['removed'] for r in results),1)
        self.assertEqual(self.deletes,[EVENT])

    async def test_replay_of_old_csv_never_recreates_deleted_ai(self):
        await self.registered()
        events=verified_events([self.source],[remote(self.source)],USER,self.journal)
        result=await write_events(self.client,events,self.journal)
        self.assertEqual(result,{'created':0,'skipped':1})
        self.client.commit.assert_not_awaited()
        self.assertEqual(self.deletes,[EVENT])

    async def test_regenerated_csv_alias_still_finds_origin_for_manual_cleanup(self):
        real_matches=self.client.calendar_import_matches
        self.client.calendar_import_matches=AsyncMock(return_value=[{'status':'new'}])
        events=verified_events([self.source],[remote(self.source)],USER,self.journal)
        await write_events(self.client,events,self.journal)
        self.client.calendar_import_matches=real_matches
        self.details[KEEP]=self.manual
        self.history.state(USER,EVENT,'ready')
        source=copy.deepcopy(self.source);source['Description']='補充說明\n'+source['Description']
        newer=verified_events([source],[remote(source)],USER,self.journal)
        self.assertNotEqual(newer[0]['key'],self.key)
        self.assertEqual(await write_events(self.client,newer,self.journal),{'created':0,'skipped':1})
        self.assertEqual(self.deletes,[EVENT])
        self.client.commit.assert_awaited_once()

    async def test_no_manual_event_defers_without_delete_or_model(self):
        await self.registered(); del self.details[KEEP]
        self.assertEqual((await self.run_cleanup())['removed'],0)
        self.assertEqual(self.row()['state'],'ready')
        self.client.rpc.reset_mock()
        self.assertEqual((await self.run_cleanup())['checked'],0)
        self.client.rpc.assert_not_awaited()

    async def test_managed_ai_relabelled_as_manual_is_not_accepted_as_human(self):
        await self.registered()
        self.history.begin('18','other-key')
        self.history.register('18',KEEP,'other-key',self.line,self.manual)
        self.assertEqual((await self.run_cleanup())['removed'],0)
        self.assertFalse(self.deletes)

    async def test_managed_edit_updates_baseline_but_unexpected_human_edits_do_not(self):
        await self.registered()
        before=copy.deepcopy(self.ai); self.ai[0][4]='77'
        self.history.advance(USER,before,self.ai)
        row=self.row(); value=self._workflow.store.decode(self.history.scope(USER,EVENT),row['payload'])
        self.assertEqual(digest(value['detail']),digest(self.ai))
        self.assertEqual((await self.run_cleanup())['removed'],1)

    async def test_shared_ai_is_preserved_if_manual_does_not_cover_all_recipients(self):
        await self.registered()
        before=copy.deepcopy(self.ai); self.ai[0][15]='Ru17,Ru18,Ru19'
        self.history.advance(USER,before,self.ai)
        self.assertEqual((await self.run_cleanup())['needs_review'],1)
        self.assertFalse(self.deletes)

    async def test_shared_ai_can_be_removed_when_manual_covers_all_recipients(self):
        await self.registered()
        before=copy.deepcopy(self.ai); self.ai[0][15]='Ru17,Ru18,Ru19'
        self.history.advance(USER,before,self.ai)
        self.manual[0][15]='Ru17,Ru19'
        self.assertEqual((await self.run_cleanup())['removed'],1)

    async def test_missing_encryption_key_does_not_reset_calendar_backup_history(self):
        from examples.official_document_workflow.infrastructure.storage.automation_store import AutomationStore
        await self.registered()
        key=self._workflow.settings.key
        key.unlink()
        with self.assertRaises(ValueError):
            AutomationStore(key,dsn=self._eip_test_dsn,schema=self._eip_test_schema)
        self.assertFalse(key.exists())

    async def test_manual_preview_is_read_only(self):
        from examples.official_document_workflow.infrastructure.eip.calendar_sync import (
            check_existing_events,
        )
        await self.registered()
        # A new candidate can adopt the manual schedule at a different clock.
        source=copy.deepcopy(self.source); source['Description']='補充說明\n'+source['Description']
        events=verified_events([source],[remote(source)],USER,self.journal)
        await check_existing_events(events,self.journal,self.client.calendar_import_matches,reviewer=AsyncMock())
        self.assertFalse(self.deletes)

    async def test_hourly_checks_even_when_no_new_documents_or_tasks(self):
        await self.registered()
        store=SimpleNamespace(active=Mock(return_value=True),account=Mock(return_value={'scan_start':0}),
                              discover=Mock(),tasks=Mock(return_value=[]),finish=Mock())
        self.client.login=AsyncMock();self.client.close=AsyncMock()
        self.client.automatic_documents=AsyncMock(return_value=[])
        # Exercise the account dispatcher with a real cleanup capability and no tasks.
        store.account.side_effect=lambda owner,**kw: {'scan_start':0,'generation':'generation','identity':USER,'account':'test','password':'fake'}
        account=AutomaticAccount(store,self.journal,None,factory=lambda:self.client,verify=AsyncMock(return_value=True))
        await account.run('owner','generation')
        self.assertEqual(self.deletes,[EVENT])
        self.assertIn('清理 1 筆',store.finish.call_args.args[2])
