"""Validate independent graders and real isolated-role execution without fees."""
from __future__ import annotations

import asyncio
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from openpyxl import load_workbook

from company_agent.config import Root
from company_agent.config import Settings
from company_agent.evals.benchmark import Budget
from company_agent.evals.benchmark import MeteredInvocation
from company_agent.evals.data_replacement import classify
from company_agent.evals.data_replacement import export_pack
from company_agent.evals.data_replacement import fixture_inputs
from company_agent.evals.data_replacement import JobBudget
from company_agent.evals.data_replacement import model_slot
from company_agent.evals.data_replacement import provider_stop
from company_agent.evals.data_replacement import run_role
from company_agent.evals.data_replacement import schedule
from company_agent.evals.data_replacement import summarize
from company_agent.evals.data_replacement_cases import BY_ID
from company_agent.evals.data_replacement_cases import CASES
from company_agent.evals.data_replacement_cases import fingerprint
from company_agent.evals.data_replacement_cases import grade
from company_agent.evals.data_replacement_cases import prepare
from company_agent.evals.data_replacement_cases import preserved
from company_agent.evals.data_replacement_cases import role_plan
from company_agent.evals.data_replacement_cases import save
from company_agent.evals.data_replacement_report import decision
from company_agent.services.files.operations import ToolBox
from company_agent.services.office.schemas import AnalyzeSpreadsheet
from company_agent.services.office.schemas import EditSpreadsheet
from company_agent.services.office.schemas import ReadSpreadsheet
from company_agent.tests.test_agent import call
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_enterprise_completion import finish
from company_agent.tests.test_multi_agent import RawRoleModel


