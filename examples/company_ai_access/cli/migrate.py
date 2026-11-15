"""Offline, transactional SQLite import. Preview is the default; source stays intact."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from cryptography.fernet import Fernet
from psycopg.types.json import Jsonb

from examples.company_ai_access.storage.registry import Store, database_config, digest


@contextmanager
def stopped_registry(directory):
    path = directory / 'service.lock'
    if path.is_symlink():
        raise ValueError('AI access lease must not be a symlink.')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def migrate(directory: Path, *, apply=False):
    database_config()
    source = directory / 'access.sqlite3'
    key = directory / 'access.key'
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise ValueError('AI access state must be a private directory.')
    for path in (source, key):
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise ValueError('Migration requires the original private SQLite database and access.key.')
    cipher = Fernet(key.read_bytes())
    # Also excludes the old SQLite service, which has no PostgreSQL lease.
    with stopped_registry(directory):
        with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as old:
            old.row_factory = sqlite3.Row
            old.execute('BEGIN')
            snapshot = {table: [dict(row) for row in old.execute('SELECT * FROM ' + table + ' ORDER BY ' + order)]
                        for table, order in (('records', 'kind,id'), ('audit', 'seq'), ('limits', 'id'))}
        for row in snapshot['records']:
            data = json.loads(row['data'])
            if data['id'] != row['id'] or data.get('subject', '') != row['subject']:
                raise ValueError('Source record identity does not match its payload.')
            if data.get('key_box'):
                raw = cipher.decrypt(data['key_box'].encode()).decode()
                if digest(raw) != data.get('key_hash'):
                    raise ValueError('Source credential hash mismatch.')
        fingerprint = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        report = {'counts': {table: len(rows) for table, rows in snapshot.items()}, 'snapshot_sha256': fingerprint, 'applied': False}
        if not apply:
            return report
        store = Store(directory, legacy_import=True)
        with store.lease(), store.db() as db:
            metadata = db.execute('SELECT legacy_hash FROM registry_metadata WHERE id=1 FOR UPDATE').fetchone()
            if metadata['legacy_hash']:
                if metadata['legacy_hash'] != fingerprint:
                    raise ValueError('Source differs from the already imported snapshot.')
                return {**report, 'applied': True, 'already_imported': True}
            if any(db.execute('SELECT EXISTS(SELECT 1 FROM ' + table + ') AS present').fetchone()['present']
                   for table in snapshot):
                raise ValueError('Refusing to merge into a nonempty PostgreSQL registry.')
            for row in snapshot['records']:
                db.execute('INSERT INTO records VALUES (%s,%s,%s,%s)',
                           (row['kind'], row['id'], row['subject'], Jsonb(json.loads(row['data']))))
            for row in snapshot['audit']:
                db.execute('INSERT INTO audit(seq,at,event,subject,device) VALUES (%s,%s,%s,%s,%s)',
                           tuple(row[name] for name in ('seq', 'at', 'event', 'subject', 'device')))
            for row in snapshot['limits']:
                db.execute('INSERT INTO limits VALUES (%s,%s,%s)', tuple(row[name] for name in ('id', 'count', 'until')))
            db.execute("SELECT setval(pg_get_serial_sequence('audit','seq'),COALESCE((SELECT max(seq) FROM audit),1),EXISTS(SELECT 1 FROM audit))")
            # Verify complete logical content before recording success.
            for table, order in (('records', 'kind,id'), ('audit', 'seq'), ('limits', 'id')):
                imported = db.execute('SELECT * FROM ' + table + ' ORDER BY ' + order).fetchall()
                expected = snapshot[table]
                if table == 'records':
                    expected = [{**row, 'data': json.loads(row['data'])} for row in expected]
                if imported != expected:
                    raise ValueError('PostgreSQL import verification failed.')
            db.execute('UPDATE registry_metadata SET legacy_hash=%s WHERE id=1', (fingerprint,))
        return {**report, 'applied': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Import into an empty PostgreSQL registry; default only previews source.')
    args = parser.parse_args()
    try:
        report = migrate(Path(os.environ['AI_ACCESS_STATE_DIR']), apply=args.apply)
    except Exception as exc:
        # Drivers may include DSNs, payloads or constraints containing key hashes.
        raise SystemExit('Migration failed (' + type(exc).__name__ + '); no source data was removed.') from None
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
