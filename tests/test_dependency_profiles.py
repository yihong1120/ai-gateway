"""Exercise locked installation boundaries without installing or contacting services."""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DependencyProfileTests(unittest.TestCase):
    def selected(self, *selection):
        result = subprocess.run(
            [
                'uv', 'export', '--frozen', '--no-dev', '--no-header', '--no-hashes',
                '--no-annotate', '--no-emit-project', *selection,
            ],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
        return {
            name.lower().replace('_', '-'): version.split(';', 1)[0].strip()
            for line in result.stdout.splitlines() if '==' in line
            for name, version in [line.split('==', 1)]
        }

    def test_device_api_does_not_install_model_or_document_stacks(self):
        selected = self.selected('--extra', 'company-ai-access')
        self.assertTrue({'psycopg-binary', 'psycopg-pool', 'cryptography'} <= selected.keys())
        self.assertFalse({'litellm', 'prisma', 'langgraph', 'pymupdf', 'redis'} & selected.keys())

    def test_agent_extra_includes_document_and_database_dependencies(self):
        selected = self.selected('--extra', 'company-agent')
        self.assertTrue({'langgraph', 'python-docx', 'pymupdf', 'psycopg-binary'} <= selected.keys())
        self.assertLess(int(selected['cryptography'].split('.', 1)[0]), 49)

    def test_independent_mcp_edge_does_not_install_vault_database_or_model_stacks(self):
        selected = self.selected('--extra', 'company-mcp')
        self.assertEqual(selected['mcp'], '2.3.0')
        self.assertFalse({
            'psycopg', 'psycopg-binary', 'langgraph',
            'pymupdf', 'openai', 'sqlalchemy',
        } & selected.keys())
        incompatible = subprocess.run(
            ['uv', 'export', '--frozen', '--extra', 'company-mcp', '--group', 'litellm'],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertNotEqual(incompatible.returncode, 0)

    def test_all_applications_exclude_proxy_server(self):
        selected = self.selected('--all-extras')
        self.assertTrue({'redis', 'asyncpg', 'pymupdf', 'langgraph', 'psycopg-binary'} <= selected.keys())
        self.assertFalse({'litellm', 'litellm-proxy-extras', 'prisma'} & selected.keys())

    def test_worker_groups_keep_their_original_small_runtimes(self):
        office = self.selected('--only-group', 'office-worker')
        self.assertEqual(office['openpyxl'], '3.1.5')
        self.assertEqual(office['pymupdf'], '1.26.7')
        self.assertEqual(set(office), {'openpyxl', 'pymupdf', 'et-xmlfile'})
        cad = self.selected('--only-group', 'cad-worker')
        self.assertEqual(cad['ezdxf'], '1.4.4')
        self.assertFalse({'pymupdf', 'litellm', 'fastapi'} & cad.keys())

    def test_litellm_runtime_is_explicit_and_cannot_mix_with_applications(self):
        selected = self.selected('--only-group', 'litellm')
        self.assertEqual(selected['litellm'], '1.109.1')
        self.assertEqual(selected['prisma'], '0.15.0')
        self.assertEqual(selected['psycopg'], '3.3.4')
        result = subprocess.run(
            ['uv', 'export', '--frozen', '--extra', 'company-ai-access', '--group', 'litellm'],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
