"""Linux mounted-folder adapter. Virtual paths + per-operation ACLs.

Directory descriptors and O_NOFOLLOW prevent symlink traversal; no shell,
deletion, overwrite or arbitrary absolute paths are offered to the model.
"""
from __future__ import annotations

import hashlib
import os
import stat
import uuid
from contextlib import contextmanager

from company_agent.config import Root


def relative_parts(path: str):
    parts = path.split('/')
    if (
        not path
        or len(path) > 1000
        or '\\' in path
        or ':' in path
        or any(not p or p.startswith('.') or any(ord(c) < 32 for c in p) for p in parts)
    ):
        raise ValueError('路徑不合法；僅接受根目錄內的非隱藏相對路徑。')
    return parts


class FolderAccess:
    def __init__(self, roots: list[Root], owner: str, max_bytes=20_000_000):
        if len({r.name for r in roots}) != len(roots):
            raise ValueError('設定有重複的資料根目錄名稱。')
        self.roots = {r.name: r for r in roots}
        self.owner, self.max_bytes = owner, max_bytes

    def allowed(self, name, write=False):
        root = self.roots.get(name)
        if (
            not root
            or not root.model_access
            or self.owner not in root.readers
            or (write and self.owner not in root.writers)
        ):
            raise ValueError('沒有此資料位置的權限或未允許模型處理。')
        return root

    def visible_roots(self):
        return [
            {'name': r.name, 'can_publish': self.owner in r.writers}
            for r in self.roots.values()
            if r.model_access and self.owner in r.readers
        ]

    @contextmanager
    def directory(self, name, parents=(), write=False):
        root = self.allowed(name, write)
        fd = None
        try:
            # Walk even the configured absolute root without following links.
            fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
            for part in [p for p in root.path.split('/') if p] + list(parents):
                if part in ('.', '..'):
                    raise ValueError('設定路徑不可含相對目錄。')
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = nxt
            yield fd
        finally:
            if fd is not None:
                os.close(fd)

    def read(self, name, path):
        parts = relative_parts(path)
        with self.directory(name, parts[:-1]) as parent:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, 'rb') as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise ValueError('僅可讀取一般檔案，不接受硬連結或特殊裝置。')
                if before.st_size > self.max_bytes:
                    raise ValueError('檔案超過大小上限。')
                data = stream.read(self.max_bytes + 1)
                after = os.fstat(stream.fileno())
                if len(data) > self.max_bytes or (before.st_size, before.st_mtime_ns) != (
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise ValueError('檔案在讀取期間變更或超過大小上限。')
                return data, hashlib.sha256(data).hexdigest()

    def scan(self, name, cap=300):
        """Bounded traversal; each scan owns its counters and result buffer."""
        scan = FolderScan(cap)
        with self.directory(name) as fd:
            scan.walk(fd, '', 0)
        return sorted(scan.found, key=lambda f: f['path']), scan.incomplete

    def publish_new(self, name, path, data):
        """Same-directory staging + atomic no-replace link; existing files untouched."""
        parts = relative_parts(path)
        if len(data) > self.max_bytes:
            raise ValueError('輸出超過檔案大小上限。')
        with self.directory(name, parts[:-1], write=True) as parent:
            temp = '.company-agent-' + uuid.uuid4().hex
            fd = os.open(
                temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent,
            )
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(
                    temp, parts[-1], src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False,
                )
                os.fsync(parent)
            finally:
                os.unlink(temp, dir_fd=parent)


class FolderScan:
    def __init__(self, cap):
        self.cap = cap
        self.found = []
        self.visited = 0
        self.incomplete = False

    def walk(self, fd, prefix, depth):
        with os.scandir(fd) as entries:
            for entry in entries:
                if self.visited >= self.cap:
                    self.incomplete = True
                    break
                self.visited += 1
                if entry.name.startswith('.') or entry.is_symlink():
                    continue
                path = prefix + entry.name
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    if depth >= 8:
                        self.incomplete = True
                        continue
                    child = os.open(
                        entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd,
                    )
                    try:
                        self.walk(child, path + '/', depth + 1)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    self.found.append(
                        {'path': path, 'bytes': info.st_size, 'mtime_ns': info.st_mtime_ns},
                    )
