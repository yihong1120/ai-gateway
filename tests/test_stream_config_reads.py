"""Stream list counts stay group-wide without per-configuration SQL reads."""

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from examples.db_management.services import stream_config_services as services


class StreamConfigReadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        self.addAsyncCleanup(self.engine.dispose)
        async with self.engine.begin() as connection:
            await connection.execute(text('CREATE TABLE group_info (id INTEGER PRIMARY KEY, max_allowed_streams INTEGER)'))
            await connection.execute(text('CREATE TABLE stream_configs (id INTEGER PRIMARY KEY, group_id INTEGER, site_id INTEGER)'))
            await connection.execute(text('INSERT INTO group_info VALUES (1, 10), (2, 20), (3, 30)'))
            await connection.execute(text('INSERT INTO stream_configs VALUES (1, 1, 100), (2, 1, 200), (3, 3, 100)'))
        self.db = AsyncSession(self.engine)
        self.addAsyncCleanup(self.db.close)
        self.selects = []

        @event.listens_for(self.engine.sync_engine, 'before_cursor_execute')
        def count_select(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().startswith('SELECT'):
                self.selects.append(statement)

    async def test_group_counts_include_other_sites_and_empty_groups(self):
        self.assertEqual(await services._group_stream_limits({1, 2}, self.db), {1: (2, 10), 2: (0, 20)})
        self.assertEqual(len(self.selects), 1)
        self.assertEqual(await services.get_group_stream_limit(1, self.db), (2, 10))

    async def test_missing_group_preserves_not_found_and_empty_list_does_no_sql(self):
        self.assertEqual(await services._group_stream_limits(set(), self.db), {})
        self.assertEqual(self.selects, [])
        with self.assertRaises(HTTPException) as failure:
            await services.get_group_stream_limit(99, self.db)
        self.assertEqual((failure.exception.status_code, failure.exception.detail), (404, 'Group not found'))

    async def test_list_serialisation_loads_all_group_totals_once(self):
        def config(identifier, group):
            return SimpleNamespace(
                id=identifier, group_id=group, stream_name='camera', video_url='rtsp://internal/camera',
                model_key='detector', recognition_enabled=True, work_start_hour=7, work_end_hour=18,
                detect_no_safety_vest_or_helmet=False, detect_near_machinery_or_vehicle=False,
                detect_in_restricted_area=False, detect_in_utility_pole_restricted_area=False,
                detect_machinery_close_to_pole=False, expire_date=None,
                updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )

        configs = [config(1, 1), config(2, 1), config(3, 2)]
        with patch.object(services, '_get_site_or_404', AsyncMock(return_value=object())), \
                patch.object(services, '_site_permission') as permission, \
                patch.object(services, 'is_super_admin', return_value=True), \
                patch.object(services, 'list_stream_configs', AsyncMock(return_value=configs)):
            result = await services._list_site_stream_config_reads(100, self.db, 'administrator')
        permission.assert_called_once()
        self.assertEqual(len(self.selects), 1)
        self.assertEqual([item.total_stream_in_group for item in result], [2, 2, 0])
        self.assertEqual([item.max_allowed_streams for item in result], [10, 10, 20])
        self.assertEqual(result[0].video_url, configs[0].video_url)
        self.assertNotIn('group_id', result[0].model_dump())