class DataEvaluationTests(unittest.TestCase):
    def test_exact_baseline_fixture_reuse_rejects_source_tampering_and_definition_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root/'job'
            sources = folder/'sources'
            sources.mkdir(parents=True)
            case = BY_ID['D01']
            data, gold = prepare(case, 123)
            for name, value in data.items():
                (sources/name).write_bytes(value)
            (folder/'expected.private.json').write_text(json.dumps(gold))
            report = {
                'completed_at': '2026-10-18T00:00:00+00:00', 'unrun': [], 'cases': [case.public()],
                'runs': [{'case': case.id, 'seed': 123, 'folder': 'job', 'source_sha256': fingerprint(sources)}],
            }
            path = root/'report.json'
            path.write_text(json.dumps(report))
            reused, oracle = fixture_inputs(path, case, 123)
            self.assertEqual(reused, data)
            self.assertEqual(oracle, gold)
            (sources/'source.xlsx').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'hashes changed'):
                fixture_inputs(path, case, 123)
            report['cases'][0]['prompt'] = 'changed'
            path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, 'definition differs'):
                fixture_inputs(path, case, 123)

    def test_candidate_configs_differ_only_in_data_model(self):
        folder = Path(__file__).parents[1] / 'configs'
        argon = Settings.model_validate_json((folder / 'config.mixed-candidate.example.json').read_text()).model_dump()
        flash = Settings.model_validate_json(
            (folder / 'config.data-flash-candidate.example.json').read_text(),
        ).model_dump()
        self.assertEqual(flash['role_models']['data']['model'], 'gemini-3.8-flash')
        flash['role_models']['data']['model'] = 'gemini-4-argon'
        self.assertEqual(flash, argon)

    def test_cases_include_real_role_workflows_and_separate_negative_controls(self):
        self.assertEqual(len(CASES), 26)
        self.assertEqual(len(BY_ID), len(CASES))
        self.assertEqual(sum(c.lane == 'role' for c in CASES), 22)
        self.assertEqual(sum(c.lane == 'workflow' for c in CASES), 4)
        self.assertEqual(sum(c.control for c in CASES), 5)
        for case in CASES:
            sources, gold = prepare(case, 123)
            self.assertTrue(sources)
            for name, data in sources.items():
                self.assertEqual(Path(name).name, name)
                if name.endswith('.xlsx'):
                    book = load_workbook(io.BytesIO(data))
                    self.assertTrue(book.sheetnames)
            self.assertEqual(gold['files'], [] if case.control else list(case.outputs))
            self.assertEqual(role_plan(case, gold)['files'], [{'name': name} for name in case.outputs])

    def test_seeded_numeric_variants_change_gold_without_leaking_answers_into_prompt(self):
        case = BY_ID['D01']
        _, first = prepare(case, 123)
        _, same = prepare(case, 123)
        _, other = prepare(case, 456)
        self.assertEqual(first, same)
        self.assertNotEqual(first['stats'], other['stats'])
        self.assertNotIn('expected', json.dumps(role_plan(case, first)))
        self.assertNotIn('gold', case.public())

    def test_pair_schedule_preserves_equal_coverage_and_balances_order(self):
        planned = schedule(CASES, 2, 123)
        self.assertEqual(planned, schedule(CASES, 2, 123))
        self.assertEqual(len(planned), 52)
        self.assertEqual(sum(p['arms'][0] == 'flash' for p in planned), 26)
        self.assertTrue(all(set(p['arms']) == {'flash', 'argon'} for p in planned))
        self.assertNotEqual(planned[0]['seed'], planned[-1]['seed'])

    def test_pagination_oracle_omits_sentinel_and_tail_outlier(self):
        sources, gold = prepare(BY_ID['D06'], 123)
        book = load_workbook(io.BytesIO(sources['source.xlsx']))
        self.assertEqual(gold['evidence'][0]['end_row'], 124)
        self.assertEqual(book['工程']['A125'].value, '正式資料結束')
        self.assertEqual(gold['stats']['count'], 123)
        self.assertLess(gold['stats']['sum'], 999999)

    def test_mixed_types_exclude_booleans_dates_numeric_strings_and_formulas(self):
        _, gold = prepare(BY_ID['D02'], 123)
        self.assertEqual(gold['stats']['count'], 4)
        self.assertIsNone(prepare(BY_ID['D05'], 123)[1]['stats']['mean'])

    def test_offline_pack_has_all_cases_inputs_and_no_claimed_paid_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pack'
            plan = export_pack(path, CASES, 2, 123, 2)
            self.assertEqual(plan['paid_calls'], 0)
            self.assertFalse(plan['live'])
            self.assertEqual(plan['planned_jobs'], 104)
            self.assertTrue(
                all((path / name).is_file() for name in
                    ('manifest.json', 'cases.csv', 'cases.xlsx', 'synthetic_inputs.zip')),
            )

    def test_concurrent_job_budgets_keep_costs_separate(self):
        parent = Budget(10)
        a, b = JobBudget(parent), JobBudget(parent)
        a.reserve(2)
        b.reserve(3)
        a.settle(2, 0.5)
        b.settle(3, 1)
        self.assertEqual((a.charged, b.charged, parent.charged), (0.5, 1, 1.5))

    def test_summary_pairs_only_same_repeat_and_excludes_controls_from_positive_rate(self):
        rows = []
        for case, repeat, arm, control, passed in [
            ('D01', 1, 'flash', False, True),
            ('D01', 1, 'argon', False, False),
            ('D18', 1, 'flash', True, True),
            ('D01', 2, 'argon', False, True),
        ]:
            rows.append({
                'case': case, 'repeat': repeat, 'arm': arm, 'lane': 'role', 'control': control,
                'passed': passed, 'seconds': 1, 'accounted_usd': 0.01, 'status': 'returned',
                'source_sha256': {'source.xlsx': 'same'}, 'calls': [], 'critical_failures': [],
            })
        result = summarize(rows)
        self.assertEqual(result['paired_counts']['flash_only_pass'], 1)
        self.assertEqual(len(result['pairs']), 1)
        self.assertEqual(result['arms']['flash']['groups']['role_positive']['n'], 1)
        self.assertEqual(result['arms']['flash']['groups']['role_control']['n'], 1)

    def test_workflow_without_data_is_attributed_to_coordinator(self):
        row = {'status': 'returned', 'passed': False, 'calls': [{'role': 'coordinator'}]}
        self.assertEqual(classify(BY_ID['W01'], row), 'coordinator_before_data')

    def test_transient_failure_circuit_is_per_model_and_auth_stops_immediately(self):
        streaks = {'flash': 0, 'argon': 0}
        limited = {'arm': 'flash', 'calls': [{'status': 'failed', 'error_type': 'RateLimitError'}]}
        self.assertIsNone(provider_stop(limited, streaks))
        self.assertIsNone(provider_stop({'arm': 'argon', 'calls': [{'status': 'returned'}]}, streaks))
        self.assertIsNone(provider_stop(limited, streaks))
        self.assertEqual(provider_stop(limited, streaks), 'new_pairs')
        self.assertEqual(
            provider_stop(
                {
                    'arm': 'argon', 'calls': [
                        {'status': 'failed', 'error_type': 'AuthenticationError'},
                    ],
                }, streaks,
            ), 'hard',
        )

    def test_complete_coverage_policy_keeps_transient_failures_and_still_stops_auth(self):
        streaks = {'flash': 0, 'argon': 0}
        failure = {'arm': 'flash', 'calls': [{'status': 'failed', 'error_type': 'RateLimitError'}]}
        for _ in range(10):
            self.assertIsNone(provider_stop(failure, streaks, limit=0))
        self.assertEqual(streaks['flash'], 10)
        failure['calls'][0]['error_type'] = 'AuthenticationError'
        self.assertEqual(provider_stop(failure, streaks, limit=0), 'hard')

    def test_replacement_conclusion_requires_complete_safe_role_and_workflow_evidence(self):
        rows = []
        for case, lane, control in [('D01', 'role', False), ('D18', 'role', True), ('W01', 'workflow', False)]:
            for arm in ('flash', 'argon'):
                rows.append({
                    'case': case, 'lane': lane, 'control': control, 'arm': arm, 'repeat': 1,
                    'passed': True, 'seconds': 1 if arm == 'flash' else 3, 'accounted_usd': 0.01,
                    'status': 'returned', 'calls': [], 'critical_failures': [], 'source_sha256': {},
                })
        report = {'runs': rows, 'unrun': [], 'source_version_unchanged': True}
        self.assertIn('限定場景替換候選', decision(report))
        rows[0]['critical_failures'] = ['accepted_wrong_data']
        self.assertIn('不建議全面替換', decision(report))
        report['unrun'] = [{'case': 'D02'}]
        self.assertIn('不能判定', decision(report))


class ModelGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_gate_excludes_another_process_but_keeps_other_model_available(self):
        with tempfile.TemporaryDirectory() as directory:
            probe = (
                'import fcntl,os,sys; fd=os.open(sys.argv[1],os.O_RDWR); '
                'fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)'
            )
            async with model_slot(directory, 'flash'):
                result = subprocess.run(
                    [sys.executable, '-c', probe, directory + '/flash.lock'],
                    capture_output=True, check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                async with asyncio.timeout(1), model_slot(directory, 'argon'):
                    pass
            result = subprocess.run(
                [sys.executable, '-c', probe, directory + '/flash.lock'],
                capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0)

    async def test_cancelled_waiter_releases_descriptor_and_does_not_steal_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            async def wait():
                async with model_slot(directory, 'flash'):
                    return True
            async with model_slot(directory, 'flash'):
                task = asyncio.create_task(wait())
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            async with asyncio.timeout(1), model_slot(directory, 'flash'):
                pass


class DataRoleTests(unittest.IsolatedAsyncioTestCase, Fixture):
    async def test_role_failure_preserves_partial_files_sources_and_safe_status(self):
        case = BY_ID['D01']
        sources, _ = prepare(case, 123)
        self.file('source.xlsx', sources['source.xlsx'])
        self.config.roots = [Root(name='demo', path=str(self.root), readers=['alice'], model_access=True)]

        def fail(_):
            raise RuntimeError('private provider body')

        model = RawRoleModel(
            data=[
                call('read_spreadsheet', {'root': 'demo', 'path': 'source.xlsx'}, 'read'),
                call(
                    'create_spreadsheet', {
                        'name': 'checked.xlsx', 'sheets': [
                            {'name': '結果', 'rows': [['部分成果', 1]]},
                        ],
                    }, 'create',
                ), fail,
            ],
        )
        with self.assertRaises(RuntimeError):
            await run_role(self.settings(), model, 'alice', case, self.store, asyncio.Lock())
        with self.store.db() as db:
            job = db.execute('SELECT id FROM jobs ORDER BY created DESC LIMIT 1').fetchone()[0]
        result = self.store.result('alice', job)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(len(result['files']), 1)
        self.assertEqual(len(result['sources']), 1)
        self.assertNotIn('private provider body', str(result))

    def test_preservation_oracle_accepts_real_narrow_edit_and_rejects_unrequested_change(self):
        case = BY_ID['D14']
        sources, gold = prepare(case, 123)
        self.file('source.xlsx', sources['source.xlsx'])
        self.config.enable_spreadsheet_reports = True
        box = ToolBox(self.config, self.store, 'alice', self.job)
        read = box.read_spreadsheet(ReadSpreadsheet(root='project', path='source.xlsx'))
        result = box.edit_spreadsheet(
            EditSpreadsheet(
                root='project', path='source.xlsx', sha256=read['sha256'],
                name='revised.xlsx', changes=[
                    {'sheet': '工程', 'cell': 'B2', 'expected': 10, 'value': 12},
                    {'sheet': '第二表', 'cell': 'A1', 'expected': '原值', 'value': '已核對'},
                ],
            ),
        )
        revised = self.store.read_artifact('alice', result['id'], self.job)[2]
        self.assertTrue(preserved(sources['source.xlsx'], revised, gold['changed_cells']))
        book = load_workbook(io.BytesIO(revised))
        book['工程']['A3'] = '不應修改'
        self.assertFalse(preserved(sources['source.xlsx'], save(book), gold['changed_cells']))

    def settings(self):
        self.config.structured_completion = True
        self.config.orchestration_mode = 'hybrid'
        self.config.office_execution_mode = 'specialist'
        self.config.require_delivery_plan = True
        return self.config

    async def test_isolated_role_uses_real_tools_and_recovers_from_injected_parser_fault(self):
        case = BY_ID['D17']
        sources, gold = prepare(case, 123)
        for name, data in sources.items():
            self.file(name, data)
        self.config.roots = [Root(name='demo', path=str(self.root), readers=['alice'], model_access=True)]
        ids = []
        args = AnalyzeSpreadsheet.model_validate(gold['evidence'][0]).model_dump()

        def create(messages):
            value = json.loads(messages[-1].content)
            stats = value['statistics_evidence']['result']
            rows = [
                [label, stats[key]] for label, key in
                [('合計', 'sum'), ('筆數', 'count'), ('最小', 'min'), ('最大', 'max'), ('平均', 'mean')]
            ]
            return call('create_spreadsheet', {'name': 'checked.xlsx', 'sheets': [{'name': '結果', 'rows': rows}]}, 'create')

        def complete(messages):
            ids.append(json.loads(messages[-1].content)['id'])
            return finish('complete_assignment', ids=ids)

        model = RawRoleModel(
            data=[
                call('analyze_spreadsheet', args, 'first'),
                call('analyze_spreadsheet', args, 'corrected'), create, complete,
            ],
        )
        before = fingerprint(self.root)
        result, fault = await run_role(self.settings(), model, 'alice', case, self.store, asyncio.Lock())
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(result['architecture'], 'isolated_data_role')
        self.assertTrue(fault)
        self.assertEqual(len(result['tool_feedback']), 1)
        files = {f['name']: self.store.read_artifact('alice', f['id'], result['job_id'])[2] for f in result['files']}
        evidence = self.store.evidence('alice', result['job_id'])
        with self.store.db() as db:
            receipts = [
                {'call_id': key, 'name': name, 'result': json.loads(value) if value else None}
                for key, name, value in db.execute(
                    'SELECT call_id,name,result FROM tool_receipts WHERE job=%s',
                    (result['job_id'],),
                ).fetchall()
            ]
        calls = [{'role': 'data', 'tool_names': ['analyze_spreadsheet', 'create_spreadsheet']}]
        checks = grade(case, result, files, self.root, before, gold, evidence, receipts, calls, fault)
        self.assertTrue(all(checks.values()), checks)
        simultaneous = [{**r, 'call_id': evidence[0]['evidence_id']} for r in receipts]
        checks = grade(case, result, files, self.root, before, gold, evidence, simultaneous, calls, fault)
        self.assertFalse(checks['statistics_before_output'])
        # A wrong numeric answer cannot pass just because the task says completed.
        book = load_workbook(io.BytesIO(files['checked.xlsx']))
        book['結果']['B1'] = 999999
        checks = grade(case, result, {'checked.xlsx': save(book)}, self.root, before, gold, evidence, [], calls, fault)
        self.assertFalse(checks['cells_checked.xlsx'])
        checks = grade(case, result, files, self.root, before, gold, [], [], calls, fault)
        self.assertFalse(checks['actual_statistics_scope_1'])

    async def test_cancelled_provider_call_retains_unknown_cost_and_failure_record(self):
        parent = Budget(10)
        budget = JobBudget(parent)

        class Cancelled:
            async def ainvoke(self, messages):
                raise asyncio.CancelledError()

        outer = SimpleNamespace(
            model=None, rates={'input': 0.00001, 'output': 0.00001},
            budget=budget, name='test', calls=[],
        )
        with self.assertRaises(asyncio.CancelledError):
            await MeteredInvocation(outer, Cancelled(), 'data', []).ainvoke([])
        self.assertEqual(outer.calls[0]['error_type'], 'CancelledError')
        self.assertGreater(outer.calls[0]['reserved_unknown_usd'], 0)
        self.assertEqual(outer.calls[0]['reserved_unknown_usd'], budget.charged)
