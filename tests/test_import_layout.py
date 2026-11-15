"""Keep dependencies in module headers without crossing optional host lifetimes."""
from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from examples.shared.runtime.modules import ModuleReference

ROOT = Path(__file__).resolve().parents[1]


class ImportLayoutTests(unittest.TestCase):
    def test_source_imports_stay_in_module_headers(self):
        paths = subprocess.check_output(
            ['rg', '--files', '-g', '*.py', '-g', '!artifacts/**'], cwd=ROOT, text=True,
        ).splitlines()
        violations = []
        for name in paths:
            tree = ast.parse((ROOT / name).read_text())
            parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
            boundary = float('inf')
            for node in tree.body:
                if isinstance(node, (ast.Import, ast.ImportFrom)) or (
                    isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ) or (isinstance(node, ast.If) and 'TYPE_CHECKING' in ast.unparse(node.test)):
                    continue
                boundary = node.lineno
                break
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                current = node
                while current in parents:
                    current = parents[current]
                    if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        violations.append(f'{name}:{node.lineno}: nested import')
                        break
                else:
                    if node.lineno > boundary:
                        violations.append(f'{name}:{node.lineno}: import outside header')
        self.assertEqual(violations, [])


class ModuleReferenceTests(unittest.TestCase):
    def test_optional_dependency_is_not_loaded_by_declaration(self):
        name = '_missing_import_layout_dependency'
        reference = ModuleReference(name)
        self.assertNotIn(name, sys.modules)
        with self.assertRaises(ModuleNotFoundError):
            reference.load()

    def test_binding_observes_current_host_module_instead_of_caching_a_user_or_resolver(self):
        name = '_import_layout_host'
        first, second = ModuleType(name), ModuleType(name)
        first.resolver, second.resolver = object(), object()
        reference = ModuleReference(name)
        with patch.dict(sys.modules, {name: first}):
            self.assertIs(reference.attribute('resolver'), first.resolver)
        with patch.dict(sys.modules, {name: second}):
            self.assertIs(reference.attribute('resolver'), second.resolver)

    def test_grouped_members_preserve_alias_values(self):
        host = ModuleType('_import_layout_members')
        host.first, host.second = object(), object()
        with patch.dict(sys.modules, {host.__name__: host}):
            self.assertEqual(ModuleReference(host.__name__).attributes('first', 'second'), (host.first, host.second))

    def test_missing_member_remains_an_import_error(self):
        host = ModuleType('_import_layout_missing_member')
        with patch.dict(sys.modules, {host.__name__: host}), self.assertRaises(ImportError):
            ModuleReference(host.__name__).attribute('absent')

    def test_package_child_and_plain_dotted_import_bind_the_same_module_as_python(self):
        reference = ModuleReference('xml.etree')
        child = reference.attribute('ElementTree')
        self.assertEqual(child.__name__, 'xml.etree.ElementTree')
        dotted = ModuleReference('xml.etree.ElementTree')
        self.assertIs(dotted.load(), child)
        self.assertEqual(dotted.load(top_level=True).__name__, 'xml')

    def test_existing_child_in_module_cache_is_available_during_cycle_resolution(self):
        parent = ModuleType('_import_layout_parent')
        parent.__path__ = []
        child = ModuleType('_import_layout_parent.child')
        with patch.dict(sys.modules, {parent.__name__: parent, child.__name__: child}):
            self.assertIs(ModuleReference(parent.__name__).attribute('child'), child)
