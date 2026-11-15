"""Real transactions, contention and offline migration; no external services."""

import asyncio
from pathlib import Path
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
import psycopg

from examples.company_ai_access.cli.migrate import migrate, stopped_registry
from examples.company_ai_access.storage.registry import Store, database_config, digest
from examples.company_ai_access.storage.repository import Repository
from tests.ai_access_postgres import access_database


class Registry(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        access_database(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.registry = Store(self.directory)
        self.store = await Repository(self.registry).__aenter__()
        self.addAsyncCleanup(self.store.__aexit__, None, None, None)

    async def test_concurrent_limits_and_expiration(self):
        allowed = await asyncio.gather(*(self.store.limit('same', 100, maximum=5, window=60) for _ in range(30)))
        self.assertEqual(sum(allowed), 5)
        self.assertFalse(await self.store.limit('same', 159, maximum=5, window=60))
        self.assertTrue(await self.store.limit('same', 160, maximum=5, window=60))

    async def test_reads_do_not_wait_for_uncommitted_writer(self):
        await self.store.put('device', {'id': 'a', 'key_hash': 'hash', 'subject': 'alice'})
        async with self.store.db() as writer:
            await writer.execute("UPDATE records SET subject='bob' WHERE id='a'")
            row = await asyncio.wait_for(self.store.get('device', 'a'), timeout=2)
            self.assertEqual(row['subject'], 'alice')

    async def test_bulk_write_rolls_back_and_hash_index_enforces_uniqueness(self):
        with self.assertRaises(psycopg.errors.UniqueViolation):
            await self.store.put_many(
                [
                    ('device', {'id': 'a', 'key_hash': 'same'}),
                    ('device', {'id': 'b', 'key_hash': 'same'}),
                ]
            )
        self.assertEqual((await self.store.list('device')), [])

    async def test_registry_key_and_version_checked_on_restart(self):
        original = (self.directory / 'access.key').read_bytes()
        (self.directory / 'access.key').write_bytes(Fernet.generate_key())
        with self.assertRaisesRegex(ValueError, 'does not match'):
            Store(self.directory)
        (self.directory / 'access.key').write_bytes(original)
        async with self.store.db() as db:
            await db.execute('UPDATE schema_version SET version=99')
        with self.assertRaisesRegex(ValueError, 'schema version'):
            Store(self.directory)

    async def test_single_runtime_lease(self):
        async with self.store.lease():
            with self.assertRaises(RuntimeError):
                async with self.store.lease():
                    self.fail('duplicate lease')
        async with self.store.lease():
            pass

    def legacy(self):
        raw = 'synthetic-device-key'
        record = {
            'id': 'device',
            'subject': 'alice',
            'key_hash': digest(raw),
            'key_box': self.store.seal(raw),
        }
        path = self.directory / 'access.sqlite3'
        with sqlite3.connect(path) as db:
            db.executescript("""CREATE TABLE records(kind TEXT,id TEXT,subject TEXT,data TEXT);
                CREATE TABLE audit(seq INTEGER PRIMARY KEY,at INTEGER,event TEXT,subject TEXT,device TEXT);
                CREATE TABLE limits(id TEXT,count INTEGER,until INTEGER);""")
            db.execute(
                'INSERT INTO records VALUES (?,?,?,?)',
                ('device', 'device', 'alice', json.dumps(record)),
            )
            db.execute("INSERT INTO audit VALUES (7,100,'enrolled','alice','device')")
            db.execute("INSERT INTO limits VALUES ('a',2,200)")
        path.chmod(0o600)
        return path, record

    async def test_import_preview_apply_restart_and_idempotence(self):
        source, record = self.legacy()
        before = source.read_bytes()
        key = (self.directory / 'access.key').read_bytes()
        preview = migrate(self.directory)
        self.assertFalse(preview['applied'])
        self.assertEqual((await self.store.list('device')), [])
        with self.assertRaisesRegex(ValueError, 'Migrate'):
            Store(self.directory)
        report = migrate(self.directory, apply=True)
        self.assertTrue(report['applied'])
        self.assertEqual(report['counts'], {'records': 1, 'audit': 1, 'limits': 1})
        Store(self.directory)
        restarted = self.store
        self.assertEqual((await restarted.by_key('synthetic-device-key')), record)
        self.assertEqual(restarted.open(record['key_box']), 'synthetic-device-key')
        await restarted.audit(101, 'next')
        async with restarted.db() as db:
            self.assertEqual(
                (await (await db.execute('SELECT max(seq) AS seq FROM audit')).fetchone())['seq'],
                8,
            )
        self.assertTrue(migrate(self.directory, apply=True)['already_imported'])
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual((self.directory / 'access.key').read_bytes(), key)

    async def test_import_refuses_live_service_and_nonempty_destination(self):
        self.legacy()
        with stopped_registry(self.directory), self.assertRaises(BlockingIOError):
            migrate(self.directory, apply=True)
        async with self.store.lease():
            with self.assertRaises(RuntimeError):
                migrate(self.directory, apply=True)
        await self.store.put('device', {'id': 'existing'})
        with self.assertRaisesRegex(ValueError, 'nonempty'):
            migrate(self.directory, apply=True)
        self.assertEqual((await self.store.list('device')), [{'id': 'existing'}])

    async def test_import_rolls_back_on_duplicate_device_hash(self):
        source, record = self.legacy()
        with sqlite3.connect(source) as db:
            other = {**record, 'id': 'other'}
            db.execute(
                'INSERT INTO records VALUES (?,?,?,?)',
                ('device', 'other', 'alice', json.dumps(other)),
            )
        with self.assertRaises(psycopg.errors.UniqueViolation):
            migrate(self.directory, apply=True)
        self.assertEqual((await self.store.list('device')), [])
        async with self.store.db() as db:
            self.assertIsNone(
                (await (await db.execute('SELECT legacy_hash FROM registry_metadata')).fetchone())['legacy_hash']
            )

    async def test_changed_legacy_snapshot_is_rejected(self):
        source, _ = self.legacy()
        migrate(self.directory, apply=True)
        with sqlite3.connect(source) as db:
            db.execute('UPDATE limits SET count=3')
        with self.assertRaisesRegex(ValueError, 'differs'):
            migrate(self.directory, apply=True)


class Configuration(unittest.TestCase):
    def test_database_required_and_local_only(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(ValueError, 'AI_ACCESS_DATABASE_URL'),
        ):
            database_config()
        for dsn in (
            'postgresql://remote.example/db',
            'host=localhost hostaddr=192.0.2.1 dbname=test',
        ):
            with (
                patch.dict(os.environ, {'AI_ACCESS_DATABASE_URL': dsn}),
                self.assertRaises(ValueError),
            ):
                database_config()
