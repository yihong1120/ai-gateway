"""Dynamic dataclass versions stay registered only while active or within idle capacity."""
from __future__ import annotations

import sys
import unittest
import uuid
from contextlib import ExitStack

from examples.shared.runtime.module_registry import ModuleRegistry


class ModuleRegistryTests(unittest.TestCase):
    def registry(self, limit=0):
        return ModuleRegistry('test_dynamic_' + uuid.uuid4().hex, __file__, idle_limit=limit)

    def test_active_versions_remain_registered_and_dataclass_identities_are_stable(self):
        registry = self.registry()
        code = 'from dataclasses import dataclass\n@dataclass\nclass Record:\n    value: str\n'
        with registry.acquire(code) as first:
            with registry.acquire(code) as same:
                self.assertIs(first.Record, same.Record)
            with ExitStack() as active:
                others = [active.enter_context(registry.acquire(code + f'\nVERSION={i}')) for i in range(12)]
                self.assertTrue(all(sys.modules.get(m.__name__) is m for m in [first, *others]))
                self.assertIsInstance(first.Record('owned'), first.Record)
            self.assertTrue(all(m.__name__ not in sys.modules for m in others))
            self.assertIs(sys.modules[first.__name__], first)
        self.assertNotIn(first.__name__, sys.modules)
        self.assertFalse(registry._entries)

    def test_idle_lru_eviction_removes_sys_modules_and_registry_together(self):
        registry = self.registry(limit=2)
        names = []
        for i in range(12):
            with registry.acquire(f'VERSION={i}') as module:
                names.append(module.__name__)
        self.assertEqual(set(registry._entries), set(names[-2:]))
        self.assertTrue(all(name not in sys.modules for name in names[:-2]))
        # Release the last two test modules rather than retaining them globally.
        registry.idle_limit = 0
        with registry.acquire('VERSION=11'):
            pass
        self.assertTrue(all(name not in sys.modules for name in names))

    def test_compile_failure_and_caller_failure_release_registrations(self):
        registry = self.registry()
        before = set(sys.modules)
        with self.assertRaises(SyntaxError):
            with registry.acquire('class invalid syntax'):
                pass
        self.assertEqual(set(sys.modules), before)
        with self.assertRaises(ValueError):
            with registry.acquire('VERSION=1') as module:
                name = module.__name__
                raise ValueError('caller cancelled')
        self.assertNotIn(name, sys.modules)
