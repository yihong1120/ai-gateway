"""Private source preparation and bounded, resumable orphan discovery."""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import math
import os
import shutil
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from psycopg.conninfo import conninfo_to_dict


def _sync(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class SourceSnapshots:
    def __init__(self, root, store):
        self.store = store
        connection = conninfo_to_dict(store.dsn)
        identity = [
            store.schema, *(
                connection.get(k)
                for k in ('host', 'hostaddr', 'port', 'dbname', 'user', 'service')
            ),
        ]
        self.namespace = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        self.root = Path(root)
        self.pending = self.root / '.pending' / self.namespace
        for directory in (self.root, self.root / '.pending', self.pending):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if directory.is_symlink() or not directory.is_dir() or directory.stat().st_mode & 0o077:
                raise ValueError('附件目錄必須為私人一般資料夾。')
        self._scans, self._turn, self._guard = {}, 0, Lock()

    def marker(self, key):
        return self.pending / (key + '.json')

    @contextmanager
    def prepare(self, inputs):
        key = str(uuid.uuid4())
        directory = self.root / ('.preparing_' + key)
        ready = False
        try:
            with open(self.marker(key), 'x+b') as lease:
                os.chmod(lease.name, 0o600)
                fcntl.flock(lease, fcntl.LOCK_EX)

                def state(value):
                    lease.seek(0)
                    lease.truncate()
                    lease.write(
                        json.dumps({
                            'version': 1, 'namespace': self.namespace,
                            'id': key, 'state': value,
                        }).encode(),
                    )
                    lease.flush()
                    os.fsync(lease.fileno())
                state('preparing')
                _sync(self.pending)
                directory.mkdir(mode=0o700)
                manifest = []
                for index, (name, data) in enumerate(inputs, 1):
                    filename = f'input_{index}' + Path(name).suffix.lower()
                    descriptor = os.open(directory / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, 'wb') as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    manifest.append({'path': filename, 'display_name': name})
                _sync(directory)
                target = self.root / key
                directory.rename(target)
                directory = target
                _sync(self.root)
                state('ready')
                ready = True
                yield key, manifest
        finally:
            if not ready:
                try:
                    if directory.exists():
                        shutil.rmtree(directory)
                    self.marker(key).unlink(missing_ok=True)
                except OSError:
                    logging.getLogger(__name__).warning('Incomplete source cleanup deferred')

    def discard(self, key):
        try:
            shutil.rmtree(self.root / key)
            self.marker(key).unlink(missing_ok=True)
        except OSError as error:
            logging.getLogger(__name__).warning('Unused source cleanup deferred (%s)', type(error).__name__)

    def registered(self, key):
        try:
            self.marker(key).unlink(missing_ok=True)
        except OSError as error:
            logging.getLogger(__name__).warning('Source receipt cleanup deferred (%s)', type(error).__name__)

    def close(self):
        with self._guard:
            for scan in self._scans.values():
                scan.close()
            self._scans.clear()

    def _next(self, kind):
        if kind not in self._scans:
            self._scans[kind] = os.scandir(self.pending if kind == 'pending' else self.root)
        try:
            return next(self._scans[kind])
        except StopIteration:
            self._scans.pop(kind).close()
            return None

    def sweep(self, *, older_than=86400, limit=100, max_seconds=.1):
        if (
            not math.isfinite(older_than) or older_than < 86400 or type(limit) is not int or not 1 <= limit <= 1000
            or not math.isfinite(max_seconds) or not 0 < max_seconds <= 10
        ):
            raise ValueError('Recovery needs a one-day grace, bounded visits and a finite time budget.')
        start, cutoff = time.monotonic(), time.time() - older_than
        visited = inspected = removed = 0
        exhausted = set()
        with self._guard:
            while visited < limit and len(exhausted) < 2 and time.monotonic() - start < max_seconds:
                kind = ('pending', 'legacy')[self._turn % 2]
                self._turn += 1
                if kind in exhausted:
                    continue
                entry = self._next(kind)
                if entry is None:
                    exhausted.add(kind)
                    continue
                visited += 1  # Every entry, including invalid and unowned entries, consumes budget.
                try:
                    if kind == 'pending':
                        raw = entry.name.removesuffix('.json')
                        marker = Path(entry.path)
                        if not entry.name.endswith('.json') or not entry.is_file(follow_symlinks=False):
                            continue
                    else:
                        raw = entry.name.removeprefix('.preparing_')
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        marker = Path(entry.path) / '.snapshot'
                    if str(uuid.UUID(raw)) != raw:
                        continue
                    info = marker.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_mtime > cutoff:
                        continue
                    descriptor = os.open(marker, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
                except (FileNotFoundError, ValueError):
                    continue
                with os.fdopen(descriptor, 'r+b') as lease:
                    try:
                        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    inspected += 1
                    try:
                        state = json.loads(lease.read(512))
                    except (ValueError, UnicodeError):
                        continue
                    if (
                        not isinstance(state, dict) or state != {
                            'version': 1, 'namespace': self.namespace,
                            'id': raw, 'state': state.get('state'),
                        } or state['state'] not in {'preparing', 'ready'}
                    ):
                        continue
                    with self.store.db() as db:
                        if not db.execute(
                            'SELECT pg_try_advisory_xact_lock(%s)',
                            (self.store.lock_id('mutations'),),
                        ).fetchone()[0]:
                            continue
                        registered = db.execute('SELECT 1 FROM web_jobs WHERE id=%s', (raw,)).fetchone()
                    if registered:
                        marker.unlink(missing_ok=True)
                        continue
                    # A crash can occur between directory rename and ready-marker fsync.
                    for directory in (self.root / raw, self.root / ('.preparing_' + raw)):
                        if directory.exists():
                            if directory.is_symlink():
                                raise ValueError('Source directory changed during recovery.')
                            shutil.rmtree(directory)
                    marker.unlink(missing_ok=True)
                    removed += 1
        return {
            'visited': visited, 'inspected': inspected, 'removed': removed,
            'elapsed': time.monotonic() - start,
        }
