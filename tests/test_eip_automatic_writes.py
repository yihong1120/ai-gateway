"""Every automatic write shares authorization checks and never retries on expiry."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

from examples.official_document_workflow.infrastructure.eip import automatic_session
from examples.official_document_workflow.infrastructure.eip.automatic_session import (
    AutomaticSession,
)


class AutomaticWriteTests(unittest.IsolatedAsyncioTestCase):
    async def check_operations(self, *, active=True, expired=False):
        for operation in ('commit', 'attach', 'project'):
            with self.subTest(operation=operation):
                store = SimpleNamespace(active=Mock(return_value=active))
                factory = Mock()
                session = AutomaticSession(store, 'owner', 'generation', factory, AsyncMock(return_value=True))
                write = AsyncMock(side_effect=ConnectionError('upstream detail') if expired else None)
                session.adapter = SimpleNamespace(user_id='17', commit=write)
                session.usable = True
                with patch.object(automatic_session.calendar_attachments, 'attach_event', write), \
                        patch.object(automatic_session.calendar_projects, 'assign_project', write):
                    if operation == 'commit':
                        call = session.commit('line')
                    elif operation == 'attach':
                        call = session.attach_calendar_files('id', 'line', [])
                    else:
                        call = session.assign_calendar_project('id', 'line', 'project', sharing=['Ru17'])
                    if not active:
                        with self.assertRaises(ValueError):
                            await call
                        write.assert_not_awaited()
                    elif expired:
                        with self.assertRaises(ConnectionError) as raised:
                            await call
                        self.assertEqual('upstream detail', str(raised.exception))
                        self.assertFalse(session.usable)
                        write.assert_awaited_once()
                    else:
                        await call
                        write.assert_awaited_once()
                factory.assert_not_called()

    async def test_expiry_never_logs_in_or_replays_any_write(self):
        await self.check_operations(expired=True)

    async def test_revoked_binding_blocks_every_write(self):
        await self.check_operations(active=False)
