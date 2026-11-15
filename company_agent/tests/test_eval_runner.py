"""Evaluation resources close on setup errors, cancellation and report failures."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from company_agent.evals.runner import Budget, evaluation, own_sandbox, own_model, release_model, track_report, write_json
from examples.shared.runtime.database import _current_scope


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_failure_closes_model_pool_scope_and_preserves_safe_report(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'report.json'
            report = {'runs': []}
            captured = []
            model, fixture = object(), MagicMock()
            @evaluation
            async def run():
                captured.append(_current_scope.get())
                own_model(model)
                own_sandbox('test-dsn')
                track_report(path, report)
                raise ValueError('PRIVATE setup diagnostic')
            with patch('company_agent.evals.sandbox.isolated_schemas', return_value=fixture), \
                    patch('company_agent.evals.runner.close_model', new=AsyncMock()) as close:
                with self.assertRaises(ValueError):
                    await run()
            close.assert_awaited_once_with(model)
            fixture.__exit__.assert_called_once()
            result = json.loads(path.read_text())
            self.assertEqual(result['execution']['error_type'], 'ValueError')
            self.assertEqual(result['execution']['status'], 'error')
            self.assertNotIn('PRIVATE', path.read_text())
            self.assertTrue(captured[0]._closed)

    async def test_vision_cli_does_not_require_an_app_job_service_and_closes_on_setup_failure(self):
        from company_agent.evals.smoke_vision import main
        from company_agent.api.service import ServiceConfiguration

        model = object()
        user = {'id': 'owner'}
        with patch('examples.identity.adapters.webui.verified_user', new=AsyncMock(return_value=user)), \
                patch.object(ServiceConfiguration, 'model', return_value=model) as construct, \
                patch('company_agent.evals.sandbox.isolated_schemas', side_effect=RuntimeError('sandbox setup failed')), \
                patch('company_agent.evals.runner.close_model', new=AsyncMock()) as close, \
                patch.dict('os.environ', {'COMPANY_AGENT_DATABASE_URL': 'test-dsn'}):
            with self.assertRaisesRegex(RuntimeError, 'sandbox setup failed'):
                await main('owner')
        construct.assert_called_once_with(user)
        close.assert_awaited_once_with(model)

    async def test_live_smoke_closes_model_when_agent_construction_fails(self):
        from company_agent.evals.smoke import run

        model = object()
        with tempfile.TemporaryDirectory() as temporary, \
                patch('company_agent.evals.smoke.GatewayModel', return_value=model), \
                patch('company_agent.evals.smoke.DocumentAgent', side_effect=ValueError('invalid setup')), \
                patch('company_agent.evals.runner.close_model', new=AsyncMock()) as close:
            with self.assertRaisesRegex(ValueError, 'invalid setup'):
                await run(Path(temporary) / 'output', live=True)
        close.assert_awaited_once_with(model)

    async def test_explicit_release_does_not_close_model_twice(self):
        model = object()
        @evaluation
        async def run():
            own_model(model)
            await release_model(model)
        with patch('company_agent.evals.runner.close_model', new=AsyncMock()) as close:
            await run()
        close.assert_awaited_once()

    async def test_cancelled_run_closes_all_models_even_if_one_cleanup_fails(self):
        entered = asyncio.Event()
        models = [object(), object()]
        @evaluation
        async def run():
            for model in models:
                own_model(model)
            entered.set()
            await asyncio.Event().wait()
        with patch('company_agent.evals.runner.close_model', new=AsyncMock(side_effect=[RuntimeError('close failed'), None])) as close:
            task = asyncio.create_task(run())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(close.await_count, 2)

    async def test_report_failure_does_not_skip_fixture_cleanup(self):
        fixture, model = MagicMock(), object()
        @evaluation
        async def run(path):
            own_sandbox('test-dsn')
            own_model(model)
            track_report(path, {'runs': []})
        with patch('company_agent.evals.sandbox.isolated_schemas', return_value=fixture), \
                patch('company_agent.evals.runner.write_json', side_effect=OSError('disk unavailable')), \
                patch('company_agent.evals.runner.close_model', new=AsyncMock()) as close:
            with self.assertRaises(ExceptionGroup):
                await run(Path('unused.json'))
        close.assert_awaited_once()
        fixture.__exit__.assert_called_once()


class SharedBudgetTests(unittest.TestCase):
    def test_threaded_reservations_cannot_exceed_the_batch_budget(self):
        budget = Budget(1)
        def reserve(_):
            try:
                budget.reserve(.1)
                return 1
            except ValueError:
                return 0
        with ThreadPoolExecutor(max_workers=8) as pool:
            accepted = sum(pool.map(reserve, range(40)))
        self.assertEqual(accepted, 10)
        self.assertLessEqual(budget.charged, 1)

    def test_atomic_report_serialization_failure_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'report.json'
            write_json(path, {'version': 1})
            with self.assertRaises(TypeError):
                write_json(path, {'invalid': object()})
            self.assertEqual(json.loads(path.read_text()), {'version': 1})
            self.assertEqual(list(path.parent.iterdir()), [path])


class SandboxTests(unittest.TestCase):
    def test_nested_sandboxes_restore_bindings_and_drop_only_their_schemas_on_failure(self):
        import os
        import psycopg
        from psycopg import sql
        from company_agent.evals.sandbox import isolated_schemas
        from examples.official_document_workflow.runtime import current_resources

        dsn = os.getenv('COMPANY_AGENT_TEST_DATABASE_URL')
        if not dsn:
            self.skipTest('Set COMPANY_AGENT_TEST_DATABASE_URL for sandbox cleanup tests')
        original = {key: os.environ.get(key) for key in ('COMPANY_AGENT_DATABASE_URL', 'COMPANY_AGENT_DB_SCHEMA')}
        names = []
        with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
            with isolated_schemas(dsn):
                outer = current_resources()
                outer_schema = os.environ['COMPANY_AGENT_DB_SCHEMA']
                names.extend([outer_schema, outer.settings.schema])
                with psycopg.connect(dsn) as db:
                    for name in names:
                        db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(name)))
                with isolated_schemas(dsn):
                    inner = current_resources()
                    names.extend([os.environ['COMPANY_AGENT_DB_SCHEMA'], inner.settings.schema])
                    with psycopg.connect(dsn) as db:
                        for name in names[2:]:
                            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(name)))
                    self.assertIsNot(inner, outer)
                self.assertIs(current_resources(), outer)
                self.assertEqual(os.environ['COMPANY_AGENT_DB_SCHEMA'], outer_schema)
                raise RuntimeError('synthetic failure')
        self.assertEqual({key: os.environ.get(key) for key in original}, original)
        with psycopg.connect(dsn) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM pg_namespace WHERE nspname=ANY(%s)', (names,)).fetchone()[0], 0)
