"""Keep active dynamic modules registered; bound idle versions and evict both maps."""
from __future__ import annotations

import hashlib
import sys
import types
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from threading import RLock


@dataclass
class _Module:
    value: types.ModuleType
    users: int = 0


class ModuleRegistry:
    def __init__(self, prefix, filename, *, idle_limit=8):
        if idle_limit < 0:
            raise ValueError('Idle module capacity must be nonnegative.')
        self.prefix, self.filename, self.idle_limit = prefix, filename, idle_limit
        self._entries = OrderedDict()
        self._lock = RLock()

    def _trim(self):
        idle = [name for name, entry in self._entries.items() if entry.users == 0]
        for name in idle[:max(0, len(idle) - self.idle_limit)]:
            entry = self._entries.pop(name)
            if sys.modules.get(name) is entry.value:
                del sys.modules[name]

    @contextmanager
    def acquire(self, content):
        name = self.prefix + hashlib.sha256(content.encode()).hexdigest()
        with self._lock:
            entry = self._entries.get(name)
            if entry is None:
                module = types.ModuleType(name)
                module.__file__ = self.filename
                if name in sys.modules:
                    raise RuntimeError('Dynamic module name is already registered.')
                sys.modules[name] = module
                try:
                    exec(compile(content, name, 'exec'), module.__dict__)
                except BaseException:
                    sys.modules.pop(name, None)
                    raise
                entry = self._entries[name] = _Module(module)
            entry.users += 1
            self._entries.move_to_end(name)
            self._trim()
        try:
            yield entry.value
        finally:
            with self._lock:
                entry.users -= 1
                self._entries.move_to_end(name)
                self._trim()
