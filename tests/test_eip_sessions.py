from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

import httpx

from examples.official_document_workflow.infrastructure.eip.calendar_sync import write_events
from examples.official_document_workflow.infrastructure.eip.netask import Netask
from examples.official_document_workflow.infrastructure.eip.sessions import manual_session
from examples.official_document_workflow.infrastructure.storage.automation_store import AutomationStore
from examples.official_document_workflow.infrastructure.storage.state import Journal
from examples.official_document_workflow.infrastructure.storage.state import Link
from examples.official_document_workflow.infrastructure.storage.state import State
from tests.eip_calendar_fixture import new_calendar_matches
from tests.eip_postgres import EIPPostgresTestCase


def adapter():
    return SimpleNamespace(
        user_id='17', login=AsyncMock(), close=AsyncMock(),
        documents=AsyncMock(return_value=[]), commit=AsyncMock(return_value=None),
        calendar_import_matches=AsyncMock(side_effect=new_calendar_matches),
    )


class SessionTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = AutomationStore(root/'keys/key')
        self.store.bind('alice', '17', 'account', 'password')
        self.state = State()
        self.new = adapter()
        self.factory = Mock(return_value=self.new)

    async def asyncTearDown(self): self.tmp.cleanup()

    def session(self, **kwargs):
        return manual_session('alice', self.state, self.store, factory=self.factory, **kwargs)

    def old_link(self):
        old = adapter()
        link = Link(old, 'account', binding_generation=self.store.account('alice')['generation'])
        self.state.links['alice'] = link
        return link

    async def test_restart_restore_preserves_schedule_and_origin(self):
        config = self.store.account('alice')
        async with self.session() as s:
            await s.read('documents', 'inbox', '2026-09-01', '2026-09-06', 0)
        async with self.session():
            pass
        self.new.login.assert_awaited_once_with('account', 'password')
        self.assertEqual(self.store.account('alice'), config)

    async def test_parallel_requests_share_one_login(self):
        async def query():
            async with self.session() as s:
                return await s.read('documents')
        await asyncio.gather(*(query() for _ in range(10)))
        self.new.login.assert_awaited_once()

    async def test_expired_session_keeps_same_binding_handles(self):
        old = self.old_link()
        old.expires = 0
        old.documents['handle'] = {'id': '10'}
        generation = old.generation
        async with self.session() as s:
            self.assertIs(s.link, old)
            self.assertEqual(s.link.generation, generation)
            self.assertIn('handle', s.link.documents)

    async def test_documents_expiry_propagates_without_relogin(self):
        old = self.old_link()
        previous = old.adapter
        failure = ConnectionError('expired')
        previous.documents = AsyncMock(side_effect=failure)
        with self.assertRaises(ConnectionError) as caught:
            async with self.session() as s:
                await s.read('documents')
        self.assertIs(caught.exception, failure)
        previous.documents.assert_awaited_once()
        self.factory.assert_not_called()
        self.assertEqual(old.expires, 0)
        previous.commit.assert_not_awaited()

    async def test_calendar_expiry_propagates_without_relogin(self):
        old = self.old_link()
        previous = old.adapter
        failure = ConnectionError('expired')
        previous.calendar = AsyncMock(side_effect=failure)
        with self.assertRaises(ConnectionError) as caught:
            async with self.session() as s:
                await s.read('calendar', '2026-09-01', '2026-09-30')
        self.assertIs(caught.exception, failure)
        previous.calendar.assert_awaited_once()
        self.factory.assert_not_called()
        self.assertEqual(old.expires, 0)
        previous.commit.assert_not_awaited()

    async def test_calendar_detail_is_read_only_and_not_retried(self):
        old = self.old_link()
        previous = old.adapter
        previous.calendar_detail = AsyncMock(return_value=[['detail']])
        async with self.session() as session:
            self.assertEqual(await session.read('calendar_detail', 'event'), [['detail']])
            with self.assertRaises(ValueError):
                await session.read('commit', 'wire')
        previous.calendar_detail.assert_awaited_once_with('event')
        failure = ConnectionError('expired')
        previous.calendar_detail.side_effect = failure
        with self.assertRaises(ConnectionError) as caught:
            async with self.session() as session:
                await session.read('calendar_detail', 'event')
        self.assertIs(caught.exception, failure)
        self.assertEqual(previous.calendar_detail.await_count, 2)
        self.factory.assert_not_called()
        previous.commit.assert_not_awaited()

    async def test_subsequent_manual_request_can_reconnect(self):
        old = self.old_link()
        old.adapter.documents.side_effect = ConnectionError('expired')
        self.new.documents.side_effect = ConnectionError('expired')
        for _ in range(2):
            with self.assertRaises(ConnectionError):
                async with self.session() as s:
                    await s.read('documents')
        self.new.login.assert_awaited_once()

    async def test_generic_failure_is_not_auth_failure(self):
        old = self.old_link()
        old.adapter.documents.side_effect = ValueError('bad RPC')
        with self.assertRaises(ValueError):
            async with self.session() as s:
                await s.read('documents')
        self.factory.assert_not_called()

    async def test_failed_login_closes_adapter_and_new_binding_can_recover(self):
        self.new.login.side_effect = ValueError('rejected')
        for _ in range(2):
            with self.assertRaises(ValueError):
                async with self.session():
                    pass
        self.assertEqual(self.new.login.await_count, 2)
        self.assertEqual(self.new.close.await_count, 2)
        self.assertNotIn('alice', self.state.links)
        self.new.login.side_effect = None
        self.store.bind('alice', '17', 'account', 'updated-password')
        async with self.session():
            pass
        self.assertEqual(self.new.login.await_count, 3)

    async def test_missing_binding_never_guesses_credentials(self):
        self.store.pause('alice', forget=True)
        with self.assertRaises(ValueError):
            async with self.session():
                pass
        self.factory.assert_not_called()

    async def test_paused_schedule_allows_explicit_manual_use_without_resuming(self):
        self.store.pause('alice')
        config = self.store.account('alice')
        async with self.session():
            pass
        self.assertEqual(self.store.account('alice'), config)

    async def test_unbind_during_login_cannot_resurrect_session(self):
        async def login(*args): self.store.pause('alice', forget=True)
        self.new.login.side_effect = login
        with self.assertRaises(ValueError):
            async with self.session():
                pass
        self.assertNotIn('alice', self.state.links)
        self.new.close.assert_awaited_once()

    async def test_identity_mismatch_never_publishes_session(self):
        self.new.user_id = '18'
        with self.assertRaises(ValueError):
            async with self.session():
                pass
        self.assertNotIn('alice', self.state.links)

    async def test_rebound_identity_invalidates_old_handles(self):
        old = self.old_link()
        old.documents['h'] = {'id': '10'}
        self.store.bind('alice', '18', 'other', 'password')
        self.new.user_id = '18'
        async with self.session() as s:
            self.assertIsNot(s.link, old)
            self.assertEqual(s.link.documents, {})

    async def test_calendar_write_not_replayed_on_auth_error(self):
        old = self.old_link()
        old.adapter.commit.side_effect = ConnectionError('expired')
        original_adapter = old.adapter
        journal = Journal()
        events = [{'key': 'event', 'line': 'verified-line'}]
        with self.assertRaises(ConnectionError):
            async with self.session() as s:
                await write_events(s.link.adapter, events, journal)
        self.assertEqual(journal.status('event'), 'unknown')
        async with self.session() as s:
            with self.assertRaises(ValueError):
                await write_events(s.link.adapter, events, journal)
            with self.assertRaises(ValueError):
                await s.read('commit', 'verified-line')
        original_adapter.commit.assert_awaited_once_with('verified-line')
        self.new.commit.assert_not_awaited()

    async def test_expiry_sweep_does_not_delete_renewed_link(self):
        old = self.old_link()
        await self.state.unlink('alice', expired_only=True)
        self.assertIs(self.state.links['alice'], old)


class RemoteExpiryTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_only_auth_redirects_or_401_are_expiry(self):
        cases = [
            (401, '', ConnectionError),
            (302, '/login', ConnectionError),
            (302, 'https://other.test/login', ValueError),
            (500, '', TimeoutError),
            (403, '', ValueError),
        ]
        for code, location, error in cases:
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda req: httpx.Response(code, headers={'location': location}),
                ),
            )
            n = Netask(client)
            try:
                with self.assertRaises(error):
                    await n.request('GET', '/fo/test')
            finally:
                await n.close()

    async def test_known_login_form_not_generic_rpc_error(self):
        n = Netask()
        try:
            n.request = AsyncMock(return_value=b'<form><input name="_token" value="x"></form>')
            with self.assertRaises(ConnectionError):
                await n.rpc('List')
            n.request.return_value = b'<html>unexpected response</html>'
            with self.assertRaises(ValueError) as caught:
                await n.rpc('List')
            self.assertNotIsInstance(caught.exception, ConnectionError)
        finally:
            await n.close()
