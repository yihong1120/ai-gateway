"""Identity core stays host-free; explicit adapters own lazy host access."""
from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = ROOT / 'examples/identity'


def _imports(path):
    parts = path.relative_to(ROOT).with_suffix('').parts
    package = '.'.join(parts[:-1])
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            module = resolve_name('.' * node.level + (node.module or ''), package) if node.level else node.module or ''
            yield module
            yield from (module + '.' + alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)


def _within(name, namespace):
    return name == namespace or name.startswith(namespace + '.')


class IdentityBoundaryTests(unittest.TestCase):
    def test_identity_has_no_management_or_bff_implementation_imports(self):
        self.assertTrue(IDENTITY.is_dir())
        for path in IDENTITY.rglob('*.py'):
            for imported in _imports(path):
                self.assertFalse(any(_within(imported, namespace) for namespace in (
                    'examples.db_management', 'examples.bff',
                )), f'{path}: {imported}')

    def test_only_explicit_adapters_can_import_host_code_and_core_cannot_import_adapters(self):
        for path in IDENTITY.rglob('*.py'):
            if path.is_relative_to(IDENTITY / 'adapters'):
                continue
            for imported in _imports(path):
                self.assertFalse(any(_within(imported, namespace) for namespace in (
                    'examples.openwebui', 'open_webui', 'examples.identity.adapters',
                )), f'{path}: {imported}')

    def test_old_identity_namespace_and_scattered_route_sources_are_removed(self):
        self.assertFalse((ROOT / 'examples/auth').exists())
        self.assertFalse((ROOT / 'examples/openwebui/identity.py').exists())
        self.assertTrue((IDENTITY / 'adapters/webui.py').is_file())
        for name in ['auth', 'users', 'oauth', 'password_reset', 'native_social']:
            self.assertTrue((IDENTITY / f'routers/{name}.py').is_file())
            self.assertFalse((ROOT / f'examples/db_management/routers/{name}.py').exists())
