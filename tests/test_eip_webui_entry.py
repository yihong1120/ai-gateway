from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from official_document_workflow.infrastructure.eip.webui_entry import ensure_entry
from official_document_workflow.infrastructure.eip.webui_entry import ENTRY_ID


class WebUIEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_entry_preserves_existing_notices_and_is_idempotent(self):
        previous = {'id': 'company-notice', 'content': 'existing'}
        config = SimpleNamespace(get=AsyncMock(return_value=[previous]), upsert=AsyncMock())
        await ensure_entry(config)
        banners = config.upsert.call_args.args[0]['ui.banners']
        self.assertEqual(banners[0], previous)
        self.assertEqual(banners[1]['id'], ENTRY_ID)
        self.assertIn('](/eip)', banners[1]['content'])
        config.get.return_value = banners
        await ensure_entry(config)
        config.upsert.assert_awaited_once()

    async def test_admin_customization_not_overwritten(self):
        config = SimpleNamespace(
            get=AsyncMock(
                return_value=[{'id': ENTRY_ID, 'content': 'custom'}],
            ), upsert=AsyncMock(),
        )
        await ensure_entry(config)
        config.upsert.assert_not_awaited()
